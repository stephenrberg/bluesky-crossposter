import traceback
import requests
import tempfile
import os
import re
from urllib.parse import urlparse, urlunparse, parse_qs
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

def handle_klipy_mastodon_mp4(raw_url, post_id):
    """
    Parses the validated Klipy link layout, extracts the server-side mp4 parameter string,
    replaces the trailing .gif reference with the direct video hash filename, downloads the loop
    natively to a local temp vector file, and yields the absolute tracking path.
    """
    try:
        parsed_url = urlparse(raw_url)
        query_params = parse_qs(parsed_url.query)
        
        mp4_id = query_params.get('mp4', [None])[0]
        
        if mp4_id:
            path_segments = parsed_url.path.split('/')
            if path_segments:
                path_segments[-1] = f"{mp4_id}.mp4" 
                new_path = "/".join(path_segments)
                
                video_url = f"https://{parsed_url.netloc}{new_path}"
                logger.info(f"Formed clean target video path destination for Mastodon: {video_url}")
            
                response = requests.get(video_url, timeout=15)
                if response.status_code == 200:
                    fd, temp_path = tempfile.mkstemp(suffix=".mp4")
                    with open(fd, "wb") as f:
                        f.write(response.content)
                    
                    return temp_path
                    
    except Exception as e:
        logger.error(f"Failed to pull remote Klipy video loop binary for Mastodon local payload: {e}")
        
    return None

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
    # --- ADVANCED INLINE GIF RESOLUTION ENGINE ---
    # Intercept first thread index block to verify if inline text GIFs exist
    if text_content and len(text_content) > 0:
        klipy_match = re.search(r'(https://static\.klipy\.com/[^\s\n\r]+)', text_content[0])
        if klipy_match:
            raw_url = klipy_match.group(1)
            logger.info(f"Targeting authenticated native Mastodon GIF text element: {raw_url}")
            
            # Request the native .mp4 alternative down to temp folder bounds
            local_video_path = handle_klipy_mastodon_mp4(raw_url, item["id"])
            if local_video_path:
                temp_files_to_clean.append(local_video_path)
                
                # Upload directly as a synchronous video asset mapping
                logger.info(f"Uploading looping video to Mastodon framework instance...")
                res = mastodon_client.media_post(local_video_path, description="Looping animation", synchronous=True)
                media_ids.append(res.id)
                
                # Aggressively remove the link and trailing spaces right here from the parent index array slice
                text_content[0] = re.sub(re.escape(raw_url) + r'\s*', '', text_content[0]).strip()

    # If post includes native images, upload them directly (only run if no Klipy GIF was processed)
    if item["post"].media and not media_ids:
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
    elif not media_ids:
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

        # 1. Match trailing hashtags that are either at the end OR followed optionally by a URL
        trailing_soup_pattern = r'((?:\s*#\w+)+)\s*(https?://[^\s<>"]+)?\s*$'
        trailing_soup_match = re.search(trailing_soup_pattern, text_post)
        
        if trailing_soup_match:
            raw_soup = trailing_soup_match.group(1)
            # Capture the URL if it was sitting below the hashtag block
            url_at_end = trailing_soup_match.group(2)
            
            # Extract and clean the soup into a single uniform line
            found_tags = re.findall(r'#(\w+)', raw_soup)
            hashtag_soup = " ".join(f"#{tag}" for tag in found_tags)
            
            # Slice off the entire trailing cluster (tags + potential URL) from the body
            base_text = text_post[:trailing_soup_match.start()].strip()
            
            # 2. Check if a lone URL sits right before the trailing soup was removed
            trailing_url_pattern = r'(https?://[^\s<>"]+)\s*$'
            url_before_match = re.search(trailing_url_pattern, base_text)
            
            target_url = None
            if url_at_end:
                target_url = url_at_end
            elif url_before_match:
                target_url = url_before_match.group(1)
                base_text = re.sub(trailing_url_pattern, '', base_text).strip()
            
            # Assemble the structured post: Core Text -> URL -> Tags
            if target_url:
                final_post = f"{base_text}\n\n{target_url}\n\n{hashtag_soup}"
            else:
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