"""Durable metadata-only receipts for a remote Archive listener."""

import asyncio
import json
import os
from contextlib import contextmanager
from contextvars import ContextVar
from time import time
from types import SimpleNamespace
from urllib.parse import urlsplit
from uuid import uuid4

from aiohttp import ClientSession, ClientTimeout

from astrbot.api import logger

from .video_slice import _onebot_reply_message

_SEND_CONTEXT = ContextVar("parser_receipt_send_context", default=None)


def is_onebot_send_ack_timeout(exc):
    text = str(exc)
    return (
        (
            getattr(exc, "retcode", None) == 1200
            or "retcode=1200" in text
            or "'retcode': 1200" in text
        )
        and "Timeout" in text
        and "NTEvent" in text
        and "sendMsg" in text
        and "onMsgInfoListUpdate" in text
    )


@contextmanager
def capture_sends(outbox, event, result, source):
    """Bind only this ordinary QQ parse invocation, not unrelated event sends."""
    token = _SEND_CONTEXT.set((outbox, event, result, source) if source else None)
    try:
        yield
    finally:
        _SEND_CONTEXT.reset(token)


def track_send_attempt(event, message):
    context = _SEND_CONTEXT.get()
    if context is None or context[1] is not event:
        return None
    return _SendAttempt(context, message)


class _SendAttempt:
    def __init__(self, context, message):
        self.outbox, self.event, self.result, self.source = context
        self.id = str(uuid4())
        self.started_at = int(time())
        self.message = message
        self.pending = False
        self.known_ids = set()
        original = getattr(message, "record_sent", None)
        self.has_confirmation_hook = callable(original)
        if self.has_confirmation_hook:

            def record_sent(raw_ids, components):
                original(raw_ids, components)
                receipt = message.sent_receipts[-1]
                ids = tuple(str(value) for value in receipt.raw_ids if value)
                if self.pending:
                    self.outbox._schedule_confirmation(self, ids)
                else:
                    self.known_ids.update(ids)

            message.record_sent = record_sent

    async def mark_pending(self, reason):
        self.pending = True
        try:
            payload = {
                "receipt_id": self.id,
                "source": self.source,
                "content": content_metadata(self.result),
                "state": "awaiting_confirmation",
                "send_attempt": {
                    "id": self.id,
                    "started_at": self.started_at,
                    "timed_out_at": int(time()),
                    "reason": reason,
                    "component_types": [
                        type(part).__name__ for part in self.message.chain
                    ],
                    "known_raw_ids": sorted(self.known_ids),
                    "confirmation_hook": "same_chain_record_sent"
                    if self.has_confirmation_hook
                    else "unavailable",
                },
            }
            async with self.outbox._mutation_lock:
                await asyncio.to_thread(
                    atomic_json, self.outbox.root / f"{self.id}.json", payload
                )
            self.outbox.events[self.outbox._event_key(self.source)] = self.event
            self.outbox._log_awaiting(payload)
            self.outbox.start()
            self.outbox.wake.set()
        except Exception as exc:
            # Persistence failure must never turn an uncertain send into a retry.
            logger.error(
                "Parser receipt persistence_failed: %s (%s)",
                self.id,
                type(exc).__name__,
            )


class ReceiptMetadataError(ValueError):
    pass


class ReceiptDeliveryError(ValueError):
    def __init__(self, status):
        self.status = status
        super().__init__(f"parser_receipt_rejected:{status}")


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
        self._mutation_lock = asyncio.Lock()
        self._confirmation_tasks = set()
        self._awaiting_logged = set()

    @property
    def enabled(self):
        return bool(self.config.receipt_url and self.config.receipt_token)

    def start(self):
        if self.enabled and self.task is None:
            self.task = asyncio.create_task(self.run())

    async def drain_confirmations(self):
        while self._confirmation_tasks:
            await asyncio.gather(
                *tuple(self._confirmation_tasks), return_exceptions=True
            )

    async def close(self):
        await self.drain_confirmations()
        if self.task:
            self.task.cancel()
            await asyncio.gather(self.task, return_exceptions=True)
            self.task = None

    @staticmethod
    def _event_key(source):
        return (
            str(source["platform_id"]),
            str(source["bot_id"]),
            str(source["group_id"]),
        )

    def _log_awaiting(self, payload):
        receipt_id = payload["receipt_id"]
        if receipt_id not in self._awaiting_logged:
            self._awaiting_logged.add(receipt_id)
            logger.info("Parser receipt awaiting_confirmation: %s", receipt_id)

    def _schedule_confirmation(self, attempt, raw_ids):
        task = asyncio.create_task(self._confirm_bound_attempt(attempt, raw_ids))
        self._confirmation_tasks.add(task)
        task.add_done_callback(self._confirmation_tasks.discard)

    async def _confirm_bound_attempt(self, attempt, raw_ids):
        """Called only by the exact chain's platform ACK hook; never by history."""
        ids = tuple(
            dict.fromkeys(value for value in raw_ids if value not in attempt.known_ids)
        )
        if not ids:
            return
        if len(ids) != 1:
            logger.warning(
                "Parser receipt confirmation_failed: %s (ambiguous_ack)", attempt.id
            )
            return
        try:
            async with self._mutation_lock:
                path = self.root / f"{attempt.id}.json"
                if not path.exists():
                    return  # Already accepted, or persistence failed; never recreate it.
                payload = json.loads(await asyncio.to_thread(path.read_text))
                if payload.get("send_attempt", {}).get("id") != attempt.id:
                    return
                if payload.get("state") != "awaiting_confirmation":
                    return  # Duplicate ACK, including after enrichment.
                payload["raw_ids"] = list(ids)
                payload["state"] = "acknowledged"
                await asyncio.to_thread(atomic_json, path, payload)
            logger.info("Parser receipt confirmation_bound: %s", attempt.id)
            self.wake.set()
        except Exception as exc:
            logger.warning(
                "Parser receipt confirmation_failed: %s (%s)",
                attempt.id,
                type(exc).__name__,
            )

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
        self.events[self._event_key(source)] = event
        self.start()
        self.wake.set()

    async def deliver(self, path, session, event):
        payload = json.loads(await asyncio.to_thread(path.read_text))
        if payload.get("state") == "awaiting_confirmation":
            self._log_awaiting(payload)
            return
        if "deliveries" not in payload:
            deliveries = []
            try:
                for raw_id in payload["raw_ids"]:
                    response, error = await asyncio.wait_for(
                        _onebot_reply_message(event, raw_id), timeout=15
                    )
                    if error:
                        raise ReceiptMetadataError(
                            "parser_receipt_delivery_identity_unavailable"
                        )
                    raw = response.get("data", response) if response else {}
                    group_id = str(raw.get("group_id") or "")
                    user_id = str(raw.get("user_id") or "")
                    order = str(raw.get("real_seq") or "")
                    timestamp = int(raw.get("time") or 0)
                    if (
                        group_id != payload["source"]["group_id"]
                        or user_id != str(payload["source"]["bot_id"])
                        or user_id != str(event.get_self_id())
                        or not order.isdigit()
                        or int(order) <= 0
                        or timestamp <= 0
                    ):
                        raise ReceiptMetadataError(
                            "parser_receipt_delivery_identity_unavailable"
                        )
                    delivery = {
                        "raw_id": raw_id,
                        "group_id": group_id,
                        "user_id": user_id,
                        "ts": timestamp,
                        "fingerprint": f"{group_id}:{user_id}:{order}",
                    }
                    if not any(
                        d["fingerprint"] == delivery["fingerprint"] for d in deliveries
                    ):
                        deliveries.append(delivery)
            except ReceiptMetadataError:
                raise
            except Exception as exc:
                raise ReceiptMetadataError(
                    "parser_receipt_metadata_query_failed"
                ) from exc
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
            json={
                key: payload[key]
                for key in ("receipt_id", "source", "content", "deliveries")
            },
            headers={"Authorization": f"Bearer {self.config.receipt_token}"},
            allow_redirects=False,
        ) as response:
            if (
                response.status != 200
                or (await response.json()).get("status") != "accepted"
            ):
                raise ReceiptDeliveryError(response.status)
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
                        if payload.get("state") == "awaiting_confirmation":
                            self._log_awaiting(payload)
                            continue
                        event = self.events.get(self._event_key(payload["source"]))
                        if "deliveries" not in payload and event is None:
                            adapter = self.context.get_platform_inst(
                                payload["source"]["platform_id"]
                            )
                            if adapter is None:
                                raise ReceiptMetadataError(
                                    "parser_receipt_adapter_unavailable"
                                )
                            bot_id = payload["source"]["bot_id"]
                            event = SimpleNamespace(
                                bot=adapter.get_client(), get_self_id=lambda: bot_id
                            )
                        await self.deliver(path, session, event)
                    except Exception as exc:
                        failed = True
                        if isinstance(exc, ReceiptMetadataError):
                            logger.warning(
                                "Parser receipt enrichment_failed: %s (%s)",
                                path.name,
                                type(exc).__name__,
                            )
                        else:
                            logger.warning(
                                "Parser receipt delivery_failed: %s (%s, http_status=%s)",
                                path.name,
                                type(exc).__name__,
                                exc.status
                                if isinstance(exc, ReceiptDeliveryError)
                                else "unavailable",
                            )
                delay = min(delay * 2, 60) if failed else 1
                try:
                    await asyncio.wait_for(
                        self.wake.wait(), timeout=delay if failed else 3600
                    )
                except asyncio.TimeoutError:
                    pass
