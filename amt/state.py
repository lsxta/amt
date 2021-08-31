import json
import logging

from . import cookie_manager


def json_decoder(obj):
    if "server_id" in obj:
        return MediaData(obj)
    if "number" in obj:
        return ChapterData(obj)
    return obj


class State:
    version = 1

    def __init__(self, settings, session):
        self.settings = settings
        self.session = session
        self.bundles = {}
        self.media = {}
        self.all_media = {}
        self.hashes = {}
        self.cookie_hash = None

    @staticmethod
    def get_hash(json_dict):
        json_str = json.dumps(json_dict, indent=4, sort_keys=True)
        return hash(json_str), json_str

    def read_file_as_dict(self, file_name, object_hook=json_decoder):
        try:
            with open(file_name, "r") as jsonFile:
                logging.debug("Loading file %s", file_name)

                json_dict = json.load(jsonFile, object_hook=object_hook)
                self.hashes[file_name] = State.get_hash(json_dict)[0]
                return json_dict
        except FileNotFoundError:
            return {}

    def save_to_file(self, file_name, json_dict):
        h, json_str = State.get_hash(json_dict)
        if self.hashes.get(file_name, 0) == h:
            return False
        self.hashes[file_name] = h
        try:
            with open(file_name, "w") as jsonFile:
                jsonFile.write(json_str)
            logging.info("Persisting state to %s", file_name)
        except FileNotFoundError:
            return False

        return True

    def save(self):
        self.save_session_cookies()
        self.save_to_file(self.settings.get_bundle_metadata_file(), self.bundles)
        self.save_to_file(self.settings.get_metadata(), self.all_media)
        for media_data in self.media.values():
            self.save_to_file(self.settings.get_chapter_metadata_file(media_data), media_data.chapters)

    def load(self):
        self.load_session_cookies()
        self.load_bundles()
        self.load_media()

    def load_bundles(self):
        self.bundles = self.read_file_as_dict(self.settings.get_bundle_metadata_file())

    def load_media(self):
        self.all_media = self.read_file_as_dict(self.settings.get_metadata())
        if not self.all_media:
            self.all_media = dict(media={}, disabled_media={})
        self.media = self.all_media["media"]
        self.disabled_media = self.all_media["disabled_media"]

        for key, media_data in list(self.media.items()):
            if key != media_data.global_id:
                del self.media[key]
                self.media[media_data.global_id] = media_data
            if not media_data.chapters:
                media_data.chapters = self.read_file_as_dict(self.settings.get_chapter_metadata_file(media_data))

    def _set_session_hash(self):
        """
        Sets saved cookie_hash
        @return True iff the hash is different than the already saved one

        """
        cookie_hash = hash(str(self.session.cookies))
        if cookie_hash != self.cookie_hash:
            self.cookie_hash = cookie_hash
            return True
        return False

    def load_session_cookies(self):
        """ Load session from disk """

        if self.settings.no_load_session:
            return

        for path in self.settings.get_cookie_files():
            try:
                with open(path, "r") as f:
                    cookie_manager.load_cookies(f, self.session)
            except FileNotFoundError:
                pass
        self._set_session_hash()

    def save_session_cookies(self, force=False):
        """ Save session to disk """
        if self.settings.no_save_session or not self._set_session_hash():
            return False

        with open(self.settings.get_cookie_file(), "w") as f:
            for cookie in self.session.cookies:
                l = [cookie.domain, str(cookie.domain_specified), cookie.path, str(cookie.secure).upper(), str(cookie.expires) if cookie.expires else "", cookie.name, cookie.value]
                f.write("\t".join(l) + "\n")
        return True

    def is_out_of_date(self):
        return self.all_media.get("version", 0) != self.version

    def update_verion(self):
        self.all_media["version"] = self.version

    def configure_media(self, server_list):
        for key in list(self.media.keys()):
            if self.media[key]["server_id"] not in server_list:
                self.disabled_media[key] = self.media[key]
                del self.media[key]

        for key in list(self.disabled_media.keys()):
            if self.disabled_media[key]["server_id"] in server_list:
                self.media[key] = self.disabled_media[key]
                del self.disabled_media[key]

    def mark_bundle_as_read(self, bundle_name):
        bundled_data = self.bundles[bundle_name]
        for data in bundled_data:
            self.media[data["media_id"]]["chapters"][data["chapter_id"]]["read"] = True

    def get_lead_media_data(self, bundle):
        bundled_data = self.bundles[bundle] if isinstance(bundle, str) else bundle
        for data in bundled_data:
            return self.media[data["media_id"]]


class MediaData(dict):
    def __init__(self, backing_map):
        self.chapters = {}
        if "chapters" in backing_map:
            self.chapters = backing_map["chapters"]
            del backing_map["chapters"]

        super().__init__(backing_map)

    def __getitem__(self, key):
        if key == "chapters":
            return self.chapters
        else:
            return super().__getitem__(key)

    def get_sorted_chapters(self):
        return sorted(self["chapters"].values(), key=lambda x: x["number"])

    @property
    def global_id(self):
        return "{}:{}{}{}".format(self["server_id"], self["id"], (self["season_id"] if self["season_id"] else ""), self.get("lang", "")[:3])

    def copy_fields_to(self, dest):
        for key in ("offset", "progress", "progressVolumes", "trackers"):
            assert key in dest
            dest[key] = self.get(key)

    def get_last_chapter_number(self):
        return max(self["chapters"].values(), key=lambda x: x["number"])["number"] if self["chapters"] else 0

    def get_last_read(self):
        return max(filter(lambda x: x["read"], self["chapters"].values()), key=lambda x: x["number"], default={"number": 0})["number"]

    def get_labels(self, reverse=False):
        labels = [self.global_id, self["name"], self["server_id"], self["media_type"]]
        if reverse:
            labels.reverse()
        return labels


class ChapterData(dict):
    def __init__(self, backing_map):
        super().__init__(backing_map)
