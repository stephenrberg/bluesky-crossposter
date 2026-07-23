import traceback
from pytumblr import TumblrRestClient
from main.functions import logger
from main.connections import tumblr_connect  
from main.db import database
from main.alerts import send_failure_alert
import re
import time
import json
import requests
import os
import tempfile
from urllib.parse import urlparse, urlunparse

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

def handle_klipy_tumblr_gif(raw_url, post_id):
    """
    Downloads the clean source GIF file from Klipy directly onto the local host system
    so the Tumblr API client can attach it natively as a media parameter.
    """
    try:
        parsed_url = urlparse(raw_url)
        # Strip trailing parameters to isolate the raw static GIF asset path
        clean_path = urlunparse((parsed_url.scheme, parsed_url.netloc, parsed_url.path, '', '', ''))
        
        logger.info(f"Downloading original source GIF for Tumblr upload: {clean_path}")
        
        response = requests.get(clean_path, timeout=15)
        if response.status_code == 200:
            fd, temp_path = tempfile.mkstemp(suffix=".gif")
            with open(fd, "wb") as f:
                f.write(response.content)
            
            return temp_path
            
    except Exception as e:
        logger.error(f"Failed to pull remote Klipy GIF binary for Tumblr payload: {e}")
        
    return None

def wait_for_media_processing(tumblr_client, blog_name, draft_id, media_type="photo"):
    """
    Polls the Tumblr API using exponential backoff until the draft media 
    has been fully processed and its CDN URLs are ready.
    """
    delay = 2
    max_delay = 60
    attempts = 0
    max_attempts = 10
    
    while attempts < max_attempts:
        try:
            info = tumblr_client.posts(blog_name, id=draft_id)
            if "posts" in info and len(info["posts"]) > 0:
                post_data = info["posts"][0]
                
                if media_type == "video":
                    video_url = post_data.get("video_url")
                    # Ensure video URL exists and post state isn't stuck processing
                    if video_url and post_data.get("state") != "transcoding":
                        return post_data
                else:  # photo
                    photos = post_data.get("photos", [])
                    if photos and photos[0].get("original_size", {}).get("url"):
                        return post_data
                        
        except Exception as e:
            logger.warning(f"Error checking draft {draft_id} status: {e}")
            
        logger.info(f"Draft {draft_id} ({media_type}) still processing. Retrying in {delay}s...")
        time.sleep(delay)
        delay = min(delay * 2, max_delay)
        attempts += 1
        
    raise TimeoutError(f"Tumblr took too long to process the draft {media_type} asset.")

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
    
    # Track any local temporary files generated in this run context to ensure quick cleanup
    temp_files_to_clean = []
    drafts_to_delete = []
    klipy_gif_local_path = None
    
    # --- PRECISE EMBED DESTRUCTION & SCRUBBING ENGINE ---
    # Scan the text content components for presence of the dynamic Klipy URL block
    # Doing this prior to thread splitting ensures text strings are cleanly updated upstream
    klipy_match = re.search(r'(https://static\.klipy\.com/[^\s\n\r]+)', full_raw_text)
    if klipy_match:
        raw_url = klipy_match.group(1)
        logger.info(f"Targeting authenticated native Tumblr GIF text element: {raw_url}")
        
        # Pull the asset down to the host partition
        local_gif = handle_klipy_tumblr_gif(raw_url, item["id"])
        if local_gif:
            klipy_gif_local_path = local_gif
            temp_files_to_clean.append(local_gif)
            
            # Aggressively remove the link and trailing spaces right out of all parts of the thread text
            text_content = [re.sub(re.escape(raw_url) + r'\s*', '', part).strip() for part in text_content]
    
    # Re-stitch the normalized clean content strings together
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
                raise Exception(f"Parent post {reply_to_post} found in database but not found on Tumblr API.")
        except Exception as fetch_err:
            raise Exception(f"Failed to fetch metadata for parent post {reply_to_post}: {fetch_err}")

        # Combine, lowercase, and deduplicate tags from both posts
        merged_tags_set = set(tag.lower() for tag in tags + parent_tags)
        combined_tags = list(merged_tags_set)

        # --- MEDIA WITHIN THREADS WORKAROUND (PURE HTML STRATEGY) ---
        embedded_media_html = ""
        contains_video = False
        
        # If an embedded text GIF was processed, prioritize it inside the HTML thread container logic
        if klipy_gif_local_path:
            try:
                logger.info(f"Uploading thread text GIF proxy asset to CDN: {klipy_gif_local_path}")
                media_res = tumblr_client.create_photo(TUMBLR_BLOG_NAME, state="draft", data=klipy_gif_local_path)
                if media_res and "id" in media_res:
                    photo_info = wait_for_media_processing(tumblr_client, TUMBLR_BLOG_NAME, media_res["id"], media_type="photo")
                    final_photo_id = photo_info.get("id", media_res["id"])
                    photos = photo_info.get("photos", [])
                    
                    for p in photos:
                        img_url = p.get("original_size", {}).get("url")
                        if img_url:
                            embedded_media_html += f'<img src="{img_url}"><br><br>'
                    
                    drafts_to_delete.append(final_photo_id)
            except Exception as media_upload_err:
                logger.error(f"Failed to inline thread Klipy GIF to HTML block: {media_upload_err}")
                
        elif item["post"].media:
            media_paths = [media_item["filename"] for media_item in item["post"].media]
            
            for path in media_paths:
                try:
                    if path.lower().endswith((".mp4", ".mov")):
                        contains_video = True
                        logger.info(f"Uploading thread video asset proxy to CDN: {path}")
                        media_res = tumblr_client.create_video(TUMBLR_BLOG_NAME, state="draft", data=path)
                        if media_res and "id" in media_res:
                            # Use backoff polling loop to ensure it is processed
                            video_info = wait_for_media_processing(tumblr_client, TUMBLR_BLOG_NAME, media_res["id"], media_type="video")
                            
                            # CRITICAL FIX: Extract the post-transcoded final ID from the processing response
                            final_video_id = video_info.get("id", media_res["id"])
                            video_url = video_info.get("video_url", "")
                            
                            if video_url:
                                embedded_media_html += f'<video controls src="{video_url}" width="100%"></video><br><br>'
                            
                            drafts_to_delete.append(final_video_id)
                    else:
                        logger.info(f"Uploading thread image asset proxy to CDN: {path}")
                        media_res = tumblr_client.create_photo(TUMBLR_BLOG_NAME, state="draft", data=path)
                        if media_res and "id" in media_res:
                            # Use backoff polling loop to ensure it is processed
                            photo_info = wait_for_media_processing(tumblr_client, TUMBLR_BLOG_NAME, media_res["id"], media_type="photo")
                            
                            # Extract final post ID in case image normalization changed the placeholder
                            final_photo_id = photo_info.get("id", media_res["id"])
                            photos = photo_info.get("photos", [])
                            
                            for p in photos:
                                img_url = p.get("original_size", {}).get("url")
                                if img_url:
                                    embedded_media_html += f'<img src="{img_url}"><br><br>'
                            
                            drafts_to_delete.append(final_photo_id)
                except Exception as media_upload_err:
                    logger.error(f"Failed to inline thread media to HTML block: {media_upload_err}")
            
        # Convert text newlines to standard HTML line breaks for layout rendering
        html_text = combined_text.replace("\n", "<br>")
        final_comment = f"{embedded_media_html}{html_text}".strip()

        try:
            response = tumblr_client.reblog(
                TUMBLR_BLOG_NAME,
                id=reply_to_post,
                reblog_key=reblog_key,
                comment=final_comment,
                tags=combined_tags,
                format="html"
            )
            
            if response and "id" in response:
                initial_id = str(response["id"])
                final_post_id = initial_id
                
                # Double-check post ID if a video was included in the thread
                if contains_video:
                    logger.info("Video detected in thread. Resolving published post ID from blog timeline...")
                    for _ in range(12):  # Poll every 5s up to 60s total
                        time.sleep(5)
                        try:
                            recent = tumblr_client.posts(TUMBLR_BLOG_NAME, limit=5)
                            posts_list = recent.get("posts", [])
                            
                            for p in posts_list:
                                reblog_parent = str(p.get("reblogged_from_id", ""))
                                if reblog_parent == str(reply_to_post):
                                    final_post_id = str(p["id"])
                                    break
                            
                            if final_post_id != initial_id:
                                logger.info(f"Corrected video thread post ID: {initial_id} -> {final_post_id}")
                                break
                        except Exception as verify_err:
                            logger.warning(f"Failed to resolve video thread post ID: {verify_err}")

                database.update(item["id"], "tumblr", final_post_id)
                logger.info(f"Successfully threaded to Tumblr! Post ID: {final_post_id}")
            else:
                raise Exception(f"Tumblr API returned an unexpected payload during thread reblog: {response}")
        finally:
            for f in temp_files_to_clean:
                if os.path.exists(f):
                    os.remove(f)
            for draft_id in drafts_to_delete:
                try:
                    tumblr_client.delete_post(TUMBLR_BLOG_NAME, draft_id)
                except Exception as del_err:
                    logger.warning(f"Failed to delete proxy draft {draft_id}: {del_err}")
        return

    media = item["post"].media

    # --- MEDIA / ROOT GIF POST HANDLING ---
    # Trigger photo/GIF mapping workflow if a Klipy asset was isolated OR native media is available
    if media or klipy_gif_local_path:
        cull_pattern = r'(?<=\n)(?:\s*#\w+)+\s*$|(?<=[\.!\?])(?:\s+#\w+)+\s*$'
        combined_text = re.sub(cull_pattern, "", combined_text).rstrip()
        combined_text = re.sub(r'#(\w+)', lambda m: m.group(1), combined_text)
        
        url_pattern = r'(https?://[^\s<>"]+)'
        combined_text = re.sub(url_pattern, r'[\1](\1)', combined_text)

        try:
            if klipy_gif_local_path:
                logger.info(f"Uploading text-extracted GIF post to Tumblr blog '{TUMBLR_BLOG_NAME}'")
                response = tumblr_client.create_photo(
                    TUMBLR_BLOG_NAME, state="published", tags=tags, format="markdown",
                    caption=combined_text, data=klipy_gif_local_path
                )
            else:
                media_paths = [media_item["filename"] for media_item in media]
                is_video = any(path.lower().endswith((".mp4", ".mov")) for path in media_paths)

                if is_video:
                    logger.info(f"Uploading video post to Tumblr blog '{TUMBLR_BLOG_NAME}'")
                    video_caption = combined_text.replace("\n", "<br>")
                    response = tumblr_client.create_video(
                        TUMBLR_BLOG_NAME, state="published", tags=tags, format="markdown",
                        caption=video_caption, data=media_paths[0] 
                    )
                    
                    # --- MASSIVE WAIT / VERIFICATION LOOP FOR ROOT VIDEO TRANSCODING ---
                    if response and "id" in response:
                        temp_id = str(response["id"])
                        final_id = temp_id
                        
                        logger.info(f"Root video post submitted (Initial ID: {temp_id}). Entering extended transcoding wait...")
                        
                        # Poll every 5s for up to 3 minutes (36 retries)
                        for attempt in range(36):
                            time.sleep(5)
                            try:
                                recent = tumblr_client.posts(TUMBLR_BLOG_NAME, limit=3)
                                posts_list = recent.get("posts", [])
                                
                                if posts_list:
                                    top_post = posts_list[0]
                                    top_id = str(top_post["id"])
                                    top_state = top_post.get("state", "")
                                    
                                    # When finished, the post gets published and obtains its final ID
                                    if top_state != "transcoding":
                                        final_id = top_id
                                        logger.info(f"Transcoding complete after {(attempt+1)*5}s! Permanent Post ID resolved: {final_id}")
                                        break
                            except Exception as e:
                                logger.warning(f"Error checking root video transcode state: {e}")
                        
                        # Store the final resolved ID back into response so the DB update grabs it
                        response["id"] = final_id
                else:
                    logger.info(f"Uploading photo post to Tumblr blog '{TUMBLR_BLOG_NAME}'")
                    response = tumblr_client.create_photo(
                        TUMBLR_BLOG_NAME, state="published", tags=tags, format="markdown",
                        caption=combined_text, data=media_paths
                    )
        finally:
            for f in temp_files_to_clean:
                if os.path.exists(f):
                    os.remove(f)
            
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