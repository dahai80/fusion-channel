from __future__ import annotations

import logging
from collections.abc import Awaitable, Callable
from typing import Any

from fusion_channel.models import InboundEnvelope, OutboundMessage, Receipt, RejectedEnvelope

logger = logging.getLogger(__name__)

ArchiveFn = Callable[[str, dict[str, Any]], Awaitable[None]]


class ArchiveHook:
    # Full-fidelity archive: inbound raw, outbound intent, receipts.
    # Default implementation persists through the ChannelStore; consumers
    # override to route into their own audit tables (ai-family sms history).

    def __init__(self, store):
        self.store = store

    async def on_inbound(self, envelope: InboundEnvelope) -> bool:
        # Returns False when (channel_id, message_id) already archived — duplicate.
        saved = await self.store.save_inbound(
            envelope.channel_id,
            envelope.message_id,
            {"kind": "inbound", "envelope": envelope.model_dump(mode="json")},
        )
        if saved:
            logger.info("archive inbound channel=%s message_id=%s", envelope.channel_id, envelope.message_id[:24])
        return saved

    async def on_inbound_dedup(self, envelope: InboundEnvelope) -> bool:
        return await self.on_inbound(envelope)

    async def on_rejected(self, rejected: RejectedEnvelope) -> None:
        await self.store.save_rejected(rejected.channel_id, rejected.reason, rejected.raw)
        logger.warning("archive rejected channel=%s reason=%s", rejected.channel_id, rejected.reason)

    async def on_outbound(self, message: OutboundMessage) -> None:
        logger.info(
            "archive outbound intent channel=%s conversation=%s", message.channel_id, message.conversation.id[:24]
        )

    async def on_receipt(self, receipt: Receipt) -> None:
        await self.store.save_receipt(receipt.channel_id, receipt.message_id, receipt.status, receipt.raw)
        logger.info(
            "archive receipt channel=%s message_id=%s status=%s",
            receipt.channel_id,
            receipt.message_id[:24],
            receipt.status,
        )


class AuditHook:
    # Credential/config changes, rejected requests, permanent failures —
    # routed to the consumer's audit chain.

    def __init__(self):
        self._sinks: list[Callable[[dict], Awaitable[None]]] = []

    def add_sink(self, sink: Callable[[dict], Awaitable[None]]) -> None:
        self._sinks.append(sink)

    async def emit(self, actor: str, action: str, target: str, detail: dict | None = None) -> None:
        event = {
            "actor": actor,
            "action": action,
            "target": target,
            "detail": detail or {},
        }
        logger.info("audit action=%s target=%s", action, target)
        for sink in self._sinks:
            try:
                await sink(event)
            except Exception as exc:
                logger.error("audit sink failed action=%s err=%s", action, exc)
