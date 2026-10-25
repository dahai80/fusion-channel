from __future__ import annotations

import logging

import httpx

from fusion_channel.adapters import HttpAdapterMixin, register
from fusion_channel.capabilities import matrix_capabilities
from fusion_channel.models import (
    ChannelRequest,
    Conversation,
    ExternalIdentity,
    InboundEnvelope,
    Interaction,
    MediaRef,
    OutboundMessage,
    SendResult,
    TestResult,
    VerifyResult,
    WebhookResponse,
)
from fusion_channel.spi import ChannelAdapter, CredentialField
from fusion_channel.verify import verify_whatsapp_signature

logger = logging.getLogger(__name__)

GRAPH_BASE = "https://graph.facebook.com"


@register
class WhatsAppCloudAdapter(HttpAdapterMixin, ChannelAdapter):
    # Meta Cloud API: X-Hub-Signature-256 over the raw body with the app
    # secret. No rich cards (CAN_CARDS=none) — command fallback text only.

    @classmethod
    def adapter_id(cls) -> str:
        return "whatsapp_cloud"

    @classmethod
    def capabilities(cls):
        return matrix_capabilities(cls.adapter_id())

    @classmethod
    def credential_schema(cls) -> list[CredentialField]:
        return [
            CredentialField("phone_number_id", required=True, secret=False, pattern=r"^[0-9]+$"),
            CredentialField("access_token", required=True, secret=True),
            CredentialField("app_secret", required=True, secret=True),
            CredentialField("verify_token", required=False, secret=True),
        ]

    def verify_request(self, request: ChannelRequest) -> VerifyResult:
        signature = request.header("X-Hub-Signature-256")
        ok = verify_whatsapp_signature(self.credentials.get("app_secret", ""), request.body, signature)
        if not ok:
            logger.warning("whatsapp signature rejected channel=%s", self.channel_id)
        return VerifyResult(ok=ok, reason="" if ok else "signature_mismatch")

    def parse_inbound(self, request: ChannelRequest) -> InboundEnvelope:
        payload = request.json_body or {}
        entry = (payload.get("entry") or [{}])[0]
        change = (entry.get("changes") or [{}])[0]
        value = change.get("value", {})
        messages = value.get("messages") or []
        statuses = value.get("statuses") or []
        if not messages:
            # Status webhook = delivery receipt, not a message.
            status = statuses[0] if statuses else {}
            message_id = status.get("id", "") or payload.get("field", "status")
            return InboundEnvelope(
                channel_id=self.channel_id,
                message_id=f"status:{message_id}",
                conversation=Conversation(type="direct", id="status"),
                sender=ExternalIdentity(channel_id=self.channel_id, external_id="whatsapp"),
                raw=payload,
            )
        message = messages[0]
        from_number = message.get("from", "")
        message_type = message.get("type", "text")
        text = ""
        media: list[MediaRef] = []
        if message_type == "text":
            text = message.get("text", {}).get("body", "")
        elif message_type in ("image", "document", "audio", "video"):
            obj = message.get(message_type, {})
            media = [MediaRef(media_id=obj.get("id", ""), mime_type=obj.get("mime_type"))]
        elif message_type == "button":
            # Template quick-reply button: normalize to interaction.
            button = message.get("button", {})
            text = button.get("text", "")
        interaction: Interaction | None = None
        if message_type == "interactive":
            interactive = message.get("interactive", {})
            button_reply = interactive.get("button_reply", {})
            list_reply = interactive.get("list_reply", {})
            if button_reply or list_reply:
                reply = button_reply or list_reply
                interaction = Interaction(
                    kind="button",
                    action=reply.get("id", ""),
                    message_id=reply.get("id", ""),
                    payload={"title": reply.get("title", "")},
                    actor=ExternalIdentity(channel_id=self.channel_id, external_id=from_number),
                )
        return InboundEnvelope(
            channel_id=self.channel_id,
            message_id=message.get("id", ""),
            conversation=Conversation(type="direct", id=from_number),
            sender=ExternalIdentity(channel_id=self.channel_id, external_id=from_number),
            text=text,
            media=media,
            interaction=interaction,
            raw=payload,
        )

    def webhook_response(self, request: ChannelRequest, envelope) -> WebhookResponse:
        return WebhookResponse(status_code=200, body="ok", media_type="text/plain")

    async def send(self, message: OutboundMessage) -> SendResult:
        rendered = message.meta.get("rendered") or {}
        text = rendered.get("text") or message.text
        to_number = message.conversation.id
        phone_number_id = self.credentials.get("phone_number_id", "")
        if not phone_number_id:
            return SendResult(ok=False, error="whatsapp credentials not configured")
        body = {
            "messaging_product": "whatsapp",
            "recipient_type": "individual",
            "to": to_number,
            "type": "text",
            "text": {"body": text},
        }
        try:
            async with self._client() as client:
                resp = await client.post(
                    f"{GRAPH_BASE}/v20.0/{phone_number_id}/messages",
                    headers={"Authorization": f"Bearer {self.credentials.get('access_token', '')}"},
                    json=body,
                )
            if resp.status_code in (200, 201):
                messages = resp.json().get("messages", [])
                return SendResult(
                    ok=True, provider_message_id=messages[0].get("id", "") if messages else "", raw=resp.json()
                )
            return SendResult(ok=False, error=f"whatsapp api error status={resp.status_code} body={resp.text[:200]}")
        except httpx.HTTPError as exc:
            return SendResult(ok=False, error=f"whatsapp http error: {exc}")

    async def test_connection(self) -> TestResult:
        phone_number_id = self.credentials.get("phone_number_id", "")
        if not phone_number_id:
            return TestResult(ok=False, detail="phone_number_id missing")
        try:
            async with self._client() as client:
                resp = await client.get(
                    f"{GRAPH_BASE}/v20.0/{phone_number_id}",
                    headers={"Authorization": f"Bearer {self.credentials.get('access_token', '')}"},
                    params={"fields": "display_phone_number,verified_name"},
                )
            if resp.status_code == 200:
                return TestResult(ok=True, detail="phone number fetch ok")
            return TestResult(ok=False, detail=f"status={resp.status_code} body={resp.text[:200]}")
        except httpx.HTTPError as exc:
            return TestResult(ok=False, detail=f"http error: {exc}")
