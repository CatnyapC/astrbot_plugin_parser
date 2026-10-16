"""Durable metadata-only receipts for a remote Archive listener."""

import asyncio
import hashlib
import json
import os
import time
from contextvars import ContextVar
from pathlib import Path
from urllib.parse import urlsplit
from uuid import uuid4

from aiohttp import ClientSession, ClientTimeout

from astrbot.api import logger
from astrbot.core.message.components import File, Image, Node, Nodes, Record, Video

active_receipt = ContextVar("parser_active_receipt", default=None)


def media_fingerprints(chain):
    fingerprints = set()
    for segment in chain:
        if isinstance(segment, Nodes):
            fingerprints.update(media_fingerprints(segment.nodes))
        elif isinstance(segment, Node):
            fingerprints.update(media_fingerprints(segment.content))
        elif isinstance(segment, (Image, Video, Record, File)):
            value = segment.file_ if isinstance(segment, File) else segment.file
            if not value:
                continue
            if value.startswith("file:///"):
                value = "/" + value.removeprefix("file:///").lstrip("/")
            with Path(value).open("rb") as stream:
                fingerprints.add(hashlib.file_digest(stream, "sha256").hexdigest())
    return sorted(fingerprints)


async def push_chain_receipt(chain):
    if not (context := active_receipt.get()):
        return
    outbox, result, source = context
    try:
        fingerprints = await asyncio.to_thread(media_fingerprints, chain)
        await outbox.enqueue(result, source, fingerprints)
    except Exception:
        logger.exception("Parser fingerprint receipt persistence failed")


def content_metadata(result):
    data = {"platform": result.platform.name}
    for key in ("title", "text", "url", "timestamp"):
        value = getattr(result, key, None)
        if value:
            data[key] = value[:16000] if isinstance(value, str) else value
    if result.author and result.author.name:
        data["author"] = result.author.name[:16000]
    summary = str(result.extra.get("info") or "").strip()
    # Only this parser field currently has a verified platform AI-summary source.
    if result.platform.name == "bilibili" and summary.startswith("AI总结:"):
        if value := summary.removeprefix("AI总结:").strip():
            data["ai_summary"] = value[:16000]
    durations = [c.duration for c in result.contents if getattr(c, "duration", 0)]
    if durations:
        data["durations"] = durations
    return data


def atomic_json(path, payload):
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(".tmp")
    fd = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w") as stream:
        json.dump(payload, stream, ensure_ascii=False)
        stream.flush()
        os.fsync(stream.fileno())
    temporary.replace(path)


class ReceiptOutbox:
    def __init__(self, config):
        self.config = config
        self.root = config.data_dir / "receipts"
        self.wake = asyncio.Event()
        self.task = None

    @property
    def enabled(self):
        return bool(self.config.receipt_url and self.config.receipt_token)

    def start(self):
        if self.enabled and self.task is None:
            self.task = asyncio.create_task(self.run())

    async def close(self):
        if self.task:
            self.task.cancel()
            await asyncio.gather(self.task, return_exceptions=True)
            self.task = None

    async def enqueue(self, result, source, fingerprints):
        if not self.enabled or not fingerprints:
            return
        receipt_id = str(uuid4())
        payload = {
            "receipt_id": receipt_id,
            "created_at": time.time_ns(),
            "source": source,
            "content": content_metadata(result),
            "fingerprints": fingerprints,
        }
        await asyncio.to_thread(atomic_json, self.root / f"{receipt_id}.json", payload)
        self.start()
        self.wake.set()

    async def deliver(self, path, session):
        payload = json.loads(await asyncio.to_thread(path.read_text))
        url = self.config.receipt_url
        parsed = urlsplit(url)
        if parsed.scheme != "https" and not (
            parsed.scheme == "http"
            and parsed.hostname in {"127.0.0.1", "::1", "localhost"}
        ):
            raise ValueError("parser_receipt_requires_https_or_loopback_tunnel")
        async with session.post(
            url,
            json=payload,
            headers={"Authorization": f"Bearer {self.config.receipt_token}"},
            allow_redirects=False,
        ) as response:
            if (
                response.status != 200
                or (await response.json()).get("status") != "accepted"
            ):
                raise ValueError(f"parser_receipt_rejected:{response.status}")
        await asyncio.to_thread(path.unlink)

    async def run(self):
        delay = 1
        async with ClientSession(
            timeout=ClientTimeout(total=15),
            json_serialize=lambda value: json.dumps(value, ensure_ascii=False),
        ) as session:
            while True:
                self.wake.clear()
                failed = False
                for path in sorted(self.root.glob("*.json")):
                    try:
                        await self.deliver(path, session)
                    except Exception as exc:
                        failed = True
                        logger.warning(
                            "Parser receipt push pending: %s (%s)",
                            path.name,
                            type(exc).__name__,
                        )
                delay = min(delay * 2, 60) if failed else 1
                try:
                    await asyncio.wait_for(
                        self.wake.wait(), timeout=delay if failed else 3600
                    )
                except asyncio.TimeoutError:
                    pass
