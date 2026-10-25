from __future__ import annotations

import logging

import httpx

from fusion_channel.adapters import HttpAdapterMixin, redact_secrets, register
from fusion_channel.capabilities import matrix_capabilities
from fusion_channel.models import (
    ChannelRequest,
    Conversation,
    ExternalIdentity,
    GroupInfo,
    InboundEnvelope,
    Interaction,
    MediaRef,
    OutboundMessage,
    SendResult,
    TestResult,
    VerifyResult,
    WebhookResponse,
)
from fusion_channel.spi import ChannelAdapter, CredentialField, GroupOps
from fusion_channel.verify import verify_telegram_secret_token

logger = logging.getLogger(__name__)

TELEGRAM_BASE = "https://api.telegram.org"


class TelegramGroupOps(GroupOps):
    def __init__(self, adapter: TelegramBotAdapter):
        self.adapter = adapter

    async def _call(self, method: str, params: dict) -> dict:
        token = self.adapter.credentials.get("bot_token", "")
        async with self.adapter._client() as client:
            resp = await client.post(f"{TELEGRAM_BASE}/bot{token}/{method}", json=params)
        payload = resp.json()
        if not payload.get("ok"):
            raise RuntimeError(f"telegram {method} error={payload.get('description')}")
        return payload.get("result", {})

    async def list(self) -> list[GroupInfo]:
        updates = await self._call("getUpdates", {"limit": 100})
        groups: dict[str, GroupInfo] = {}
        for update in updates:
            message = update.get("message") or update.get("channel_post") or {}
            chat = message.get("chat", {})
            if chat.get("type") in ("group", "supergroup"):
                groups.setdefault(chat["id"], GroupInfo(group_id=str(chat["id"]), name=chat.get("title", "")))
        return list(groups.values())

    async def info(self, group_id: str) -> GroupInfo:
        return GroupInfo(group_id=group_id)


@register
class TelegramBotAdapter(HttpAdapterMixin, ChannelAdapter):
    # Bot API webhook with secret_token header check; inline keyboard cards
    # (CAN_CARDS=full); group chats with @bot mentions.

    @classmethod
    def adapter_id(cls) -> str:
        return "telegram_bot"

    @classmethod
    def capabilities(cls):
        return matrix_capabilities(cls.adapter_id())

    @classmethod
    def credential_schema(cls) -> list[CredentialField]:
        return [
            CredentialField("bot_token", required=True, secret=True, pattern=r"^[0-9]+:[A-Za-z0-9_-]+$"),
            CredentialField("secret_token", required=True, secret=True),
        ]

    def verify_request(self, request: ChannelRequest) -> VerifyResult:
        received = request.header("X-Telegram-Bot-Api-Secret-Token")
        ok = verify_telegram_secret_token(self.credentials.get("secret_token", ""), received)
        if not ok:
            logger.warning("telegram secret token rejected channel=%s", self.channel_id)
        return VerifyResult(ok=ok, reason="" if ok else "token_mismatch")

    def parse_inbound(self, request: ChannelRequest) -> InboundEnvelope:
        update = request.json_body or {}
        callback = update.get("callback_query")
        if callback:
            message = callback.get("message", {})
            chat = message.get("chat", {})
            data = callback.get("data", "")
            action, _, code = data.partition(":")
            return InboundEnvelope(
                channel_id=self.channel_id,
                message_id=f"callback:{callback.get('id', '')}",
                conversation=Conversation(
                    type="group" if chat.get("type") in ("group", "supergroup") else "direct",
                    id=str(chat.get("id", "")),
                ),
                sender=ExternalIdentity(
                    channel_id=self.channel_id, external_id=str(callback.get("from", {}).get("id", ""))
                ),
                interaction=Interaction(
                    kind="button",
                    action=action,
                    message_id=str(callback.get("id", "")),
                    payload={"code": code, "data": data},
                    actor=ExternalIdentity(
                        channel_id=self.channel_id, external_id=str(callback.get("from", {}).get("id", ""))
                    ),
                ),
                raw=update,
            )
        message = update.get("message") or update.get("edited_message") or {}
        chat = message.get("chat", {})
        from_user = message.get("from", {})
        text = message.get("text", "")
        entities = message.get("entities") or []
        mentions: list[ExternalIdentity] = []
        for entity in entities:
            if entity.get("type") == "mention":
                offset, length = entity.get("offset", 0), entity.get("length", 0)
                mentions.append(
                    ExternalIdentity(channel_id=self.channel_id, external_id=text[offset : offset + length])
                )
        media: list[MediaRef] = []
        for media_key in ("photo", "document", "audio", "video", "voice"):
            if media_key in message:
                obj = message[media_key]
                if isinstance(obj, list):
                    obj = obj[-1] if obj else {}
                media = [MediaRef(media_id=str(obj.get("file_id", "")), mime_type=obj.get("mime_type"))]
                break
        return InboundEnvelope(
            channel_id=self.channel_id,
            message_id=f"tg:{update.get('update_id', '')}",
            conversation=Conversation(
                type="group" if chat.get("type") in ("group", "supergroup") else "direct",
                id=str(chat.get("id", "")),
                thread_id=str(message.get("message_thread_id", "")) or None,
            ),
            sender=ExternalIdentity(
                channel_id=self.channel_id,
                external_id=str(from_user.get("id", "")),
                display_name=from_user.get("username", ""),
            ),
            text=text,
            media=media,
            mentions=mentions,
            raw=update,
        )

    def webhook_response(self, request: ChannelRequest, envelope) -> WebhookResponse:
        return WebhookResponse(status_code=200, body="ok", media_type="text/plain")

    async def send(self, message: OutboundMessage) -> SendResult:
        rendered = message.meta.get("rendered") or {}
        card_payload = rendered.get("card_payload")
        token = self.credentials.get("bot_token", "")
        body: dict = {"chat_id": message.conversation.id, "text": rendered.get("text") or message.text}
        if message.reply_to:
            body["reply_to_message_id"] = message.reply_to
        method = "sendMessage"
        if card_payload and card_payload.get("reply_markup"):
            body["reply_markup"] = card_payload["reply_markup"]
        try:
            async with self._client() as client:
                resp = await client.post(f"{TELEGRAM_BASE}/bot{token}/{method}", json=body)
            payload = resp.json()
            if payload.get("ok"):
                return SendResult(
                    ok=True, provider_message_id=str(payload.get("result", {}).get("message_id", "")), raw=payload
                )
            return SendResult(ok=False, error=f"telegram error={payload.get('description')}")
        except httpx.HTTPError as exc:
            return SendResult(ok=False, error=f"telegram http error: {redact_secrets(str(exc))}")

    async def test_connection(self) -> TestResult:
        token = self.credentials.get("bot_token", "")
        if not token:
            return TestResult(ok=False, detail="bot_token missing")
        try:
            async with self._client() as client:
                resp = await client.post(f"{TELEGRAM_BASE}/bot{token}/getMe")
            payload = resp.json()
            if payload.get("ok"):
                return TestResult(ok=True, detail=f"getMe ok username={payload.get('result', {}).get('username')}")
            return TestResult(ok=False, detail=f"getMe error={payload.get('description')}")
        except httpx.HTTPError as exc:
            return TestResult(ok=False, detail=f"http error: {redact_secrets(str(exc))}")

    async def group_ops(self) -> GroupOps:
        return TelegramGroupOps(self)
