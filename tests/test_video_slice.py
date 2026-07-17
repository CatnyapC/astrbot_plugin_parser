from __future__ import annotations

import asyncio
import json
import sys
from pathlib import Path
from types import SimpleNamespace

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from core.data import ParseResult, VideoContent
from core.video_slice import (
    VideoSliceCacheIndex,
    VideoSliceCommandService,
    parse_parserclip_slice_command,
)


class DummyEvent:
    def __init__(
        self,
        *,
        text: str = "/parserclip slice --source reply --start 2 --duration 3 --requester u1 --nonce n1",
        group_id: str = "g1",
        sender_id: str = "bot1",
        self_id: str = "bot1",
        raw: dict | None = None,
    ) -> None:
        self.message_str = text
        self._group_id = group_id
        self._sender_id = sender_id
        self._self_id = self_id
        self.message_obj = SimpleNamespace(raw_message=raw or {"message_id": "src1", "reply_to_message_id": "src-video"})

    def get_group_id(self) -> str:
        return self._group_id

    def get_sender_id(self) -> str:
        return self._sender_id

    def get_self_id(self) -> str:
        return self._self_id


class DummySender:
    def __init__(self) -> None:
        self.results: list[ParseResult] = []

    async def send_parse_result(self, _event, result: ParseResult) -> bool:
        self.results.append(result)
        return True


def _cfg(tmp_path: Path, **overrides):
    data = {
        "cache_dir": tmp_path,
        "max_size": 20 * 1024 * 1024,
        "admins_id": ["admin1"],
        "parser_video_slice_controller_ids": "controller1",
        "parser_video_slice_max_duration_sec": 60,
        "parser_video_slice_timeout_sec": 10,
    }
    data.update(overrides)
    return SimpleNamespace(**data)


def _video(tmp_path: Path, name: str = "source.mp4") -> Path:
    path = tmp_path / name
    path.write_bytes(b"video")
    return path


def test_parse_parserclip_slice_command_contract() -> None:
    parsed = parse_parserclip_slice_command(
        "/parserclip slice --source latest --start 90 --duration 10 --requester 10001 --nonce pvs-g1-10001-123"
    )

    assert parsed is not None
    assert parsed.source == "latest"
    assert parsed.start_sec == 90
    assert parsed.duration_sec == 10
    assert parsed.requester_id == "10001"
    assert parsed.nonce == "pvs-g1-10001-123"


def test_cache_source_resolver_reply_current_and_latest(tmp_path: Path) -> None:
    cache = VideoSliceCacheIndex()
    first = cache.record(group_id="g1", path=_video(tmp_path, "a.mp4"), source_raw_id="r1")
    assert first is not None
    assert cache.resolve(group_id="g1", source="reply", reply_raw_id="r1")[0] == first
    assert cache.resolve(group_id="g1", source="current")[0] == first
    assert cache.resolve(group_id="g1", source="latest")[0] == first

    second = cache.record(group_id="g1", path=_video(tmp_path, "b.mp4"), source_raw_id="r2")
    assert second is not None
    assert cache.resolve(group_id="g1", source="current")[0] == second
    assert cache.resolve(group_id="g1", source="latest")[0] == second
    assert cache.resolve(group_id="g2", source="current")[1] == "cache_empty"


def test_controller_allowlist_and_group_guard(tmp_path: Path) -> None:
    service = VideoSliceCommandService(
        cfg=_cfg(tmp_path),
        sender=DummySender(),
        cache_index=VideoSliceCacheIndex(),
        run_process=lambda *_: _ok_probe(),
    )

    rejected = asyncio.run(service.handle(DummyEvent(sender_id="ordinary", self_id="bot1")))
    assert rejected.status == "rejected"
    assert "无权" in rejected.message

    private = asyncio.run(service.handle(DummyEvent(group_id="", sender_id="bot1", self_id="bot1")))
    assert private.status == "rejected"
    assert "群聊" in private.message


def test_slice_uses_ffprobe_ffmpeg_fallback_and_sender_path(tmp_path: Path) -> None:
    source = _video(tmp_path)
    cache = VideoSliceCacheIndex()
    cache.record(group_id="g1", path=source, source_raw_id="src-video", duration=20)
    sender = DummySender()
    commands: list[list[str]] = []

    async def run_process(cmd: list[str], _timeout: float) -> tuple[int, str, str]:
        commands.append(cmd)
        if cmd[0] == "ffprobe":
            return 0, json.dumps({"streams": [{"codec_type": "video"}], "format": {"duration": "20.0"}}), ""
        if "h264_videotoolbox" in cmd:
            return 1, "", "encoder failed"
        Path(cmd[-1]).write_bytes(b"clip")
        return 0, "", ""

    service = VideoSliceCommandService(
        cfg=_cfg(tmp_path),
        sender=sender,
        cache_index=cache,
        run_process=run_process,
        platform_system=lambda: "Darwin",
    )
    result = asyncio.run(service.handle(DummyEvent(sender_id="bot1", self_id="bot1")))

    assert result.status == "ok"
    assert result.sent is True
    assert [cmd[0] for cmd in commands] == ["ffprobe", "ffmpeg", "ffmpeg"]
    assert "h264_videotoolbox" in commands[1]
    assert "libx264" in commands[2]
    assert "-ss" in commands[2]
    assert "-t" in commands[2]
    assert isinstance(sender.results[0].send_groups[0].contents[0], VideoContent)


def test_nonce_duplicate_suppresses_second_upload(tmp_path: Path) -> None:
    source = _video(tmp_path)
    cache = VideoSliceCacheIndex()
    cache.record(group_id="g1", path=source, source_raw_id="src-video", duration=20)
    sender = DummySender()

    async def run_process(cmd: list[str], _timeout: float) -> tuple[int, str, str]:
        if cmd[0] == "ffprobe":
            return 0, json.dumps({"streams": [{"codec_type": "video"}], "format": {"duration": "20.0"}}), ""
        Path(cmd[-1]).write_bytes(b"clip")
        return 0, "", ""

    service = VideoSliceCommandService(
        cfg=_cfg(tmp_path),
        sender=sender,
        cache_index=cache,
        run_process=run_process,
        platform_system=lambda: "Linux",
    )

    first = asyncio.run(service.handle(DummyEvent(sender_id="bot1", self_id="bot1")))
    second = asyncio.run(service.handle(DummyEvent(sender_id="bot1", self_id="bot1")))

    assert first.status == "ok"
    assert second.status == "duplicate"
    assert len(sender.results) == 1


async def _ok_probe() -> tuple[int, str, str]:
    return 0, json.dumps({"streams": [{"codec_type": "video"}], "format": {"duration": "20.0"}}), ""
