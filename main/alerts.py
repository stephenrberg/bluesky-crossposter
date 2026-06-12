import os
import json
import time
import requests
from settings import auth

# Configuration
COOLDOWN_SECONDS = 3600  # 1 hour timeout per individual platform
COOLDOWN_FILE = "./.alert_cooldowns.json"  # Swapped to .json extension for key/value storage

def send_failure_alert(platform_name, error_details):
    # 0. Safety Check: Exit immediately if Pushover credentials are blank/missing
    token = getattr(auth, 'PUSHOVER_TOKEN', None)
    user = getattr(auth, 'PUSHOVER_USER', None)
    
    if not token or not user:
        print(f"[Alert Error]: Cannot alert for {platform_name}. Pushover credentials are blank or unconfigured.")
        return

    current_time = time.time()
    platform_key = platform_name.lower().strip()

    # 1. Load existing cooldown dictionary mapping platforms to timestamps
    cooldowns = {}
    if os.path.exists(COOLDOWN_FILE):
        try:
            with open(COOLDOWN_FILE, "r") as f:
                cooldowns = json.load(f)
        except (json.JSONDecodeError, OSError):
            # If the file is corrupted or empty, just start fresh
            pass

    # 2. Check if THIS specific platform is in a cooldown window
    if platform_key in cooldowns:
        last_alert_time = cooldowns[platform_key]
        if current_time - last_alert_time < COOLDOWN_SECONDS:
            print(f"[Alert Throttled]: {platform_name} failed, but silencing notification due to individual cooldown.")
            return

    # 3. Prepare the payload (Truncate massive logs so Pushover accepts it)
    clean_error = str(error_details)
    if len(clean_error) > 500:
        clean_error = clean_error[:500] + "\n...[Truncated]"

    payload = {
        "token": token,
        "user": user,
        "title": f"🚨 Crossposter Crash: {platform_name.upper()}",
        "message": f"Pipeline failed on {platform_name}.\n\nError:\n{clean_error}",
        "priority": 1,
        "sound": "falling"
    }

    # 4. Fire the notification
    try:
        response = requests.post("https://api.pushover.net/1/messages.json", data=payload, timeout=5)
        
        # 5. If successful, update ONLY this platform's unique timestamp and save the JSON
        if response.status_code == 200:
            cooldowns[platform_key] = current_time
            with open(COOLDOWN_FILE, "w") as f:
                json.dump(cooldowns, f, indent=2)
            print(f"Pushover alert dispatched for {platform_name}.")
        else:
            print(f"Pushover rejected request: {response.text}")
            
    except Exception as e:
        print(f"Failed to communicate with Pushover API: {e}")