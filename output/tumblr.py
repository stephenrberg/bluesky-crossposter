import traceback
from pytumblr import TumblrRestClient
from main.functions import logger
from main.connections import tumblr_connect  
from main.db import database
from main.alerts import send_failure_alert
import re

def extract_all_hashtags(text):
    if not text:
        return []
    raw_tags = re.findall(r'#(\w+)', text)
    seen = set()
    unique_tags = []
    for tag in raw_tags:
        lower_tag = tag.lower()
        if lower_tag not in seen:
            seen.add(lower_tag)
            unique_tags.append(lower_tag)
    return unique_tags

def process_tumblr_text(text):
    if not text:
        return ""
    cull_pattern = r'(?<=\n)(?:\s*#\w+)+\s*$|(?<=[\.!\?])(?:\s+#\w+)+\s*$'
    cleaned_text = re.sub(cull_pattern, "", text).rstrip()
    
    def remove_hash_symbol(match):
        return match.group(1)
        
    cleaned_text = re.sub(r'#(\w+)', remove_hash_symbol, cleaned_text)

    url_pattern = r'(https?://[^\s<>"]+)'
    cleaned_text = re.sub(url_pattern, r'[\1](\1)', cleaned_text)

    return cleaned_text.strip()

# Function for processing output queue
def output(queue):
    for item in queue:
        try:
            if item["type"] == "repost":
                repost(item)
            else:
                post(item)
        except Exception as e:
            database.failed_post(item["id"], "tumblr")
            logger.error(f"Failed to post {item['id']} to Tumblr: {e}")
            logger.debug(traceback.format_exc())
            send_failure_alert("Tumblr", e)

# Function for handling reblogs (reposts)
def repost(item):
    tumblr_client = tumblr_connect()
    post_id, blog_name = database.get_id(item["id"], "tumblr")
    logger.info(f"Reblogging post {post_id} from blog {blog_name} on Tumblr")
    
    from settings.auth import TUMBLR_BLOG_NAME
    response = tumblr_client.reblog(TUMBLR_BLOG_NAME, id=post_id)
    
    if response and "id" in response:
        database.update(item["id"], "tumblr", str(response["id"]))

# Function for sending posts
def post(item):
    tumblr_client = tumblr_connect()
    from settings.auth import TUMBLR_BLOG_NAME
    
    text_content = item["post"].text_content("tumblr")
    full_raw_text = " ".join(text_content)
    tags = extract_all_hashtags(full_raw_text)
    
    # Stitch the raw content together first so we can parse across the whole post structure
    combined_text = "\n\n".join(text_content)
    
    # --- THREAD / REPLY HANDLING ---
    reply_id = item["post"].info.get("reply_id")
    if reply_id:
        reply_to_post = database.get_id(reply_id, "tumblr")
        if not reply_to_post:
            logger.info(f"Can't continue thread since reference {reply_id} has not been crossposted to Tumblr")
            return
        
        # Clean the text structure for Tumblr
        combined_text = process_tumblr_text(combined_text)
        logger.info(f"Threading post onto existing Tumblr post {reply_to_post}")
        
        # --- DYNAMIC REBLOG KEY & HASHTAG FETCHING ---
        parent_tags = []
        try:
            # Look up the parent post to retrieve its valid reblog_key and original tags
            parent_post_data = tumblr_client.posts(TUMBLR_BLOG_NAME, id=reply_to_post)
            if "posts" in parent_post_data and len(parent_post_data["posts"]) > 0:
                parent_post = parent_post_data["posts"][0]
                reblog_key = parent_post.get("reblog_key")
                parent_tags = parent_post.get("tags", [])
            else:
                raise Exception("Post found in database but not found on Tumblr.")
        except Exception as fetch_err:
            raise Exception(f"Failed to fetch metadata for parent post {reply_to_post}: {fetch_err}")

        # Combine, lowercase, and deduplicate tags from both posts
        merged_tags_set = set(tag.lower() for tag in tags + parent_tags)
        combined_tags = list(merged_tags_set)

        # --- MEDIA WITHIN THREADS WORKAROUND ---
        # Upload assets as unlisted drafts to extract official CDN paths,
        # then assemble the layout with media blocks sitting FIRST.
        if item["post"].media:
            media_paths = [media_item["filename"] for media_item in item["post"].media]
            embedded_media_markdown = ""
            
            for path in media_paths:
                try:
                    if path.lower().endswith((".mp4", ".mov")):
                        logger.info(f"Uploading thread video asset proxy to CDN: {path}")
                        media_res = tumblr_client.create_video(TUMBLR_BLOG_NAME, state="draft", data=path)
                        if media_res and "id" in media_res:
                            video_info = tumblr_client.posts(TUMBLR_BLOG_NAME, id=media_res["id"])
                            video_url = video_info["posts"][0].get("video_url", "")
                            if video_url:
                                embedded_media_markdown += f"<video controls src='{video_url}' width='100%'></video>\n\n"
                            tumblr_client.delete_post(TUMBLR_BLOG_NAME, media_res["id"])
                    else:
                        logger.info(f"Uploading thread image asset proxy to CDN: {path}")
                        media_res = tumblr_client.create_photo(TUMBLR_BLOG_NAME, state="draft", data=path)
                        if media_res and "id" in media_res:
                            photo_info = tumblr_client.posts(TUMBLR_BLOG_NAME, id=media_res["id"])
                            photos = photo_info["posts"][0].get("photos", [])
                            for p in photos:
                                img_url = p.get("original_size", {}).get("url")
                                if img_url:
                                    # REMOVED PLACEHOLDER TEXT TO DROP THE ALT BADGE
                                    embedded_media_markdown += f"![]({img_url})\n\n"
                            tumblr_client.delete_post(TUMBLR_BLOG_NAME, media_res["id"])
                except Exception as media_upload_err:
                    logger.error(f"Failed to inline thread media to Markdown block: {media_upload_err}")
            
            # Sequence Change: Places images/videos at the absolute top, followed by the text
            combined_text = f"{embedded_media_markdown}{combined_text}".strip()

        # Tumblr creates threads by reblogging the parent post with a comment
        response = tumblr_client.reblog(
            TUMBLR_BLOG_NAME,
            id=reply_to_post,
            reblog_key=reblog_key,
            comment=combined_text,
            tags=combined_tags,
            format="markdown"
        )
        
        if response and "id" in response:
            database.update(item["id"], "tumblr", str(response["id"]))
            logger.info(f"Successfully threaded to Tumblr! Post ID: {response['id']}")
        else:
            raise Exception(f"Tumblr API returned an unexpected payload during thread reblog: {response}")
        return

    media = item["post"].media

    # --- MEDIA POST HANDLING ---
    if media:
        # For media posts, we just clean up inline hashtags and convert URLs to markdown links
        # (We don't drop trailing links or make Link Posts if images/videos are attached)
        cull_pattern = r'(?<=\n)(?:\s*#\w+)+\s*$|(?<=[\.!\?])(?:\s+#\w+)+\s*$'
        combined_text = re.sub(cull_pattern, "", combined_text).rstrip()
        combined_text = re.sub(r'#(\w+)', lambda m: m.group(1), combined_text)
        
        # Auto-wrap raw URLs into clickable markdown format
        url_pattern = r'(https?://[^\s<>"]+)'
        combined_text = re.sub(url_pattern, r'[\1](\1)', combined_text)

        media_paths = [media_item["filename"] for media_item in media]
        is_video = any(path.lower().endswith((".mp4", ".mov")) for path in media_paths)

        if is_video:
            logger.info(f"Uploading video post to Tumblr blog '{TUMBLR_BLOG_NAME}'")
            response = tumblr_client.create_video(
                TUMBLR_BLOG_NAME, state="published", tags=tags, format="markdown",
                caption=combined_text, data=media_paths[0] 
            )
        else:
            logger.info(f"Uploading photo post to Tumblr blog '{TUMBLR_BLOG_NAME}'")
            response = tumblr_client.create_photo(
                TUMBLR_BLOG_NAME, state="published", tags=tags, format="markdown",
                caption=combined_text, data=media_paths
            )
            
    # --- TEXT ONLY / LINK POST HANDLING ---
    else:
        # Extract native Bluesky external embed items if present
        embed_url = None
        bsky_thumb_url = None
        bsky_embed = item["post"].info.get("embed", {}) if hasattr(item["post"], "info") else item["post"].get("embed", {})
        
        if bsky_embed and bsky_embed.get("$type") == "app.bsky.embed.external":
            external_data = bsky_embed.get("external", {})
            embed_url = external_data.get("uri")
            bsky_thumb_url = external_data.get("thumb")

        # Prioritize using a URL from a rich link card if available, else look in the text
        target_url = embed_url if embed_url else None
        if not target_url:
            any_url_match = re.search(r'(https?://[^\s<>"]+)', combined_text)
            if any_url_match:
                target_url = any_url_match.group(1)
        
        if target_url:
            # Scrub the trailing URL if it sits at the absolute bottom
            trailing_url_pattern = r'\n\s*(https?://[^\s<>"]+)\s*$'
            trailing_match = re.search(trailing_url_pattern, combined_text)
            if trailing_match and trailing_match.group(1) == target_url:
                combined_text = re.sub(trailing_url_pattern, "", combined_text).strip()
            
            # Now that the URL is missing, scrub the exposed trailing hashtag soup
            cull_pattern = r'(?<=\n)(?:\s*#\w+)+\s*$|(?<=[\.!\?])(?:\s+#\w+)+\s*$'
            combined_text = re.sub(cull_pattern, "", combined_text).rstrip()
            
            # Convert any leftover inline hashtags into clean text
            combined_text = re.sub(r'#(\w+)', lambda m: m.group(1), combined_text)
            
            # Convert remaining inline links to clickable markdown links
            url_pattern = r'(https?://[^\s<>"]+)'
            combined_text = re.sub(url_pattern, r'[\1](\1)', combined_text)
            
            # Append markdown target URL back to text description if it was removed
            if target_url not in combined_text:
                combined_text = f"{combined_text}\n\n[{target_url}]({target_url})".strip()

            # STRATEGY FALLBACK: If we have a valid link poster thumbnail from Bluesky, use a Photo Post.
            # This completely bypasses Tumblr's broken web crawler extraction logic.
            if bsky_thumb_url:
                logger.info(f"Uploading image-backed card proxy as Photo Post to Tumblr blog '{TUMBLR_BLOG_NAME}'")
                response = tumblr_client.create_photo(
                    TUMBLR_BLOG_NAME,
                    state="published",
                    tags=tags,
                    format="markdown",
                    caption=combined_text,
                    source=bsky_thumb_url  # pytumblr accepts remote web URLs inside the source parameter
                )
            else:
                # Fallback to structural link type if no thumbnail was captured
                logger.info(f"Uploading rich LINK post container to Tumblr blog '{TUMBLR_BLOG_NAME}'")
                response = tumblr_client.create_link(
                    TUMBLR_BLOG_NAME,
                    state="published",
                    tags=tags,
                    url=target_url,
                    description=combined_text
                )
        else:
            # Standard Text Post Fallback (Clean tags, convert remaining text inline)
            logger.info(f"Uploading standard text post to Tumblr blog '{TUMBLR_BLOG_NAME}'")
            cull_pattern = r'(?<=\n)(?:\s*#\w+)+\s*$|(?<=[\.!\?])(?:\s+#\w+)+\s*$'
            combined_text = re.sub(cull_pattern, "", combined_text).rstrip()
            combined_text = re.sub(r'#(\w+)', lambda m: m.group(1), combined_text)
            
            response = tumblr_client.create_text(
                TUMBLR_BLOG_NAME, state="published", tags=tags, format="markdown",
                body=combined_text
            )

    # Database update verification block
    if response and "id" in response:
        database.update(item["id"], "tumblr", str(response["id"]))
        logger.info(f"Successfully posted to Tumblr! Post ID: {response['id']}")
    else:
        raise Exception(f"Tumblr API returned an unexpected payload: {response}")

# Function for deleting a post
def delete_post(origin_id):
    tumblr_client = tumblr_connect()
    from settings.auth import TUMBLR_BLOG_NAME
    post_id = database.get_id(origin_id, "tumblr")
    
    logger.info(f"Deleting Tumblr post {post_id} from blog {TUMBLR_BLOG_NAME}")
    try:
        tumblr_client.delete_post(TUMBLR_BLOG_NAME, post_id)
    except Exception as e:
        logger.error(f"Failed to delete Tumblr post {post_id}: {e}")