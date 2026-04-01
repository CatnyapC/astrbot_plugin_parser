import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

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
