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
        client=None,
    ) -> None:
        self.message_str = text
        self._group_id = group_id
        self._sender_id = sender_id
        self._self_id = self_id
        self._is_admin = is_admin
        self.message_obj = SimpleNamespace(raw_message=raw or {"message_id": "src1", "reply_to_message_id": "src-video"})
        self.client = client

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


class FakeTelegramFile:
    def __init__(self, *, payload: bytes = b"video", fail: bool = False) -> None:
        self.payload = payload
        self.fail = fail

    async def download_to_drive(self, *, custom_path: Path) -> None:
        custom_path.write_bytes(b"partial")
        if self.fail:
            raise RuntimeError("download failed")
        custom_path.write_bytes(self.payload)


class FakeTelegramClient:
    def __init__(self, file: FakeTelegramFile) -> None:
        self.file = file
        self.file_ids: list[str] = []

    async def get_file(self, file_id: str) -> FakeTelegramFile:
        self.file_ids.append(file_id)
        return self.file


def _tg_reply_raw(reply_message) -> SimpleNamespace:
    return SimpleNamespace(message=SimpleNamespace(reply_to_message=reply_message))


def _tg_video_reply(
    *,
    message_id: int = 1280,
    file_id: str = "file-1",
    file_unique_id: str = "unique-1",
    file_size: int | None = 5,
    duration: float = 20.0,
    mime_type: str = "video/mp4",
    file_name: str = "upload.mp4",
) -> SimpleNamespace:
    return SimpleNamespace(
        message_id=message_id,
        video=SimpleNamespace(
            file_id=file_id,
            file_unique_id=file_unique_id,
            file_size=file_size,
            duration=duration,
            mime_type=mime_type,
            file_name=file_name,
        ),
        document=None,
    )


def _tg_document_reply(
    *,
    message_id: int = 1280,
    file_id: str = "file-1",
    file_unique_id: str = "unique-1",
    file_size: int | None = 5,
    mime_type: str = "video/mp4",
    file_name: str = "upload.mp4",
) -> SimpleNamespace:
    return SimpleNamespace(
        message_id=message_id,
        video=None,
        document=SimpleNamespace(
            file_id=file_id,
            file_unique_id=file_unique_id,
            file_size=file_size,
            duration=0,
            mime_type=mime_type,
            file_name=file_name,
        ),
    )


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
    resolved, reason = reloaded.resolve(group_id="g1", source="reply", reply_raw_id="source-msg")
    mismatched, mismatch_reason = reloaded.resolve(group_id="g1", source="reply", reply_raw_id="parser-sent-msg")
    no_reply_resolved, no_reply_reason = reloaded.resolve(group_id="g1", source="reply")

    assert reason == ""
    assert resolved is not None
    assert resolved.path == source
    assert mismatched is None
    assert mismatch_reason == "reply_source_not_found"
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


def test_uncached_telegram_video_reply_downloads_registers_and_slices(tmp_path: Path) -> None:
    cached = _video(tmp_path, "cached.mp4")
    cache = VideoSliceCacheIndex()
    cache.record(group_id="g1", path=cached, source_raw_id="old-msg", duration=20)
    sender = DummySender()
    client = FakeTelegramClient(FakeTelegramFile(payload=b"telegram-video"))
    ffmpeg_inputs: list[str] = []

    async def run_process(cmd: list[str], _timeout: float) -> tuple[int, str, str]:
        if cmd[0] == "ffprobe":
            return 0, json.dumps({"streams": [{"codec_type": "video"}], "format": {"duration": "20.0"}}), ""
        ffmpeg_inputs.append(cmd[cmd.index("-i") + 1])
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
                text="/parserclip slice --source reply --start 1 --duration 10",
                raw=_tg_reply_raw(_tg_video_reply(message_id=1280, file_id="tg-file", file_unique_id="tg-unique")),
                client=client,
                sender_id="router-bot",
            )
        )
    )
    resolved, reason = cache.resolve(group_id="g1", source="reply", reply_raw_id="1280")

    assert result.status == "ok"
    assert len(sender.results) == 1
    assert client.file_ids == ["tg-file"]
    assert resolved is not None
    assert reason == ""
    assert resolved.path.parent == tmp_path
    assert resolved.path.name.startswith("telegram_1280_tg-unique")
    assert resolved.source_raw_id == "1280"
    assert resolved.source_key == "telegram:tg-unique"
    assert ffmpeg_inputs == [str(resolved.path)]


def test_uncached_telegram_video_document_reply_is_allowed(tmp_path: Path) -> None:
    cache = VideoSliceCacheIndex()
    sender = DummySender()
    client = FakeTelegramClient(FakeTelegramFile(payload=b"telegram-video"))

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
                text="/parserclip slice --source reply --start 1 --duration 10",
                raw=_tg_reply_raw(_tg_document_reply(message_id=1281, file_unique_id="doc-unique")),
                client=client,
                sender_id="router-bot",
            )
        )
    )

    assert result.status == "ok"
    assert len(sender.results) == 1
    assert cache.resolve(group_id="g1", source="reply", reply_raw_id="1281")[0] is not None


def test_uncached_telegram_reply_rejects_missing_size_without_download(tmp_path: Path) -> None:
    cache = VideoSliceCacheIndex()
    client = FakeTelegramClient(FakeTelegramFile(payload=b"telegram-video"))
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
                raw=_tg_reply_raw(_tg_video_reply(file_size=None)),
                client=client,
                sender_id="router-bot",
            )
        )
    )

    assert result.status == "failed"
    assert result.message == "parserclip slice 失败: reply_media_size_unknown"
    assert client.file_ids == []


def test_uncached_telegram_reply_rejects_oversize_without_path_leak(tmp_path: Path) -> None:
    cache = VideoSliceCacheIndex()
    service = VideoSliceCommandService(
        cfg=_cfg(tmp_path, max_size=4),
        sender=DummySender(),
        cache_index=cache,
        run_process=lambda *_: _ok_probe(),
    )

    result = asyncio.run(
        service.handle(
            DummyEvent(
                text="/parserclip slice --source reply --start 1 --duration 10",
                raw=_tg_reply_raw(_tg_video_reply(file_size=5)),
                client=FakeTelegramClient(FakeTelegramFile(payload=b"telegram-video")),
                sender_id="router-bot",
            )
        )
    )

    assert result.status == "failed"
    assert result.message == "parserclip slice 失败: reply_media_too_large"
    assert str(tmp_path) not in result.message


def test_uncached_telegram_reply_rejects_non_video_document(tmp_path: Path) -> None:
    cache = VideoSliceCacheIndex()
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
                raw=_tg_reply_raw(_tg_document_reply(mime_type="application/pdf")),
                client=FakeTelegramClient(FakeTelegramFile(payload=b"telegram-video")),
                sender_id="router-bot",
            )
        )
    )

    assert result.status == "failed"
    assert result.message == "parserclip slice 失败: reply_media_unsupported"


def test_uncached_telegram_download_failure_cleans_partial_and_skips_index(tmp_path: Path) -> None:
    cache = VideoSliceCacheIndex()
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
                raw=_tg_reply_raw(_tg_video_reply(message_id=1282, file_unique_id="will-fail")),
                client=FakeTelegramClient(FakeTelegramFile(payload=b"telegram-video", fail=True)),
                sender_id="router-bot",
            )
        )
    )

    assert result.status == "failed"
    assert result.message == "parserclip slice 失败: reply_media_download_failed"
    assert cache.resolve(group_id="g1", source="reply", reply_raw_id="1282")[1] == "cache_empty"
    assert not list(tmp_path.glob("*.part"))
    assert not list(tmp_path.glob("telegram_1282*"))


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
