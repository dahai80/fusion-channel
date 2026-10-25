from __future__ import annotations

import asyncio
import datetime as dt
import logging
import time
from collections.abc import Awaitable, Callable

from fusion_channel.models import OutboundMessage, SendResult, make_outbound_dedup_key
from fusion_channel.storage import ChannelStore

logger = logging.getLogger(__name__)

# Ported from ai-family sms/sender.py (M1 behavior-equivalent semantics).
RETRY_BACKOFF = [30, 120, 600]
MAX_ATTEMPTS = 3
DEFAULT_LIMIT_PER_MINUTE = 60

# Per-adapter default quotas (PRD §7.2); config can override.
DEFAULT_RATE_LIMITS_PER_MINUTE: dict[str, int] = {
    "dingtalk_robot": 20,
    "feishu_app": 50,
    "twilio_sms": 60,
    "wecom_app": 200,
    "slack_app": 60,
    "telegram_bot": 30,
    "whatsapp_cloud": 60,
    "aliyun_sms": 60,
    "tencent_sms": 60,
}


class TokenBucket:
    def __init__(self, per_minute: int):
        self.capacity = max(1, per_minute)
        self.tokens = float(self.capacity)
        self.refill_rate = self.capacity / 60.0
        self.last_refill = time.monotonic()

    def take(self) -> bool:
        now = time.monotonic()
        self.tokens = min(self.capacity, self.tokens + (now - self.last_refill) * self.refill_rate)
        self.last_refill = now
        if self.tokens >= 1.0:
            self.tokens -= 1.0
            return True
        logger.warning("rate limit hit capacity=%d/min", self.capacity)
        return False


class OutboxSender:
    # Outbound intent lands in the outbox first; the flush loop retries
    # with exponential backoff and reports permanent failures upward
    # (ai-family system.alert pattern).

    def __init__(self, store: ChannelStore, adapters: dict, rate_limits: dict[str, int] | None = None):
        self.store = store
        self.adapters = adapters
        self.rate_limits = rate_limits or {}
        self._buckets: dict[str, TokenBucket] = {}
        self.on_permanent_failure: Callable[[OutboundMessage, str], Awaitable[None]] | None = None

    def bucket_for(self, channel_id: str) -> TokenBucket:
        if channel_id not in self._buckets:
            adapter = self.adapters.get(channel_id)
            adapter_id = adapter.adapter_id() if adapter else ""
            per_minute = self.rate_limits.get(channel_id) or DEFAULT_RATE_LIMITS_PER_MINUTE.get(
                adapter_id, DEFAULT_LIMIT_PER_MINUTE
            )
            self._buckets[channel_id] = TokenBucket(per_minute)
        return self._buckets[channel_id]

    async def enqueue(self, message: OutboundMessage) -> int | None:
        dedup_key = message.dedup_key or make_outbound_dedup_key(message)
        next_retry_at = dt.datetime.now(dt.UTC).isoformat()
        record_id = await self.store.save_outbound(
            message.channel_id,
            dedup_key,
            {
                "message": message.model_dump(mode="json"),
            },
            next_retry_at,
        )
        if record_id is None:
            logger.info("outbox dedup hit channel=%s key=%s", message.channel_id, dedup_key[:32])
            return None
        logger.info("outbox enqueued id=%s channel=%s", record_id, message.channel_id)
        return record_id

    async def flush_once(self, send_fn: Callable[[OutboundMessage], Awaitable[SendResult]] | None = None) -> int:
        sent = 0
        now = dt.datetime.now(dt.UTC).isoformat()
        due = await self.store.due_outbound(now, limit=20)
        for record in due:
            message = OutboundMessage.model_validate(record.payload["message"])
            adapter = self.adapters.get(message.channel_id)
            if adapter is None and send_fn is None:
                record.status = "failed"
                record.last_error = f"unknown channel {message.channel_id}"
                await self.store.update_outbound(record)
                continue
            bucket = self.bucket_for(message.channel_id)
            if not bucket.take():
                logger.info("outbox deferred by rate limit id=%s channel=%s", record.id, message.channel_id)
                continue
            try:
                send = send_fn or adapter.send
                result = await send(message)
                if result.ok:
                    record.status = "sent"
                    record.sent_at = dt.datetime.now(dt.UTC).isoformat()
                    record.provider_message_id = result.provider_message_id
                    sent += 1
                    logger.info("outbox sent id=%s provider_id=%s", record.id, result.provider_message_id[:16])
                else:
                    raise RuntimeError(result.error or "send failed")
            except Exception as exc:
                record.attempts += 1
                record.last_error = str(exc)[:500]
                if record.attempts >= MAX_ATTEMPTS:
                    record.status = "failed"
                    logger.error("outbox failed permanently id=%s err=%s", record.id, exc)
                    if self.on_permanent_failure is not None:
                        try:
                            await self.on_permanent_failure(message, record.last_error)
                        except Exception as hook_exc:
                            logger.error("permanent failure hook error: %s", hook_exc)
                else:
                    delay = RETRY_BACKOFF[min(record.attempts - 1, len(RETRY_BACKOFF) - 1)]
                    record.next_retry_at = (dt.datetime.now(dt.UTC) + dt.timedelta(seconds=delay)).isoformat()
                    logger.warning("outbox retry id=%s attempt=%s err=%s", record.id, record.attempts, exc)
            await self.store.update_outbound(record)
        return sent

    async def run_loop(self, interval: float = 5.0) -> None:
        logger.info("outbox worker started interval=%s", interval)
        while True:
            try:
                sent = await self.flush_once()
                if sent:
                    logger.info("outbox cycle sent=%d", sent)
            except asyncio.CancelledError:
                logger.info("outbox worker cancelled")
                raise
            except Exception as exc:
                logger.error("outbox worker cycle error: %s", exc)
            await asyncio.sleep(interval)
