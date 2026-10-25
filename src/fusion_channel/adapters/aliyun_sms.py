from __future__ import annotations

import base64
import datetime as dt
import hashlib
import hmac
import json
import logging
import urllib.parse
import uuid

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

ALIYUN_SMS_ENDPOINT = "https://dysmsapi.aliyuncs.com"


def _percent_encode(value) -> str:
    return urllib.parse.quote(str(value), safe="-_.~")


@register
class AliyunSmsAdapter(HttpAdapterMixin, ChannelAdapter):
    # China-mainland SMS via Aliyun Dysmsapi RPC (signature v1). Mostly
    # one-way (CAN_RECEIVE=false): pure notification fallback channel.

    @classmethod
    def adapter_id(cls) -> str:
        return "aliyun_sms"

    @classmethod
    def capabilities(cls):
        return matrix_capabilities(cls.adapter_id())

    @classmethod
    def credential_schema(cls) -> list[CredentialField]:
        return [
            CredentialField("access_key_id", required=True, secret=False),
            CredentialField("access_key_secret", required=True, secret=True),
            CredentialField("sign_name", required=True, secret=False),
            CredentialField("template_code", required=True, secret=False),
            CredentialField("callback_secret", required=False, secret=True),
        ]

    def verify_request(self, request: ChannelRequest) -> VerifyResult:
        # Upstream reply-report callbacks (when enabled) sign the raw body
        # with a configured shared secret via HMAC-SHA256.
        secret = self.credentials.get("callback_secret", "")
        if not secret:
            return VerifyResult(ok=False, reason="callback_secret_not_configured")
        signature = request.header("X-Fusion-Signature") or request.header("X-Ca-Signature")
        ok = verify_hmac_sha256_hex(secret, request.body, signature)
        if not ok:
            logger.warning("aliyun callback signature rejected channel=%s", self.channel_id)
        return VerifyResult(ok=ok, reason="" if ok else "signature_mismatch")

    def parse_inbound(self, request: ChannelRequest) -> InboundEnvelope:
        payload = request.json_body or {}
        phone = payload.get("phone_number", "")
        content = payload.get("content", "")
        return InboundEnvelope(
            channel_id=self.channel_id,
            message_id=payload.get("message_id", "") or payload.get("biz_id", ""),
            conversation=Conversation(type="direct", id=phone),
            sender=ExternalIdentity(channel_id=self.channel_id, external_id=phone),
            text=content,
            raw=payload,
        )

    def webhook_response(self, request: ChannelRequest, envelope) -> WebhookResponse:
        return WebhookResponse(status_code=200, body="ok", media_type="text/plain")

    def _rpc_params(self, phone: str, template_param: dict | None) -> dict:
        return {
            "Action": "SendSms",
            "Version": "2017-05-25",
            "PhoneNumbers": phone,
            "SignName": self.credentials.get("sign_name", ""),
            "TemplateCode": self.credentials.get("template_code", ""),
            "TemplateParam": json.dumps(template_param or {"content": ""}, ensure_ascii=False),
            "Format": "JSON",
            "AccessKeyId": self.credentials.get("access_key_id", ""),
            "SignatureMethod": "HMAC-SHA1",
            "Timestamp": dt.datetime.now(dt.UTC).strftime("%Y-%m-%dT%H:%M:%SZ"),
            "SignatureVersion": "1.0",
            "SignatureNonce": uuid.uuid4().hex,
            "RegionId": "cn-hangzhou",
        }

    def _signed(self, params: dict) -> dict:
        # Aliyun RPC signature v1: HMAC-SHA1 over percent-encoded query.
        # percentEncode is RFC3986 strict (space -> %20, not "+"), then the
        # whole query string is percent-encoded once more for the string
        # to sign — urlencode() here produces SignatureDoesNotMatch.
        secret = self.credentials.get("access_key_secret", "") + "&"
        query = "&".join(f"{_percent_encode(k)}={_percent_encode(v)}" for k, v in sorted(params.items()))
        string_to_sign = "GET&%2F&" + _percent_encode(query)
        digest = hmac.new(secret.encode(), string_to_sign.encode(), hashlib.sha1).digest()
        signed = dict(params)
        signed["Signature"] = base64.b64encode(digest).decode()
        return signed

    async def send(self, message: OutboundMessage) -> SendResult:
        rendered = message.meta.get("rendered") or {}
        text = rendered.get("text") or message.text
        phone = message.conversation.id.split("->")[0] if "->" in message.conversation.id else message.conversation.id
        params = self._rpc_params(phone, {"content": text})
        signed = self._signed(params)
        try:
            async with self._client() as client:
                resp = await client.get(ALIYUN_SMS_ENDPOINT, params=signed)
            payload = resp.json()
            if payload.get("Code") == "OK":
                return SendResult(ok=True, provider_message_id=payload.get("BizId", ""), raw=payload)
            return SendResult(ok=False, error=f"aliyun code={payload.get('Code')} msg={payload.get('Message')}")
        except httpx.HTTPError as exc:
            return SendResult(ok=False, error=f"aliyun http error: {exc}")

    async def test_connection(self) -> TestResult:
        if not self.credentials.get("access_key_id"):
            return TestResult(ok=False, detail="access_key_id missing")
        return TestResult(
            ok=True, detail="credentials present (aliyun has no cheap echo endpoint; first send validates)"
        )
