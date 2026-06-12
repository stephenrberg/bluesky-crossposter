import traceback
import subprocess
import json
import time
import random
from main.functions import logger
from settings.auth import *
from settings import settings
from main.db import database
from main.alerts import send_failure_alert

# Function for processing output queue
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
        
        index = index +1
        if index < total_items:
            queue_cooldown = random.randint(20, 40)
            logger.info(f"Pausing for {queue_cooldown} seconds before processing the next item in the queue...")
            time.sleep(queue_cooldown)

# Function for posting tweets via Puppeteer
def post(item):
    text_content = item["post"].text_content("twitter")
    quote_id = database.get_id(item["post"].info["quote_id"], "twitter")
    reply_id = database.get_id(item["post"].info["reply_id"], "twitter")
    
    if item["post"].info["reply_id"] and not reply_id or reply_id in ["skipped", "FailedToPost", "duplicate"]:
        logger.info(f"Can't continue thread since {item['post'].info['reply_id']} has not been crossposted")
        return
        
    media = item["post"].media
    total_items = len(text_content)
    index = 0
    
    for text_post in text_content:
        logger.info(f"Posting \"{text_post}\" to Twitter via Puppeteer.")
        
        # Prepare media files payload if present
        media_paths = []
        if media:
            media_paths = [media_item["filename"] for media_item in media]
            media = [] # Media belongs only to the first tweet of a split thread

        # Prepare parameters to pass over to Node.js
        payload = {
            "text": text_post,
            "reply_id": reply_id if reply_id else None,
            "quote_id": quote_id if quote_id else None,
            "media": media_paths
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
            raise Exception("Puppeteer automated browser dispatch failed.")

        index = index +1
        if index < total_items:
            queue_cooldown = random.randint(10, 20)
            logger.info(f"Pausing for {queue_cooldown} seconds before processing the next item in the queue...")
            time.sleep(queue_cooldown)

    database.update(item["id"], "twitter", reply_id)

def delete_post(origin_id):
    logger.info("Delete operations are skipped under headless browser configurations.")