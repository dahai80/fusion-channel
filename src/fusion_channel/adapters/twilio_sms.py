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
    MediaRef,
    OutboundMessage,
    SendResult,
    TestResult,
    VerifyResult,
    WebhookResponse,
)
from fusion_channel.spi import ChannelAdapter, CredentialField
from fusion_channel.verify import verify_twilio_signature

logger = logging.getLogger(__name__)

MAX_STORED_PARAM = 500


@register
class TwilioSmsAdapter(HttpAdapterMixin, ChannelAdapter):
    # Ported from ai-family sms/twilio_webhook.py + sms/sender.py (M1):
    # HMAC-SHA1 verification, API-Key-first REST auth, MMS media refs.

    API_BASE = "https://api.twilio.com/2010-04-01"

    @classmethod
    def adapter_id(cls) -> str:
        return "twilio_sms"

    @classmethod
    def capabilities(cls):
        return matrix_capabilities(cls.adapter_id())

    @classmethod
    def credential_schema(cls) -> list[CredentialField]:
        return [
            CredentialField("account_sid", required=True, secret=False, pattern=r"^AC[0-9a-fA-F]{32}$"),
            CredentialField("auth_token", required=True, secret=True),
            CredentialField("from_number", required=True, secret=False, pattern=r"^\+[1-9]\d{6,14}$"),
            CredentialField("api_key_sid", required=False, secret=False, pattern=r"^SK[0-9a-fA-F]{32}$"),
            CredentialField("api_key_secret", required=False, secret=True),
        ]

    def verify_request(self, request: ChannelRequest) -> VerifyResult:
        signature = request.header("X-Twilio-Signature")
        token = self.credentials.get("auth_token", "")
        ok = verify_twilio_signature(request.url, request.form, signature, token)
        if not ok:
            logger.warning("twilio signature rejected channel=%s url=%s", self.channel_id, request.url)
        return VerifyResult(ok=ok, reason="" if ok else "signature_mismatch")

    def parse_inbound(self, request: ChannelRequest) -> InboundEnvelope:
        params = request.form
        sid = params.get("MessageSid", "")
        from_phone = params.get("From", "")
        to_phone = params.get("To", "")
        body = params.get("Body", "")
        try:
            num_media = int(params.get("NumMedia", "0"))
        except ValueError:
            num_media = 0
        media = [
            MediaRef(url=params[f"MediaUrl{i}"], mime_type=params.get(f"MediaContentType{i}"))
            for i in range(num_media)
            if f"MediaUrl{i}" in params
        ]
        conversation_id = f"{from_phone}->{to_phone}"
        return InboundEnvelope(
            channel_id=self.channel_id,
            message_id=sid,
            conversation=Conversation(type="direct", id=conversation_id),
            sender=ExternalIdentity(channel_id=self.channel_id, external_id=_normalize_phone(from_phone)),
            text=body,
            media=media,
            raw={"params": {k: v[:MAX_STORED_PARAM] for k, v in params.items()}},
        )

    def webhook_response(self, request: ChannelRequest, envelope) -> WebhookResponse:
        return WebhookResponse(status_code=200, body="ok", media_type="text/plain")

    def _auth(self) -> tuple[str, str]:
        account_sid = self.credentials.get("account_sid", "")
        auth_user = self.credentials.get("api_key_sid") or account_sid
        auth_pass = self.credentials.get("api_key_secret") or self.credentials.get("auth_token", "")
        return auth_user, auth_pass

    async def send(self, message: OutboundMessage) -> SendResult:
        rendered = message.meta.get("rendered") or {}
        text = rendered.get("text") or message.text
        account_sid = self.credentials.get("account_sid", "")
        from_number = self.credentials.get("from_number", "")
        if not account_sid or not from_number:
            return SendResult(ok=False, error="twilio credentials not configured")
        to_number = (
            message.conversation.id.split("->")[0] if "->" in message.conversation.id else message.conversation.id
        )
        url = f"{self.API_BASE}/Accounts/{account_sid}/Messages.json"
        auth_user, auth_pass = self._auth()
        logger.info(
            "twilio send channel=%s auth=%s from=%s",
            self.channel_id,
            "api_key" if auth_user != account_sid else "auth_token",
            from_number[:6],
        )
        try:
            async with self._client() as client:
                resp = await client.post(
                    url, data={"To": to_number, "From": from_number, "Body": text}, auth=(auth_user, auth_pass)
                )
            if resp.status_code not in (200, 201):
                return SendResult(ok=False, error=f"twilio api error status={resp.status_code} body={resp.text[:200]}")
            return SendResult(ok=True, provider_message_id=resp.json().get("sid", ""), raw={"status": resp.status_code})
        except httpx.HTTPError as exc:
            return SendResult(ok=False, error=f"twilio http error: {exc}")

    async def test_connection(self) -> TestResult:
        account_sid = self.credentials.get("account_sid", "")
        if not account_sid:
            return TestResult(ok=False, detail="account_sid missing")
        auth_user, auth_pass = self._auth()
        url = f"{self.API_BASE}/Accounts/{account_sid}.json"
        try:
            async with self._client() as client:
                resp = await client.get(url, auth=(auth_user, auth_pass))
            if resp.status_code == 200:
                return TestResult(ok=True, detail="account fetch ok")
            return TestResult(ok=False, detail=f"status={resp.status_code} body={resp.text[:200]}")
        except httpx.HTTPError as exc:
            return TestResult(ok=False, detail=f"http error: {exc}")


def _normalize_phone(phone: str) -> str:
    cleaned = "".join(c for c in phone if c.isdigit() or c == "+")
    if not cleaned.startswith("+") and cleaned.startswith("86") and len(cleaned) == 13:
        cleaned = "+" + cleaned
    return cleaned
