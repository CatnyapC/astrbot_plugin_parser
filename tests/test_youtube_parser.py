import asyncio
import re
import sys
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import pytest
from yt_dlp import YoutubeDL

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from core.data import Author, VideoContent
from core.parsers.youtube import YouTubeParser


@pytest.mark.parametrize(
    ("available", "expected"),
    [
        (["aac", "h264", "vp9"], ["h264", "aac"]),
        (["progressive_h264", "progressive_vp9"], ["progressive_h264"]),
        (["aac", "vp9"], ["vp9", "aac"]),
    ],
)
def test_youtube_prefers_h264_aac_with_fallback(tmp_path, available, expected):
    video_path = tmp_path / "video.mp4"
    info = SimpleNamespace(
        channel_id="channel",
        duration=10,
        thumbnail="cover",
        description="description",
        title="title",
        timestamp=100,
    )
    parser = object.__new__(YouTubeParser)
    parser.cfg = SimpleNamespace(max_duration=60, proxy=None)
    parser.headers = {}
    parser.cookiejar = SimpleNamespace(cookie_file=None)
    parser.downloader = SimpleNamespace(
        ytdlp_extract_info=AsyncMock(return_value=info),
        ytdlp_download_video=Mock(return_value=video_path),
    )
    parser._fetch_author_info = AsyncMock(return_value=Author("author"))
    parser.create_video_content = Mock(return_value=VideoContent(video_path))

    asyncio.run(parser.parse_video(re.search(r".+", "https://youtu.be/example")))
    selector = parser.downloader.ytdlp_download_video.call_args.kwargs["format"]
    formats = {
        "aac": {"vcodec": "none", "acodec": "mp4a.40.2", "ext": "m4a"},
        "h264": {
            "vcodec": "avc1.64001f",
            "acodec": "none",
            "ext": "mp4",
            "height": 720,
        },
        "vp9": {"vcodec": "vp9", "acodec": "none", "ext": "webm", "height": 720},
        "progressive_h264": {
            "vcodec": "avc1.64001f",
            "acodec": "mp4a.40.2",
            "ext": "mp4",
            "height": 360,
        },
        "progressive_vp9": {
            "vcodec": "vp9",
            "acodec": "opus",
            "ext": "webm",
            "height": 720,
        },
    }
    with YoutubeDL({"quiet": True}) as ydl:
        selected = ydl._select_formats(
            [
                dict(formats[key], format_id=key, url=f"https://example.test/{key}")
                for key in available
            ],
            ydl.build_format_selector(selector),
        )
    assert len(selected) == 1
    streams = selected[0].get("requested_formats", selected)
    assert [stream["format_id"] for stream in streams] == expected
