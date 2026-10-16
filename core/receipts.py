"""Durable metadata-only receipts for a remote Archive listener."""

import asyncio
import json
import os
from types import SimpleNamespace
from urllib.parse import urlsplit
from uuid import uuid4

from aiohttp import ClientSession, ClientTimeout

from astrbot.api import logger

from .video_slice import _onebot_reply_message


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
    def __init__(self, config, context):
        self.config = config
        self.context = context
        self.root = config.data_dir / "receipts"
        self.wake = asyncio.Event()
        self.task = None
        self.events = {}

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

    async def enqueue(self, event, result, source, receipts):
        if not self.enabled or not receipts:
            return
        raw_ids = list(
            dict.fromkeys(rid for receipt in receipts for rid in receipt.raw_ids)
        )
        if not raw_ids:
            return
        receipt_id = str(uuid4())
        payload = {
            "receipt_id": receipt_id,
            "source": source,
            "content": content_metadata(result),
            "raw_ids": raw_ids,
        }
        await asyncio.to_thread(atomic_json, self.root / f"{receipt_id}.json", payload)
        self.events[source["group_id"]] = event
        self.start()
        self.wake.set()

    async def deliver(self, path, session, event):
        payload = json.loads(await asyncio.to_thread(path.read_text))
        if "deliveries" not in payload:
            deliveries = []
            for raw_id in payload["raw_ids"]:
                response, error = await asyncio.wait_for(
                    _onebot_reply_message(event, raw_id), timeout=15
                )
                if error:
                    raise ValueError(error)
                raw = response.get("data", response) if response else {}
                group_id = str(raw.get("group_id") or "")
                user_id = str(raw.get("user_id") or "")
                order = str(raw.get("real_seq") or "")
                if (
                    group_id != payload["source"]["group_id"]
                    or user_id != str(event.get_self_id())
                    or not order.isdigit()
                    or int(order) <= 0
                ):
                    raise ValueError("parser_receipt_delivery_identity_unavailable")
                delivery = {
                    "raw_id": raw_id,
                    "group_id": group_id,
                    "user_id": user_id,
                    "ts": int(raw["time"]),
                    "fingerprint": f"{group_id}:{user_id}:{order}",
                }
                if not any(
                    d["fingerprint"] == delivery["fingerprint"] for d in deliveries
                ):
                    deliveries.append(delivery)
            payload.pop("raw_ids")
            payload["deliveries"] = deliveries
            await asyncio.to_thread(atomic_json, path, payload)
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
                        payload = json.loads(await asyncio.to_thread(path.read_text))
                        event = self.events.get(payload["source"]["group_id"])
                        if "deliveries" not in payload and event is None:
                            adapter = self.context.get_platform_inst(
                                payload["source"]["platform_id"]
                            )
                            if adapter is None:
                                raise ValueError("parser_receipt_adapter_unavailable")
                            bot_id = payload["source"]["bot_id"]
                            event = SimpleNamespace(
                                bot=adapter.get_client(), get_self_id=lambda: bot_id
                            )
                        await self.deliver(path, session, event)
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
