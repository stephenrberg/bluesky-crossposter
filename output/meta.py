import boto3
import os
import requests
import time
import glob
import re
from PIL import Image, ImageOps
from urllib.parse import urlparse, urlunparse, parse_qs
from settings import auth
from settings.paths import image_path
from main.functions import logger, split_text, remove_sky_hashtags
from main.db import database
from main.alerts import send_failure_alert

# --- CLOUDFLARE R2 CONFIG ---

# R2_CLIENT = boto3.client(
#     service_name='s3',
#     endpoint_url=f'https://{auth.R2_ACCOUNT_ID}.r2.cloudflarestorage.com',
#     aws_access_key_id=auth.R2_ACCESS_KEY,
#     aws_secret_access_key=auth.R2_SECRET_KEY,
#     region_name='auto'
# )
# List of domains that require the fallback image injection workaround
TARGET_DOMAINS = ["serializd.com", "goodreads.com", "backloggd.com"]

S3_CLIENT = boto3.client(
    's3',
    region_name=auth.S3_REGION,
    aws_access_key_id=auth.S3_ACCESS_KEY,
    aws_secret_access_key=auth.S3_SECRET_KEY
)


# BUCKET_NAME = auth.R2_BUCKET_NAME
# PUBLIC_URL_BASE = auth.R2_PUBLIC_URL.rstrip('/')

def process_threads_hashtags(text):
    if not text:
        return ""

    # 1. Identify all hashtags
    tags_found = list(re.finditer(r'#(\w+)', text))
    if not tags_found:
        return text.strip()

    # 2. Determine Trailing Cluster (now allows for a URL at the very end)
    # This matches: [optional space] [one or more hashtags] [optional space] [optional URL]
    trailing_pattern = r'((?:\s*#\w+)+)\s*(https?://\S+)?\s*$'
    trailing_match = re.search(trailing_pattern, text)
    
    trailing_tags_str = ""
    url_at_end = ""
    
    if trailing_match:
        trailing_tags_str = trailing_match.group(1) # The cluster of #tags
        url_at_end = trailing_match.group(2) or "" # The URL (if present)

    trailing_tags = re.findall(r'#\w+', trailing_tags_str)

    # 3. Define the Topic (Priority: In-sentence tag > First trailing tag)
    body_tags = [t.group(0) for t in tags_found if t.group(0) not in trailing_tags]
    topic_tag = body_tags[0] if body_tags else (trailing_tags[0] if trailing_tags else tags_found[0].group(0))

    # 4. Cleanup the Trailing Cluster but keep the URL
    if trailing_tags_str:
        # Cut off the tags and the URL, but we'll add the URL back later
        text = text[:trailing_match.start()].rstrip()

    # 5. Deduplication & One-Tag Rule
    # We replace EVERY hashtag we find. If it's the topic_tag, we only keep the FIRST one we see.
    topic_kept = False
    def dedupe_and_remove(match):
        nonlocal topic_kept
        found_tag = match.group(0)
        if found_tag == topic_tag and not topic_kept:
            topic_kept = True
            return found_tag
        return ""

    cleaned_text = re.sub(r'#\w+', dedupe_and_remove, text)

    # 6. Re-attach Topic/URL
    # If the topic was never "kept" (meaning it was only in the trailing cluster)
    if not topic_kept:
        cleaned_text = cleaned_text.rstrip() + f"\n\n{topic_tag}"
    
    # If there was a Letterboxd URL, put it back at the very end
    if url_at_end:
        # CLEANUP FIX: Strip out invisible unicode anomalies (like \u2060) and trailing whitespaces
        url_at_end = re.sub(r'[^\x21-\x7E]+', '', url_at_end).strip()
        cleaned_text = cleaned_text.rstrip() + f"\n\n{url_at_end}"

    # Final polish
    cleaned_text = re.sub(r' +', ' ', cleaned_text)
    return cleaned_text.strip()

def get_dominant_color(img):
    try:
        small_img = img.resize((50, 50))
        result = small_img.convert('P', palette=Image.ADAPTIVE, colors=1)
        result = result.convert('RGB')
        return result.getpixel((0, 0))
    except Exception:
        return (255, 255, 255)

def process_image_for_instagram(local_path):
    try:
        if local_path.lower().endswith((".mp4", ".mov")):
            return local_path 

        with Image.open(local_path) as img:
            img.load()
            
            if img.mode != 'RGB':
                img = img.convert('RGB')
                
            width, height = img.size
            aspect_ratio = width / height

            targets = [
                {"name": "square", "ratio": 1.0, "size": (1080, 1080)},
                {"name": "portrait", "ratio": 0.8, "size": (1080, 1350)},
                {"name": "landscape", "ratio": 1.91, "size": (1080, 566)}
            ]

            best_target = min(targets, key=lambda x: abs(x["ratio"] - aspect_ratio))
            target_size = best_target["size"]

            padding_color = get_dominant_color(img)
            new_img = ImageOps.pad(img, target_size, color=padding_color, centering=(0.5, 0.5))
            
            base_path, ext = os.path.splitext(local_path)
            processed_path = f"{base_path}_processed{ext}"
            
            new_img.save(processed_path, "JPEG", quality=95)
            logger.info(f"Successfully processed: {processed_path}")
            return processed_path

    except Exception as e:
        logger.error(f"Image processing failed: {e}")
        return local_path

# def upload_to_r2(local_path):
#     filename = os.path.basename(local_path)
#     try:
#         content_type = "video/mp4" if filename.lower().endswith(".mp4") else "image/jpeg"
#         # Reverting to the simpler upload_file method
#         R2_CLIENT.upload_file(
#             local_path, 
#             BUCKET_NAME, 
#             filename,
#             ExtraArgs={'ContentType': content_type}
#         )
#         url = f"{PUBLIC_URL_BASE}/{filename}"
#         logger.info(f"Verified R2 Upload: {url}")
#         return url
#     except Exception as e:
#         logger.error(f"R2 Upload failed: {e}")
#         return None

def upload_to_s3(file_path):
    # Use the filename as the object name in S3
    filename = os.path.basename(file_path)
    
    # Correctly identify content type for Meta's ingest bot
    content_type = "video/mp4" if filename.lower().endswith((".mp4", ".mov")) else "image/jpeg"
    
    try:
        S3_CLIENT.upload_file(
            file_path, 
            auth.S3_BUCKET_NAME,
            filename,
            ExtraArgs={'ContentType': content_type}
        )
        # Ensure the URL matches your region
        return f"https://{auth.S3_BUCKET_NAME}.s3.{auth.S3_REGION}.amazonaws.com/{filename}"
    except Exception as e:
        logger.error(f"S3 Upload failed: {e}")
        return None

def handle_klipy_gif(raw_url, post_id, image_dir):
    """
    Parses the validated Klipy link structure, pulls the dynamic mp4 parameter string,
    replaces the trailing .gif reference with the direct video hash, downloads the loop,
    and returns the public cloud destination payload.
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
                logger.info(f"Formed clean target video path destination: {video_url}")
            
                response = requests.get(video_url, timeout=15)
                if response.status_code == 200:
                    local_name = f"gif_{post_id}.mp4"
                    local_path = os.path.join(image_dir, local_name)
                    
                    with open(local_path, "wb") as f:
                        f.write(response.content)
                    
                    s3_url = upload_to_s3(local_path)
                    
                    if os.path.exists(local_path):
                        os.remove(local_path)
                        
                    if s3_url:
                        return s3_url, True
                    
    except Exception as e:
        logger.error(f"Failed to cleanly intercept or swap Klipy directory target parameters: {e}")
        
    return None, False

def output(platform, queue_items):
    image_dir = image_path 

    for item in queue_items:
        source_post = item["post"]
        post_id = item["id"] 
        public_urls = []
        is_video = False
        temp_files = []
        
        # --- CONDITIONAL CARD PREVIEW EMBED DISPATCHER ---
        embed_url = None
        bsky_thumb_url = None
        
        bsky_embed = source_post.info.get("embed", {})
        if bsky_embed and bsky_embed.get("$type") == "app.bsky.embed.external":
            external_data = bsky_embed.get("external", {})
            temp_embed_url = external_data.get("uri")
            temp_thumb_url = external_data.get("thumb")
            
            if temp_embed_url:
                lower_url = temp_embed_url.lower()
                # ONLY force proxy images to download if we are currently posting to Threads
                if platform == "threads" and any(domain in lower_url for domain in TARGET_DOMAINS):
                    embed_url = temp_embed_url
                    bsky_thumb_url = temp_thumb_url
                else:
                    # For Instagram, or non-target domains, completely drop the preview image
                    embed_url = temp_embed_url

        parent_meta_id = database.get_id(source_post.info.get("reply_id"), platform)
        if platform == "instagram" and source_post.info.get("reply_id"):
            logger.info(f"Skipping Instagram for {post_id}: Instagram does not support threads.")
            database.update(post_id, platform, "skipped")
            database.save()
            continue

        if platform == "threads" and source_post.info.get("reply_id") and not parent_meta_id:
            logger.info(f"Threads: Parent {source_post.info['reply_id']} not found yet. Waiting...")
            continue # Leave in queue for next run

        raw_text = source_post.info.get("text", "")
        
        # --- PRECISE EMBED DESTRUCTION & SCRUBBING ENGINE ---
        klipy_s3_url = None
        klipy_is_video = False
        
        klipy_match = re.search(r'(https://static\.klipy\.com/[^\s]+)', raw_text)
        if klipy_match:
            raw_url = klipy_match.group(1)
            logger.info(f"Targeting authenticated native GIF text element: {raw_url}")
            
            s3_link, is_video_loop = handle_klipy_gif(raw_url, post_id, image_dir)
            if s3_link:
                klipy_s3_url = s3_link
                klipy_is_video = is_video_loop
                public_urls.append(klipy_s3_url)
                is_video = True  
                
                raw_text = raw_text.replace(raw_url, "").strip()
                embed_url = None
                bsky_thumb_url = None
        
        if platform == "threads":
            # Apply the logic to find 1 topic tag and clean the rest
            post_text = process_threads_hashtags(raw_text)
        else:
            # Use your standard hashtag remover for Instagram/others
            post_text = remove_sky_hashtags(raw_text)

        has_native_media = bool(source_post.info.get('media')) or bool(klipy_s3_url)
        has_proxy_image = bool(bsky_thumb_url)

        # Safeguard: Instagram skips right away if it has no native media assets
        if platform == "instagram" and (not has_native_media or klipy_s3_url):
            logger.info(f"Skipping Instagram: Post ID {post_id} is link/text-only. Embed image ignored for IG.")
            database.update(post_id, platform, "skipped") 
            database.save()
            continue
      
        logger.info(f"Posting \"{post_text}\" to {platform} (Media count: {len(public_urls)}, Is Video: {is_video})")

        # 1. Process regular attachment media if present
        if has_native_media and not klipy_s3_url:
            all_files = glob.glob(os.path.join(image_dir, "*"))
            
            this_post_files = []
            for f in all_files:
                fname = os.path.basename(f)
                
                # Check if it starts with the post_id followed by a boundary (like an underscore or extension dot)
                # and explicitly reject any temp files
                starts_with_id = fname.startswith(str(post_id))
                is_temp_file = "_processed" in fname or "thumb_" in fname
                
                if starts_with_id and not is_temp_file:
                    this_post_files.append(f)
            
            # Sort files to maintain sequence consistency
            this_post_files.sort()

            if this_post_files:
                for path in this_post_files:
                    work_path = path
                    if platform == "instagram" and not path.lower().endswith((".mp4", ".mov")):
                        work_path = process_image_for_instagram(path)
                        if work_path != path:
                            temp_files.append(work_path)

                    content_url = upload_to_s3(work_path)
                    if content_url:
                        public_urls.append(content_url)
                        if path.lower().endswith((".mp4", ".mov")):
                            is_video = True
            else:
                logger.error(f"Media expected but files not found for ID {post_id}")
                continue

        # 2. Alternatively, route targeted external link card thumb through S3 bucket (Threads Only)
        elif has_proxy_image and platform == "threads" and not klipy_s3_url:
            logger.info(f"Downloading external proxy link card thumbnail from: {bsky_thumb_url}")
            try:
                response = requests.get(bsky_thumb_url, timeout=15)
                if response.status_code == 200:
                    ext = ".jpg"
                    if "image/png" in response.headers.get("Content-Type", ""):
                        ext = ".png"
                        
                    local_thumb_name = f"thumb_{post_id}{ext}"
                    local_thumb_path = os.path.join(image_dir, local_thumb_name)
                    
                    with open(local_thumb_path, "wb") as f:
                        f.write(response.content)
                    temp_files.append(local_thumb_path)
                    
                    content_url = upload_to_s3(local_thumb_path)
                    if content_url:
                        public_urls.append(content_url)
                        logger.info(f"Proxy link card thumbnail routed to S3 successfully: {content_url}")
            except Exception as thumb_err:
                logger.error(f"Failed to process external proxy link card image: {thumb_err}")
        
        # 3. Publish execution
        success, new_meta_id = publish_to_meta(
            platform, post_text, public_urls, is_video, reply_id=parent_meta_id, embed_url=embed_url
        )
        
        # Cleanup processed temp files
        for f in temp_files:
            try:
                if os.path.exists(f):
                    os.remove(f)
                    logger.info(f"Cleaned up temp file: {f}")
            except Exception as e:
                logger.error(f"Failed to delete temp file {f}: {e}")
        
        if success:
            database.update(post_id, platform, new_meta_id)
            database.save() 
        else:
            logger.error(f"Failed to post to {platform}")
            send_failure_alert(platform, f"Failed to post to {platform}")

def publish_to_meta(platform, text, media_urls, is_video, reply_id=None, embed_url=None):
    if not media_urls and platform == "instagram":
        return False, None

    token = auth.IG_ACCESS_TOKEN if platform == "instagram" else auth.THREADS_ACCESS_TOKEN
    user_id = auth.IG_USER_ID if platform == "instagram" else auth.THREADS_USER_ID
    
    if platform == "instagram":
        base = f"https://graph.facebook.com/v21.0/{user_id}/media"
        pub = f"https://graph.facebook.com/v21.0/{user_id}/media_publish"
        text_key = 'caption'
    else:
        base = f"https://graph.threads.net/v1.0/{user_id}/threads"
        pub = f"https://graph.threads.net/v1.0/{user_id}/threads_publish"
        text_key = 'text'

    # --- SINGLE VIDEO LOGIC ---
    if is_video and media_urls:
        video_url = media_urls[0]
        payload = {
            'access_token': token, 
            text_key: text,
            'media_type': 'REELS' if platform == "instagram" else 'VIDEO',
            'video_url': video_url
        }

    # --- CAROUSEL LOGIC (Multiple Images) ---
    elif len(media_urls) > 1 and not is_video:
        child_ids = []
        logger.info(f"Creating carousel with {len(media_urls)} images...")
        
        for url in media_urls:
            child_payload = {
                'access_token': token,
                'image_url': url,
                'media_type': 'IMAGE'
            }

            # Instagram needs this flag; Threads does not (and might error if it's there)
            if platform == "instagram":
                child_payload['is_carousel_item'] = 'true'
              
            res = requests.post(base, data=child_payload).json()
            child_id = res.get('id')
            if child_id:
                child_ids.append(child_id)
            else:
                logger.error(f"Failed to create child container for {url}: {res}")

        if len(child_ids) < len(media_urls):
            logger.error("Not all images were processed. Aborting carousel.")
            return False, None

        # Create the Parent Container
        payload = {
            'access_token': token,
            'media_type': 'CAROUSEL',
            'children': ','.join(child_ids),
            text_key: text
        }

    # --- SINGLE IMAGE LOGIC ---
    elif media_urls:
        payload = {
            'access_token': token, 
            text_key: text,
            'media_type': 'IMAGE', 
            'image_url': media_urls[0]
        }
    
    # --- TEXT OR NATIVE LINK ATTACHMENT ---
    else:
        payload = {'access_token': token, text_key: text, 'media_type': 'TEXT'}
        if platform == "threads" and embed_url:
            payload['link_attachment'] = embed_url

    if platform == "threads" and reply_id:
        payload['reply_to_id'] = reply_id

    # 1. Create Main Container
    res = requests.post(base, data=payload).json()
    container_id = res.get('id')
    if not container_id:
        logger.error(f"META API ERROR: {res}")
        return False, None

    # --- PROCESSING WAIT ---
    if platform == 'threads':
        wait_time = 150 if is_video else (60 + (len(media_urls) - 1) * 10)
        logger.info(f"Threads container {container_id} created. Waiting {wait_time}s...")
        time.sleep(wait_time)
    else:
        # Polling Instagram
        max_retries = 20
        for i in range(max_retries):
            status_url = f"https://graph.facebook.com/v21.0/{container_id}"
            status_res = requests.get(status_url, params={'fields': 'status_code', 'access_token': token}).json()
            status = status_res.get('status_code')
            logger.info(f"Instagram status for {container_id}: {status}")

            if status == "FINISHED":
                break
            elif status == "ERROR":
                logger.error(f"Instagram reports container error: {status_res}")
                return False, None
            time.sleep(20)
        else:
            logger.error(f"Instagram timed out waiting for container {container_id}")
            return False, None

    # 2. Final Publish
    final = requests.post(pub, data={'creation_id': container_id, 'access_token': token})
    
    if final.status_code == 200:
        published_meta_id = final.json().get('id')
        logger.info(f"Successfully posted to {platform}: {published_meta_id}")
        return True, published_meta_id
    else:
        logger.error(f"Final {platform} Publish Failed: {final.json()}")
        return False, None