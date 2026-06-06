import re
import time
from itertools import chain
from typing import Any, ClassVar

from aiohttp import ClientError
from bs4 import BeautifulSoup, Tag

from ..config import PluginConfig
from ..data import ParseResult, Platform
from ..download import Downloader
from ..exception import ParseException
from .base import BaseParser, handle


class TwitterParser(BaseParser):
    # 平台信息
    platform: ClassVar[Platform] = Platform(name="twitter", display_name="推特")
    _X_API_FIELDS: ClassVar[dict[str, str]] = {
        "expansions": "attachments.media_keys,author_id",
        "tweet.fields": "possibly_sensitive,author_id,created_at,text",
        "media.fields": (
            "url,preview_image_url,variants,type,width,height,duration_ms,alt_text"
        ),
        "user.fields": "username,name",
    }

    def __init__(self, config: PluginConfig, downloader: Downloader):
        super().__init__(config, downloader)
        self.mycfg = config.parser.twitter
        self.xdown_headers = self.headers.copy()
        self.xdown_headers.update(
            {
                "Accept": "application/json, text/plain, */*",
                "Content-Type": "application/x-www-form-urlencoded",
                "Origin": "https://xdown.app",
                "Referer": "https://xdown.app/",
            }
        )
        self.xdown_url = "https://xdown.app/api/ajaxSearch"
        self._x_api_cache: dict[str, tuple[float, dict[str, Any]]] = {}

    async def _req_xdown_api(self, url: str) -> dict[str, Any]:
        async with self.session.post(
            url=self.xdown_url,
            data={"q": url, "lang": "zh-cn"},
            headers=self.xdown_headers,
        ) as resp:
            if resp.status >= 400:
                raise ClientError(f"xdown API {resp.status} {resp.reason}")
            return await resp.json()

    def _x_api_enabled(self) -> bool:
        return bool(getattr(self.mycfg, "x_api_enable", False))

    def _x_api_token(self) -> str:
        mode = (getattr(self.mycfg, "x_api_auth_mode", None) or "bearer").strip()
        if mode == "oauth2_user":
            token = getattr(self.mycfg, "x_api_user_bearer_token", None) or ""
        else:
            token = getattr(self.mycfg, "x_api_bearer_token", None) or ""
        return token.strip()

    def _x_api_cache_ttl(self) -> int:
        value = getattr(self.mycfg, "x_api_cache_ttl_seconds", None)
        try:
            ttl = int(value or 3600)
        except (TypeError, ValueError):
            ttl = 3600
        return max(0, ttl)

    def _tweet_id_from_match(self, searched: re.Match[str]) -> str:
        return searched.group("tweet_id")

    async def _req_x_api_post(self, tweet_id: str) -> dict[str, Any]:
        if not self._x_api_enabled():
            raise ParseException("X API fallback 未启用")

        token = self._x_api_token()
        if not token:
            raise ParseException("X API token 未配置")

        now = time.monotonic()
        ttl = self._x_api_cache_ttl()
        if ttl > 0 and (cached := self._x_api_cache.get(tweet_id)) is not None:
            cached_at, data = cached
            if now - cached_at < ttl:
                return data
            self._x_api_cache.pop(tweet_id, None)

        timeout = getattr(self.mycfg, "x_api_timeout_seconds", None) or 15
        try:
            timeout = max(1, int(timeout))
        except (TypeError, ValueError):
            timeout = 15

        headers = {
            "Accept": "application/json",
            "Authorization": f"Bearer {token}",
        }
        async with self.session.get(
            f"https://api.x.com/2/tweets/{tweet_id}",
            params=self._X_API_FIELDS,
            headers=headers,
            proxy=self.proxy,
            timeout=timeout,
        ) as resp:
            if resp.status == 401 or resp.status == 403:
                raise ParseException("x_api_forbidden_or_token_invalid")
            if resp.status == 404:
                raise ParseException("x_api_post_not_found")
            if resp.status == 429:
                raise ParseException("x_api_rate_limited")
            if resp.status >= 400:
                raise ParseException("x_api_unavailable")
            data = await resp.json()

        if ttl > 0:
            self._x_api_cache[tweet_id] = (now, data)
        return data

    async def _parse_x_api(self, tweet_id: str, source_url: str) -> ParseResult:
        resp = await self._req_x_api_post(tweet_id)

        post = resp.get("data")
        if not isinstance(post, dict):
            raise ParseException("x_api_post_not_found")

        includes = resp.get("includes")
        if not isinstance(includes, dict):
            includes = {}

        media_by_key: dict[str, dict[str, Any]] = {}
        media_items = includes.get("media")
        if isinstance(media_items, list):
            for item in media_items:
                if isinstance(item, dict) and isinstance(item.get("media_key"), str):
                    media_by_key[item["media_key"]] = item

        media_keys = (
            post.get("attachments", {}).get("media_keys")
            if isinstance(post.get("attachments"), dict)
            else None
        )
        if not isinstance(media_keys, list) or not media_keys:
            raise ParseException("x_api_no_media")

        image_urls: list[str] = []
        dynamic_urls: list[str] = []
        video_contents = []
        for raw_key in media_keys:
            if not isinstance(raw_key, str):
                continue
            media = media_by_key.get(raw_key)
            if not media:
                continue
            media_type = media.get("type")
            if media_type == "photo":
                if isinstance(media.get("url"), str):
                    image_urls.append(media["url"])
                continue
            if media_type in {"video", "animated_gif"}:
                video_url = self._best_x_api_video_variant(media)
                if video_url is None:
                    continue
                if media_type == "animated_gif":
                    dynamic_urls.append(video_url)
                    continue
                duration = media.get("duration_ms") or 0
                try:
                    duration_sec = float(duration) / 1000
                except (TypeError, ValueError):
                    duration_sec = 0.0
                video_contents.append(
                    self.create_video_content(
                        video_url,
                        media.get("preview_image_url"),
                        duration=duration_sec,
                    )
                )

        contents = []
        contents.extend(video_contents)
        if image_urls:
            contents.extend(self.create_image_contents(image_urls))
        if dynamic_urls:
            contents.extend(self.create_dynamic_contents(dynamic_urls))

        if not contents:
            raise ParseException("x_api_media_url_missing")

        users = includes.get("users")
        author_name = "无用户名"
        if isinstance(users, list) and users and isinstance(users[0], dict):
            author_name = str(users[0].get("username") or users[0].get("name") or author_name)

        return self.result(
            title=post.get("text") if isinstance(post.get("text"), str) else None,
            author=self.create_author(author_name),
            contents=contents,
            url=source_url,
        )

    @staticmethod
    def _best_x_api_video_variant(media: dict[str, Any]) -> str | None:
        variants = media.get("variants")
        if not isinstance(variants, list):
            return None
        best_url = None
        best_bitrate = -1
        for variant in variants:
            if not isinstance(variant, dict):
                continue
            if variant.get("content_type") != "video/mp4":
                continue
            url = variant.get("url")
            if not isinstance(url, str):
                continue
            bitrate = variant.get("bit_rate", 0)
            try:
                bitrate_int = int(bitrate or 0)
            except (TypeError, ValueError):
                bitrate_int = 0
            if bitrate_int >= best_bitrate:
                best_bitrate = bitrate_int
                best_url = url
        return best_url

    @handle(
        "twitter.com",
        r"https?://(?:www\.)?twitter\.com/[0-9-a-zA-Z_]{1,20}/status/(?P<tweet_id>[0-9]+)(?:/(?:video|photo)/[0-9]+)?(?:\?[^\s]*)?",
    )
    @handle(
        "x.com",
        r"https?://(?:www\.)?x\.com/(?:i/status|[0-9-a-zA-Z_]{1,20}/status)/(?P<tweet_id>[0-9]+)(?:/(?:video|photo)/[0-9]+)?(?:\?[^\s]*)?",
    )
    async def _parse(self, searched: re.Match[str]) -> ParseResult:
        # 从匹配对象中获取原始URL
        url = searched.group(0)
        tweet_id = self._tweet_id_from_match(searched)
        try:
            resp = await self._req_xdown_api(url)
            if resp.get("status") != "ok":
                raise ParseException("解析失败")

            html_content = resp.get("data")

            if html_content is None:
                raise ParseException("解析失败, 数据为空")

            result = self.parse_twitter_html(html_content)
            if not result.contents:
                raise ParseException("解析失败, 未返回媒体")
        except (ClientError, ParseException):
            if not self._x_api_enabled():
                raise
            result = await self._parse_x_api(tweet_id, url)

        if result.url is None:
            result.url = url
        return result

    def parse_twitter_html(self, html_content: str) -> ParseResult:
        """解析 Twitter HTML 内容

        Args:
            html_content (str): Twitter HTML 内容

        Returns:
            ParseResult: 解析结果
        """
        soup = BeautifulSoup(html_content, "html.parser")

        # 初始化数据
        title = None
        cover_url = None
        video_url = None
        images_urls = []
        dynamic_urls = []

        # 1. 提取缩略图链接
        thumb_tag = soup.find("img")
        if isinstance(thumb_tag, Tag):
            if cover := thumb_tag.get("src"):
                cover_url = str(cover)

        # 2. 提取下载链接
        tw_button_tags = soup.find_all("a", class_="tw-button-dl")
        abutton_tags = soup.find_all("a", class_="abutton")
        for tag in chain(tw_button_tags, abutton_tags):
            if not isinstance(tag, Tag):
                continue
            href = tag.get("href")
            if href is None:
                continue

            href = str(href)
            text = tag.get_text(strip=True)
            if "下载 MP4" in text:
                video_url = href
                break
            elif "下载图片" in text:
                images_urls.append(href)
            elif "下载 gif" in text:
                dynamic_urls.append(href)

        # 3. 提取标题
        title_tag = soup.find("h3")
        if title_tag:
            title = title_tag.get_text(strip=True)

        # 简洁的构建方式
        contents = []

        # 添加视频内容
        if video_url:
            contents.append(self.create_video_content(video_url, cover_url))

        # 添加图片内容
        if images_urls:
            contents.extend(self.create_image_contents(images_urls))

        # 添加动态内容
        if dynamic_urls:
            contents.extend(self.create_dynamic_contents(dynamic_urls))

        return self.result(
            title=title,
            author=self.create_author("无用户名"),
            contents=contents,
        )
        # # 4. 提取Twitter ID
        # twitter_id_input = soup.find("input", {"id": "TwitterId"})
        # if (
        #     twitter_id_input
        #     and isinstance(twitter_id_input, Tag)
        #     and (value := twitter_id_input.get("value"))
        #     and isinstance(value, str)
        # ):
