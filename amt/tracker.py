class Tracker():
    id = None

    def __init__(self, session, settings=None):
        self.settings = settings
        self.session = session

    def get_media_dict(self, id, media_type, name, progress, progressVolumes=None, score=0, timeSpent=0, year=0, season=None, genres=[], tags=[], studio=[]):
        return {"id": id, "media_type": media_type, "name": name, "progress": progress, "progressVolumes": progressVolumes,
                "score": score, "timeSpent": timeSpent, "year": year, "season": season, "genres": genres, "tags": tags, "studio": studio
                }

    def auth(self):
        pass

    def update(self, list_of_updates):
        pass

    def get_full_list_data(self, user_name=None, id=None):
        return self.get_tracker_list(user_name, id, status=None)

    def get_tracker_list(self, user_name=None, id=None, status="CURRENT"):
        pass
