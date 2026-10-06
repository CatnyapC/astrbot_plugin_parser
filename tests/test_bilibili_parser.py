import sys
from pathlib import Path
from types import SimpleNamespace

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import pytest
from bilibili_api.video import (
    AudioQuality,
    AudioStreamDownloadURL,
    VideoCodecs,
    VideoQuality,
    VideoStreamDownloadURL,
)
from core.exception import DownloadException
from core.parsers.bilibili import BilibiliParser


class _FakeDetector:
    def __init__(self, streams):
        self.streams = streams

    def detect(self, **_kwargs):
        return self.streams


class _BrokenBestStreamDetector(_FakeDetector):
    def __init__(self, _download_url_data):
        super().__init__(
            [
                VideoStreamDownloadURL(
                    url="https://example.test/fallback.m4s",
                    video_quality=VideoQuality._720P,
                    video_codecs=None,  # type: ignore[arg-type]
                ),
                AudioStreamDownloadURL(
                    url="https://example.test/fallback-audio.m4s",
                    audio_quality=AudioQuality._192K,
                ),
            ]
        )

    def detect_best_streams(self, **_kwargs):
        raise AttributeError("'NoneType' object has no attribute 'value'")


class _FakeVideo:
    async def get_download_url(self, *, page_index):
        assert page_index == 0
        return {}


class _FakeCredential:
    def has_sessdata(self):
        return True


class _AISummaryFailingVideo:
    credential = _FakeCredential()

    async def get_info(self):
        return {
            "bvid": "BV1xx411c7mD",
            "title": "title",
            "desc": "desc",
            "duration": 10,
            "owner": {"mid": 1, "name": "up", "face": ""},
            "stat": {
                "view": 1,
                "danmaku": 0,
                "reply": 0,
                "favorite": 0,
                "coin": 0,
                "share": 0,
                "like": 0,
            },
            "pubdate": 1,
            "ctime": 1,
            "pic": None,
            "pages": [{"part": "part", "ctime": 1, "duration": 10}],
        }

    async def get_cid(self, page_index):
        assert page_index == 0
        return 1

    async def get_ai_conclusion(self, _cid):
        raise RuntimeError("账号未登录")


def _parser() -> BilibiliParser:
    parser = object.__new__(BilibiliParser)
    parser.video_quality = VideoQuality._720P
    parser.video_codecs = VideoCodecs.AVC
    return parser


def test_bilibili_fallback_selects_video_when_stream_codec_is_missing():
    parser = _parser()
    missing_codec = VideoStreamDownloadURL(
        url="https://example.test/missing.m4s",
        video_quality=VideoQuality._720P,
        video_codecs=None,  # type: ignore[arg-type]
    )
    lower_quality = VideoStreamDownloadURL(
        url="https://example.test/low.m4s",
        video_quality=VideoQuality._480P,
        video_codecs=VideoCodecs.AVC,
    )
    audio = AudioStreamDownloadURL(
        url="https://example.test/audio.m4s",
        audio_quality=AudioQuality._192K,
    )

    streams = parser._detect_best_streams_fallback(
        _FakeDetector([lower_quality, missing_codec, audio]),
        VideoStreamDownloadURL,
        AudioStreamDownloadURL,
    )
    video_stream, audio_stream = parser._split_download_streams(
        streams,
        VideoStreamDownloadURL,
        AudioStreamDownloadURL,
    )

    assert video_stream.url == "https://example.test/missing.m4s"
    assert audio_stream is not None
    assert audio_stream.url == "https://example.test/audio.m4s"


def test_bilibili_split_download_streams_rejects_missing_video_stream():
    with pytest.raises(DownloadException, match="未找到可下载的视频流"):
        BilibiliParser._split_download_streams(
            [None],
            VideoStreamDownloadURL,
            AudioStreamDownloadURL,
        )


@pytest.mark.asyncio
async def test_bilibili_extract_download_urls_falls_back_on_best_stream_error(
    monkeypatch,
):
    import bilibili_api.video

    monkeypatch.setattr(
        bilibili_api.video,
        "VideoDownloadURLDataDetecter",
        _BrokenBestStreamDetector,
    )

    video_url, audio_url = await _parser().extract_download_urls(video=_FakeVideo())

    assert video_url == "https://example.test/fallback.m4s"
    assert audio_url == "https://example.test/fallback-audio.m4s"


@pytest.mark.asyncio
async def test_bilibili_parse_video_continues_when_ai_summary_fails(tmp_path):
    parser = _parser()
    parser.cfg = SimpleNamespace(cache_dir=tmp_path, max_duration=3600)
    parser.downloader = SimpleNamespace()
    parser.headers = {}
    (tmp_path / "BV1xx411c7mD-1.mp4").write_bytes(b"video")

    async def fake_get_video(**_kwargs):
        return _AISummaryFailingVideo()

    parser._get_video = fake_get_video

    result = await parser.parse_video(bvid="BV1xx411c7mD")

    assert result.title == "title"
    assert result.extra["info"] == "哔哩哔哩 cookie 未配置或失效, 无法使用 AI 总结"
    assert len(result.video_contents) == 1
    assert await result.video_contents[0].get_path() == tmp_path / "BV1xx411c7mD-1.mp4"
