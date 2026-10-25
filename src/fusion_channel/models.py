from __future__ import annotations

import datetime as dt
import hashlib
import json
import logging
import time
from typing import Any

from pydantic import BaseModel, Field

logger = logging.getLogger(__name__)

CONVERSATION_TYPES = ("direct", "group", "broadcast")


class Conversation(BaseModel):
    type: str = "direct"  # direct | group | broadcast
    id: str  # channel-side conversation key (phone pair, chat_id, channel_id...)
    title: str | None = None
    thread_id: str | None = None  # topic / reply thread, CAN_THREADS channels only


class ExternalIdentity(BaseModel):
    channel_id: str = ""
    external_id: str  # E.164 phone / open_id / user_id...
    display_name: str | None = None
    tenant: str | None = None  # reserved for multi-tenant (PRD §12.4)


class MediaRef(BaseModel):
    url: str = ""
    mime_type: str | None = None
    media_id: str | None = None  # provider-side media id (MediaUrl0, file_id...)
    size: int | None = None


class CardAction(BaseModel):
    action: str  # semantic action key, e.g. "approve" / "reject"
    label: str


class Card(BaseModel):
    title: str
    body: str = ""
    actions: list[CardAction] = Field(default_factory=list)
    fallback_text: str = ""  # used when CAN_CARDS == "none"

    def build_fallback_text(self) -> str:
        if self.fallback_text:
            return self.fallback_text
        if not self.actions:
            return f"{self.title}\n{self.body}".strip()
        hints = " / ".join(a.label for a in self.actions)
        return f"{self.title}\n{self.body}\n（{hints}）".strip()


class ApprovalCard(Card):
    code: str = ""  # e.g. "AP-123456", echoed in fallback and command parse

    def build_fallback_text(self) -> str:
        if self.fallback_text:
            return self.fallback_text
        parts = [self.title]
        if self.code:
            parts.append(f"编号：{self.code}")
        if self.body:
            parts.append(self.body)
        if self.actions:
            hints = " 或 ".join(f"{a.label} {self.code}" for a in self.actions)
            parts.append(f"回复：{hints}")
        return "\n".join(parts)


class Interaction(BaseModel):
    kind: str  # "button" | "command"
    action: str  # semantic action, e.g. "approve"
    message_id: str  # anti-replay / idempotency key
    payload: dict[str, Any] = Field(default_factory=dict)
    actor: ExternalIdentity | None = None
    conversation: Conversation | None = None


class InboundEnvelope(BaseModel):
    channel_id: str
    message_id: str  # provider-side unique id, dedup key
    conversation: Conversation
    sender: ExternalIdentity
    text: str = ""
    media: list[MediaRef] = Field(default_factory=list)
    mentions: list[ExternalIdentity] = Field(default_factory=list)
    interaction: Interaction | None = None
    raw: dict[str, Any] = Field(default_factory=dict)  # full original payload, archive-first
    received_at: dt.datetime = Field(default_factory=lambda: dt.datetime.now(dt.UTC))
    tenant: str | None = None


class RejectedEnvelope(BaseModel):
    channel_id: str
    reason: str
    raw: dict[str, Any] = Field(default_factory=dict)
    received_at: dt.datetime = Field(default_factory=lambda: dt.datetime.now(dt.UTC))


class OutboundMessage(BaseModel):
    channel_id: str
    conversation: Conversation
    text: str = ""
    card: Card | None = None
    media: list[MediaRef] = Field(default_factory=list)
    reply_to: str | None = None
    dedup_key: str | None = None
    meta: dict[str, Any] = Field(default_factory=dict)


class SendResult(BaseModel):
    ok: bool
    provider_message_id: str = ""
    raw: dict[str, Any] = Field(default_factory=dict)
    error: str | None = None


class VerifyResult(BaseModel):
    ok: bool
    reason: str = ""


class TestResult(BaseModel):
    ok: bool
    detail: str = ""
    latency_ms: float | None = None


class ChannelRequest(BaseModel):
    method: str = "POST"
    url: str
    headers: dict[str, str] = Field(default_factory=dict)
    body: bytes = b""
    form: dict[str, str] = Field(default_factory=dict)  # urlencoded form, Twilio style
    json_body: Any = None  # parsed JSON body

    def header(self, name: str) -> str:
        return self.headers.get(name.lower(), self.headers.get(name, ""))


class WebhookResponse(BaseModel):
    status_code: int = 200
    body: str = "ok"
    media_type: str = "text/plain"


class GroupInfo(BaseModel):
    group_id: str
    name: str = ""
    members: list[ExternalIdentity] = Field(default_factory=list)


class Receipt(BaseModel):
    channel_id: str
    message_id: str
    status: str  # delivered | read | failed
    raw: dict[str, Any] = Field(default_factory=dict)
    received_at: dt.datetime = Field(default_factory=lambda: dt.datetime.now(dt.UTC))


def make_outbound_dedup_key(message: OutboundMessage, window_seconds: int = 3600) -> str:
    window = int(time.time() // window_seconds)
    basis = json.dumps(
        {
            "channel_id": message.channel_id,
            "conversation": message.conversation.model_dump(),
            "text": message.text,
            "card": message.card.model_dump() if message.card else None,
            "reply_to": message.reply_to,
        },
        ensure_ascii=False,
        sort_keys=True,
    )
    digest = hashlib.sha256(basis.encode("utf-8")).hexdigest()[:16]
    key = f"{message.channel_id}:{window}:{digest}"
    logger.debug("outbound dedup key=%s", key)
    return key
