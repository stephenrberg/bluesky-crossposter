import json, os, re
from main.functions import logger
import settings.paths as paths

# Map of Bluesky users that are allowed to be mentioned when crossposting. Loaded once, the first time it is needed.
# Format of the json file (keys are Bluesky handles or DIDs, values are either a string used for every service
# or an object with one username per service):
# {
#     "alice.bsky.social": {"mastodon": "@alice@mastodon.social", "twitter": "@alice_tw"},
#     "bob.bsky.social": "Bob"
# }
mentions_path = getattr(paths, "mentions_path", paths.base_path + "db/mentions.json")
_mention_map = None


def normalize(key):
    return key.strip().lstrip("@").lower()


def get_map():
    global _mention_map
    if _mention_map is not None:
        return _mention_map
    _mention_map = {}
    if not os.path.isfile(mentions_path):
        logger.debug(f"No mention map found at {mentions_path}")
        return _mention_map
    try:
        with open(mentions_path, "r", encoding="utf-8") as f:
            data = json.load(f)
        if not isinstance(data, dict):
            raise ValueError("top level element must be an object")
        _mention_map = {normalize(k): v for k, v in data.items() if not k.startswith("_")}
        logger.info(f"Loaded {len(_mention_map)} allowed mention(s) from {mentions_path}")
    except Exception as e:
        logger.error(f"Could not load mention map {mentions_path}: {e}")
    return _mention_map


# Returns a dict {service: replacement} for an allowed user, or None if the user is not in the map.
# A service without an entry in the map has the value None, and posts mentioning the user are not sent to that service.
def lookup(handle, did, services):
    mention_map = get_map()
    entry = mention_map.get(normalize(handle))
    if entry is None and did:
        entry = mention_map.get(normalize(did))
    if entry is None:
        return None
    fallback = None
    if isinstance(entry, str):
        return {service: entry for service in services}
    if isinstance(entry, dict):
        return {service: entry.get(service) or fallback for service in services}
    logger.error(f"Invalid mention map entry for {handle}, expected a string or an object.")
    return None


# Replaces mentions in text with the username for the given service
def apply(text, mentions, service):
    for mention, replacements in (mentions or {}).items():
        replacement = replacements.get(service) or mention.lstrip("@")
        # Not matching if the handle continues into more of a handle (such as "@bob.bsky.social.example"),
        # but still allowing a trailing period ending a sentence.
        text = re.sub(re.escape(mention) + r"(?![\w-]|\.\w)", lambda _: replacement, text)
    return text


# Posts mentioning a user are only sent to services where the map has a username for that user
def allowed(mentions, service):
    return all(replacements.get(service) for replacements in (mentions or {}).values())
