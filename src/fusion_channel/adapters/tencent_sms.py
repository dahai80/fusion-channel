from __future__ import annotations

import datetime as dt
import hashlib
import hmac
import json
import logging
import time

import httpx

from fusion_channel.adapters import HttpAdapterMixin, register
from fusion_channel.capabilities import matrix_capabilities
from fusion_channel.models import (
    ChannelRequest,
    Conversation,
    ExternalIdentity,
    InboundEnvelope,
    OutboundMessage,
    SendResult,
    TestResult,
    VerifyResult,
    WebhookResponse,
)
from fusion_channel.spi import ChannelAdapter, CredentialField
from fusion_channel.verify import verify_hmac_sha256_hex

logger = logging.getLogger(__name__)

TENCENT_SMS_HOST = "sms.tencentcloudapi.com"


@register
class TencentSmsAdapter(HttpAdapterMixin, ChannelAdapter):
    # China-mainland SMS via Tencent Cloud TC3 (signature v3). One-way
    # notification channel (CAN_RECEIVE=false), delivery receipts via
    # callback signature check.

    @classmethod
    def adapter_id(cls) -> str:
        return "tencent_sms"

    @classmethod
    def capabilities(cls):
        return matrix_capabilities(cls.adapter_id())

    @classmethod
    def credential_schema(cls) -> list[CredentialField]:
        return [
            CredentialField("secret_id", required=True, secret=False),
            CredentialField("secret_key", required=True, secret=True),
            CredentialField("sdk_app_id", required=True, secret=False, pattern=r"^[0-9]+$"),
            CredentialField("sign_name", required=True, secret=False),
            CredentialField("template_id", required=True, secret=False),
            CredentialField("callback_secret", required=False, secret=True),
        ]

    def verify_request(self, request: ChannelRequest) -> VerifyResult:
        secret = self.credentials.get("callback_secret", "")
        if not secret:
            return VerifyResult(ok=False, reason="callback_secret_not_configured")
        signature = request.header("X-Fusion-Signature")
        ok = verify_hmac_sha256_hex(secret, request.body, signature)
        if not ok:
            logger.warning("tencent callback signature rejected channel=%s", self.channel_id)
        return VerifyResult(ok=ok, reason="" if ok else "signature_mismatch")

    def parse_inbound(self, request: ChannelRequest) -> InboundEnvelope:
        payload = request.json_body or {}
        phone = payload.get("phone_number", "")
        content = payload.get("content", "")
        return InboundEnvelope(
            channel_id=self.channel_id,
            message_id=payload.get("message_id", "") or payload.get("serial_no", ""),
            conversation=Conversation(type="direct", id=phone),
            sender=ExternalIdentity(channel_id=self.channel_id, external_id=phone),
            text=content,
            raw=payload,
        )

    def webhook_response(self, request: ChannelRequest, envelope) -> WebhookResponse:
        return WebhookResponse(status_code=200, body="ok", media_type="text/plain")

    def _tc3_headers(self, payload: bytes) -> dict[str, str]:
        # Tencent Cloud API 3.0 signature (TC3-HMAC-SHA256).
        secret_id = self.credentials.get("secret_id", "")
        secret_key = self.credentials.get("secret_key", "")
        timestamp = int(time.time())
        date = dt.datetime.fromtimestamp(timestamp, dt.UTC).strftime("%Y-%m-%d")
        service = "sms"
        hashed_payload = hashlib.sha256(payload).hexdigest()
        canonical = (
            "POST\n/\n\ncontent-type:application/json\nhost:"
            + TENCENT_SMS_HOST
            + f"\n\ncontent-type;host\n{hashed_payload}"
        )
        credential_scope = f"{date}/{service}/tc3_request"
        string_to_sign = (
            f"TC3-HMAC-SHA256\n{timestamp}\n{credential_scope}\n" + hashlib.sha256(canonical.encode()).hexdigest()
        )

        def _hmac_sha256(key: bytes, msg: str) -> bytes:
            return hmac.new(key, msg.encode(), hashlib.sha256).digest()

        secret_date = _hmac_sha256(("TC3" + secret_key).encode(), date)
        secret_service = _hmac_sha256(secret_date, service)
        secret_signing = _hmac_sha256(secret_service, "tc3_request")
        signature = hmac.new(secret_signing, string_to_sign.encode(), hashlib.sha256).hexdigest()
        return {
            "Authorization": (
                f"TC3-HMAC-SHA256 Credential={secret_id}/{credential_scope}, "
                f"SignedHeaders=content-type;host, Signature={signature}"
            ),
            "Content-Type": "application/json",
            "X-TC-Action": "SendSms",
            "X-TC-Version": "2021-01-11",
            "X-TC-Timestamp": str(timestamp),
            "X-TC-Region": "ap-guangzhou",
        }

    async def send(self, message: OutboundMessage) -> SendResult:
        rendered = message.meta.get("rendered") or {}
        text = rendered.get("text") or message.text
        phone = message.conversation.id.split("->")[0] if "->" in message.conversation.id else message.conversation.id
        body = json.dumps(
            {
                "PhoneNumberSet": [f"+86{phone.lstrip('+86')}"],
                "SmsSdkAppId": self.credentials.get("sdk_app_id", ""),
                "SignName": self.credentials.get("sign_name", ""),
                "TemplateId": self.credentials.get("template_id", ""),
                "TemplateParamSet": [text],
            },
            ensure_ascii=False,
        ).encode("utf-8")
        headers = self._tc3_headers(body)
        try:
            async with self._client() as client:
                resp = await client.post(f"https://{TENCENT_SMS_HOST}/", content=body, headers=headers)
            payload = resp.json().get("Response", {})
            if "Error" not in payload:
                send_status = (payload.get("SendStatusSet") or [{}])[0]
                return SendResult(
                    ok=send_status.get("Code") == "Ok",
                    provider_message_id=send_status.get("SerialNo", ""),
                    raw=payload,
                    error=send_status.get("Message"),
                )
            error = payload.get("Error", {})
            return SendResult(ok=False, error=f"tencent code={error.get('Code')} msg={error.get('Message')}")
        except httpx.HTTPError as exc:
            return SendResult(ok=False, error=f"tencent http error: {exc}")

    async def test_connection(self) -> TestResult:
        if not self.credentials.get("secret_id") or not self.credentials.get("secret_key"):
            return TestResult(ok=False, detail="credentials missing")
        return TestResult(ok=True, detail="credentials present (first send validates)")
