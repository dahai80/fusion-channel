from __future__ import annotations

import hmac
import json
import logging
import re
import time

import httpx

from fusion_channel.adapters import HttpAdapterMixin, register
from fusion_channel.capabilities import matrix_capabilities
from fusion_channel.models import (
    ChannelRequest,
    Conversation,
    ExternalIdentity,
    GroupInfo,
    InboundEnvelope,
    MediaRef,
    OutboundMessage,
    SendResult,
    TestResult,
    VerifyResult,
    WebhookResponse,
)
from fusion_channel.spi import ChannelAdapter, CredentialField, GroupOps
from fusion_channel.verify import feishu_decrypt

logger = logging.getLogger(__name__)

FEISHU_BASE = "https://open.feishu.cn/open-apis"


class FeishuGroupOps(GroupOps):
    def __init__(self, adapter: FeishuAppAdapter):
        self.adapter = adapter

    async def _token(self) -> str:
        return await self.adapter.tenant_access_token()

    async def list(self) -> list[GroupInfo]:
        token = await self._token()
        async with self.adapter._client() as client:
            resp = await client.get(
                f"{FEISHU_BASE}/im/v1/chats",
                params={"page_size": 100},
                headers={"Authorization": f"Bearer {token}"},
            )
        data = self.adapter._unwrap(resp)
        return [GroupInfo(group_id=item["chat_id"], name=item.get("name", "")) for item in data.get("items", [])]

    async def info(self, group_id: str) -> GroupInfo:
        token = await self._token()
        async with self.adapter._client() as client:
            resp = await client.get(
                f"{FEISHU_BASE}/im/v1/chats/{group_id}",
                headers={"Authorization": f"Bearer {token}"},
            )
        data = self.adapter._unwrap(resp)
        members: list[ExternalIdentity] = []
        async with self.adapter._client() as client:
            member_resp = await client.get(
                f"{FEISHU_BASE}/im/v1/chats/{group_id}/members",
                params={"page_size": 100},
                headers={"Authorization": f"Bearer {token}"},
            )
        member_data = self.adapter._unwrap(member_resp)
        members = [
            ExternalIdentity(channel_id=self.adapter.channel_id, external_id=item.get("member_id", ""))
            for item in member_data.get("items", [])
        ]
        return GroupInfo(group_id=group_id, name=data.get("name", ""), members=members)


@register
class FeishuAppAdapter(HttpAdapterMixin, ChannelAdapter):
    # Event subscription with Encrypt Key + Verification Token, challenge
    # handshake, interactive cards (CAN_CARDS=full), group operations.

    @classmethod
    def adapter_id(cls) -> str:
        return "feishu_app"

    @classmethod
    def capabilities(cls):
        return matrix_capabilities(cls.adapter_id())

    @classmethod
    def credential_schema(cls) -> list[CredentialField]:
        return [
            CredentialField("app_id", required=True, secret=False, pattern=r"^cli_[0-9a-zA-Z]+$"),
            CredentialField("app_secret", required=True, secret=True),
            CredentialField("encrypt_key", required=True, secret=True),
            CredentialField("verification_token", required=True, secret=True),
        ]

    async def tenant_access_token(self) -> str:
        cached = getattr(self, "_token_cache", None)
        if cached and cached[1] > time.monotonic():
            return cached[0]
        async with self._client() as client:
            resp = await client.post(
                f"{FEISHU_BASE}/auth/v3/tenant_access_token/internal",
                json={
                    "app_id": self.credentials.get("app_id", ""),
                    "app_secret": self.credentials.get("app_secret", ""),
                },
            )
        if resp.status_code != 200:
            raise RuntimeError(f"feishu token error status={resp.status_code} body={resp.text[:200]}")
        payload = resp.json()
        if payload.get("code") != 0:
            raise RuntimeError(f"feishu token error code={payload.get('code')} msg={payload.get('msg')}")
        token = payload.get("tenant_access_token", "")
        expire = int(payload.get("expire", 120))
        # Refresh 120s before server-side expiry so long-lived processes
        # never send with a stale token.
        self._token_cache = (token, time.monotonic() + max(60, expire - 120))
        return token

    def _unwrap(self, resp: httpx.Response) -> dict:
        if resp.status_code != 200:
            raise RuntimeError(f"feishu api error status={resp.status_code} body={resp.text[:200]}")
        payload = resp.json()
        if payload.get("code") != 0:
            raise RuntimeError(f"feishu api error code={payload.get('code')} msg={payload.get('msg')}")
        return payload.get("data", {})

    def _decrypt_event(self, request: ChannelRequest) -> dict:
        body = request.json_body or {}
        encrypted = body.get("encrypt", "")
        return feishu_decrypt(self.credentials.get("encrypt_key", ""), encrypted) if encrypted else body

    def verify_request(self, request: ChannelRequest) -> VerifyResult:
        # Hard rules: verification_token must be configured, the event must
        # arrive encrypted, and the token must match. An unconfigured token
        # rejects everything instead of accepting everything.
        expected = self.credentials.get("verification_token", "")
        if not expected:
            logger.error("feishu verification_token not configured channel=%s — rejecting", self.channel_id)
            return VerifyResult(ok=False, reason="verification_token_missing")
        body = request.json_body
        if not isinstance(body, dict) or not body.get("encrypt"):
            logger.warning("feishu event not encrypted channel=%s", self.channel_id)
            return VerifyResult(ok=False, reason="not_encrypted")
        try:
            event = self._decrypt_event(request)
        except Exception as exc:
            logger.warning("feishu decrypt failed channel=%s err=%s", self.channel_id, exc)
            return VerifyResult(ok=False, reason="decrypt_failed")
        # url_verification carries the token top-level; event v2 nests it in header.
        token = ""
        if isinstance(event, dict):
            token = event.get("token") or (event.get("header") or {}).get("token", "")
        if not token:
            return VerifyResult(ok=False, reason="token_missing")
        if not hmac.compare_digest(expected.encode("utf-8"), token.encode("utf-8")):
            logger.warning("feishu token mismatch channel=%s", self.channel_id)
            return VerifyResult(ok=False, reason="token_mismatch")
        return VerifyResult(ok=True)

    def parse_inbound(self, request: ChannelRequest) -> InboundEnvelope:
        event = self._decrypt_event(request)
        if event.get("type") == "url_verification":
            # Not a message; surfaced through webhook_response for challenge echo.
            return InboundEnvelope(
                channel_id=self.channel_id,
                message_id="url_verification",
                conversation=Conversation(type="direct", id="challenge"),
                sender=ExternalIdentity(channel_id=self.channel_id, external_id="feishu"),
                raw=event,
            )
        header = event.get("header", {})
        event_id = header.get("event_id", "") or event.get("uuid", "")
        inner = event.get("event", {})
        if not inner and "event" not in event:
            inner = event
        message = inner.get("message", {})
        chat_id = message.get("chat_id", "") or inner.get("chat_id", "")
        chat_type = message.get("chat_type", "p2p")
        msg_type = message.get("message_type", "text")
        content_raw = message.get("content", "{}")
        try:
            content = json.loads(content_raw) if isinstance(content_raw, str) else content_raw
        except ValueError:
            content = {}
        text = content.get("text", "") if msg_type == "text" else ""
        if text:
            # Feishu at-mentions embed <at user_id="...">Name</at> in text.
            text = re.sub(r"<at [^>]*>[^<]*</at>", "", text).strip()
        sender_id = (
            message.get("sender", "").get("id")
            if isinstance(message.get("sender"), dict)
            else inner.get("operator_id", {}).get("open_id", "")
        )
        mentions = [
            ExternalIdentity(channel_id=self.channel_id, external_id=m.get("id", ""), display_name=m.get("name", ""))
            for m in message.get("mentions", [])
        ]
        media: list[MediaRef] = []
        if msg_type == "image":
            media = [MediaRef(media_id=content.get("image_key", ""), mime_type="image")]
        return InboundEnvelope(
            channel_id=self.channel_id,
            message_id=event_id or message.get("message_id", ""),
            conversation=Conversation(
                type="group" if chat_type == "group" else "direct",
                id=chat_id,
                thread_id=message.get("thread_id", ""),
            ),
            sender=ExternalIdentity(channel_id=self.channel_id, external_id=sender_id),
            text=text,
            media=media,
            mentions=mentions,
            raw=event,
        )

    def webhook_response(self, request: ChannelRequest, envelope) -> WebhookResponse:
        if envelope is not None and envelope.message_id == "url_verification":
            challenge = envelope.raw.get("challenge", "")
            return WebhookResponse(
                status_code=200, body=json.dumps({"challenge": challenge}), media_type="application/json"
            )
        return WebhookResponse(status_code=200, body="ok", media_type="text/plain")

    async def send(self, message: OutboundMessage) -> SendResult:
        rendered = message.meta.get("rendered") or {}
        card_payload = rendered.get("card_payload")
        receive_id = message.conversation.id
        token = await self.tenant_access_token()
        if card_payload and card_payload.get("msg_type") == "interactive":
            body = {
                "receive_id": receive_id,
                "msg_type": "interactive",
                "content": json.dumps(card_payload["card"], ensure_ascii=False),
            }
        else:
            text = rendered.get("text") or message.text
            body = {
                "receive_id": receive_id,
                "msg_type": "text",
                "content": json.dumps({"text": text}, ensure_ascii=False),
            }
        try:
            async with self._client() as client:
                resp = await client.post(
                    f"{FEISHU_BASE}/im/v1/messages",
                    params={"receive_id_type": "chat_id" if message.conversation.type == "group" else "open_id"},
                    headers={"Authorization": f"Bearer {token}"},
                    json=body,
                )
            data = self._unwrap(resp)
            return SendResult(ok=True, provider_message_id=data.get("message_id", ""), raw={"status": resp.status_code})
        except RuntimeError as exc:
            return SendResult(ok=False, error=str(exc))
        except httpx.HTTPError as exc:
            return SendResult(ok=False, error=f"feishu http error: {exc}")

    async def test_connection(self) -> TestResult:
        try:
            token = await self.tenant_access_token()
            return TestResult(ok=bool(token), detail="tenant_access_token ok")
        except RuntimeError as exc:
            return TestResult(ok=False, detail=str(exc))
        except httpx.HTTPError as exc:
            return TestResult(ok=False, detail=f"http error: {exc}")

    async def group_ops(self) -> GroupOps:
        return FeishuGroupOps(self)
