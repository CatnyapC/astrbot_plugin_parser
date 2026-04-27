import sys
from pathlib import Path
from types import MethodType

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from core.data import Author, ParseResult
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


def test_twitter_parse_backfills_source_url_into_resource_id():
    parser = object.__new__(TwitterParser)

    async def fake_req_xdown_api(self, url: str):
        return {"status": "ok", "data": url}

    def fake_parse_twitter_html(self, _html: str) -> ParseResult:
        return ParseResult(
            platform=TwitterParser.platform,
            author=Author(name="无用户名"),
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
