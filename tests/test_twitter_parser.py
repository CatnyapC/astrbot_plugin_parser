import asyncio
import sys
from pathlib import Path
from types import MethodType, SimpleNamespace

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import pytest
from core.data import Author, ImageContent, ParseResult, VideoContent
from core.exception import ParseException
from core.parsers.twitter import TwitterParser


def test_twitter_parser_matches_x_and_legacy_twitter_urls():
    patterns = dict(TwitterParser._key_patterns)

    assert "x.com" in patterns
    assert "twitter.com" in patterns

    assert patterns["x.com"].search(
        "https://x.com/ninifox16/status/2038547680780243032?s=20"
    )
    assert patterns["twitter.com"].search(
        "https://twitter.com/ninifox16/status/2038547680780243032?s=46&t=BsSRashN7mpzqghnxkxupg"
    )
    assert patterns["x.com"].search(
        "https://x.com/i/status/2063227101793378744?utm=1"
    )
    assert patterns["x.com"].search(
        "https://x.com/ninifox16/status/2038547680780243032/video/1?s=20"
    )


def test_twitter_parse_backfills_source_url_into_resource_id():
    parser = object.__new__(TwitterParser)

    async def fake_req_xdown_api(self, url: str):
        return {"status": "ok", "data": url}

    def fake_parse_twitter_html(self, _html: str) -> ParseResult:
        return ParseResult(
            platform=TwitterParser.platform,
            author=Author(name="无用户名"),
            contents=[ImageContent(Path("twitter.jpg"))],
        )

    parser._req_xdown_api = MethodType(fake_req_xdown_api, parser)
    parser.parse_twitter_html = MethodType(fake_parse_twitter_html, parser)

    pattern = dict(TwitterParser._key_patterns)["x.com"]
    url1 = "https://x.com/user/status/111?s=20"
    url2 = "https://x.com/user/status/222?s=20"
    canonical_url1 = pattern.search(url1).group(0)
    canonical_url2 = pattern.search(url2).group(0)

    result1 = __import__("asyncio").run(parser._parse(pattern.search(url1)))
    result2 = __import__("asyncio").run(parser._parse(pattern.search(url2)))

    assert result1.url == canonical_url1
    assert result2.url == canonical_url2
    assert result1.get_resource_id() != result2.get_resource_id()


class _FakeDownloader:
    def download_img(self, url: str, **_kwargs):
        return Path(f"{url.rsplit('/', 1)[-1] or 'image'}.jpg")

    def download_video(self, url: str, **_kwargs):
        return Path(f"{url.rsplit('/', 1)[-1] or 'video'}.mp4")


def _parser_with_api(*, enabled=True, token="token") -> TwitterParser:
    parser = object.__new__(TwitterParser)
    parser.mycfg = SimpleNamespace(
        x_api_enable=enabled,
        x_api_auth_mode="bearer",
        x_api_bearer_token=token,
        x_api_user_bearer_token="",
        x_api_cache_ttl_seconds=3600,
        x_api_timeout_seconds=15,
    )
    parser.headers = {}
    parser.downloader = _FakeDownloader()
    return parser


def test_twitter_xdown_success_does_not_call_x_api():
    parser = _parser_with_api(enabled=True)
    called = False

    async def fake_req_xdown_api(self, url: str):
        return {
            "status": "ok",
            "data": '<a class="abutton" href="https://cdn.example/p.jpg">下载图片</a>',
        }

    async def fake_req_x_api_post(self, _tweet_id: str):
        nonlocal called
        called = True
        return {}

    parser._req_xdown_api = MethodType(fake_req_xdown_api, parser)
    parser._req_x_api_post = MethodType(fake_req_x_api_post, parser)

    pattern = dict(TwitterParser._key_patterns)["x.com"]
    result = __import__("asyncio").run(
        parser._parse(pattern.search("https://x.com/i/status/2063227101793378744"))
    )

    assert not called
    assert len(result.img_contents) == 1


def test_twitter_xdown_empty_falls_back_to_x_api_photo():
    parser = _parser_with_api(enabled=True)

    async def fake_req_xdown_api(self, url: str):
        return {"status": "ok", "data": None}

    async def fake_req_x_api_post(self, tweet_id: str):
        assert tweet_id == "2063227101793378744"
        return {
            "data": {
                "id": tweet_id,
                "text": "hidden post",
                "attachments": {"media_keys": ["3_1"]},
                "author_id": "42",
            },
            "includes": {
                "media": [
                    {
                        "media_key": "3_1",
                        "type": "photo",
                        "url": "https://pbs.twimg.com/media/photo.jpg",
                    }
                ],
                "users": [{"id": "42", "username": "author"}],
            },
        }

    parser._req_xdown_api = MethodType(fake_req_xdown_api, parser)
    parser._req_x_api_post = MethodType(fake_req_x_api_post, parser)

    pattern = dict(TwitterParser._key_patterns)["x.com"]
    result = __import__("asyncio").run(
        parser._parse(pattern.search("https://x.com/i/status/2063227101793378744"))
    )

    assert result.title == "hidden post"
    assert result.author.name == "author"
    assert len(result.img_contents) == 1


def test_twitter_media_permalink_is_canonicalized_for_xdown():
    parser = _parser_with_api(enabled=False)
    requested_urls = []

    async def fake_req_xdown_api(self, url: str):
        requested_urls.append(url)
        return {
            "status": "ok",
            "data": '<a class="abutton" href="https://cdn.example/p.jpg">下载图片</a>',
        }

    parser._req_xdown_api = MethodType(fake_req_xdown_api, parser)

    pattern = dict(TwitterParser._key_patterns)["x.com"]
    result = asyncio.run(
        parser._parse(
            pattern.search("https://x.com/user/status/123/video/1?s=20")
        )
    )

    assert requested_urls == ["https://x.com/i/status/123"]
    assert result.url == "https://x.com/user/status/123/video/1?s=20"


def test_twitter_xdown_timeout_falls_back_to_x_api_video():
    parser = _parser_with_api(enabled=True)

    async def fake_req_xdown_api(self, url: str):
        assert url == "https://x.com/i/status/2069918493554659648"
        raise asyncio.TimeoutError

    async def fake_req_x_api_post(self, tweet_id: str):
        assert tweet_id == "2069918493554659648"
        return {
            "data": {
                "id": tweet_id,
                "attachments": {"media_keys": ["7_1"]},
            },
            "includes": {
                "media": [
                    {
                        "media_key": "7_1",
                        "type": "video",
                        "duration_ms": 1000,
                        "preview_image_url": "https://pbs.twimg.com/cover.jpg",
                        "variants": [
                            {
                                "content_type": "video/mp4",
                                "bit_rate": 832000,
                                "url": "https://video.twimg.com/high.mp4",
                            }
                        ],
                    }
                ]
            },
        }

    parser._req_xdown_api = MethodType(fake_req_xdown_api, parser)
    parser._req_x_api_post = MethodType(fake_req_x_api_post, parser)

    pattern = dict(TwitterParser._key_patterns)["x.com"]
    result = asyncio.run(
        parser._parse(
            pattern.search("https://x.com/BRK_gif/status/2069918493554659648/video/1")
        )
    )

    assert len(result.video_contents) == 1
    assert result.video_contents[0].source_key == "https://video.twimg.com/high.mp4"


def test_twitter_x_api_video_picks_highest_bitrate_variant():
    parser = _parser_with_api(enabled=True)

    async def fake_req_x_api_post(self, _tweet_id: str):
        return {
            "data": {
                "id": "1",
                "attachments": {"media_keys": ["7_1"]},
            },
            "includes": {
                "media": [
                    {
                        "media_key": "7_1",
                        "type": "video",
                        "duration_ms": 12000,
                        "preview_image_url": "https://pbs.twimg.com/cover.jpg",
                        "variants": [
                            {"content_type": "application/x-mpegURL", "url": "m3u8"},
                            {
                                "content_type": "video/mp4",
                                "bit_rate": 256000,
                                "url": "https://video.twimg.com/low.mp4",
                            },
                            {
                                "content_type": "video/mp4",
                                "bit_rate": 832000,
                                "url": "https://video.twimg.com/high.mp4",
                            },
                        ],
                    }
                ]
            },
        }

    parser._req_x_api_post = MethodType(fake_req_x_api_post, parser)

    result = __import__("asyncio").run(
        parser._parse_x_api("1", "https://x.com/i/status/1")
    )

    assert len(result.video_contents) == 1
    video = result.video_contents[0]
    assert isinstance(video, VideoContent)
    assert video.source_key == "https://video.twimg.com/high.mp4"
    assert video.duration == 12


def test_twitter_api_enabled_without_token_fails_before_request():
    parser = _parser_with_api(enabled=True, token="")

    with pytest.raises(ParseException, match="X API token 未配置"):
        __import__("asyncio").run(parser._req_x_api_post("1"))


def test_twitter_xdown_headers_do_not_include_cookie():
    parser = object.__new__(TwitterParser)
    parser.headers = {"User-Agent": "ua"}
    parser.xdown_headers = parser.headers.copy()
    parser.xdown_headers.update({"Accept": "application/json"})

    assert "cookie" not in parser.xdown_headers
