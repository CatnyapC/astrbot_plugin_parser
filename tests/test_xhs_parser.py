import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from core.parsers.xhs import XHSParser


def test_xhs_parser_matches_short_link_domains():
    patterns = dict(XHSParser._key_patterns)

    assert patterns["xhslink.com"].search("https://xhslink.com/o/9YVxqQZFNWV")
    assert patterns["xhslink.cn"].search("https://xhslink.cn/o/7w9lW5tigeE")

