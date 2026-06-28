import traceback
import requests
import tempfile
import os
import re
from main.functions import logger
from main.connections import mastodon_connect
from settings.auth import MASTODON_HANDLE, MASTODON_INSTANCE
from settings import settings
from main.db import database
from main.alerts import send_failure_alert


# Function for processing output queue
def output(queue):
    for item in queue:
        try:
            if item["type"] == "repost":
                repost(item)
            else:
                post(item)
        except Exception as e:
            database.failed_post(item["id"], "mastodon")
            logger.error(f"Failed to post {item['id']}: {e}")
            logger.debug(traceback.format_exc())
            send_failure_alert("Mastodon", e)


# Function for reposting posts.
def repost(item):
    mastodon_client = mastodon_connect()
    post_id = database.get_id(item["id"], "mastodon")
    a = mastodon_client.status_reblog(post_id)
    database.update(item["id"], "mastodon")
    logger.info(f"Reposted post on Mastodon: {post_id}")
    logger.debug(a)

# Function for sending posts
def post(item):
    mastodon_client = mastodon_connect()
    text_content = item["post"].text_content("mastodon")
    reply_to_post = database.get_id(item["post"].info["reply_id"], "mastodon")
    # Checking to see if post is a reply to a post that has not been crossposted
    if item["post"].info["reply_id"] and not reply_to_post:
        logger.info(f"Can't continue thread since {item['post'].info['reply_id']} has not been crossposted")
        return
    # Since mastodon does not have a quote repost function, quote posts are turned into replies. If the post is both
    # a reply and a quote post, the quote is replaced with a url to the post quoted.
    if item["type"] == "quote" and item["post"].info["reply_id"]:
        post_url = MASTODON_INSTANCE + "@" + MASTODON_HANDLE + "/" + str(item["post"].info["quote_id"])
        reply_to_post = database.get_id(item["post"].info["reply_id"], "mastodon")
        text_content = item["post"].text_content(f"\n{post_url}")
    elif item["type"] == "quote":
        reply_to_post = database.get_id(item["post"].info["quote_id"], "mastodon")
    # Doing a second check to see if post is a reply or quote of a post that has been skipped or failed to br crossposted.
    if reply_to_post in ["skipped", "FailedToPost", "duplicate"]:
        logger.info(f"Post is a reply to or qoute post of a post that has not been crossposted.")
        return
    visibility = set_visibility(item["post"])
    # If language is not used to toggle what posts to send, it is used simply as the language of the post.
    # Mastodon only takes one language per post, so the first one in the list is used.
    language = None
    if not settings.lang_toggle["mastodon"]:
        language = item["post"].info["language"][0]
        
    media_ids = []
    # If post includes images, images are uploaded so that they can be included in the toot
    temp_files_to_clean = []

    # If post includes native images, upload them directly
    if item["post"].media:
        for media_item in item["post"].media:
            # If alt text was added to the image on bluesky, it's also added to the image on mastodon,
            # otherwise it will be uploaded without alt text.
            alt = media_item["alt"]
            # Abiding by alt character limit
            if len(alt) > 1500:
                alt = alt[:1496] + "..."
            logger.info(f"Uploading media {media_item['filename']} with alt: {alt} to mastodon")
            res = mastodon_client.media_post(media_item["filename"], description=alt, synchronous=True)
            media_ids.append(res.id)
            
    # WORKAROUND: If post is text-only but has a link card object pointing to our problematic platforms
    else:
        embed_url = None
        bsky_thumb_url = None
        
        # Safely extract embed structures from the base object
        bsky_embed = item["post"].info.get("embed", {}) if hasattr(item["post"], "info") else item["post"].get("embed", {})
        if bsky_embed and bsky_embed.get("$type") == "app.bsky.embed.external":
            external_data = bsky_embed.get("external", {})
            embed_url = external_data.get("uri")
            bsky_thumb_url = external_data.get("thumb")

        if embed_url and bsky_thumb_url:
            is_problematic_scraper = any(d in embed_url.lower() for d in ["backloggd.com", "serializd.com", "goodreads.com"])
            
            if is_problematic_scraper:
                logger.info(f"Detected problematic card wrapper on Mastodon pipeline. Fetching remote thumbnail...")
                try:
                    # Download the image from the Bluesky CDN
                    img_response = requests.get(bsky_thumb_url, timeout=15)
                    if img_response.status_code == 200:
                        # Establish a localized file inside the system temp folder cleanly
                        fd, temp_path = tempfile.mkstemp(suffix=".jpg")
                        
                        # Use Python's built-in open() context manager via the file descriptor
                        with open(fd, 'wb') as f:
                            f.write(img_response.content)
                        
                        temp_files_to_clean.append(temp_path)
                        
                        # Upload to your target Mastodon instance
                        logger.info(f"Uploading fallback image card banner to Mastodon...")
                        res = mastodon_client.media_post(
                            temp_path, 
                            description=f"Review cover art for {embed_url}", 
                            synchronous=True
                        )
                        media_ids.append(res.id)
                except Exception as thumb_err:
                    logger.error(f"Failed to fetch or attach proxy link thumbnail on Mastodon: {thumb_err}")

    # --- ADVANCED HASHTAG & URL RE-ORDERING ---
    formatted_text_content = []
    for text_post in text_content:
        if not text_post:
            formatted_text_content.append(text_post)
            continue

        # 1. Match ONLY the cluster of hashtags at the absolute end of the post.
        # This ignores inline hashtags like #gamedev in the middle of a sentence.
        trailing_soup_match = re.search(r'((?:\s*#\w+)+\s*$)', text_post)
        
        if trailing_soup_match:
            raw_soup = trailing_soup_match.group(1)
            
            # Extract and clean the soup into a single uniform line
            found_tags = re.findall(r'#(\w+)', raw_soup)
            hashtag_soup = " ".join(f"#{tag}" for tag in found_tags)
            
            # Remove just the trailing soup from the main post body
            # (Using rsplit or slicing up to the match index to guarantee we don't touch the body)
            soup_start_idx = text_post.rfind(raw_soup)
            base_text = text_post[:soup_start_idx]
            
            # 2. Check if a lone URL sits right before where the trailing soup was
            trailing_url_pattern = r'(https?://[^\s<>"]+)\s*$'
            url_match = re.search(trailing_url_pattern, base_text)
            
            if url_match:
                target_url = url_match.group(1)
                # Strip the URL off the new bottom of the text
                base_text = re.sub(trailing_url_pattern, '', base_text)
                base_text = base_text.strip()
                final_post = f"{base_text}\n\n{target_url}\n\n{hashtag_soup}"
            else:
                base_text = base_text.strip()
                final_post = f"{base_text}\n\n{hashtag_soup}"
                
            # Normalize to avoid any accidental triple breaks
            final_post = re.sub(r'\n{3,}', '\n\n', final_post)
            formatted_text_content.append(final_post.strip())
        else:
            # If there is no hashtag soup cluster at the end, leave the post completely untouched
            formatted_text_content.append(text_post)
            
    text_content = formatted_text_content
    # Process and publish the thread text
    for text_post in text_content:
        logger.info(f"Posting \"{text_post}\" to Mastodon")
        logger.debug(f"mastodon_client.status_post({text_post}, in_reply_to_id={reply_to_post}, media_ids={media_ids}, visibility={visibility}, language={language})")
        a = mastodon_client.status_post(text_post, in_reply_to_id=reply_to_post, media_ids=media_ids, visibility=visibility, language=language)
        logger.debug(a)
        reply_to_post = a["id"]
        # setting media ids to empty to not end up posting the media in every post in the thread
        media_ids = []
        database.update(item["id"], "mastodon", a["id"])
        
    # --- PROXIED STORAGE CLEANUP ---
    for temp_file in temp_files_to_clean:
        try:
            if os.path.exists(temp_file):
                os.remove(temp_file)
                logger.info(f"Cleaned up temporary proxy card file: {temp_file}")
        except Exception as cleanup_err:
            logger.error(f"Failed to delete temp storage file {temp_file}: {cleanup_err}")
            
    logger.info("Posted to mastodon")

# Function for deleting post. Takes ID of post from origin (Bluesky)
def delete_post(origin_id):
    mastodon_client = mastodon_connect()
    post_id = database.get_id(origin_id, "mastodon")
    logger.info("deleting toot " + str(post_id))
    try:
        a = mastodon_client.status_delete(post_id)
        logger.debug(a)
    except Exception as e:
        logger.debug(e)
        if "Record not found" in str(e):
            logger.info(f"Toot with id {post_id} does not exist")

# Function for translating visibility settings to Mastodon specifics. More information about this in readme.
def set_visibility(post):
    if settings.mastodon_visibility == "inherit":
        return settings.privacy[post.info["privacy"]]["mastodon"]
    elif settings.mastodon_visibility == "unlisted":
        return "unlisted"
    elif settings.mastodon_visibility == "public":
        return "public"
    elif settings.mastodon_visibility == "hybrid" and (post.info["reply_id"] or post.info["quote_id"]):
        return "unlisted"
    elif settings.mastodon_visibility == "hybrid":
        return "public"