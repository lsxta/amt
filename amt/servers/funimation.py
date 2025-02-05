import logging
import os
import re

from bs4 import BeautifulSoup

from ..job import Job
from ..server import Server, get_extension
from ..util.media_type import MediaType


class GenericFunimation(Server):
    alias = "funimation"

    CSRF_NAME = "csrfmiddlewaretoken"
    domain = "funimation.com"
    login_url = "https://www.funimation.com/log-in/"
    prod_api_base = "https://prod-api-funimationnow.dadcdigital.com"
    login_api_url = prod_api_base + "/api/auth/login/"
    episode_url = prod_api_base + "/api/funimation/episodes/?limit=99999&title_id={}"

    api_base = "https://www.funimation.com/api"
    show_api_url = api_base + "/experience/{}/"
    sources_api_url = api_base + "/showexperience/{}/?pinst_id=23571113"

    media_type = MediaType.ANIME

    def _get_csrf(self):
        r = self.session_get(self.login_url)
        soup = self.soupify(BeautifulSoup, r)
        return soup.find("input", {"name": self.CSRF_NAME})["value"]

    def needs_authentication(self):
        return not self.session.cookies.get("src_user_id", domain=self.domain)

    @property
    def is_premium(self):
        state = self.session.cookies.get("rlildup", domain=self.domain)
        return "premium" in state.lower() if state else False

    def login(self, username, password):

        r = self.session_post(self.login_api_url,
                              data={"username": username, "password": password, self.CSRF_NAME: self._get_csrf()},
                              headers={"Referer": "https://www.funimation.com/log-in/"})

        data = r.json()
        try:
            self.session.cookies.set("src_token", data["token"], domain=self.domain)
            self.session.cookies.set("src_user_id", str(data["user"]["id"]), domain=self.domain)
            self.session.cookies.set("rlildup", data["rlildup_cookie"], domain=self.domain)
            return True
        except KeyError:
            logging.info(data["error"])
            return False

    def download_subtitles(self, media_data, chapter_data, dir_path):
        r = self.session_get(self.show_api_url.format(chapter_data["alt_id"]))

        for season in r.json()["seasons"]:
            for chapter in season["episodes"]:
                video = chapter["languages"][media_data["lang"].lower()]["alpha"]
                exp = video["simulcast"] if "simulcast" in video else video["uncut"]
                if exp["experienceId"] == int(chapter_data["alt_id"]) and "textTracks" in exp["sources"][0]:
                    for track in exp["sources"][0]["textTracks"]:
                        if self.settings.is_allowed_text_lang(track["language"], media_data):
                            subtitle_src = track["src"]
                            ext = get_extension(subtitle_src)
                            basename = self.settings.get_page_file_name(media_data, chapter_data, ext=ext)
                            path = os.path.join(dir_path, basename)
                            if not os.path.exists(path):
                                r = self.session_get(subtitle_src)
                                with open(path, "wb") as fp:
                                    fp.write(r.content)
                    break

    def get_stream_urls(self, media_data=None, chapter_data=None):
        chapter_id = chapter_data["alt_id"]
        r = self.session_get(self.sources_api_url.format(chapter_id))
        return [item["src"] for item in r.json()["items"]]


class Funimation(GenericFunimation):
    id = "funimation"
    search_url = "https://api-funimation.dadcdigital.com/xml/longlist/content/page/?id=search&q={}"
    list_url = "https://api-funimation.dadcdigital.com/xml/longlist/content/page/?id=shows&limit={}"
    new_api_episdoe_url = "https://title-api.prd.funimationsvc.com/v1/shows/{}/episodes/{}/?region=US&deviceType=web&locale=en"
    stream_url_regex = re.compile("funimation.com/v/([^/]*)/([^/]*)")

    def _get_media_list(self, url, limit=None):
        r = self.session_get(url)
        soup = self.soupify(BeautifulSoup, r)
        media_data = []
        job = Job(self.settings.threads, raiseException=True)
        for item in soup.findAll("item")[:limit]:
            id = item.find("id").text
            job.add(lambda id=id: (self.session_get(self.episode_url.format(id)), id))

        for r, id in job.run():
            title = item.find("title").text
            data = r.json()
            season_data = {(item["item"]["seasonId"], item["item"]["seasonTitle"], audio) for item in data["items"] for audio in item["audio"]}
            experiences = {item["item"]["seasonId"]: item["mostRecentSvod"]["experience"] for item in data["items"]}

            for seasonId, seasonTitle, lang in season_data:
                experience = experiences[seasonId]
                media_data.append(self.create_media_data(id=id, name=title, season_id=seasonId, season_title=seasonTitle, alt_id=experience, lang=lang.lower()))

        return media_data

    def get_media_list(self, limit=2):
        return self._get_media_list(self.list_url.format(limit if limit else 0))

    def search(self, term, alt_id=None, limit=2):
        return self._get_media_list(self.search_url.format(term), limit=limit)

    def _get_episode_id(self, url):
        match = self.stream_url_regex.search(url)
        r = self.session_get(self.new_api_episdoe_url.format(match.group(1), match.group(2)))
        video_info = []
        for video in r.json()["videoList"]:
            lang_code_score = [self.settings.get_prefered_lang_key(self, lang=lang["languageCode"]) for lang in video["spokenLanguages"]]
            lang_name_score = [self.settings.get_prefered_lang_key(self, lang=lang["name"]) for lang in video["spokenLanguages"]]
            video_info.append((min(lang_code_score + lang_name_score), video["id"]))
        return str(min(sorted(video_info))[1])

    def update_media_data(self, media_data: dict, r=None):
        if not r:
            r = self.session_get(self.show_api_url.format(media_data["alt_id"]))

        for season in r.json()["seasons"]:
            if season["seasonPk"] == media_data["season_id"]:
                for chapter in season["episodes"]:
                    if chapter["languages"] and media_data["lang"] in chapter["languages"]:
                        video = chapter["languages"][media_data["lang"]]["alpha"]
                        exp = video["simulcast"] if "simulcast" in video else video["uncut"]
                        alt_exp = video["uncut"] if "uncut" in video else video["simulcast"]
                        premium = exp["svodOnly"]
                        special = chapter["mediaCategory"] != "episode"
                        self.update_chapter_data(media_data, id=alt_exp["experienceId"], number=chapter["episodeId"], title=chapter["episodeTitle"], premium=premium, special=special, alt_id=exp["experienceId"])

    def get_media_data_from_url(self, url):
        chapter_id = self._get_episode_id(url)
        r = self.session_get(self.show_api_url.format(chapter_id))
        data = r.json()
        for season in data["seasons"]:
            for episode in season["episodes"]:
                for lang, videos in episode["languages"].items():
                    video = videos["alpha"]
                    for typeKey in ("simulcast", "uncut"):
                        if typeKey not in video:
                            continue
                        exp = video[typeKey]
                        if int(exp["experienceId"]) == int(chapter_id):
                            media_data = self.create_media_data(id=data["showId"], name=data["showTitle"], season_id=season["seasonPk"], season_title=season["seasonTitle"], alt_id=chapter_id, lang=lang.lower())
                            self.update_media_data(media_data, r=r)
                            return media_data

    def get_chapter_id_for_url(self, url):
        chapter_id = self._get_episode_id(url)
        return chapter_id


class FunimationLibrary(GenericFunimation):
    id = "funimationlib"
    has_free_chapters = False

    list_url = GenericFunimation.prod_api_base + "/api/funimation/library/"
    search_url = GenericFunimation.prod_api_base + "/api/funimation/library/?search={}"
    episode_url = GenericFunimation.prod_api_base + "/api/funimation/library/?show={}&season={}"
    languages = ["Japanese", "English"]

    def _get_media_list_helper(self, data):
        for media_info in data["items"]:
            for season in media_info["seasons"]:
                for lang in self.languages:
                    yield self.create_media_data(id=media_info["slug"], name=media_info["title"], season_id=season["id"], season_title=season["title"], alt_id=media_info["id"], lang=lang, season_number=season["number"])

    def get_auth_header(self):
        t = self.session.cookies.get("src_token", domain=self.domain)
        return {"Authorization": "Token " + t}

    def get_media_list(self, limit=None):
        if self.needs_authentication():
            return []
        data = self.session.get(self.list_url, headers=self.get_auth_header()).json()
        return list(self._get_media_list_helper(data))[:limit]

    def search(self, term, limit=None):
        if self.needs_authentication():
            return []
        data = self.session.get(self.search_url.format(term), headers=self.get_auth_header()).json()
        return list(self._get_media_list_helper(data))[:limit]

    def update_media_data(self, media_data):
        data = self.session.get(self.episode_url.format(media_data["alt_id"], media_data["season_number"]), headers=self.get_auth_header()).json()
        for media_info in data["items"]:
            for season in media_info["seasons"]:
                if season["id"] == media_data["season_id"]:
                    for chapter in season["episodes"]:
                        exp = list(filter(lambda x: x["language"] == media_data["lang"], chapter["experiences"]))
                        if exp:
                            self.update_chapter_data(media_data, id=chapter["id"], number=chapter["number"] or 1, title=chapter["title"], premium=True, alt_id=exp[-1]["id"])
