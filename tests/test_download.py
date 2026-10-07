import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from core.download import _is_ytdlp_auth_cookie_error


def test_ytdlp_auth_cookie_error_matches_explicit_cookie_messages():
    assert _is_ytdlp_auth_cookie_error(
        "ERROR: [youtube] abc: Sign in to confirm you're not a bot. "
        "Use --cookies-from-browser or --cookies for the authentication."
    )
    assert _is_ytdlp_auth_cookie_error(
        "YouTube account cookies are no longer valid. "
        "They have been rotated in the browser as a security measure."
    )


def test_ytdlp_auth_cookie_error_does_not_match_plain_403():
    assert not _is_ytdlp_auth_cookie_error(
        "ERROR: unable to download video data: HTTP Error 403: Forbidden"
    )
