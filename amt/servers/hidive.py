import re

from bs4 import BeautifulSoup

from ..server import Server
from ..util.media_type import MediaType


class Hidive(Server):
    id = "hidive"
    media_type = MediaType.ANIME
    has_free_chapters = False

    base_url = "https://www.hidive.com"
    domain = ".hidive.com"
    search_url = base_url + "/search?q={}"
    list_url = base_url + "/dashboard"
    login_url = base_url + "/account/login"
    episode_list_url = base_url + "/tv/{}"
    episode_list_pattern = "/stream/{}/(s.*e(.*))"

    stream_url_regex = re.compile(r"/stream/([^/]*)/([^/]*)")
    series_url_regex = re.compile(r"/(?:tv|movies)/([^/]*)")

    def needs_authentication(self):
        return not self.session.cookies.get("Welcomed", domain=self.domain)

    @property
    def is_premium(self):
        status = self.session.cookies.get("UserStatus", domain=self.domain)
        return status and status in ("Trial")

    def login(self, username, password):
        r = self.session_get(self.login_url)
        soup = self.soupify(BeautifulSoup, r)
        form = soup.find("form", {"id": "form-login"})
        data = {}
        for input_elements in form.findAll("input"):
            data[input_elements["name"]] = input_elements.get("value", "")
        data["Email"] = username
        data["Password"] = password

        r = self.session_post(self.base_url + form["action"], data=data)
        return not self.needs_authentication()

    def find_links_from_url(self, url, regex):
        r = self.session.get(url)
        soup = self.soupify(BeautifulSoup, r)
        for link in soup.findAll("a"):
            href = link.get("data-playurl", "") or link.get("href", "")
            match = regex.search(href)
            if match:
                yield link, match
        print(url, regex)

    def _get_media_list(self, url, regex):
        media_data = []
        seen_ids = set()
        for link, match in self.find_links_from_url(url, regex):
            media_id = match.group(1)
            title = link.get("data-title") or link.getText().strip()
            if title and media_id not in seen_ids:
                media_data.append(self.create_media_data(id=media_id, name=title))
                seen_ids.add(media_id)
        return media_data

    def get_media_list(self, limit=2):
        return self._get_media_list(self.list_url, self.stream_url_regex)[:limit]

    def search(self, term, alt_id=None, limit=2):
        return self._get_media_list(self.search_url.format(term), self.series_url_regex)[:limit]

    def update_media_data(self, media_data: dict, r=None):
        regex = re.compile(self.episode_list_pattern.format(media_data["id"]))
        for link, match in self.find_links_from_url(self.episode_list_url.format(media_data["id"]), regex):
            parent = link.parent.parent.parent.parent
            element = parent.find(lambda x: x.getText().strip() and (x.get("data-original-title") or x.get("title")))
            if element:
                title = element.get("data-original-title") or element.get("title")
                self.update_chapter_data(media_data, id=match.group(1), number=match.group(2), title=title, premium=True)

    def get_stream_urls(self, media_data=None, chapter_data=None):
        episode_data_url = "https://www.hidive.com/play/settings"
        r = self.session_post(episode_data_url, data={"Title": media_data["id"], "Key": chapter_data["id"], "PlayerId": "f4f895ce1ca713ba263b91caeb1daa2d08904783"})
        data = r.json()
        print(data)
        return [item["src"] for item in r.json()["items"]]

    def get_media_data_from_url(self, url):
        media_id = self.stream_url_regex.search(url).group(1)
        r = self.session_get(url)
        soup = self.soupify(BeautifulSoup, r)
        title = soup.find("a", {"id": "TitleDetails"}).getText()
        return self.create_media_data(id=media_id, name=title)

    def get_chapter_id_for_url(self, url):
        return self.stream_url_regex.search(url).group(2)
