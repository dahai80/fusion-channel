from __future__ import annotations

import logging
import time
import xml.etree.ElementTree as ET

import httpx

from fusion_channel.adapters import HttpAdapterMixin, register
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
from fusion_channel.verify import verify_wecom_signature, wecom_decrypt

logger = logging.getLogger(__name__)

WECOM_BASE = "https://qyapi.weixin.qq.com/cgi-bin"


class WeComGroupOps(GroupOps):
    def __init__(self, adapter: WeComAppAdapter):
        self.adapter = adapter

    async def list(self) -> list[GroupInfo]:
        token = await self.adapter.access_token()
        async with self.adapter._client() as client:
            resp = await client.get(f"{WECOM_BASE}/appchat/get", params={"access_token": token})
        payload = resp.json()
        if payload.get("errcode") != 0:
            raise RuntimeError(f"wecom appchat get errcode={payload.get('errcode')} msg={payload.get('errmsg')}")
        chat = payload.get("chat_info", {})
        return [GroupInfo(group_id=chat.get("chatid", ""), name=chat.get("name", ""))]

    async def info(self, group_id: str) -> GroupInfo:
        groups = await self.list()
        for group in groups:
            if group.group_id == group_id:
                return group
        return GroupInfo(group_id=group_id)


@register
class WeComAppAdapter(HttpAdapterMixin, ChannelAdapter):
    # Self-built app callback: msg_signature = sha1(sort(token, ts, nonce,
    # encrypt)), payload AES-256-CBC with encoding_aes_key. Partial cards
    # (textcard) plus text fallback rendered together (PRD §6.3).

    @classmethod
    def adapter_id(cls) -> str:
        return "wecom_app"

    @classmethod
    def capabilities(cls):
        return matrix_capabilities(cls.adapter_id())

    @classmethod
    def credential_schema(cls) -> list[CredentialField]:
        return [
            CredentialField("corp_id", required=True, secret=False),
            CredentialField("agent_id", required=True, secret=False),
            CredentialField("corp_secret", required=True, secret=True),
            CredentialField("token", required=True, secret=True),
            CredentialField("encoding_aes_key", required=True, secret=True, pattern=r"^[0-9A-Za-z]{43}$"),
        ]

    async def access_token(self) -> str:
        cached = getattr(self, "_token_cache", None)
        if cached and cached[1] > time.monotonic():
            return cached[0]
        async with self._client() as client:
            resp = await client.get(
                f"{WECOM_BASE}/gettoken",
                params={
                    "corpid": self.credentials.get("corp_id", ""),
                    "corpsecret": self.credentials.get("corp_secret", ""),
                },
            )
        payload = resp.json()
        if payload.get("errcode") != 0:
            raise RuntimeError(f"wecom token errcode={payload.get('errcode')} msg={payload.get('errmsg')}")
        token = payload.get("access_token", "")
        expires_in = int(payload.get("expires_in", 7200))
        # Refresh 120s before server-side expiry so long-lived processes
        # never send with a stale token.
        self._token_cache = (token, time.monotonic() + max(60, expires_in - 120))
        return token

    def verify_request(self, request: ChannelRequest) -> VerifyResult:
        body = request.json_body or {}
        encrypt_xml = body.get("encrypt", "")
        msg_signature = body.get("msg_signature", "")
        timestamp = body.get("timestamp", "")
        nonce = body.get("nonce", "")
        ok = verify_wecom_signature(self.credentials.get("token", ""), timestamp, nonce, encrypt_xml, msg_signature)
        if not ok:
            logger.warning("wecom signature rejected channel=%s", self.channel_id)
        return VerifyResult(ok=ok, reason="" if ok else "signature_mismatch")

    def _decrypt_xml(self, request: ChannelRequest) -> ET.Element | None:
        body = request.json_body or {}
        encrypt_xml = body.get("encrypt", "")
        if not encrypt_xml:
            return None
        plain = wecom_decrypt(
            self.credentials.get("encoding_aes_key", ""),
            encrypt_xml,
            expected_receive_id=self.credentials.get("corp_id", ""),
        )
        return ET.fromstring(plain.decode("utf-8"))

    def parse_inbound(self, request: ChannelRequest) -> InboundEnvelope:
        root = self._decrypt_xml(request)
        if root is None:
            return InboundEnvelope(
                channel_id=self.channel_id,
                message_id="empty",
                conversation=Conversation(type="direct", id="unknown"),
                sender=ExternalIdentity(channel_id=self.channel_id, external_id="unknown"),
                raw=request.json_body or {},
            )
        from_user = root.findtext("FromUserName", "")
        to_user = root.findtext("ToUserName", "")
        content = root.findtext("Content", "")
        msg_id = root.findtext("MsgId", "") or root.findtext("CreateTime", "")
        chat_id = root.findtext("ChatId", "")
        # App chat messages carry group context; direct app messages do not.
        conversation_type = "group" if chat_id else "direct"
        conversation_id = chat_id or f"{from_user}->{to_user}"
        return InboundEnvelope(
            channel_id=self.channel_id,
            message_id=msg_id,
            conversation=Conversation(type=conversation_type, id=conversation_id),
            sender=ExternalIdentity(channel_id=self.channel_id, external_id=from_user),
            text=content,
            raw={"xml": _element_to_dict(root)},
        )

    def webhook_response(self, request: ChannelRequest, envelope) -> WebhookResponse:
        return WebhookResponse(status_code=200, body="ok", media_type="text/plain")

    async def send(self, message: OutboundMessage) -> SendResult:
        rendered = message.meta.get("rendered") or {}
        card_payload = rendered.get("card_payload")
        token = await self.access_token()
        text = rendered.get("text") or message.text
        body: dict
        if message.conversation.type == "group":
            body = {"chatid": message.conversation.id, "msgtype": "text", "text": {"content": text}}
            url = f"{WECOM_BASE}/appchat/send"
            if card_payload and card_payload.get("msgtype") == "textcard":
                body["msgtype"] = "textcard"
                body["textcard"] = card_payload["textcard"]
        else:
            url = f"{WECOM_BASE}/message/send"
            body = {
                "touser": message.conversation.id,
                "msgtype": "text",
                "agentid": self.credentials.get("agent_id", ""),
                "text": {"content": text},
            }
            if card_payload and card_payload.get("msgtype") == "textcard":
                body["msgtype"] = "textcard"
                body["textcard"] = card_payload["textcard"]
        try:
            async with self._client() as client:
                resp = await client.post(url, params={"access_token": token}, json=body)
            payload = resp.json()
            if payload.get("errcode") == 0:
                return SendResult(ok=True, raw=payload)
            return SendResult(ok=False, error=f"wecom errcode={payload.get('errcode')} msg={payload.get('errmsg')}")
        except httpx.HTTPError as exc:
            return SendResult(ok=False, error=f"wecom http error: {exc}")

    async def test_connection(self) -> TestResult:
        try:
            token = await self.access_token()
            return TestResult(ok=bool(token), detail="access_token ok")
        except RuntimeError as exc:
            return TestResult(ok=False, detail=str(exc))
        except httpx.HTTPError as exc:
            return TestResult(ok=False, detail=f"http error: {exc}")

    async def group_ops(self) -> GroupOps:
        return WeComGroupOps(self)


def _element_to_dict(element: ET.Element) -> dict:
    return {child.tag: (child.text or "") for child in element}
