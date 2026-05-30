from __future__ import annotations

import asyncio
import json
import sys
from pathlib import Path
from types import SimpleNamespace

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from core.clean import CacheCleaner
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
        is_admin: bool = False,
        raw: dict | None = None,
    ) -> None:
        self.message_str = text
        self._group_id = group_id
        self._sender_id = sender_id
        self._self_id = self_id
        self._is_admin = is_admin
        self.message_obj = SimpleNamespace(raw_message=raw or {"message_id": "src1", "reply_to_message_id": "src-video"})

    def get_group_id(self) -> str:
        return self._group_id

    def get_sender_id(self) -> str:
        return self._sender_id

    def get_self_id(self) -> str:
        return self._self_id

    def is_admin(self) -> bool:
        return self._is_admin


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


def test_parse_parserclip_slice_command_accepts_missing_legacy_requester() -> None:
    parsed = parse_parserclip_slice_command(
        "/parserclip slice --source latest --start 90 --duration 10 --nonce pvs-g1-123"
    )

    assert parsed is not None
    assert parsed.requester_id == ""
    assert parsed.nonce == "pvs-g1-123"


def test_parse_parserclip_slice_command_accepts_missing_legacy_nonce() -> None:
    parsed = parse_parserclip_slice_command(
        "/parserclip slice --source latest --start 90 --duration 10"
    )

    assert parsed is not None
    assert parsed.requester_id == ""
    assert parsed.nonce == ""


def test_parse_parserclip_slice_command_accepts_safe_reply_refs() -> None:
    direct = parse_parserclip_slice_command(
        "/parserclip slice --source reply --reply-raw-id 1280 --start 1 --duration 10"
    )
    typed = parse_parserclip_slice_command(
        "/parserclip slice --source-ref reply:1281 --start 1 --duration 10"
    )

    assert direct is not None
    assert direct.reply_raw_id == "1280"
    assert typed is not None
    assert typed.source == "reply"
    assert typed.reply_raw_id == "1281"


def test_cache_source_resolver_reply_current_latest_and_ambiguous(tmp_path: Path) -> None:
    cache = VideoSliceCacheIndex()
    first = cache.record(group_id="g1", path=_video(tmp_path, "a.mp4"), source_raw_id="r1")
    assert first is not None
    assert cache.resolve(group_id="g1", source="reply", reply_raw_id="r1")[0] == first
    assert cache.resolve(group_id="g1", source="current")[0] == first
    assert cache.resolve(group_id="g1", source="latest")[0] == first

    second = cache.record(group_id="g1", path=_video(tmp_path, "b.mp4"), source_raw_id="r2")
    assert second is not None
    assert cache.resolve(group_id="g1", source="current")[0] == second
    latest, reason = cache.resolve(group_id="g1", source="latest")
    assert latest is None
    assert reason == "source_ambiguous"
    assert cache.resolve(group_id="g2", source="current")[1] == "cache_empty"


def test_cache_index_persists_and_reply_singleton_fallback(tmp_path: Path) -> None:
    persist_path = tmp_path / "video_slice_index.json"
    source = _video(tmp_path, "persisted.mp4")
    cache = VideoSliceCacheIndex(persist_path=persist_path)
    entry = cache.record(group_id="g1", path=source, source_raw_id="source-msg", duration=12)

    assert entry is not None
    reloaded = VideoSliceCacheIndex(persist_path=persist_path)
    resolved, reason = reloaded.resolve(group_id="g1", source="reply", reply_raw_id="parser-sent-msg")
    no_reply_resolved, no_reply_reason = reloaded.resolve(group_id="g1", source="reply")

    assert reason == ""
    assert resolved is not None
    assert resolved.path == source
    assert no_reply_reason == ""
    assert no_reply_resolved == resolved


def test_cache_index_prunes_missing_and_old_entries(tmp_path: Path) -> None:
    persist_path = tmp_path / "video_slice_index.json"
    keep = _video(tmp_path, "keep.mp4")
    missing = _video(tmp_path, "missing.mp4")
    old = _video(tmp_path, "old.mp4")
    cache = VideoSliceCacheIndex(persist_path=persist_path)
    cache.record(group_id="g1", path=keep, created_at=30)
    cache.record(group_id="g1", path=missing, created_at=40)
    cache.record(group_id="g1", path=old, created_at=10)
    missing.unlink()

    removed = cache.prune(older_than=20)
    reloaded = VideoSliceCacheIndex(persist_path=persist_path)

    assert removed == 2
    assert reloaded.resolve(group_id="g1", source="current")[0].path == keep


def test_cache_cleaner_prunes_video_slice_index(tmp_path: Path) -> None:
    cache_dir = tmp_path / "cache"
    cache_dir.mkdir()
    persist_path = tmp_path / "video_slice_index.json"
    video = cache_dir / "video.mp4"
    video.write_bytes(b"video")
    index = VideoSliceCacheIndex(persist_path=persist_path)
    index.record(group_id="g1", path=video, created_at=10)

    cfg = SimpleNamespace(
        cache_dir=cache_dir,
        clean_cron="30 2 * * *",
        timezone="UTC",
        ensure_dir=lambda path: Path(path).mkdir(parents=True, exist_ok=True) or Path(path),
    )
    cleaner = CacheCleaner(cfg, video_slice_cache=index, start_scheduler=False)
    asyncio.run(cleaner._clean_plugin_cache())
    asyncio.run(cleaner.stop())

    assert cache_dir.is_dir()
    assert not video.exists()
    assert json.loads(persist_path.read_text(encoding="utf-8"))["entries"] == []


def test_group_guard_still_rejects_private(tmp_path: Path) -> None:
    service = VideoSliceCommandService(
        cfg=_cfg(tmp_path),
        sender=DummySender(),
        cache_index=VideoSliceCacheIndex(),
        run_process=lambda *_: _ok_probe(),
    )

    private = asyncio.run(service.handle(DummyEvent(group_id="", sender_id="bot1", self_id="bot1")))
    assert private.status == "rejected"
    assert "群聊" in private.message


def test_ordinary_non_admin_can_execute_slice(tmp_path: Path) -> None:
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

    result = asyncio.run(service.handle(DummyEvent(sender_id="ordinary", self_id="bot1")))

    assert result.status == "ok"
    assert result.sent is True
    assert len(sender.results) == 1


def test_explicit_reply_ref_resolves_correct_cache_entry(tmp_path: Path) -> None:
    original = _video(tmp_path, "original.mp4")
    prior_slice = _video(tmp_path, "prior_slice.mp4")
    cache = VideoSliceCacheIndex()
    original_entry = cache.record(group_id="g1", path=original, sent_raw_id="tg-video-1", duration=20)
    cache.record(group_id="g1", path=prior_slice, sent_raw_id="tg-slice-1", duration=7)
    sender = DummySender()

    async def run_process(cmd: list[str], _timeout: float) -> tuple[int, str, str]:
        if cmd[0] == "ffprobe":
            return 0, json.dumps({"streams": [{"codec_type": "video"}], "format": {"duration": "20.0"}}), ""
        assert cmd[cmd.index("-i") + 1] == str(original)
        Path(cmd[-1]).write_bytes(b"clip")
        return 0, "", ""

    service = VideoSliceCommandService(
        cfg=_cfg(tmp_path),
        sender=sender,
        cache_index=cache,
        run_process=run_process,
        platform_system=lambda: "Linux",
    )

    result = asyncio.run(
        service.handle(
            DummyEvent(
                text="/parserclip slice --source reply --reply-raw-id tg-video-1 --start 1 --duration 10",
                raw={"message_id": "cmd1"},
                sender_id="router-bot",
            )
        )
    )

    assert result.status == "ok"
    assert original_entry is not None
    assert result.cache_id == original_entry.cache_id
    assert len(sender.results) == 1


def test_event_reply_metadata_takes_priority_over_explicit_ref(tmp_path: Path) -> None:
    event_reply_video = _video(tmp_path, "event_reply.mp4")
    explicit_reply_video = _video(tmp_path, "explicit_reply.mp4")
    cache = VideoSliceCacheIndex()
    event_entry = cache.record(group_id="g1", path=event_reply_video, sent_raw_id="event-ref", duration=20)
    cache.record(group_id="g1", path=explicit_reply_video, sent_raw_id="explicit-ref", duration=20)

    async def run_process(cmd: list[str], _timeout: float) -> tuple[int, str, str]:
        if cmd[0] == "ffprobe":
            return 0, json.dumps({"streams": [{"codec_type": "video"}], "format": {"duration": "20.0"}}), ""
        assert cmd[cmd.index("-i") + 1] == str(event_reply_video)
        Path(cmd[-1]).write_bytes(b"clip")
        return 0, "", ""

    service = VideoSliceCommandService(
        cfg=_cfg(tmp_path),
        sender=DummySender(),
        cache_index=cache,
        run_process=run_process,
        platform_system=lambda: "Linux",
    )

    result = asyncio.run(
        service.handle(
            DummyEvent(
                text="/parserclip slice --source reply --reply-raw-id explicit-ref --start 1 --duration 10",
                raw={"message_id": "cmd1", "reply_to_message_id": "event-ref"},
                sender_id="router-bot",
            )
        )
    )

    assert result.status == "ok"
    assert event_entry is not None
    assert result.cache_id == event_entry.cache_id


def test_missing_reply_ref_still_requires_reply_without_path_leak(tmp_path: Path) -> None:
    first = _video(tmp_path, "first.mp4")
    second = _video(tmp_path, "second.mp4")
    cache = VideoSliceCacheIndex()
    cache.record(group_id="g1", path=first, sent_raw_id="first-ref", duration=20)
    cache.record(group_id="g1", path=second, sent_raw_id="second-ref", duration=20)
    service = VideoSliceCommandService(
        cfg=_cfg(tmp_path),
        sender=DummySender(),
        cache_index=cache,
        run_process=lambda *_: _ok_probe(),
    )

    result = asyncio.run(
        service.handle(
            DummyEvent(
                text="/parserclip slice --source reply --start 1 --duration 10",
                raw={"message_id": "cmd1"},
                sender_id="router-bot",
            )
        )
    )

    assert result.status == "failed"
    assert result.message == "parserclip slice 失败: reply_required"
    assert str(first) not in result.message
    assert str(second) not in result.message


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


def test_generated_idempotency_key_suppresses_second_upload(tmp_path: Path) -> None:
    source = _video(tmp_path)
    cache = VideoSliceCacheIndex()
    cache.record(group_id="g1", path=source, source_raw_id="src-video", duration=20)
    sender = DummySender()
    commands: list[list[str]] = []

    async def run_process(cmd: list[str], _timeout: float) -> tuple[int, str, str]:
        commands.append(cmd)
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
    assert [cmd[0] for cmd in commands] == ["ffprobe", "ffmpeg"]
    assert service._rate_limit_count == 1


def test_legacy_nonce_command_still_executes(tmp_path: Path) -> None:
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

    result = asyncio.run(
        service.handle(
            DummyEvent(
                text="/parserclip slice --source reply --start 2 --duration 3 --requester old --nonce old-nonce",
                sender_id="ordinary",
                self_id="bot1",
            )
        )
    )

    assert result.status == "ok"
    assert len(sender.results) == 1


def test_global_rate_limit_rejects_thirty_first_request(tmp_path: Path) -> None:
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

    for idx in range(30):
        result = asyncio.run(
            service.handle(
                DummyEvent(
                    text=f"/parserclip slice --source reply --start 2 --duration {idx + 1}",
                    sender_id="ordinary",
                    self_id="bot1",
                )
            )
        )
        assert result.status == "ok"

    limited = asyncio.run(
        service.handle(
            DummyEvent(
                text="/parserclip slice --source reply --start 2 --duration 31",
                sender_id="ordinary",
                self_id="bot1",
            )
        )
    )

    assert limited.status == "rate_limited"
    assert "每小时最多30次" in limited.message

    restarted = VideoSliceCommandService(
        cfg=_cfg(tmp_path),
        sender=DummySender(),
        cache_index=cache,
        run_process=run_process,
        platform_system=lambda: "Linux",
    )
    after_restart = asyncio.run(
        restarted.handle(
            DummyEvent(
                text="/parserclip slice --source reply --start 2 --duration 31",
                sender_id="ordinary",
                self_id="bot1",
            )
        )
    )

    assert after_restart.status == "ok"


async def _ok_probe() -> tuple[int, str, str]:
    return 0, json.dumps({"streams": [{"codec_type": "video"}], "format": {"duration": "20.0"}}), ""
