from settings import settings
from output import twitter, mastodon, bluesky, meta
from main.db import database

# Function for processing post queue
def send_posts(queues):
    # Running through and posting to each included service
    for service in queues:
        if not queues[service]:
            continue
            
        if service == "bluesky":
            bluesky.output(queues[service])
        elif service == "mastodon":
            mastodon.output(queues[service])
        elif service == "twitter":
            twitter.output(queues[service])
        # Add the new Meta services here
        elif service == "instagram":
            meta.output("instagram", queues[service])
        elif service == "threads":
            meta.output("threads", queues[service])

    # Running through and deleting deleted posts
    for id in database.deleted:
        if settings.outputs["twitter"] and settings.input_source != "twitter" and database.get_id(id, "twitter"):
            twitter.delete_post(id)
        if settings.outputs["mastodon"] and settings.input_source != "mastodon" and database.get_id(id, "mastodon"):
            mastodon.delete_post(id)
        if settings.outputs["bluesky"] and settings.input_source != "bluesky" and database.get_id(id, "bluesky"):
            bluesky.delete_post(id)
        # Note: Threads and Instagram APIs generally don't support deleting via API for 3rd party apps yet
        database.remove(id)