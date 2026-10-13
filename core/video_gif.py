from __future__ import annotations

import asyncio
import math
import shlex
import uuid
from pathlib import Path
from typing import Any

import numpy as np
from PIL import Image

from .data import ImageContent, ParseResult
from .video_slice import (
    VIDEO_SLICE_PLATFORM,
    VideoSliceCommandService,
    VideoSliceResult,
    _event_raw_id,
    _path_is_under,
    _reply_raw_id,
)

GIF_FPS = 12
GIF_MAX_DURATION = 60
GIF_MAX_INPUT_SIZE = 100 * 1024 * 1024
GIF_MAX_OUTPUT_SIZE = 10 * 1024 * 1024
GIF_MIN_CYCLE_CONTRAST = 4.0


def parse_parserclip_gif_command(text: str) -> dict[str, str] | None:
    try:
        parts = shlex.split(str(text or ""))
    except ValueError:
        if str(text or "").lstrip().startswith(("/parserclip gif", "parserclip gif")):
            raise ValueError("invalid_arguments") from None
        return None
    if len(parts) < 2 or parts[0].lstrip("/") != "parserclip" or parts[1] != "gif":
        return None
    options = {"source": "latest", "cycle": "off"}
    seen = set()
    rest = parts[2:]
    while rest:
        key = rest.pop(0)
        if "=" in key:
            key, value = key.split("=", 1)
        elif rest:
            value = rest.pop(0)
        else:
            raise ValueError("invalid_arguments")
        key = key.removeprefix("--") if key.startswith("--") else ""
        if key not in options or key in seen:
            raise ValueError("invalid_arguments")
        options[key] = value
        seen.add(key)
    if options["source"] not in {"latest", "reply"} or options["cycle"] not in {
        "off",
        "auto",
    }:
        raise ValueError("invalid_arguments")
    return options


def find_cycle_frames(frames: np.ndarray) -> int:
    # shortcut: 12 FPS sampling supports loops >= 0.25s; use native-frame analysis for shorter loops.
    frames = np.asarray(frames, dtype=np.float32).reshape(len(frames), -1)
    for period in range(GIF_FPS // 4, len(frames) // 2 + 1):
        cycle = frames[:period]
        contrast = np.abs(cycle - cycle[0]).mean(axis=1)
        if np.quantile(contrast, 0.8) < GIF_MIN_CYCLE_CONTRAST:
            continue
        errors = np.abs(frames[period:] - frames[:-period]).mean(axis=1)
        tolerance = min(3.0, float(contrast.mean()) * 0.15)
        if np.quantile(errors, 0.95) > tolerance or errors.max() > tolerance * 2:
            continue
        transitions = np.abs(cycle[1:] - cycle[:-1]).mean(axis=1)
        seam = np.abs(cycle[-1] - cycle[0]).mean()
        if seam > transitions.max() + 2:
            continue
        return period
    raise ValueError("cycle_not_found")


class VideoGifCommandService:
    def __init__(self, video_service: VideoSliceCommandService) -> None:
        self.video_service = video_service
        self._lock = asyncio.Lock()
        self._completed_keys: set[tuple[str, str, str]] = set()

    async def handle(self, event: Any) -> VideoSliceResult:
        try:
            command = parse_parserclip_gif_command(getattr(event, "message_str", ""))
            if command is None:
                return VideoSliceResult("ignored", "")
            group_id = str(event.get_group_id() or "")
            if not group_id:
                raise ValueError("group_required")
            service = self.video_service
            raw_id = _event_raw_id(event)
            if not raw_id:
                raise ValueError("command_raw_id_required")
            key = (group_id, str(event.get_sender_id() or ""), raw_id)
            async with self._lock:
                if key in self._completed_keys:
                    return VideoSliceResult(
                        "duplicate", "parserclip gif 已处理，忽略重复请求"
                    )
                if not service._consume_rate_limit():
                    raise ValueError("rate_limited")
                result = await self._convert(event, command, group_id, raw_id)
                if result.sent:
                    self._completed_keys.add(key)
                return result
        except ValueError as exc:
            return VideoSliceResult("failed", f"parserclip gif 失败: {exc}")
        except Exception:
            return VideoSliceResult("failed", "parserclip gif 失败: conversion_failed")

    async def _convert(
        self, event: Any, command: dict[str, str], group_id: str, raw_id: str
    ) -> VideoSliceResult:
        service = self.video_service
        reply_raw_id = _reply_raw_id(event)
        if command["source"] == "reply" and not reply_raw_id:
            raise ValueError("reply_required")
        entry, reason = service.cache_index.resolve(
            group_id=group_id,
            source=command["source"],
            reply_raw_id=reply_raw_id,
            strict_reply=True,
        )
        if entry is None and command["source"] == "reply":
            for loader in (
                service._cache_replied_telegram_video,
                service._cache_replied_onebot_video,
            ):
                entry, reason = await loader(
                    event=event,
                    group_id=group_id,
                    reply_raw_id=reply_raw_id,
                    resolve_reason=reason,
                )
                if entry is not None:
                    break
        if entry is None:
            raise ValueError(reason)
        root = Path(service.cfg.cache_dir).resolve()
        if not _path_is_under(entry.path, root) or not entry.path.is_file():
            raise ValueError("cache_source_invalid")
        if entry.path.stat().st_size > GIF_MAX_INPUT_SIZE:
            raise ValueError("input_too_large")
        probe = await service._probe_video(entry.path)
        if probe.get("status") != "ok":
            raise ValueError(str(probe.get("reason") or "probe_failed"))
        duration = float(probe.get("duration") or 0)
        if not math.isfinite(duration) or duration <= 0:
            raise ValueError("duration_unknown")
        if duration > GIF_MAX_DURATION:
            raise ValueError("duration_too_long")
        output = root / f"parser_gif_{uuid.uuid4().hex}.gif"
        samples = output.with_suffix(".gray")
        sent = False
        try:
            if command["cycle"] == "auto":
                await self._ffmpeg(
                    [
                        "-i",
                        str(entry.path),
                        "-an",
                        "-vf",
                        f"fps={GIF_FPS},scale=32:32",
                        "-frames:v",
                        str(GIF_FPS * GIF_MAX_DURATION),
                        "-pix_fmt",
                        "gray",
                        "-f",
                        "rawvideo",
                        str(samples),
                    ]
                )
                frames = np.frombuffer(samples.read_bytes(), dtype=np.uint8).reshape(
                    -1, 32 * 32
                )
                if len(frames) < GIF_FPS // 2:
                    raise ValueError("cycle_not_found")
                duration = await asyncio.to_thread(find_cycle_frames, frames)
                duration /= GIF_FPS
            args = ["-i", str(entry.path)]
            if command["cycle"] == "auto":
                args += ["-t", str(duration)]
            args += [
                "-an",
                "-filter_complex",
                f"fps={GIF_FPS},scale='min(480,iw)':'min(480,ih)':force_original_aspect_ratio=decrease,split[a][b];[a]palettegen[p];[b][p]paletteuse",
                "-loop",
                "0",
                str(output),
            ]
            await self._ffmpeg(args)
            if output.stat().st_size > min(
                GIF_MAX_OUTPUT_SIZE,
                int(getattr(service.cfg, "max_size", 0) or GIF_MAX_OUTPUT_SIZE),
            ):
                raise ValueError("output_too_large")
            with Image.open(output) as image:
                if image.format != "GIF" or image.info.get("loop") != 0:
                    raise ValueError("invalid_gif")
            sent = await service.sender.send_parse_result(
                event,
                ParseResult(
                    platform=VIDEO_SLICE_PLATFORM,
                    contents=[
                        ImageContent(output, source_key=f"parsergif:{output.name}")
                    ],
                    extra={"reply_raw_id": raw_id},
                ),
            )
            if not sent:
                raise ValueError("send_failed")
            return VideoSliceResult(
                "ok", "", sent=True, cache_id=entry.cache_id, output_name=output.name
            )
        finally:
            samples.unlink(missing_ok=True)
            if not sent:
                output.unlink(missing_ok=True)

    async def _ffmpeg(self, args: list[str]) -> None:
        code, _stdout, _stderr = await self.video_service.run_process(
            ["ffmpeg", "-y", "-v", "error", *args], 120.0
        )
        if code != 0:
            raise ValueError("ffmpeg_timeout" if code == 124 else "ffmpeg_failed")
