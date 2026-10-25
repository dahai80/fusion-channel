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
    OutboundMessage,
    SendResult,
    TestResult,
    VerifyResult,
    WebhookResponse,
)
from fusion_channel.spi import ChannelAdapter, CredentialField, GroupOps
from fusion_channel.verify import verify_dingtalk_signature

logger = logging.getLogger(__name__)

DINGTALK_BASE = "https://oapi.dingtalk.com"


class DingTalkGroupOps(GroupOps):
    def __init__(self, adapter: DingTalkRobotAdapter):
        self.adapter = adapter

    async def list(self) -> list[GroupInfo]:
        # Group robots only know their own conversation; the webhook URL
        # pins a single chat.
        return [GroupInfo(group_id=self.adapter.webhook_token(), name="dingtalk-group")]

    async def info(self, group_id: str) -> GroupInfo:
        return GroupInfo(group_id=group_id, name="dingtalk-group")


@register
class DingTalkRobotAdapter(HttpAdapterMixin, ChannelAdapter):
    # Group robot with HMAC-SHA256 signing mode (timestamp\nsecret, base64).
    # Plaintext-keyword mode is not acceptable (PRD §3.1) — secret required.

    @classmethod
    def adapter_id(cls) -> str:
        return "dingtalk_robot"

    @classmethod
    def capabilities(cls):
        return matrix_capabilities(cls.adapter_id())

    @classmethod
    def credential_schema(cls) -> list[CredentialField]:
        return [
            CredentialField("access_token", required=True, secret=False),
            CredentialField("secret", required=True, secret=True),
        ]

    def webhook_token(self) -> str:
        return self.credentials.get("access_token", "")

    def _webhook_url(self) -> str:
        import time

        from fusion_channel.verify import compute_dingtalk_sign

        timestamp = int(time.time() * 1000)
        sign = compute_dingtalk_sign(self.credentials.get("secret", ""), timestamp)
        return f"{DINGTALK_BASE}/robot/send?access_token={self.webhook_token()}&timestamp={timestamp}&sign={sign}"

    def verify_request(self, request: ChannelRequest) -> VerifyResult:
        # Outbound-only robots have no callback; when a callback URL is
        # configured (企业内部应用回调), the same sign scheme applies.
        params = request.json_body or {}
        timestamp = str(params.get("timestamp", ""))
        sign = params.get("sign", "")
        ok = verify_dingtalk_signature(self.credentials.get("secret", ""), timestamp, sign)
        if not ok:
            logger.warning("dingtalk sign rejected channel=%s", self.channel_id)
        return VerifyResult(ok=ok, reason="" if ok else "sign_mismatch")

    def parse_inbound(self, request: ChannelRequest) -> InboundEnvelope:
        params = request.json_body or {}
        text = params.get("text", {}).get("content", "")
        sender_id = params.get("senderStaffId") or params.get("senderId", "")
        conversation_id = params.get("conversationId", "")
        sender_nick = params.get("senderNick", "")
        at_users = params.get("atUsers", [])
        mentions = [
            ExternalIdentity(channel_id=self.channel_id, external_id=item.get("dingtalkId", "")) for item in at_users
        ]
        return InboundEnvelope(
            channel_id=self.channel_id,
            # msgId only — sessionId is constant per chat and would mute the
            # whole group in dedup; registry synthesizes an id when msgId is
            # absent.
            message_id=params.get("msgId", ""),
            conversation=Conversation(type="group", id=conversation_id),
            sender=ExternalIdentity(channel_id=self.channel_id, external_id=sender_id, display_name=sender_nick),
            text=text.strip(),
            mentions=mentions,
            raw=params,
        )

    def webhook_response(self, request: ChannelRequest, envelope) -> WebhookResponse:
        return WebhookResponse(status_code=200, body="ok", media_type="text/plain")

    async def send(self, message: OutboundMessage) -> SendResult:
        rendered = message.meta.get("rendered") or {}
        card_payload = rendered.get("card_payload")
        body = card_payload or {"msgtype": "text", "text": {"content": rendered.get("text") or message.text}}
        try:
            async with self._client() as client:
                resp = await client.post(self._webhook_url(), json=body)
            payload = resp.json()
            if resp.status_code == 200 and payload.get("errcode") == 0:
                return SendResult(ok=True, raw=payload)
            return SendResult(ok=False, error=f"dingtalk errcode={payload.get('errcode')} msg={payload.get('errmsg')}")
        except httpx.HTTPError as exc:
            return SendResult(ok=False, error=f"dingtalk http error: {redact_secrets(str(exc))}")

    async def test_connection(self) -> TestResult:
        # Static check only: every robot send lands in a real production
        # group, so a connectivity probe must not post. Validates that the
        # credentials are present and a fresh sign computes.
        import time

        from fusion_channel.verify import compute_dingtalk_sign

        if not self.webhook_token() or not self.credentials.get("secret"):
            return TestResult(ok=False, detail="missing access_token or secret")
        try:
            sign = compute_dingtalk_sign(self.credentials.get("secret", ""), int(time.time() * 1000))
            if not sign:
                return TestResult(ok=False, detail="sign computation failed")
        except Exception as exc:
            return TestResult(ok=False, detail=f"sign computation error: {redact_secrets(str(exc))}")
        return TestResult(ok=True, detail="credentials present, sign valid (static check, no group post)")

    async def group_ops(self) -> GroupOps:
        return DingTalkGroupOps(self)
