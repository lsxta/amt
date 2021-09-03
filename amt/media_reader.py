import importlib
import inspect
import logging
import os
import pkgutil
import random
import re
import shutil

import requests
from requests.adapters import HTTPAdapter
from urllib3 import Retry

from . import servers, trackers
from .job import Job
from .server import Server
from .servers.custom import get_local_server_id
from .settings import Settings
from .state import State
from .tracker import Tracker
from .util.media_type import MediaType

SERVERS = set()
TRACKERS = set()


def import_sub_classes(m, base_class, results):
    for _finder, name, _ispkg in pkgutil.iter_modules(m.__path__, m.__name__ + "."):
        try:
            module = importlib.import_module(name)
            for _name, obj in dict(inspect.getmembers(module, inspect.isclass)).items():
                if issubclass(obj, base_class) and obj.id:
                    results.add(obj)
        except ImportError:
            pass


import_sub_classes(servers, Server, SERVERS)
import_sub_classes(trackers, Tracker, TRACKERS)


class MediaReader:

    _servers = {}
    _trackers = []
    primary_tracker = None

    def __init__(self, server_list=SERVERS, tracker_list=TRACKERS, settings=None):
        self.settings = settings if settings else Settings()
        self.session = requests.Session()
        self.state = State(self.settings, self.session)
        self._servers = {}
        self._trackers = []

        if self.settings.max_retires:
            for prefix in ("http://", "https://"):
                self.session.mount(prefix, HTTPAdapter(max_retries=Retry(total=self.settings.max_retires, status_forcelist=self.settings.status_to_retry)))

        self.session.headers.update({
            "Accept": "text/html,application/xhtml+xml,application/xml;q=1.0,image/webp,image/apng,*/*;q=1.0",
            "Connection": "keep-alive",
            "User-Agent": self.settings.user_agent
        })

        for cls in server_list:
            if cls.id:
                instance = cls(self.session, self.settings)
                if not self.settings.allow_only_official_servers or instance.official:
                    assert instance.id not in self._servers, "Duplicate server id"
                    self._servers[instance.id] = instance
        for cls in tracker_list:
            if cls.id:
                instance = cls(self.session, self.settings)
                self._trackers.append(instance)
        self.set_primary_tracker(self.get_trackers()[0])
        self.state.load()
        self.state.configure_media(self._servers)
        self.media = self.state.media
        self.bundles = self.state.bundles

    # Helper methods
    def select_media(self, term, results, prompt, no_print=False):  # pragma: no cover
        raise NotImplementedError

    def for_each(self, func, media_list, raiseException=False):
        return Job(self.settings.threads, [lambda x=media_data: func(x) for media_data in media_list], raiseException=raiseException).run()

    def get_servers(self):
        return self._servers.values()

    def get_servers_ids(self):
        return self._servers.keys()

    def get_servers_ids_with_logins(self):
        return [k for k in self._servers.keys() if self.get_server(k).has_login]

    def get_server(self, id):
        return self._servers.get(id, None)

    def get_media_ids(self):
        return self.media.keys()

    def get_media(self, media_type=None, name=None, shuffle=False):
        if isinstance(name, dict):
            yield name
            return
        media = self.media.values()
        if shuffle:
            media = list(media)
            random.shuffle(media)
        for media_data in media:
            if name is not None and name not in (media_data["server_id"], media_data["name"], media_data.global_id):
                continue
            if media_type and media_data["media_type"] & media_type == 0:
                continue
            yield media_data

    def get_single_media(self, media_type=None, name=None):
        if isinstance(name, dict):
            return name
        return next(self.get_media(media_type=media_type, name=name))

    def get_unreads(self, media_type, name=None, shuffle=False, limit=None, any_unread=False):
        count = 0
        for media_data in self.get_media(media_type, name, shuffle):
            server = self.get_server(media_data["server_id"])

            lastRead = media_data.get_last_read()
            for chapter in media_data.get_sorted_chapters():
                if not chapter["read"] and (any_unread or chapter["number"] > lastRead):
                    yield server, media_data, chapter
                    count += not chapter["special"]
                    if limit and count == limit:
                        return

    # Method related to adding/removing media and searching for media

    def add_media(self, media_data, no_update=False):
        global_id = media_data.global_id
        if global_id in self.media:
            raise ValueError("{} {} is already known".format(global_id, media_data["name"]))

        logging.debug("Adding %s", global_id)
        self.media[global_id] = media_data
        os.makedirs(self.settings.get_media_dir(media_data), exist_ok=True)
        return [] if no_update else self.update_media(media_data)

    def search_for_media(self, term, server_id=None, media_type=None, exact=False, servers_to_exclude=[], limit=None, raiseException=False):
        def func(x): return x.search(term, limit=limit)
        if server_id:
            results = func(self.get_server(server_id))
        else:
            results = self.for_each(func, filter(lambda x: x.id not in servers_to_exclude and (media_type is None or media_type & x.media_type), self.get_servers()), raiseException=raiseException)
        if exact:
            results = list(filter(lambda x: x["name"] == term, results))
        return results

    def search_add(self, term, server_id=None, media_type=None, limit=None, exact=False, servers_to_exclude=[], no_add=False, media_id=None, sort_func=None):
        results = self.search_for_media(term, server_id=server_id, media_type=media_type, exact=exact, servers_to_exclude=servers_to_exclude, limit=limit)
        results = list(filter(lambda x: not media_id or str(x["id"]) == str(media_id), results))
        if sort_func:
            results.sort(key=sort_func)
        if len(results) == 0:
            return None
        media_data = self.select_media(term, results, "Select media: ")
        if not no_add and media_data:
            self.add_media(media_data)
        return media_data

    def add_from_url(self, url):
        for server in self.get_servers():
            if server.can_stream_url(url):
                media_data = server.get_media_data_from_url(url)
                if media_data:
                    self.add_media(media_data)
                return media_data
        raise ValueError("Could not find media to add")

    def remove_media(self, media_data=None, id=None):
        if id:
            media_data = self.get_single_media(name=id)
        del self.media[media_data.global_id]

    def import_media(self, files, media_type, link=False, name=None, no_update=False):
        func = shutil.move if not link else os.link

        local_server_id = get_local_server_id(media_type)
        custom_server_dir = self.settings.get_server_dir(local_server_id)
        os.makedirs(custom_server_dir, exist_ok=True)
        assert os.path.exists(custom_server_dir)
        names = set()
        volume_regex = r"(_|\s)?vol[ume-]*[\w\s]*(\d+)"
        for file in files:
            logging.info("Trying to import %s (dir: %s)", file, os.path.isdir(file))
            media_name = name
            if not name:
                match = re.search(r"(\[[\w ]*\]|\d+[.-:]?)?\s*([\w\-]+\w+[\w';:\. ]*\w[!?]*)(.*\.\w+)$", re.sub(volume_regex, "", os.path.basename(file)))
                if not match:
                    if os.path.isdir(file):
                        self.import_media(map(lambda x: os.path.join(file, x), os.listdir(file)), media_type, link=link, no_update=True)
                        continue
                assert match
                media_name = match.group(2)
                logging.info("Detected name %s", media_name)
            if os.path.isdir(file):
                shutil.move(file, os.path.join(custom_server_dir, name or ""))
            else:
                path = os.path.join(custom_server_dir, media_name)
                os.makedirs(path, exist_ok=True)
                dest = os.path.join(path, os.path.basename(file))
                logging.info("Importing to %s", dest)
                func(file, dest)
            if media_name not in names:
                if not any([x["name"] == media_name for x in self.get_media()]):
                    self.search_add(media_name, server_id=local_server_id, exact=True)
                names.add(media_name)

        if not no_update:
            [self.update_media(media_data) for media_data in self.get_media(name=local_server_id)]

    ############# Upgrade and migration

    def migrate(self, name, exact=False, move_self=False, force_same_id=False):
        media_list = []
        last_read_list = []
        for media_data in list(self.get_media(name=name)):
            self.remove_media(media_data)
            if move_self:
                def func(x): return -sum([media_data.get(key, None) == x[key] for key in x])
                new_media_data = self.search_add(media_data["name"], exact=exact, server_id=media_data["server_id"], media_id=media_data["id"] if force_same_id else None, sort_func=func)
            else:
                new_media_data = self.search_add(media_data["name"], exact=exact, media_type=media_data["media_type"], servers_to_exclude=[media_data["server_id"]])
            media_data.copy_fields_to(new_media_data)
            media_list.append(new_media_data)
            last_read_list.append(media_data.get_last_read())

        self.for_each(self.update_media, media_list)
        for media_data, last_read in zip(media_list, last_read_list):
            self.mark_chapters_until_n_as_read(new_media_data, last_read)

    def upgrade_state(self, force=False):
        if self.state.is_out_of_date() or force:
            self.migrate(None, move_self=True, force_same_id=True)
            self.state.update_verion()

    # Updating media

    def update(self, name=None, media_type=None, download=False, media_type_to_download=MediaType.MANGA, replace=False, ignore_errors=False):
        logging.info("Updating: download %s", download)
        def func(x): return self.update_media(x, download, media_type_to_download=media_type_to_download, replace=replace)
        return self.for_each(func, self.get_media(media_type=media_type, name=name), raiseException=not ignore_errors)

    def update_media(self, media_data, download=False, media_type_to_download=MediaType.MANGA, limit=None, page_limit=None, replace=False):
        """
        Return set of updated chapters or a False-like value
        """
        server = self.get_server(media_data["server_id"])
        if server.sync_removed:
            replace = True

        def get_chapter_ids(chapters):
            return {x for x in chapters if not chapters[x]["premium"]} if self.settings.free_only else set(chapters.keys())
        chapter_ids = get_chapter_ids(media_data["chapters"])
        if replace:
            chapters = dict(media_data["chapters"])
            media_data["chapters"].clear()

        try:
            server.update_media_data(media_data)
        except:
            if replace:
                media_data["chapters"] = chapters
            raise

        current_chapter_ids = get_chapter_ids(media_data["chapters"])
        new_chapter_ids = current_chapter_ids - chapter_ids

        if replace:
            for chapter in chapters:
                if chapter in media_data["chapters"]:
                    media_data["chapters"][chapter]["read"] = chapters[chapter]["read"]

        new_chapters = sorted([media_data["chapters"][x] for x in new_chapter_ids], key=lambda x: x["number"])
        assert len(new_chapter_ids) == len(new_chapters)
        if download and (media_type_to_download is None or media_type_to_download & media_data["media_type"]):
            for chapter_data in new_chapters[:limit]:
                server.download_chapter(media_data, chapter_data, page_limit)
        return new_chapters

    # Downloading

    def download_specific_chapters(self, name=None, media_data=None, start=0, end=0):
        media_data = self.get_single_media(name=name)
        server = self.get_server(media_data["server_id"])
        if not end:
            end = start
        for chapter in media_data.get_sorted_chapters():
            if start <= chapter["number"] and (end <= 0 or chapter["number"] <= end):
                server.download_chapter(media_data, chapter)
                if end == start:
                    break

    def download_unread_chapters(self, name=None, media_type=None, limit=0, ignore_errors=False, any_unread=False, page_limit=None):
        """Downloads all chapters that are not read"""
        def download_selected_chapters(x):
            server, media_data, chapter = x
            return server.download_chapter(media_data, chapter, page_limit=page_limit)
        return sum(self.for_each(download_selected_chapters, self.get_unreads(media_type, name=name, any_unread=any_unread, limit=limit), raiseException=not ignore_errors))

    def bundle_unread_chapters(self, name=None, shuffle=False, limit=None, ignore_errors=False):
        paths = []
        bundle_data = []
        self.download_unread_chapters(name=name, media_type=MediaType.MANGA, limit=limit, ignore_errors=ignore_errors)
        for server, media_data, chapter in self.get_unreads(MediaType.MANGA, name=name, shuffle=shuffle, limit=limit):
            if server.is_fully_downloaded(media_data, chapter):
                paths.append(server.get_children(media_data, chapter))
                bundle_data.append(dict(media_id=media_data.global_id, chapter_id=chapter["id"]))
        if not paths:
            return None

        logging.info("Bundling %s", paths)
        name = self.settings.bundle(paths, media_data=self.state.get_lead_media_data(bundle_data))
        self.state.bundles[name] = bundle_data
        return name

    def read_bundle(self, name):
        bundle_name = os.path.join(self.settings.bundle_dir, name) if name else max(self.state.bundles.keys())
        if bundle_name in self.bundles and self.settings.open_bundle_viewer(bundle_name, self.state.get_lead_media_data(bundle_name)):
            self.state.mark_bundle_as_read(bundle_name)
            return True
        return False

    # Viewing chapters and marking read

    def mark_chapters_until_n_as_read(self, media_data, N, force=False):
        """Marks all chapters whose numerical index <=N as read"""
        for chapter in media_data["chapters"].values():
            if chapter["number"] <= N:
                chapter["read"] = True
            elif force:
                chapter["read"] = False

    def mark_read(self, name=None, media_type=None, N=0, force=False, abs=False):
        for media_data in self.get_media(media_type=media_type, name=name):
            last_read = media_data.get_last_chapter_number() + N if not abs else N
            if not force:
                last_read = max(media_data.get_last_read(), last_read)
            self.mark_chapters_until_n_as_read(media_data, last_read, force=force)

    def get_media_by_chapter_id(self, server_id, chapter_id):
        for media in self.get_media():
            if media["server_id"] == server_id:
                if chapter_id in media["chapters"]:
                    return media, media["chapters"][chapter_id]
        return None, None

    def stream(self, url, cont=False, download=False, quality=0):
        for server in self.get_servers():
            if server.can_stream_url(url):
                chapter_id = server.get_chapter_id_for_url(url)
                media_data, chapter = self.get_media_by_chapter_id(server.id, chapter_id)
                if not chapter:
                    media_data = server.get_media_data_from_url(url)
                    chapter = media_data["chapters"][chapter_id]
                dir_path = server._get_dir(media_data, chapter)
                if download:
                    server.download_chapter(media_data, chapter)
                else:
                    if not server.is_fully_downloaded(media_data, chapter):
                        server.pre_download(media_data, chapter, dir_path=dir_path)
                    streamable_url = server.get_stream_url(media_data, chapter, quality=quality)
                    logging.info("Streaming %s", streamable_url)
                    if self.settings.open_viewer(streamable_url, media_data=media_data, chapter_data=chapter, wd=dir_path):
                        chapter["read"] = True
                        if cont:
                            return 1 + self.play(name=media_data)
                return 1
        logging.error("Could not find any matching server")
        return False

    def get_stream_url(self, name=None, shuffle=False):
        for server, media_data, chapter in self.get_unreads(MediaType.ANIME, name=name, shuffle=shuffle):
            for url in server.get_stream_urls(media_data, chapter):
                print(url)

    def get_chapters(self, media_type, name, num_list, force_abs=False):
        media_data = self.get_single_media(media_type=media_type, name=name)
        last_read = media_data.get_last_read()
        num_list = list(map(lambda x: last_read + x if x <= 0 and not force_abs else x, num_list))
        server = self.get_server(media_data["server_id"])
        for chapter in media_data.get_sorted_chapters():
            if chapter["number"] in num_list:
                yield server, media_data, chapter

    def play(self, name=None, media_type=None, shuffle=False, limit=None, num_list=None, quality=0, any_unread=False, force_abs=False):
        num = 0
        for server, media_data, chapter in (self.get_chapters(media_type, name, num_list, force_abs=force_abs) if num_list else self.get_unreads(media_type, name=name, limit=limit, shuffle=shuffle, any_unread=any_unread)):
            dir_path = server._get_dir(media_data, chapter)
            if media_data["media_type"] == MediaType.ANIME:
                if not server.is_fully_downloaded(media_data, chapter):
                    server.pre_download(media_data, chapter, dir_path=dir_path)
            else:
                server.download_chapter(media_data, chapter)
            success = self.settings.open_viewer(
                server.get_children(media_data, chapter)if server.is_fully_downloaded(media_data, chapter) else server.get_stream_url(media_data, chapter, quality=quality),
                media_data=media_data, chapter_data=chapter, wd=dir_path)
            if success:
                num += 1
                chapter["read"] = True
                if num == limit:
                    break
            else:
                return False
        return num

    # Tacker related functions

    def get_trackers(self):
        return self._trackers

    def get_primary_tracker(self):
        return self.primary_tracker

    def set_primary_tracker(self, tracker):
        self.primary_tracker = tracker

    def get_secondary_trackers(self):
        return [x for x in self.get_trackers() if x != self.get_primary_tracker()]

    def get_tracked_media(self, tracker_id, tracking_id):
        media_data_list = []
        for media_data in self.get_media():
            tacker_info = self.get_tracker_info(media_data, tracker_id)
            if tacker_info and tacker_info[0] == tracking_id:
                media_data_list.append(media_data)
        return media_data_list

    def has_tracker_info(self, media_data, tracker_id=None):
        return self.get_tracker_info(media_data, tracker_id=tracker_id) is not None

    def get_tracker_info(self, media_data, tracker_id=None):
        if not tracker_id:
            tracker_id = self.get_primary_tracker().id
        return media_data["trackers"].get(tracker_id, None)

    def track(self, media_data, tracker_id, tracking_id, tracker_title=None):
        media_data["trackers"][tracker_id] = (tracking_id, tracker_title)

    def remove_tracker(self, name, media_type=None, tracker_id=None):
        if not tracker_id:
            tracker_id = self.get_primary_tracker().id
        for media_data in self.get_media(name=name, media_type=media_type):
            del media_data["trackers"][tracker_id]

    def copy_tracker(self, src, dst):
        src_media_data = self.get_single_media(name=src)
        dst_media_data = self.get_single_media(name=dst)
        if self.has_tracker_info(src_media_data):
            tracking_id, tracker_title = self.get_tracker_info(src_media_data)
            self.track(dst_media_data, self.get_primary_tracker().id, tracking_id, tracker_title)

    def sync_progress(self, force=False, media_type=None, dry_run=False):
        data = []
        tracker = self.get_primary_tracker()
        for media_data in self.get_media():
            if not media_type or media_data["media_type"] == media_type:
                tracker_info = self.get_tracker_info(media_data=media_data, tracker_id=self.get_primary_tracker().id)
                if tracker_info and (force or media_data["progress"] < int(media_data.get_last_read())):
                    data.append((tracker_info[0], media_data.get_last_read(), media_data["progressVolumes"]))
                    last_read = media_data.get_last_read()
                    logging.info("Preparing to update %s from %d to %d", media_data["name"], media_data["progress"], last_read)
                    media_data["progress"] = last_read

        if data and not dry_run:
            tracker.update(data)
        return True if data else False

    def _search_for_tracked_media(self, name, media_type, exact=False, local_only=False):
        def name_matches_media(name, media_data):
            return (name.lower().startswith(media_data["name"].lower()) or
                    name.lower().startswith(media_data["season_title"].lower()) or
                    name.lower() in (media_data["name"].lower(), media_data["season_title"].lower()))

        alt_names = dict.fromkeys([name, re.sub(r"\W*$", "", name), re.sub(r"[^\w\d\s]+.*$", "", name)])
        media_data = None

        for name in alt_names:
            known_matching_media = list(filter(lambda x: not self.get_tracker_info(x) and
                                               (not media_type or media_type & x["media_type"]) and
                                               (name_matches_media(name, x)), self.get_media()))
            if known_matching_media:
                break

        if known_matching_media:
            logging.debug("Checking among known media")
            media_data = self.select_media(name, known_matching_media, "Select from known media: ")

        elif not local_only:
            for name in alt_names:
                media_data = self.search_add(name, media_type=media_type)
                if media_data:
                    break
        if not media_data:
            logging.info("Could not find media %s", name)
            return False
        return media_data

    def load_from_tracker(self, user_id=None, user_name=None, media_type=None, exact=True, local_only=False, update_progress_only=False, force=False):
        tracker = self.get_primary_tracker()
        data = tracker.get_tracker_list(user_name=user_name) if user_name else tracker.get_tracker_list(id=user_id)
        count = 0
        new_count = 0

        unknown_media = []
        for entry in data:
            if media_type and not entry["media_type"] & media_type:
                logging.debug("Skipping %s", entry)
                continue
            media_data_list = self.get_tracked_media(tracker.id, entry["id"])
            if not media_data_list:
                if update_progress_only:
                    continue
                media_data = self._search_for_tracked_media(entry["name"], entry["media_type"], exact=exact, local_only=local_only)
                if media_data:
                    self.track(media_data, tracker.id, entry["id"], entry["name"])
                    assert self.get_tracked_media(tracker.id, entry["id"])
                    new_count += 1
                else:
                    unknown_media.append(entry["name"])
                    continue
                media_data_list = [media_data]

            for media_data in media_data_list:
                progress = entry["progress"] if not media_data["progressVolumes"] else entry["progressVolumes"]
                self.mark_chapters_until_n_as_read(media_data, progress, force=force)
                media_data["progress"] = progress
            count += 1
        if unknown_media:
            logging.info("Could not find any of %s", unknown_media)

        self.list()
        return count, new_count
    # MISC

    def offset(self, name, offset):
        for media_data in self.get_media(name=name):
            diff_offset = offset - media_data.get("offset", 0)
            for chapter in media_data["chapters"].values():
                chapter["number"] -= diff_offset
            media_data["offset"] = offset

    def clean(self, remove_disabled_servers=False, include_external=False, remove_read=False, remove_not_on_disk=False, bundles=False):
        if remove_not_on_disk:
            for media_data in [x for x in self.get_media() if not os.path.exists(self.settings.get_chapter_metadata_file(x))]:
                logging.info("Removing metadata for %s because it doesn't exist on disk", media_data["name"])
                self.remove_media(media_data)
        media_dirs = {self.settings.get_media_dir(media_data): media_data for media_data in self.get_media()}
        if bundles:
            logging.info("Removing all bundles")
            shutil.rmtree(self.settings.bundle_dir)
            self.bundles.clear()
        for dir in os.listdir(self.settings.media_dir):
            server = self.get_server(dir)
            server_path = os.path.join(self.settings.media_dir, dir)
            if server:
                if include_external or not server.external:
                    for media_dir in os.listdir(server_path):
                        media_path = os.path.join(server_path, media_dir)
                        if media_path not in media_dirs:
                            logging.info("Removing %s because it has been removed", media_path)
                            shutil.rmtree(media_path)
                        elif remove_read:
                            media_data = media_dirs[media_path]
                            for chapter_data in media_data.get_sorted_chapters():
                                chapter_path = server._get_dir(media_data, chapter_data, skip_create=True)
                                if chapter_data["read"] and os.path.exists(chapter_path):
                                    logging.info("Removing %s because it has been read", chapter_path)
                                    shutil.rmtree(chapter_path)

            elif remove_disabled_servers:
                logging.info("Removing %s because it is not enabled", server_path)
                shutil.rmtree(server_path)
