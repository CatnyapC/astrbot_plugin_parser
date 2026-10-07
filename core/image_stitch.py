from __future__ import annotations

import asyncio
import shlex
import time
from collections import deque
from dataclasses import dataclass
from pathlib import Path
from typing import Any
from uuid import uuid4

from PIL import Image as PILImage
from PIL import ImageOps

from .data import ImageContent, ParseResult, Platform

PARSER_IMAGE_PLATFORM = Platform(name="parserimg", display_name="Parser Image")
IMAGE_STITCH_MAX_OUTPUT_PIXELS = 40_000_000


@dataclass(slots=True)
class ImageStitchCacheEntry:
    group_id: str
    paths: tuple[Path, ...]
    created_at: float = 0.0


@dataclass(slots=True)
class ImageStitchCommand:
    source: str = "latest"
    direction: str = "horizontal"


@dataclass(slots=True)
class ImageStitchResult:
    status: str
    message: str = ""
    sent: bool = False
    output_name: str = ""


class ImageStitchCacheIndex:
    def __init__(self, *, max_entries_per_group: int = 10) -> None:
        self.max_entries_per_group = max(1, int(max_entries_per_group or 10))
        self._by_group: dict[str, deque[ImageStitchCacheEntry]] = {}

    def record(
        self,
        *,
        group_id: str,
        paths: list[Path] | tuple[Path, ...],
        created_at: float | None = None,
    ) -> ImageStitchCacheEntry | None:
        group = str(group_id or "").strip()
        safe_paths = tuple(Path(path) for path in paths if Path(path).is_file())
        if not group or len(safe_paths) < 2:
            return None
        entry = ImageStitchCacheEntry(
            group_id=group,
            paths=safe_paths,
            created_at=time.time() if created_at is None else float(created_at),
        )
        bucket = self._by_group.setdefault(group, deque())
        bucket.appendleft(entry)
        while len(bucket) > self.max_entries_per_group:
            bucket.pop()
        return entry

    def latest(self, group_id: str) -> ImageStitchCacheEntry | None:
        group = str(group_id or "").strip()
        if not group:
            return None
        bucket = self._by_group.get(group)
        if not bucket:
            return None
        for entry in bucket:
            if len(entry.paths) >= 2 and all(path.is_file() for path in entry.paths):
                return entry
        return None


class ImageStitchCommandService:
    def __init__(
        self,
        *,
        cfg: Any,
        sender: Any,
        cache_index: ImageStitchCacheIndex,
    ) -> None:
        self.cfg = cfg
        self.sender = sender
        self.cache_index = cache_index

    async def handle(self, event: Any) -> ImageStitchResult:
        cmd = parse_parserimg_stitch_command(getattr(event, "message_str", ""))
        if cmd is None:
            return ImageStitchResult(status="ignored")
        group_id = event.get_group_id()
        if not group_id:
            return ImageStitchResult(status="error", message="parserimg stitch 失败: group_required")
        if cmd.source != "latest":
            return ImageStitchResult(
                status="error",
                message="parserimg stitch 失败: source_unsupported",
            )
        entry = self.cache_index.latest(str(group_id))
        if entry is None:
            return ImageStitchResult(status="error", message="parserimg stitch 失败: image_cache_empty")
        try:
            output_path = await asyncio.to_thread(
                stitch_images,
                entry.paths,
                self._output_path(),
                cmd.direction,
            )
        except ValueError as exc:
            return ImageStitchResult(status="error", message=f"parserimg stitch 失败: {exc}")
        result = ParseResult(
            platform=PARSER_IMAGE_PLATFORM,
            contents=[ImageContent(output_path, source_key=f"parserimg:{output_path.name}")],
        )
        sent = await self.sender.send_parse_result(event, result)
        if not sent:
            return ImageStitchResult(status="error", message="parserimg stitch 失败: send_failed")
        return ImageStitchResult(status="ok", sent=True, output_name=output_path.name)

    def _output_path(self) -> Path:
        cache_dir = Path(getattr(self.cfg, "cache_dir"))
        cache_dir.mkdir(parents=True, exist_ok=True)
        return cache_dir / f"parserimg_stitch_{uuid4().hex[:12]}.jpg"


def parse_parserimg_stitch_command(text: str) -> ImageStitchCommand | None:
    raw = str(text or "").strip()
    if not raw:
        return None
    try:
        parts = shlex.split(raw)
    except ValueError:
        return None
    if len(parts) < 2:
        return None
    if parts[0].lstrip("/") != "parserimg" or parts[1] != "stitch":
        return None
    opts = _parse_options(parts[2:])
    source = str(opts.get("source") or "latest").strip().lower()
    direction = str(
        opts.get("direction") or opts.get("layout") or "horizontal"
    ).strip().lower()
    if source not in {"latest", "reply"}:
        return None
    if direction not in {"horizontal", "vertical"}:
        return None
    return ImageStitchCommand(source=source, direction=direction)


def stitch_images(paths: tuple[Path, ...], output_path: Path, direction: str) -> Path:
    if len(paths) < 2:
        raise ValueError("need_at_least_two_images")

    opened: list[PILImage.Image] = []
    try:
        for path in paths:
            img = ImageOps.exif_transpose(PILImage.open(path)).convert("RGB")
            opened.append(img)
        widths = [img.width for img in opened]
        heights = [img.height for img in opened]
        if direction == "horizontal":
            size = (sum(widths), max(heights))
        elif direction == "vertical":
            size = (max(widths), sum(heights))
        else:
            raise ValueError("direction_invalid")
        if size[0] * size[1] > IMAGE_STITCH_MAX_OUTPUT_PIXELS:
            raise ValueError("image_too_large")
        canvas = PILImage.new("RGB", size, (255, 255, 255))
        offset = 0
        for img in opened:
            if direction == "horizontal":
                canvas.paste(img, (offset, 0))
                offset += img.width
            else:
                canvas.paste(img, (0, offset))
                offset += img.height
        output_path.parent.mkdir(parents=True, exist_ok=True)
        canvas.save(output_path, format="JPEG", quality=95, progressive=False)
        return output_path
    finally:
        for img in opened:
            img.close()


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
