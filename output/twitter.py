import traceback
import subprocess
import json
import time
import random
import os
import re
import requests
from urllib.parse import urlparse, urlunparse
from main.functions import logger
from settings.auth import *
from settings import settings
from main.db import database
from main.alerts import send_failure_alert

# List of domains that require the image injection workaround
TARGET_DOMAINS = ["serializd.com", "goodreads.com"]

def handle_klipy_twitter_gif(raw_url, post_id, image_dir):
    """
    Downloads the clean source GIF file from Klipy directly onto the local host system
    so the Puppeteer browser automation container can attach it natively as a media file.
    """
    try:
        parsed_url = urlparse(raw_url)
        # Strip the trailing query parameters to isolate the raw static GIF asset path
        clean_path = urlunparse((parsed_url.scheme, parsed_url.netloc, parsed_url.path, '', '', ''))
        
        logger.info(f"Downloading original source GIF for Twitter automation: {clean_path}")
        
        response = requests.get(clean_path, timeout=15)
        if response.status_code == 200:
            local_name = f"gif_{post_id}.gif"
            local_path = os.path.join(image_dir, local_name)
            
            with open(local_path, "wb") as f:
                f.write(response.content)
            
            return local_path
            
    except Exception as e:
        logger.error(f"Failed to pull remote Klipy GIF binary for local file upload: {e}")
        
    return None

def output(queue):
    total_items = len(queue)
    index = 0
    for item in queue:
        try:
            if item["type"] == "repost" and settings.retweets:
                logger.info("Reposts via browser automation are skipped to prevent flagging.")
                database.failed_post(item["id"], "twitter")
            else:
                post(item)
        except Exception as e:
            database.failed_post(item["id"], "twitter")
            logger.error(f"Failed to post {item['id']}: {e}")
            logger.debug(traceback.format_exc())
            send_failure_alert("Twitter", e)
        
        index = index + 1
        if index < total_items:
            queue_cooldown = random.randint(20, 40)
            logger.info(f"Pausing for {queue_cooldown} seconds before processing the next item in the queue...")
            time.sleep(queue_cooldown)

# Function for posting tweets via Puppeteer
def post(item):
    # Set up our working path directory for temporary GIF downloads
    from settings.paths import image_path
    image_dir = image_path

    text_content = item["post"].text_content("twitter")
    quote_id = database.get_id(item["post"].info["quote_id"], "twitter")
    reply_id = database.get_id(item["post"].info["reply_id"], "twitter")
    
    if item["post"].info["reply_id"] and not reply_id or reply_id in ["skipped", "FailedToPost", "duplicate"]:
        logger.info(f"Can't continue thread since {item['post'].info['reply_id']} has not been crossposted")
        return
        
    media = item["post"].media
    total_items = len(text_content)
    index = 0

    # --- DICTIONARY LOOKUP (UPSTREAM VIA BLUESKY.PY) ---
    embed_url = None
    bsky_thumb_url = None
    
    # Grab our fresh pre-resolved serialized embed data directly from the info object
    bsky_embed = item["post"].info.get("embed", {})
    if bsky_embed and bsky_embed.get("$type") == "app.bsky.embed.external":
        external_data = bsky_embed.get("external", {})
        temp_embed_url = external_data.get("uri")
        temp_thumb_url = external_data.get("thumb")
        
        if temp_embed_url:
            embed_url = temp_embed_url
            lower_url = temp_embed_url.lower()
            
            # Check if the target URL matches any domain in your target list
            if any(domain in lower_url for domain in TARGET_DOMAINS):
                bsky_thumb_url = temp_thumb_url
                logger.info(f"Target domain matched framework logic ({embed_url}). Enabling proxy image injection.")
            else:
                logger.info(f"Standard domain detected ({embed_url}). Skipping image injection proxy to preserve native Twitter scraper behavior.")
    
    # Track any local files generated inside this loop execution to clean up later
    local_temp_gifs = []

    for text_post in text_content:
        # --- PRECISE TWITTER GIF EXTRACTION & SCRUBBING ENGINE ---
        klipy_match = re.search(r'(https://static\.klipy\.com/[^\s\n\r]+)', text_post)
        local_gif_path = None
        
        if klipy_match:
            raw_url = klipy_match.group(1)
            logger.info(f"Targeting authenticated native Twitter GIF text element: {raw_url}")
            
            # Download file locally to your machine's temporary script dir
            local_gif_path = handle_klipy_twitter_gif(raw_url, item["id"], image_dir)
            if local_gif_path:
                local_temp_gifs.append(local_gif_path)
                
                # Aggressively slice out the raw URL string match along with immediate trailing whitespace bounds
                text_post = re.sub(re.escape(raw_url) + r'\s*', '', text_post).strip()
                embed_url = None
                bsky_thumb_url = None

        logger.info(f"Posting \"{text_post}\" to Twitter via Puppeteer.")
        
        media_paths = []
        # If an embedded Klipy GIF was processed, prioritize it natively inside the upload sequence
        if local_gif_path:
            media_paths = [local_gif_path]
            media = [] 
        elif media:
            media_paths = [media_item["filename"] for media_item in media]
            media = [] 

        # Prepare parameters to pass over to Node.js
        payload = {
            "text": text_post,
            "reply_id": reply_id if reply_id else None,
            "quote_id": quote_id if quote_id else None,
            "media": media_paths,
            "embed_url": embed_url,
            "bsky_thumb_url": bsky_thumb_url
        }

        try:
            # Execute the Puppeteer script and capture the newly created Tweet ID from stdout
            result = subprocess.run(
                ["node", "crossposter.js", json.dumps(payload)],
                capture_output=True,
                text=True,
                check=True
            )
            
            # The JS script prints the numeric Tweet ID on success
            output_lines = result.stdout.strip().split("\n")
            new_tweet_id = output_lines[-1] # Target the last printed line
            
            logger.info(f"Successfully posted! New Tweet ID: {new_tweet_id}")
            
            # Form subsequent parts of the thread as direct replies to this new ID
            quote_id = None
            reply_id = new_tweet_id
            
        except subprocess.CalledProcessError as err:
            logger.error(f"Puppeteer script crashed: {err.stderr}")
            # Ensure local trash cleanup runs even if sub-process throws an exception
            for f in local_temp_gifs:
                if os.path.exists(f):
                    os.remove(f)
            raise Exception("Puppeteer automated browser dispatch failed.")

        index = index + 1
        if index < total_items:
            queue_cooldown = random.randint(10, 20)
            logger.info(f"Pausing for {queue_cooldown} seconds before processing the next item in the queue...")
            time.sleep(queue_cooldown)

    # Post-execution cleanup: Erase temporary downloaded GIF assets from disk
    for f in local_temp_gifs:
        try:
            if os.path.exists(f):
                os.remove(f)
                logger.info(f"Cleaned up local temporary GIF asset file: {f}")
        except Exception as e:
            logger.error(f"Failed to delete local temporary GIF payload structure {f}: {e}")

    database.update(item["id"], "twitter", reply_id)

def delete_post(origin_id):
    logger.info("Delete operations are skipped under headless browser configurations.")