import os
import json
import requests
from datetime import datetime, timedelta
from pathlib import Path
from loguru import logger
from settings.paths import base_path

# Path to store token expiry tracking
TOKEN_EXPIRY_FILE = os.path.join(base_path, "token_expiry.json")

def load_token_expiry():
    """Load token expiry dates from JSON file."""
    if os.path.exists(TOKEN_EXPIRY_FILE):
        try:
            with open(TOKEN_EXPIRY_FILE, 'r') as f:
                return json.load(f)
        except Exception as e:
            logger.warning(f"Could not load token expiry file: {e}")
            return {}
    return {}

def save_token_expiry(expiry_data):
    """Save token expiry dates to JSON file."""
    try:
        with open(TOKEN_EXPIRY_FILE, 'w') as f:
            json.dump(expiry_data, f, indent=2)
    except Exception as e:
        logger.error(f"Could not save token expiry file: {e}")

def should_refresh_tokens():
    """
    Check if tokens should be refreshed based on:
    1. Last failure timestamp (retry after 24 hours on failure)
    2. Last success timestamp (refresh every 7 days)
    3. Token expiry dates (refresh if expiring within 2 days)
    """
    try:
        expiry_data = load_token_expiry()
        
        # If no expiry data exists, refresh anyway (first run)
        if not expiry_data:
            return True
        
        # Check if any token has failed and needs retry after 24 hours
        for token_name in ["THREADS_ACCESS_TOKEN", "IG_ACCESS_TOKEN"]:
            failed_key = f"{token_name}_failed"
            if failed_key in expiry_data:
                try:
                    last_failure = datetime.fromisoformat(expiry_data[failed_key])
                    hours_since_failure = (datetime.now() - last_failure).total_seconds() / 3600
                    
                    if hours_since_failure >= 24:
                        logger.info(f"Token {token_name} failed {hours_since_failure:.1f} hours ago, retrying now")
                        return True
                    else:
                        logger.debug(f"Token {token_name} failed {hours_since_failure:.1f} hours ago, will retry in {24-hours_since_failure:.1f} hours")
                except ValueError:
                    # Invalid date format, try to refresh
                    return True
        
        # Check 7-day refresh schedule
        last_refresh = expiry_data.get("last_refresh")
        if not last_refresh:
            return True
        
        try:
            last_refresh_date = datetime.fromisoformat(last_refresh)
            days_since_refresh = (datetime.now() - last_refresh_date).days
            
            if days_since_refresh >= 7:
                logger.info(f"Token refresh needed: {days_since_refresh} days since last refresh")
                return True
        except ValueError:
            return True
        
        # Check if any token with valid expiry is expiring in the next 2 days
        for token_name, expiry_str in expiry_data.items():
            if token_name in ["last_refresh", "THREADS_ACCESS_TOKEN_failed", "IG_ACCESS_TOKEN_failed"]:
                continue
            
            # Skip tokens with no expiry information (like "unknown" or "never_expires")
            if not expiry_str or expiry_str in ["unknown", "never_expires"]:
                continue
            
            try:
                expiry_date = datetime.fromisoformat(expiry_str)
                days_until_expiry = (expiry_date - datetime.now()).days
                
                if days_until_expiry <= 2:
                    logger.info(f"Token {token_name} expiring in {days_until_expiry} days")
                    return True
            except ValueError:
                # Invalid date format, skip it and rely on 7-day rule
                logger.debug(f"Skipping invalid expiry date for {token_name}: {expiry_str}")
                continue
        
        return False
    except Exception as e:
        logger.warning(f"Error checking token expiry: {e}")
        return True

def refresh_threads_token(current_token):
    """Refresh Threads access token."""
    try:
        logger.info("Refreshing Threads token...")
        response = requests.get(
            "https://graph.threads.net/refresh_access_token",
            params={
                "grant_type": "th_refresh_token",
                "access_token": current_token
            },
            timeout=10
        )
        response.raise_for_status()
        data = response.json()
        
        if "access_token" in data:
            new_token = data["access_token"]
            expiry_date = None
            
            if "expires_in" in data:
                expiry_date = (datetime.now() + timedelta(seconds=data["expires_in"])).isoformat()
                logger.info(f"Threads token refreshed! Expires: {expiry_date}")
            else:
                expiry_date = "never_expires"
                logger.info("Threads token refreshed! (No expiry info)")
            
            return new_token, expiry_date
        else:
            logger.warning("No access token in Threads response")
            return None, None
            
    except requests.exceptions.RequestException as e:
        logger.warning(f"Error refreshing Threads token: {e}")
        return None, None
    except Exception as e:
        logger.warning(f"Unexpected error refreshing Threads token: {e}")
        return None, None

def refresh_ig_token(current_token):
    """Refresh Instagram access token."""
    try:
        logger.info("Refreshing Instagram token...")
        response = requests.post(
            "https://graph.instagram.com/refresh_access_token",
            data={
                "grant_type": "ig_refresh_token",
                "access_token": current_token
            },
            timeout=10
        )
        response.raise_for_status()
        data = response.json()
        
        if "access_token" in data:
            new_token = data["access_token"]
            expiry_date = None
            
            if "expires_in" in data:
                expiry_date = (datetime.now() + timedelta(seconds=data["expires_in"])).isoformat()
                logger.info(f"Instagram token refreshed! Expires: {expiry_date}")
            else:
                expiry_date = "never_expires"
                logger.info("Instagram token refreshed! (No expiry info)")
            
            return new_token, expiry_date
        else:
            logger.warning("No access token in Instagram response")
            return None, None
            
    except requests.exceptions.RequestException as e:
        logger.warning(f"Error refreshing Instagram token: {e}")
        return None, None
    except Exception as e:
        logger.warning(f"Unexpected error refreshing Instagram token: {e}")
        return None, None

def update_auth_file(threads_token=None, ig_token=None, threads_expiry=None, ig_expiry=None):
    """Update the auth.py file with new tokens."""
    auth_file = os.path.join(base_path, "settings", "auth.py")
    
    try:
        with open(auth_file, 'r') as f:
            content = f.read()
        
        updated = False
        
        if threads_token:
            import re
            pattern = r'THREADS_ACCESS_TOKEN\s*=\s*["\']([^"\']+)["\']'
            replacement = f'THREADS_ACCESS_TOKEN = "{threads_token}"'
            content = re.sub(pattern, replacement, content)
            updated = True
            logger.info("Updated THREADS_ACCESS_TOKEN in auth.py")
        
        if ig_token:
            import re
            pattern = r'IG_ACCESS_TOKEN\s*=\s*["\']([^"\']+)["\']'
            replacement = f'IG_ACCESS_TOKEN = "{ig_token}"'
            content = re.sub(pattern, replacement, content)
            updated = True
            logger.info("Updated IG_ACCESS_TOKEN in auth.py")
        
        if updated:
            with open(auth_file, 'w') as f:
                f.write(content)
            logger.info(f"Successfully updated {auth_file}")
            return True
    except Exception as e:
        logger.error(f"Error updating auth file: {e}")
        return False
    
    return False

def refresh_tokens():
    """Main function to refresh tokens."""
    from settings.auth import THREADS_ACCESS_TOKEN, IG_ACCESS_TOKEN
    
    logger.info("Starting token refresh process...")
    
    expiry_data = load_token_expiry()
    threads_success = False
    ig_success = False
    
    # Refresh Threads token
    new_threads_token, threads_expiry = refresh_threads_token(THREADS_ACCESS_TOKEN)
    if new_threads_token:
        if update_auth_file(threads_token=new_threads_token):
            expiry_data["THREADS_ACCESS_TOKEN"] = threads_expiry or "unknown"
            # Clear failure marker on success
            expiry_data.pop("THREADS_ACCESS_TOKEN_failed", None)
            threads_success = True
            logger.info("Threads token refresh successful")
    else:
        # Mark failure timestamp for retry
        expiry_data["THREADS_ACCESS_TOKEN_failed"] = datetime.now().isoformat()
        logger.warning("Threads token refresh failed, will retry in 24 hours")
    
    # Refresh Instagram token
    new_ig_token, ig_expiry = refresh_ig_token(IG_ACCESS_TOKEN)
    if new_ig_token:
        if update_auth_file(ig_token=new_ig_token):
            expiry_data["IG_ACCESS_TOKEN"] = ig_expiry or "unknown"
            # Clear failure marker on success
            expiry_data.pop("IG_ACCESS_TOKEN_failed", None)
            ig_success = True
            logger.info("Instagram token refresh successful")
    else:
        # Mark failure timestamp for retry
        expiry_data["IG_ACCESS_TOKEN_failed"] = datetime.now().isoformat()
        logger.warning("Instagram token refresh failed, will retry in 24 hours")
    
    # Only update last_refresh if at least one token succeeded
    if threads_success or ig_success:
        expiry_data["last_refresh"] = datetime.now().isoformat()
        save_token_expiry(expiry_data)
        logger.info("Token refresh completed")
        
        # Reload auth module to use new tokens
        import importlib
        import settings.auth
        importlib.reload(settings.auth)
        return True
    else:
        # At least save failure markers
        save_token_expiry(expiry_data)
        logger.warning("No tokens were successfully refreshed")
        return False

def refresh_tokens_if_needed():
    """Check if refresh is needed and perform refresh if necessary."""
    if should_refresh_tokens():
        return refresh_tokens()
    else:
        logger.debug("Token refresh not needed at this time")
        return True
