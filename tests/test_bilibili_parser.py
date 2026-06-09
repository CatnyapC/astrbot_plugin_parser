import sys
from pathlib import Path

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
