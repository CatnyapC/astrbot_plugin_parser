from __future__ import annotations

import asyncio
import hashlib
import json
import platform
import re
import shlex
import time
from collections import deque
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from astrbot.api import logger

from .data import ParseResult, Platform, SendGroup, VideoContent

VIDEO_SLICE_PLATFORM = Platform(name="parserclip", display_name="Parser Clip")
VIDEO_SLICE_DEFAULT_MAX_DURATION_SEC = 60
VIDEO_SLICE_DEFAULT_TIMEOUT_SEC = 120
VIDEO_SLICE_DEFAULT_BITRATE = "2500k"


@dataclass(slots=True)
class VideoSliceCacheEntry:
    cache_id: str
    group_id: str
    path: Path
    duration: float
    source_raw_id: str = ""
    sent_raw_id: str = ""
    source_key: str = ""
    created_at: float = 0.0


@dataclass(slots=True)
class VideoSliceCommand:
    source: str
    start_sec: int
    duration_sec: int
    requester_id: str
    nonce: str
    cache_id: str = ""


@dataclass(slots=True)
class VideoSliceResult:
    status: str
    message: str
    sent: bool = False
    cache_id: str = ""
    output_name: str = ""


class VideoSliceCacheIndex:
    def __init__(
        self,
        *,
        max_entries_per_group: int = 20,
        persist_path: Path | None = None,
    ) -> None:
        self.max_entries_per_group = max(1, int(max_entries_per_group or 20))
        self.persist_path = Path(persist_path) if persist_path else None
        self._by_group: dict[str, deque[VideoSliceCacheEntry]] = {}
        self._load()

    def record(
        self,
        *,
        group_id: str,
        path: Path,
        duration: float = 0.0,
        source_raw_id: str = "",
        sent_raw_id: str = "",
        source_key: str = "",
        created_at: float | None = None,
    ) -> VideoSliceCacheEntry | None:
        group = str(group_id or "").strip()
        if not group:
            return None
        src = Path(path)
        if not src.is_file():
            return None
        entry = VideoSliceCacheEntry(
            cache_id=_cache_id(group_id=group, path=src, source_key=source_key),
            group_id=group,
            path=src,
            duration=max(0.0, float(duration or 0.0)),
            source_raw_id=str(source_raw_id or "").strip(),
            sent_raw_id=str(sent_raw_id or "").strip(),
            source_key=str(source_key or "").strip(),
            created_at=float(created_at if created_at is not None else time.time()),
        )
        self._append(entry)
        self._persist()
        return entry

    def _append(self, entry: VideoSliceCacheEntry) -> None:
        bucket = self._by_group.setdefault(entry.group_id, deque(maxlen=self.max_entries_per_group))
        bucket.append(entry)

    def _load(self) -> None:
        if self.persist_path is None or not self.persist_path.is_file():
            return
        try:
            payload = json.loads(self.persist_path.read_text(encoding="utf-8"))
        except Exception as exc:
            logger.warning(f"[parserclip] cache index load failed: {exc.__class__.__name__}")
            return
        entries = payload.get("entries") if isinstance(payload, dict) else payload
        if not isinstance(entries, list):
            return
        loaded: list[VideoSliceCacheEntry] = []
        for item in entries:
            if not isinstance(item, dict):
                continue
            group = str(item.get("group_id") or "").strip()
            path = Path(str(item.get("path") or ""))
            if not group or not path.is_file():
                continue
            loaded.append(
                VideoSliceCacheEntry(
                    cache_id=str(item.get("cache_id") or _cache_id(group_id=group, path=path, source_key=str(item.get("source_key") or ""))),
                    group_id=group,
                    path=path,
                    duration=max(0.0, _float_or_zero(item.get("duration"))),
                    source_raw_id=str(item.get("source_raw_id") or "").strip(),
                    sent_raw_id=str(item.get("sent_raw_id") or "").strip(),
                    source_key=str(item.get("source_key") or "").strip(),
                    created_at=_float_or_zero(item.get("created_at")),
                )
            )
        for entry in sorted(loaded, key=lambda item: item.created_at):
            self._append(entry)

    def _persist(self) -> None:
        if self.persist_path is None:
            return
        entries = []
        for bucket in self._by_group.values():
            for entry in bucket:
                entries.append(
                    {
                        "cache_id": entry.cache_id,
                        "group_id": entry.group_id,
                        "path": str(entry.path),
                        "duration": entry.duration,
                        "source_raw_id": entry.source_raw_id,
                        "sent_raw_id": entry.sent_raw_id,
                        "source_key": entry.source_key,
                        "created_at": entry.created_at,
                    }
                )
        try:
            self.persist_path.parent.mkdir(parents=True, exist_ok=True)
            tmp = self.persist_path.with_suffix(self.persist_path.suffix + ".tmp")
            tmp.write_text(json.dumps({"entries": entries}, ensure_ascii=False, indent=2), encoding="utf-8")
            tmp.replace(self.persist_path)
        except Exception as exc:
            logger.warning(f"[parserclip] cache index persist failed: {exc.__class__.__name__}")

    def resolve(
        self,
        *,
        group_id: str,
        source: str,
        reply_raw_id: str = "",
        cache_id: str = "",
    ) -> tuple[VideoSliceCacheEntry | None, str]:
        group = str(group_id or "").strip()
        bucket = list(self._by_group.get(group) or [])
        if not group:
            return None, "group_required"
        if not bucket:
            return None, "cache_empty"
        wanted_cache_id = str(cache_id or "").strip()
        if wanted_cache_id:
            for entry in reversed(bucket):
                if entry.cache_id == wanted_cache_id:
                    return entry, ""
            return None, "cache_id_not_found"
        normalized_source = str(source or "").strip().lower()
        if normalized_source == "reply":
            reply = str(reply_raw_id or "").strip()
            if not reply:
                if len(bucket) == 1:
                    return bucket[0], ""
                return None, "reply_required"
            matches = [
                entry
                for entry in bucket
                if reply in {entry.source_raw_id, entry.sent_raw_id}
            ]
            if len(matches) == 1:
                return matches[0], ""
            if len(matches) > 1:
                return None, "source_ambiguous"
            if len(bucket) == 1:
                return bucket[0], ""
            return None, "reply_source_not_found"
        if normalized_source == "current":
            return bucket[-1], ""
        if normalized_source == "latest":
            if len(bucket) == 1:
                return bucket[0], ""
            return None, "source_ambiguous"
        return None, "source_invalid"


class VideoSliceCommandService:
    def __init__(
        self,
        *,
        cfg: Any,
        sender: Any,
        cache_index: VideoSliceCacheIndex,
        run_process: Callable[[list[str], float], Awaitable[tuple[int, str, str]]] | None = None,
        platform_system: Callable[[], str] | None = None,
    ) -> None:
        self.cfg = cfg
        self.sender = sender
        self.cache_index = cache_index
        self.run_process = run_process or _run_process
        self.platform_system = platform_system or platform.system
        self._completed_nonces: set[str] = set()

    async def handle(self, event: Any) -> VideoSliceResult:
        command = parse_parserclip_slice_command(_event_text(event))
        if command is None:
            return VideoSliceResult("ignored", "")
        group_id = str(event.get_group_id() or "").strip()
        sender_id = str(event.get_sender_id() or "").strip()
        if not group_id:
            return VideoSliceResult("rejected", "parserclip slice 仅支持群聊")
        if not self._controller_allowed(event, sender_id=sender_id):
            return VideoSliceResult("rejected", "无权执行 parserclip slice")
        if command.nonce in self._completed_nonces:
            return VideoSliceResult("duplicate", "parserclip slice 已处理，忽略重复请求")
        entry, reason = self.cache_index.resolve(
            group_id=group_id,
            source=command.source,
            reply_raw_id=_reply_raw_id(event),
            cache_id=command.cache_id,
        )
        if entry is None:
            return VideoSliceResult("failed", f"parserclip slice 失败: {reason}")
        cache_root = Path(getattr(self.cfg, "cache_dir", "") or ".")
        if not _path_is_under(entry.path, cache_root):
            return VideoSliceResult("failed", "parserclip slice 失败: cache_source_invalid")
        output_path = _slice_output_path(cache_root=cache_root, nonce=command.nonce)
        probe = await self._probe_video(entry.path)
        if probe.get("status") != "ok":
            return VideoSliceResult("failed", f"parserclip slice 失败: {probe.get('reason') or 'probe_failed'}")
        duration = float(probe.get("duration") or entry.duration or 0.0)
        if duration > 0 and command.start_sec >= duration:
            return VideoSliceResult("failed", "parserclip slice 失败: start_out_of_range")
        max_duration = _positive_int(
            getattr(self.cfg, "parser_video_slice_max_duration_sec", None),
            default=VIDEO_SLICE_DEFAULT_MAX_DURATION_SEC,
        )
        if command.duration_sec > max_duration:
            return VideoSliceResult("failed", "parserclip slice 失败: duration_too_long")
        cut = await self._cut_video(entry.path, output_path, command)
        if cut.get("status") != "ok":
            return VideoSliceResult("failed", f"parserclip slice 失败: {cut.get('reason') or 'ffmpeg_failed'}")
        max_size = int(getattr(self.cfg, "max_size", 0) or 0)
        if max_size > 0 and output_path.stat().st_size > max_size:
            return VideoSliceResult("failed", "parserclip slice 失败: output_too_large")
        sent = await self.sender.send_parse_result(
            event,
            ParseResult(
                platform=VIDEO_SLICE_PLATFORM,
                title="video slice",
                contents=[VideoContent(output_path, duration=float(command.duration_sec))],
                send_groups=[SendGroup(contents=[VideoContent(output_path, duration=float(command.duration_sec))], force_merge=False)],
                extra={"info": "视频切片"},
            ),
        )
        if not sent:
            return VideoSliceResult("failed", "parserclip slice 发送失败")
        self._completed_nonces.add(command.nonce)
        return VideoSliceResult("ok", "", sent=True, cache_id=entry.cache_id, output_name=output_path.name)

    def _controller_allowed(self, event: Any, *, sender_id: str) -> bool:
        is_admin = getattr(event, "is_admin", None)
        if callable(is_admin):
            try:
                if bool(is_admin()):
                    return True
            except Exception:
                pass
        controllers = _configured_controller_ids(self.cfg)
        is_admin_user = getattr(self.cfg, "is_admin_user", None)
        if callable(is_admin_user):
            try:
                if is_admin_user(sender_id):
                    return True
            except Exception:
                pass
        admins = {str(item).strip() for item in getattr(self.cfg, "admins_id", []) if str(item).strip()}
        self_id = str(event.get_self_id() or "").strip()
        return bool(sender_id and (sender_id in controllers or sender_id in admins or sender_id == self_id))

    async def _probe_video(self, path: Path) -> dict[str, Any]:
        timeout = _positive_int(
            getattr(self.cfg, "parser_video_slice_timeout_sec", None),
            default=VIDEO_SLICE_DEFAULT_TIMEOUT_SEC,
        )
        cmd = [
            "ffprobe",
            "-v",
            "error",
            "-show_entries",
            "stream=codec_type:format=duration",
            "-of",
            "json",
            str(path),
        ]
        code, stdout, _stderr = await self.run_process(cmd, float(timeout))
        if code != 0:
            return {"status": "failed", "reason": "ffprobe_failed"}
        try:
            payload = json.loads(stdout or "{}")
        except Exception:
            return {"status": "failed", "reason": "ffprobe_invalid_json"}
        streams = list(payload.get("streams") or [])
        if not any(str(stream.get("codec_type") or "") == "video" for stream in streams if isinstance(stream, dict)):
            return {"status": "failed", "reason": "video_stream_missing"}
        try:
            duration = float(dict(payload.get("format") or {}).get("duration") or 0.0)
        except Exception:
            duration = 0.0
        return {"status": "ok", "duration": duration}

    async def _cut_video(self, source: Path, output_path: Path, command: VideoSliceCommand) -> dict[str, str]:
        timeout = _positive_int(
            getattr(self.cfg, "parser_video_slice_timeout_sec", None),
            default=VIDEO_SLICE_DEFAULT_TIMEOUT_SEC,
        )
        first = _ffmpeg_slice_command(
            source=source,
            output_path=output_path,
            start_sec=command.start_sec,
            duration_sec=command.duration_sec,
            encoder="h264_videotoolbox" if self.platform_system() == "Darwin" else "libx264",
        )
        code, _stdout, _stderr = await self.run_process(first, float(timeout))
        if code == 0 and output_path.is_file():
            return {"status": "ok"}
        fallback = _ffmpeg_slice_command(
            source=source,
            output_path=output_path,
            start_sec=command.start_sec,
            duration_sec=command.duration_sec,
            encoder="libx264",
        )
        code, _stdout, _stderr = await self.run_process(fallback, float(timeout))
        if code == 0 and output_path.is_file():
            return {"status": "ok"}
        return {"status": "failed", "reason": "ffmpeg_failed"}


async def _run_process(cmd: list[str], timeout: float) -> tuple[int, str, str]:
    try:
        process = await asyncio.create_subprocess_exec(
            *cmd,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
    except FileNotFoundError:
        return 127, "", "not_found"
    try:
        stdout, stderr = await asyncio.wait_for(process.communicate(), timeout=max(1.0, float(timeout or 1.0)))
    except TimeoutError:
        process.kill()
        await process.communicate()
        return 124, "", "timeout"
    return process.returncode, stdout.decode(errors="replace"), stderr.decode(errors="replace")


def parse_parserclip_slice_command(text: str) -> VideoSliceCommand | None:
    raw = str(text or "").strip()
    if not raw:
        return None
    try:
        parts = shlex.split(raw)
    except ValueError:
        return None
    if len(parts) < 2:
        return None
    if parts[0].lstrip("/") != "parserclip" or parts[1] != "slice":
        return None
    opts = _parse_options(parts[2:])
    source = str(opts.get("source") or "").strip().lower()
    start = _parse_int(opts.get("start"))
    duration = _parse_int(opts.get("duration"))
    requester = _clean_token(opts.get("requester"), max_len=64)
    nonce = _clean_token(opts.get("nonce"), max_len=80)
    cache_id = _clean_token(opts.get("cache-id") or opts.get("cache_id"), max_len=64)
    if source not in {"reply", "current", "latest"} or start is None or duration is None:
        return None
    if start < 0 or duration <= 0 or not requester or not nonce:
        return None
    return VideoSliceCommand(
        source=source,
        start_sec=start,
        duration_sec=duration,
        requester_id=requester,
        nonce=nonce,
        cache_id=cache_id,
    )


def _parse_options(parts: list[str]) -> dict[str, str]:
    out: dict[str, str] = {}
    idx = 0
    while idx < len(parts):
        item = parts[idx]
        if not item.startswith("--"):
            idx += 1
            continue
        key = item[2:]
        value = ""
        if "=" in key:
            key, value = key.split("=", 1)
        elif idx + 1 < len(parts) and not parts[idx + 1].startswith("--"):
            idx += 1
            value = parts[idx]
        out[key.strip()] = value.strip()
        idx += 1
    return out


def _ffmpeg_slice_command(
    *,
    source: Path,
    output_path: Path,
    start_sec: int,
    duration_sec: int,
    encoder: str,
) -> list[str]:
    cmd = [
        "ffmpeg",
        "-y",
        "-ss",
        str(max(0, int(start_sec))),
        "-t",
        str(max(1, int(duration_sec))),
        "-i",
        str(source),
        "-map",
        "0:v:0",
        "-map",
        "0:a?",
        "-c:v",
        encoder,
    ]
    if encoder == "h264_videotoolbox":
        cmd.extend(["-b:v", VIDEO_SLICE_DEFAULT_BITRATE])
    else:
        cmd.extend(["-preset", "medium", "-crf", "25"])
    cmd.extend(["-c:a", "aac", "-movflags", "+faststart", str(output_path)])
    return cmd


def _event_text(event: Any) -> str:
    return str(getattr(event, "message_str", "") or "")


def _reply_raw_id(event: Any) -> str:
    raw_obj = getattr(getattr(event, "message_obj", None), "raw_message", None)
    if isinstance(raw_obj, dict):
        for key in ("reply_to_message_id", "reply_to_raw_id"):
            value = str(raw_obj.get(key) or "").strip()
            if value:
                return value
        for item in list(raw_obj.get("message") or []):
            if isinstance(item, dict) and item.get("type") == "reply":
                value = str(dict(item.get("data") or {}).get("id") or "").strip()
                if value:
                    return value
        raw_text = str(raw_obj.get("raw_message") or "")
        match = re.search(r"\[CQ:reply,id=([^\],]+)", raw_text)
        if match:
            return match.group(1).strip()
    return ""


def _event_raw_id(event: Any) -> str:
    raw_obj = getattr(getattr(event, "message_obj", None), "raw_message", None)
    if isinstance(raw_obj, dict):
        for key in ("message_id", "raw_id", "id"):
            value = str(raw_obj.get(key) or "").strip()
            if value:
                return value
    return ""


def _path_is_under(path: Path, root: Path) -> bool:
    try:
        path.resolve(strict=False).relative_to(root.resolve(strict=False))
        return True
    except Exception:
        return False


def _slice_output_path(*, cache_root: Path, nonce: str) -> Path:
    safe_nonce = _clean_token(nonce, max_len=80) or hashlib.blake2b(str(time.time()).encode(), digest_size=6).hexdigest()
    return Path(cache_root).resolve(strict=False) / f"parserclip_{safe_nonce}.mp4"


def _configured_controller_ids(cfg: Any) -> set[str]:
    raw = getattr(cfg, "parser_video_slice_controller_ids", None)
    values: list[Any]
    if isinstance(raw, str):
        text = raw.strip()
        if not text:
            values = []
        else:
            try:
                parsed = json.loads(text)
                values = parsed if isinstance(parsed, list) else [text]
            except Exception:
                values = re.split(r"[\s,;]+", text)
    elif isinstance(raw, list):
        values = raw
    else:
        values = []
    return {str(item).strip() for item in values if str(item).strip()}


def _clean_token(value: Any, *, max_len: int) -> str:
    text = str(value or "").strip()
    text = re.sub(r"[^A-Za-z0-9_.:-]", "", text)
    return text[:max(1, int(max_len))]


def _parse_int(value: Any) -> int | None:
    try:
        return int(float(str(value).strip()))
    except Exception:
        return None


def _positive_int(value: Any, *, default: int) -> int:
    parsed = _parse_int(value)
    return max(1, parsed if parsed is not None else int(default))


def _float_or_zero(value: Any) -> float:
    try:
        return float(value or 0.0)
    except Exception:
        return 0.0


def _cache_id(*, group_id: str, path: Path, source_key: str = "") -> str:
    h = hashlib.blake2b(digest_size=8)
    for part in (group_id, path.name, source_key):
        h.update(str(part or "").encode())
        h.update(b"|")
    return h.hexdigest()


async def record_video_slice_cache_from_group(
    cache_index: VideoSliceCacheIndex,
    *,
    event: Any,
    group: SendGroup,
) -> None:
    group_id = str(event.get_group_id() or "").strip()
    if not group_id:
        return
    source_raw_id = _event_raw_id(event)
    for content in list(group.contents or []):
        if not isinstance(content, VideoContent):
            continue
        try:
            path = await content.get_path()
        except Exception as exc:
            logger.debug(f"[parserclip] skip cache record: {exc.__class__.__name__}")
            continue
        cache_index.record(
            group_id=group_id,
            path=path,
            duration=float(content.duration or 0.0),
            source_raw_id=source_raw_id,
            source_key=str(content.source_key or ""),
        )
