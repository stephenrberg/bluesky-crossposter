import json, os, shutil, arrow
from settings.paths import *
from main.post import Post
from main.functions import count_lines, get_outputs
from settings import settings
from main.functions import logger


# Post database class
class Database():
    # Updated to include new Meta services
    services = ["bluesky", "mastodon", "twitter", "instagram", "threads"]

    def __init__(self):
        # This tracks if there have been updates to the database this run, and if not the database is not resaved at the end
        self.updated = False
        # Backup function takes a backup of the database once a day.
        self.backup()
        self.read_db_file()
        self.read_cache()
        self.outputs = get_outputs()

    # Function for getting the corresponding ID for a specific service
    def get_id(self, origin_id, service):
        if not origin_id or origin_id not in self.post_list:
            return None
        
        # Defensive check for missing service keys in old records
        service_data = self.post_list[origin_id]["services"].get(service)
        if not service_data:
            return None
            
        id = service_data["id"]
        # For bluesky the uri is also needed in order to repost and respond.
        if service == "bluesky": 
            uri = service_data.get("uri", "")
            return id, uri
        return id

    # Reading database.json
    def read_db_file(self):
        logger.info("Reading local database")
        self.post_list = {}
        db_data = []
        if not os.path.exists(database_path):
            return
        
        with open(database_path, 'r') as file:
            for line in file:
                try:
                    db_data.append(json.loads(line))
                except:
                    continue

        # If the database doesn't contain the origin field, this means it is
        # still in the old format and needs to be converted.
        if db_data and "origin" not in db_data[0]:
            logger.info("Updating database.")
            self.convert_db(db_data)
            return

        for line in db_data:
            # --- AUTO-MIGRATION LOGIC ---
            # Ensure legacy entries have keys for all current services
            for service in self.services:
                if service not in line["services"]:
                    # Defaulting to 'skipped' for old posts to prevent mass reposting
                    line["services"][service] = {
                        "id": "skipped",
                        "failure": 0
                    }
                    if service == "bluesky":
                        line["services"][service]["uri"] = ""

            # Setting the identifying id to the ID relating to the current input source
            input_id = str(line["services"][settings.input_source]["id"])
            if input_id not in ["skipped", "FailedToPost", "duplicate", ""]:
                self.post_list[input_id] = line
            else:
                origin_id = str(line["services"][line["origin"]]["id"])
                self.post_list[origin_id] = line

    # Checking if an ID exists in the database (adding if not), and if so, if it has already been posted to all required outputs.
    def posted(self, id, services = [], uri = None):
        # If the ID is found it means it has not been deleted, and so it is removed from the array of deleted posts
        if id in self.deleted:
            logger.info(f"Removing post {id} from potentially deleted posts.")
            self.deleted.remove(id)
        # If the ID is not in the database, it is added
        if id not in self.post_list:
            logger.info(f"{id} not found in post list")
            self.add(id, uri)
            return False
        # If no service is given, function checks all active outputs
        if not services:
            services = self.outputs
        for service in services:

            # DEFENSIVE: Use .get() to avoid KeyError on legacy database entries
            service_data = self.post_list[id]["services"].get(service)
            if not service_data:
                logger.info(f"Service {service} not found in database for post {id}. Marking as not posted.")
                return False
            if settings.outputs.get(service) and not service_data["id"]:
                return False
            if service_data["id"] == "FailedToPost":
                logger.info(f"{id} has reached error limit for {service}.")
        return True
    
    # Checking if a post has reached failure limit or has been skipped. 
    def not_posted(self, id, services = []):
        # If no service is given, function checks all active outputs
        if not services:
            services = self.outputs
        # Only if all services has been skipped or failed, returns True
        for service in services:
            service_data = self.post_list[id]["services"].get(service)
            if not service_data:
                return False
            if not service_data["id"] or service_data["id"] not in ["skipped", "FailedToPost", "duplicate"]:
                return False
        return True

    # Adding new ID to database
    def add(self, id, uri = None):
        logger.info("Adding post to database")
        self.updated = True
        self.post_list[id] = {
            "origin": settings.input_source,
            "services": self.create_entry()
        }
        # Setting the ID of the post from the input source
        self.post_list[id]["services"][settings.input_source]["id"] = id
        # Adding uri if applicable. This only applies for Bluesky
        if uri and "bluesky" in self.post_list[id]["services"]:
            self.post_list[id]["services"]["bluesky"]["uri"] = uri
        # For any service that is not the input, and not included in active outputs, setting the id to "skipped"
        for service in self.post_list[id]["services"]:
            if service != settings.input_source and settings.outputs.get(service) == False:
                self.post_list[id]["services"][service]["id"] = "skipped"
        
        self.save()

    # Removing post from db and cache
    def remove(self, id):
        logger.info(f"Deleting post {id} from database.")
        if id in self.post_list:
            del self.post_list[id]
        if id in self.cache:
            del self.cache[id]
        self.save()

    # Updating database and cache when a post is sent
    def update(self, input_id, service, output_id = None, uri = None):
        input_id = str(input_id)
        if input_id not in self.post_list:
             return
             
        # For reposts no new output_id is given, only the cache is updated
        if output_id and service in self.post_list[input_id]["services"]:
            self.post_list[input_id]["services"][service]["id"] = output_id
        if uri and service in self.post_list[input_id]["services"]:
            self.post_list[input_id]["services"][service]["uri"] = uri
        self.cache[input_id] = arrow.utcnow()
        self.updated = True
        
        # *** AUTO-SAVE ON SUCCESSFUL POST ***
        self.save()

    # Setting a post for a service to skipped
    def skip(self, id, service):
        id = str(id)
        if id in self.post_list and service in self.post_list[id]["services"]:
            if not self.post_list[id]["services"][service]["id"]:
                self.updated = True
                self.post_list[id]["services"][service]["id"] = "skipped"
                self.save()

    #  Saving database to file
    def save(self):
        logger.info("Saving database")
        append_write = "w"
        for id in self.post_list:
            json_string = json.dumps(self.post_list[id])
            file = open(database_path, append_write)
            file.write(json_string + "\n")
            file.close()
            append_write = "a"
        self.save_cache()

    # If a post failed to send, increasing the failure counter for that service. If it reaches the max_retries-limit, setting the post ID to "FailedToPost"
    def failed_post(self, id, service):
        if id in self.post_list and service in self.post_list[id]["services"]:
            self.post_list[id]["services"][service]["failure"] += 1
            if self.post_list[id]["services"][service]["failure"] >= settings.max_retries:
                self.post_list[id]["services"][service]["id"] = "FailedToPost"
            
            self.save()


    # Reading cache-file
    def read_cache(self):
        logger.info("Reading cache of recent posts.")
        self.cache = {}
        # Adding all recent posts to deleted, and removing them when they are confirmed to not be.
        self.deleted = []
        timelimit = arrow.utcnow().shift(hours = -1)
        if not os.path.exists(post_cache_path):
            logger.info(f"{post_cache_path} not found.")
            return
        with open(post_cache_path, 'r') as file:
            for line in file:
                try:
                    post_id = str(line.split(";")[0])
                    timestamp = int(line.split(";")[1].split(".")[0])
                    timestamp = arrow.Arrow.fromtimestamp(timestamp)
                except Exception as e:
                    logger.error(e)
                    continue
                if timestamp > timelimit:
                    self.cache[post_id] = timestamp
                    self.deleted.append(post_id)
        logger.debug(f"Cache: {self.cache}")
        logger.debug(f"Deleted: {self.deleted}")

    # Saving cache to file
    def save_cache(self):
        logger.info("Saving cache.")
        logger.debug(self.cache)
        if not self.cache:
            if os.path.exists(post_cache_path):
                os.remove(post_cache_path)
            logger.info("Post cache is empty, removing cache file.")
            return
        logger.info("Saving post cache.")
        append_write = "w"
        for post_id in self.cache:
            timestamp = str(self.cache[post_id].timestamp())
            file = open(post_cache_path, append_write)
            file.write(f"{post_id};{timestamp}\n")
            file.close()
            append_write = "a"

    # The timelimit specifies the cutoff time for which posts are crossposted. This is usually based on the 
    # post_time_limit in settings, but if overflow_posts is set to "skip", meaning any posts that could
    # not be posted due to the hourly post max limit is to be skipped, then the timelimit is instead set to
    # when the last post was sent.
    def get_post_time_limit(self):
        timelimit = arrow.utcnow().shift(hours = -settings.post_time_limit)
        if settings.overflow_posts != "skip":
            return timelimit
        for post_id in self.cache:
            if timelimit < self.cache[post_id]:
                timelimit = self.cache[post_id]
        return timelimit

    # Every twelve hours a backup of the database is saved, in case something happens to the live database.
    # If the live database contains fewer lines than the backup it means something has probably gone wrong,
    # and before the live database is saved as a backup, the current backup is saved as a new file, so that
    # it can be recovered later.
    def backup(self):
        if not os.path.isfile(database_path) or (os.path.isfile(backup_path)
            and arrow.Arrow.fromtimestamp(os.stat(backup_path).st_mtime) > arrow.utcnow().shift(hours = -24)):
            return
        if os.path.isfile(backup_path):
            if count_lines(backup_path) <= count_lines(database_path):
                os.remove(backup_path)
            else:
                date = arrow.utcnow().format("YYMMDD")
                os.rename(backup_path, backup_path + "_" + date)
                logger.error("Current backup file contains more entries than current live database, backup saved")
        shutil.copyfile(database_path, backup_path)
        logger.info("Backup of database taken")

    # If database is in old format, this function will read it and convert it to the new.
    def convert_db(self, db_data):
        # Making a backup of the old database before converting
        logger.info(f"Backing up database to {database_path}_old before converting.")
        shutil.copyfile(database_path, f"{database_path}_old")
        for line in db_data:
            # Since the old format only used bluesky as input, it will always be the origin
            post = {
                "origin": "bluesky",
                "services": self.create_entry()
            }
            # Entering data from old database
            post["services"]["bluesky"] = {
                                            "id": line["skeet"],
                                            "uri": "",
                                            "failure": 0
                                        }
            post["services"]["twitter"] = {
                                            "id": line["ids"]["twitter_id"],
                                            "failure": line["failed"]["twitter"],
                                        }
            post["services"]["mastodon"] = {
                                            "id": line["ids"]["mastodon_id"],
                                            "failure": line["failed"]["mastodon"],
                                        }
            self.post_list[post["services"][settings.input_source]["id"]] = post
        self.save()

    # Dynamically creating empty entry for a post containing every available service
    def create_entry(self):
        services = {}
        for service in self.services:
            services[service] = {
                        "id": "",
                        "failure": 0
                    }
            # Bluesky requires both CID and URI to interact with posts.
            # Bluesky really is needlessly complicated.
            if service == "bluesky":
                services[service]["uri"] = ""
        return services

# Initiating database
database = Database()