from __future__ import annotations

import asyncio
import hashlib
import json
import mimetypes
import platform
import re
import shlex
import time
from collections import deque
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

import aiofiles
from aiohttp import ClientSession, ClientTimeout

from astrbot.api import logger

from .data import ParseResult, Platform, SendGroup, VideoContent

VIDEO_SLICE_PLATFORM = Platform(name="parserclip", display_name="Parser Clip")
VIDEO_SLICE_DEFAULT_MAX_DURATION_SEC = 60
VIDEO_SLICE_DEFAULT_TIMEOUT_SEC = 120
VIDEO_SLICE_DEFAULT_BITRATE = "2500k"
VIDEO_SLICE_REPLY_FALLBACK_WINDOW_SEC = 30 * 60


@dataclass(slots=True)
class VideoSliceCacheEntry:
    cache_id: str
    group_id: str
    path: Path
    duration: float
    source_raw_id: str = ""
    sent_raw_id: str = ""
    source_key: str = ""
    parser_sent_output: bool = False
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


@dataclass(slots=True)
class TelegramReplyVideoMedia:
    file_id: str
    file_unique_id: str
    file_size: int | None
    duration: float
    mime_type: str
    file_name: str
    message_id: str
    kind: str


@dataclass(slots=True)
class OneBotReplyVideoMedia:
    url: str
    file_key: str
    file_size: int | None
    duration: float
    file_name: str
    message_id: str
    kind: str


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
        parser_sent_output: bool = False,
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
            parser_sent_output=bool(parser_sent_output),
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
                    parser_sent_output=bool(item.get("parser_sent_output")),
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
                        "parser_sent_output": bool(entry.parser_sent_output),
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

    def prune(self, *, older_than: float | None = None) -> int:
        removed = 0
        next_groups: dict[str, deque[VideoSliceCacheEntry]] = {}
        for group, bucket in self._by_group.items():
            kept: deque[VideoSliceCacheEntry] = deque(maxlen=self.max_entries_per_group)
            for entry in bucket:
                is_old = older_than is not None and entry.created_at <= older_than
                if is_old or not entry.path.is_file():
                    removed += 1
                    continue
                kept.append(entry)
            if kept:
                next_groups[group] = kept
        if removed:
            self._by_group = next_groups
            self._persist()
        return removed

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
            fallback_matches = self._recent_parser_sent_output_candidates(bucket)
            if len(fallback_matches) == 1:
                return fallback_matches[0], ""
            if len(fallback_matches) > 1:
                return None, "reply_source_ambiguous"
            return None, "reply_source_not_found"
        if normalized_source == "current":
            return bucket[-1], ""
        if normalized_source == "latest":
            if len(bucket) == 1:
                return bucket[0], ""
            return None, "source_ambiguous"
        return None, "source_invalid"

    @staticmethod
    def _recent_parser_sent_output_candidates(
        bucket: list[VideoSliceCacheEntry],
    ) -> list[VideoSliceCacheEntry]:
        cutoff = time.time() - VIDEO_SLICE_REPLY_FALLBACK_WINDOW_SEC
        return [
            entry
            for entry in bucket
            if entry.created_at >= cutoff
            and (
                entry.parser_sent_output
                or entry.path.name.startswith("parserclip_")
            )
        ]


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
        self._completed_keys: set[str] = set()
        self._rate_limit_started_at = time.time()
        self._rate_limit_count = 0
        self._rate_limit_window_sec = 3600.0
        self._rate_limit_max = 30

    async def handle(self, event: Any) -> VideoSliceResult:
        command = parse_parserclip_slice_command(_event_text(event))
        if command is None:
            return VideoSliceResult("ignored", "")
        group_id = str(event.get_group_id() or "").strip()
        if not group_id:
            return VideoSliceResult("rejected", "parserclip slice 仅支持群聊")
        reply_raw_id = _reply_raw_id(event)
        entry, reason = self.cache_index.resolve(
            group_id=group_id,
            source=command.source,
            reply_raw_id=reply_raw_id,
            cache_id=command.cache_id,
        )
        if entry is None:
            if command.source == "reply" and reply_raw_id and not command.cache_id:
                entry, reason = await self._cache_replied_telegram_video(
                    event=event,
                    group_id=group_id,
                    reply_raw_id=reply_raw_id,
                    resolve_reason=reason,
                )
            if entry is None and command.source == "reply" and reply_raw_id and not command.cache_id:
                entry, reason = await self._cache_replied_onebot_video(
                    event=event,
                    group_id=group_id,
                    reply_raw_id=reply_raw_id,
                    resolve_reason=reason,
                )
            if entry is None:
                return VideoSliceResult("failed", f"parserclip slice 失败: {reason}")
        idem_key = self._idempotency_key(
            event=event,
            group_id=group_id,
            command=command,
            entry=entry,
            reply_raw_id=reply_raw_id,
        )
        if idem_key in self._completed_keys:
            return VideoSliceResult("duplicate", "parserclip slice 已处理，忽略重复请求")
        if not self._consume_rate_limit():
            return VideoSliceResult("rate_limited", "parserclip slice 失败: rate_limited 每小时最多30次")
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
        self._completed_keys.add(idem_key)
        return VideoSliceResult("ok", "", sent=True, cache_id=entry.cache_id, output_name=output_path.name)

    async def _cache_replied_telegram_video(
        self,
        *,
        event: Any,
        group_id: str,
        reply_raw_id: str,
        resolve_reason: str,
    ) -> tuple[VideoSliceCacheEntry | None, str]:
        if resolve_reason not in {"cache_empty", "reply_source_not_found"}:
            return None, resolve_reason
        reply_message = _telegram_reply_message(event)
        if reply_message is None:
            return None, resolve_reason
        media, reason = _telegram_reply_video_media(reply_message)
        if media is None:
            return None, reason
        if not media.file_id:
            return None, "reply_media_not_found"
        if not self._allow_direct_reply_media(event=event, media=media):
            return None, "reply_media_policy_blocked"
        max_size = int(getattr(self.cfg, "max_size", 0) or 0)
        if media.file_size is None:
            return None, "reply_media_size_unknown"
        if max_size > 0 and media.file_size > max_size:
            return None, "reply_media_too_large"
        cache_root = Path(getattr(self.cfg, "cache_dir", "") or ".").resolve(strict=False)
        if not cache_root.is_dir():
            cache_root.mkdir(parents=True, exist_ok=True)
        suffix = _safe_media_suffix(media.file_name, media.mime_type)
        safe_name = _clean_token(
            f"telegram_{media.message_id}_{media.file_unique_id}",
            max_len=96,
        ) or hashlib.blake2b(str(time.time()).encode(), digest_size=8).hexdigest()
        target_path = cache_root / f"{safe_name}{suffix}"
        tmp_path = cache_root / f".{safe_name}{suffix}.part"
        try:
            await self._download_telegram_file(event, media.file_id, tmp_path)
            if not tmp_path.is_file() or tmp_path.stat().st_size <= 0:
                return None, "reply_media_download_failed"
            if max_size > 0 and tmp_path.stat().st_size > max_size:
                return None, "reply_media_too_large"
            tmp_path.replace(target_path)
        except Exception:
            return None, "reply_media_download_failed"
        finally:
            try:
                tmp_path.unlink(missing_ok=True)
            except Exception:
                pass
        entry = self.cache_index.record(
            group_id=group_id,
            path=target_path,
            duration=media.duration,
            source_raw_id=reply_raw_id,
            source_key=f"telegram:{media.file_unique_id}" if media.file_unique_id else "",
            parser_sent_output=False,
        )
        if entry is None:
            try:
                target_path.unlink(missing_ok=True)
            except Exception:
                pass
            return None, "reply_media_download_failed"
        return entry, ""

    async def _cache_replied_onebot_video(
        self,
        *,
        event: Any,
        group_id: str,
        reply_raw_id: str,
        resolve_reason: str,
    ) -> tuple[VideoSliceCacheEntry | None, str]:
        if resolve_reason not in {"cache_empty", "reply_source_not_found"}:
            return None, resolve_reason
        reply_message, reason = await _onebot_reply_message(event, reply_raw_id)
        if reply_message is None:
            return None, reason or resolve_reason
        reply_group = str(dict(reply_message).get("group_id") or "").strip()
        if not reply_group:
            return None, "reply_media_group_unknown"
        if reply_group != str(group_id or "").strip():
            return None, "reply_media_cross_group"
        media, reason = await _onebot_reply_video_media(event, reply_message)
        if media is None:
            return None, reason
        if not self._allow_direct_reply_media(event=event, media=media):
            return None, "reply_media_policy_blocked"
        max_size = int(getattr(self.cfg, "max_size", 0) or 0)
        if media.file_size is None:
            return None, "reply_media_size_unknown"
        if max_size > 0 and media.file_size > max_size:
            return None, "reply_media_too_large"
        cache_root = Path(getattr(self.cfg, "cache_dir", "") or ".").resolve(strict=False)
        if not cache_root.is_dir():
            cache_root.mkdir(parents=True, exist_ok=True)
        suffix = _safe_media_suffix(media.file_name, "video/mp4")
        source_hash = hashlib.blake2b(media.file_key.encode(), digest_size=8).hexdigest()
        safe_name = _clean_token(
            f"onebot_{media.message_id}_{source_hash}",
            max_len=96,
        ) or hashlib.blake2b(str(time.time()).encode(), digest_size=8).hexdigest()
        target_path = cache_root / f"{safe_name}{suffix}"
        tmp_path = cache_root / f".{safe_name}{suffix}.part"
        try:
            await self._download_onebot_url(
                media.url,
                tmp_path,
                expected_size=media.file_size,
                max_size=max_size,
            )
            if not tmp_path.is_file() or tmp_path.stat().st_size <= 0:
                return None, "reply_media_download_failed"
            if max_size > 0 and tmp_path.stat().st_size > max_size:
                return None, "reply_media_too_large"
            tmp_path.replace(target_path)
        except ValueError as exc:
            reason = str(exc) or "reply_media_download_failed"
            return None, reason if reason.startswith("reply_media_") else "reply_media_download_failed"
        except Exception:
            return None, "reply_media_download_failed"
        finally:
            try:
                tmp_path.unlink(missing_ok=True)
            except Exception:
                pass
        entry = self.cache_index.record(
            group_id=group_id,
            path=target_path,
            duration=media.duration,
            source_raw_id=reply_raw_id,
            source_key=f"onebot:{source_hash}",
            parser_sent_output=False,
        )
        if entry is None:
            try:
                target_path.unlink(missing_ok=True)
            except Exception:
                pass
            return None, "reply_media_download_failed"
        return entry, ""

    async def _download_telegram_file(self, event: Any, file_id: str, target_path: Path) -> None:
        timeout = _positive_int(
            getattr(self.cfg, "parser_video_slice_timeout_sec", None),
            default=VIDEO_SLICE_DEFAULT_TIMEOUT_SEC,
        )
        client = getattr(event, "client", None)
        if client is None or not callable(getattr(client, "get_file", None)):
            raise RuntimeError("telegram_client_unavailable")
        tg_file = await asyncio.wait_for(client.get_file(file_id), timeout=float(timeout))
        downloader = getattr(tg_file, "download_to_drive", None)
        if not callable(downloader):
            raise RuntimeError("telegram_download_unavailable")
        await asyncio.wait_for(downloader(custom_path=target_path), timeout=float(timeout))

    async def _download_onebot_url(
        self,
        url: str,
        target_path: Path,
        *,
        expected_size: int,
        max_size: int,
    ) -> None:
        parsed = urlparse(str(url or "").strip())
        if parsed.scheme not in {"http", "https"}:
            raise ValueError("reply_media_no_url")
        timeout = _positive_int(
            getattr(self.cfg, "parser_video_slice_timeout_sec", None),
            default=VIDEO_SLICE_DEFAULT_TIMEOUT_SEC,
        )
        async with ClientSession(timeout=ClientTimeout(total=float(timeout))) as session:
            async with session.get(url, allow_redirects=True) as response:
                if response.status >= 400:
                    raise RuntimeError("download_http_failed")
                content_length = _optional_positive_int(response.headers.get("Content-Length"))
                if content_length is not None and content_length != expected_size:
                    if max_size > 0 and content_length > max_size:
                        raise ValueError("reply_media_too_large")
                downloaded = 0
                async with aiofiles.open(target_path, "wb") as file:
                    async for chunk in response.content.iter_chunked(1024 * 1024):
                        downloaded += len(chunk)
                        if max_size > 0 and downloaded > max_size:
                            raise ValueError("reply_media_too_large")
                        await file.write(chunk)
                if downloaded <= 0:
                    raise RuntimeError("download_empty")
                if expected_size > 0 and downloaded != expected_size:
                    raise RuntimeError("download_incomplete")

    def _allow_direct_reply_media(self, *, event: Any, media: TelegramReplyVideoMedia | OneBotReplyVideoMedia) -> bool:
        """Policy hook for future same-group uploaded-media scanners."""
        _ = (event, media)
        return True

    def _idempotency_key(
        self,
        *,
        event: Any,
        group_id: str,
        command: VideoSliceCommand,
        entry: VideoSliceCacheEntry,
        reply_raw_id: str,
    ) -> str:
        h = hashlib.blake2b(digest_size=16)
        for part in (
            group_id,
            str(event.get_sender_id() or "").strip(),
            command.source,
            reply_raw_id,
            entry.cache_id,
            command.cache_id,
            command.start_sec,
            command.duration_sec,
        ):
            h.update(str(part or "").encode())
            h.update(b"|")
        return h.hexdigest()

    def _consume_rate_limit(self) -> bool:
        now = time.time()
        if now - self._rate_limit_started_at >= self._rate_limit_window_sec:
            self._rate_limit_started_at = now
            self._rate_limit_count = 0
        if self._rate_limit_count >= self._rate_limit_max:
            return False
        self._rate_limit_count += 1
        return True

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
    if start < 0 or duration <= 0:
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
    reply_message = _telegram_reply_message(event)
    value = str(getattr(reply_message, "message_id", "") or "").strip()
    if value:
        return value
    try:
        chain = list(event.get_messages() or [])
    except Exception:
        chain = []
    for item in chain:
        if str(getattr(item, "type", "")).lower().endswith("reply") or item.__class__.__name__ == "Reply":
            value = str(getattr(item, "id", "") or "").strip()
            if value:
                return value
    return ""


def _telegram_reply_message(event: Any) -> Any | None:
    raw_obj = getattr(getattr(event, "message_obj", None), "raw_message", None)
    message = getattr(raw_obj, "message", None)
    return getattr(message, "reply_to_message", None)


def _telegram_reply_video_media(reply_message: Any) -> tuple[TelegramReplyVideoMedia | None, str]:
    video = getattr(reply_message, "video", None)
    if video is not None:
        return _telegram_media_from_obj(video, reply_message=reply_message, kind="video"), ""
    document = getattr(reply_message, "document", None)
    if document is not None:
        mime_type = str(getattr(document, "mime_type", "") or "")
        if mime_type.startswith("video/"):
            return _telegram_media_from_obj(document, reply_message=reply_message, kind="document"), ""
        return None, "reply_media_unsupported"
    for attr in ("photo", "sticker", "animation", "video_note", "voice", "audio"):
        if getattr(reply_message, attr, None) is not None:
            return None, "reply_media_unsupported"
    return None, "reply_media_not_found"


def _telegram_media_from_obj(media_obj: Any, *, reply_message: Any, kind: str) -> TelegramReplyVideoMedia:
    return TelegramReplyVideoMedia(
        file_id=str(getattr(media_obj, "file_id", "") or "").strip(),
        file_unique_id=str(getattr(media_obj, "file_unique_id", "") or "").strip(),
        file_size=_optional_positive_int(getattr(media_obj, "file_size", None)),
        duration=max(0.0, _float_or_zero(getattr(media_obj, "duration", 0.0))),
        mime_type=str(getattr(media_obj, "mime_type", "") or "").strip(),
        file_name=str(getattr(media_obj, "file_name", "") or "").strip(),
        message_id=str(getattr(reply_message, "message_id", "") or "").strip(),
        kind=kind,
    )


async def _onebot_reply_message(event: Any, reply_raw_id: str) -> tuple[dict[str, Any] | None, str]:
    bot = getattr(event, "bot", None)
    if bot is None:
        return None, "reply_media_not_found"
    message_id = _parse_int(reply_raw_id)
    if message_id is None:
        return None, "reply_media_not_found"
    try:
        call_action = getattr(bot, "call_action", None)
        if callable(call_action):
            payload = await call_action(action="get_msg", message_id=message_id)
        else:
            get_msg = getattr(bot, "get_msg", None)
            if not callable(get_msg):
                return None, "reply_media_not_found"
            payload = await get_msg(message_id=message_id)
    except Exception:
        return None, "reply_media_fetch_failed"
    if not isinstance(payload, dict):
        return None, "reply_media_not_found"
    return payload, ""


async def _onebot_reply_video_media(
    event: Any,
    reply_message: dict[str, Any],
) -> tuple[OneBotReplyVideoMedia | None, str]:
    message_id = str(reply_message.get("message_id") or "").strip()
    for segment in _onebot_message_segments(reply_message):
        seg_type = str(segment.get("type") or "").strip().lower()
        data = dict(segment.get("data") or {})
        if seg_type == "video":
            media = _onebot_video_segment_media(data, message_id=message_id, kind="video")
            if media is not None:
                return media, ""
            return None, "reply_media_size_unknown" if _onebot_url_from_data(data) else "reply_media_no_url"
        if seg_type == "file":
            if not _onebot_file_segment_is_video(data):
                return None, "reply_media_unsupported"
            url = _onebot_url_from_data(data)
            if not url:
                url = await _onebot_group_file_url(event, reply_message, data)
            media = _onebot_file_segment_media(data, url=url, message_id=message_id, kind="file")
            if media is not None:
                return media, ""
            if not url:
                return None, "reply_media_no_url"
            return None, "reply_media_size_unknown"
        if seg_type in {"image", "sticker", "record", "audio", "voice", "face"}:
            return None, "reply_media_unsupported"
    return None, "reply_media_not_found"


def _onebot_message_segments(reply_message: dict[str, Any]) -> list[dict[str, Any]]:
    message = reply_message.get("message")
    if isinstance(message, list):
        return [item for item in message if isinstance(item, dict)]
    return []


def _onebot_video_segment_media(
    data: dict[str, Any],
    *,
    message_id: str,
    kind: str,
) -> OneBotReplyVideoMedia | None:
    url = _onebot_url_from_data(data)
    if not url:
        return None
    file_size = _onebot_file_size(data)
    if file_size is None:
        return None
    file_name = _onebot_file_name(data)
    file_key = _onebot_file_key(data, url=url)
    return OneBotReplyVideoMedia(
        url=url,
        file_key=file_key,
        file_size=file_size,
        duration=_onebot_duration(data),
        file_name=file_name,
        message_id=message_id,
        kind=kind,
    )


def _onebot_file_segment_media(
    data: dict[str, Any],
    *,
    url: str,
    message_id: str,
    kind: str,
) -> OneBotReplyVideoMedia | None:
    if not url:
        return None
    file_size = _onebot_file_size(data)
    if file_size is None:
        return None
    return OneBotReplyVideoMedia(
        url=url,
        file_key=_onebot_file_key(data, url=url),
        file_size=file_size,
        duration=_onebot_duration(data),
        file_name=_onebot_file_name(data),
        message_id=message_id,
        kind=kind,
    )


async def _onebot_group_file_url(
    event: Any,
    reply_message: dict[str, Any],
    data: dict[str, Any],
) -> str:
    file_id = str(data.get("file_id") or data.get("file") or data.get("id") or "").strip()
    group_id = _parse_int(reply_message.get("group_id"))
    if not file_id or group_id is None:
        return ""
    bot = getattr(event, "bot", None)
    call_action = getattr(bot, "call_action", None)
    if not callable(call_action):
        return ""
    try:
        ret = await call_action(action="get_group_file_url", file_id=file_id, group_id=group_id)
    except Exception:
        return ""
    if not isinstance(ret, dict):
        return ""
    return _safe_http_url(ret.get("url") or ret.get("file_url"))


def _onebot_file_segment_is_video(data: dict[str, Any]) -> bool:
    mime = str(data.get("mime") or data.get("mime_type") or "").strip().lower()
    if mime.startswith("video/"):
        return True
    suffix = Path(_onebot_file_name(data)).suffix.lower()
    return suffix in {".mp4", ".mov", ".m4v", ".webm", ".mkv"}


def _onebot_url_from_data(data: dict[str, Any]) -> str:
    for key in ("url", "file_url", "download_url"):
        url = _safe_http_url(data.get(key))
        if url:
            return url
    file_value = str(data.get("file") or "").strip()
    return _safe_http_url(file_value)


def _safe_http_url(value: Any) -> str:
    url = str(value or "").strip()
    parsed = urlparse(url)
    if parsed.scheme in {"http", "https"} and parsed.netloc:
        return url
    return ""


def _onebot_file_size(data: dict[str, Any]) -> int | None:
    for key in ("file_size", "size", "filesize"):
        parsed = _optional_positive_int(data.get(key))
        if parsed is not None:
            return parsed
    return None


def _onebot_duration(data: dict[str, Any]) -> float:
    for key in ("duration", "seconds", "time"):
        value = _float_or_zero(data.get(key))
        if value > 0:
            return value
    return 0.0


def _onebot_file_name(data: dict[str, Any]) -> str:
    return str(data.get("file_name") or data.get("name") or data.get("file") or "upload.mp4").strip()


def _onebot_file_key(data: dict[str, Any], *, url: str) -> str:
    for key in ("file_unique_id", "file_id", "file", "id", "md5"):
        value = str(data.get(key) or "").strip()
        if value and not value.startswith(("file://", "/")):
            return value
    return hashlib.blake2b(url.encode(), digest_size=16).hexdigest()


def _safe_media_suffix(file_name: str, mime_type: str) -> str:
    suffix = Path(str(file_name or "")).suffix.lower()
    if suffix and re.fullmatch(r"\.[a-z0-9]{1,8}", suffix):
        return suffix
    guessed = mimetypes.guess_extension(str(mime_type or "").split(";", 1)[0].strip())
    if guessed and re.fullmatch(r"\.[a-z0-9]{1,8}", guessed.lower()):
        return guessed.lower()
    return ".mp4"


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


def _optional_positive_int(value: Any) -> int | None:
    parsed = _parse_int(value)
    if parsed is None or parsed <= 0:
        return None
    return parsed


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
            parser_sent_output=path.name.startswith("parserclip_"),
        )
