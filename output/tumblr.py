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

# Function for sending posts
def post(item):
    tumblr_client = tumblr_connect()
    
    text_content = item["post"].text_content("tumblr")
    full_raw_text = " ".join(text_content)
    tags = extract_all_hashtags(full_raw_text)
    
    # Stitch the raw content together first so we can parse across the whole post structure
    combined_text = "\n\n".join(text_content)
    
    from settings.auth import TUMBLR_BLOG_NAME
    
    reply_to_post = database.get_id(item["post"].info.get("reply_id"), "tumblr")
    if item["post"].info.get("reply_id") and not reply_to_post:
        logger.info(f"Can't continue thread since reference {item['post'].info['reply_id']} has not been crossposted to Tumblr")
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
        # Look for ANY URL anywhere in the text to trigger a card preview
        any_url_match = re.search(r'(https?://[^\s<>"]+)', combined_text)
        
        if any_url_match:
            target_url = any_url_match.group(1)
            logger.info(f"Uploading rich LINK post with card preview to Tumblr blog '{TUMBLR_BLOG_NAME}'")
            
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
        database.update(item["id"], "tumblr", response["id"])
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