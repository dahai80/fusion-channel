from __future__ import annotations

import json
import logging
import re

import httpx

from fusion_channel.adapters import HttpAdapterMixin, register
from fusion_channel.capabilities import matrix_capabilities
from fusion_channel.models import (
    ChannelRequest,
    Conversation,
    ExternalIdentity,
    GroupInfo,
    InboundEnvelope,
    Interaction,
    OutboundMessage,
    SendResult,
    TestResult,
    VerifyResult,
    WebhookResponse,
)
from fusion_channel.spi import ChannelAdapter, CredentialField, GroupOps
from fusion_channel.verify import verify_slack_signature

logger = logging.getLogger(__name__)

SLACK_BASE = "https://slack.com/api"


class SlackGroupOps(GroupOps):
    def __init__(self, adapter: SlackAppAdapter):
        self.adapter = adapter

    async def list(self) -> list[GroupInfo]:
        token = self.adapter.bot_token()
        async with self.adapter._client() as client:
            resp = await client.post(f"{SLACK_BASE}/conversations.list", headers={"Authorization": f"Bearer {token}"})
        payload = resp.json()
        if not payload.get("ok"):
            raise RuntimeError(f"slack conversations.list error={payload.get('error')}")
        return [GroupInfo(group_id=item["id"], name=item.get("name", "")) for item in payload.get("channels", [])]

    async def info(self, group_id: str) -> GroupInfo:
        token = self.adapter.bot_token()
        async with self.adapter._client() as client:
            resp = await client.post(
                f"{SLACK_BASE}/conversations.info",
                headers={"Authorization": f"Bearer {token}"},
                data={"channel": group_id},
            )
        payload = resp.json()
        if not payload.get("ok"):
            raise RuntimeError(f"slack conversations.info error={payload.get('error')}")
        channel = payload.get("channel", {})
        members: list[ExternalIdentity] = []
        async with self.adapter._client() as client:
            member_resp = await client.post(
                f"{SLACK_BASE}/conversations.members",
                headers={"Authorization": f"Bearer {token}"},
                data={"channel": group_id},
            )
        member_payload = member_resp.json()
        if member_payload.get("ok"):
            members = [
                ExternalIdentity(channel_id=self.adapter.channel_id, external_id=member)
                for member in member_payload.get("members", [])
            ]
        return GroupInfo(group_id=group_id, name=channel.get("name", ""), members=members)


@register
class SlackAppAdapter(HttpAdapterMixin, ChannelAdapter):
    # Events API with X-Slack-Signature (HMAC-SHA256 over v0:ts:body) and
    # Block Kit cards (CAN_CARDS=full).

    @classmethod
    def adapter_id(cls) -> str:
        return "slack_app"

    @classmethod
    def capabilities(cls):
        return matrix_capabilities(cls.adapter_id())

    @classmethod
    def credential_schema(cls) -> list[CredentialField]:
        return [
            CredentialField("bot_token", required=True, secret=True, pattern=r"^xoxb-"),
            CredentialField("signing_secret", required=True, secret=True),
        ]

    def bot_token(self) -> str:
        return self.credentials.get("bot_token", "")

    def verify_request(self, request: ChannelRequest) -> VerifyResult:
        signature = request.header("X-Slack-Signature")
        timestamp = request.header("X-Slack-Request-Timestamp")
        body = request.body.decode("utf-8", errors="replace")
        ok = verify_slack_signature(self.credentials.get("signing_secret", ""), timestamp, body, signature)
        if not ok:
            logger.warning("slack signature rejected channel=%s", self.channel_id)
        return VerifyResult(ok=ok, reason="" if ok else "signature_mismatch")

    def parse_inbound(self, request: ChannelRequest) -> InboundEnvelope:
        payload = request.json_body or {}
        if payload.get("type") == "url_verification":
            return InboundEnvelope(
                channel_id=self.channel_id,
                message_id="url_verification",
                conversation=Conversation(type="direct", id="challenge"),
                sender=ExternalIdentity(channel_id=self.channel_id, external_id="slack"),
                raw=payload,
            )
        event = payload.get("event", {})
        # block_actions payloads carry type/user/channel at the top level;
        # event_callback payloads nest them under "event".
        event_type = event.get("type", "") or payload.get("type", "")
        user = event.get("user") or payload.get("user", "")
        raw_channel = event.get("channel") or payload.get("channel", "")
        channel_id = raw_channel.get("id", "") if isinstance(raw_channel, dict) else raw_channel
        channel_type = event.get("channel_type", "channel")
        text = event.get("text", "")
        event_ts = event.get("ts", "") or payload.get("event_id", "")
        # Strip <@U123> mention markup into mention identities.
        mentions = [
            ExternalIdentity(channel_id=self.channel_id, external_id=match)
            for match in re.findall(r"<@([A-Z0-9]+)>", text)
        ]
        clean_text = re.sub(r"<@[A-Z0-9]+>", "", text).strip()
        interaction: Interaction | None = None
        if event_type == "block_actions":
            actions = payload.get("actions", [])
            if actions:
                action = actions[0]
                interaction = Interaction(
                    kind="button",
                    action=action.get("action_id", ""),
                    message_id=payload.get("trigger_id", "") or event_ts,
                    payload={"value": action.get("value", ""), "block_id": action.get("block_id", "")},
                    actor=ExternalIdentity(channel_id=self.channel_id, external_id=user),
                    conversation=Conversation(
                        type="group" if channel_type in ("channel", "group") else "direct", id=channel_id
                    ),
                )
        return InboundEnvelope(
            channel_id=self.channel_id,
            message_id=payload.get("event_id", "") or event_ts,
            conversation=Conversation(
                type="group" if channel_type in ("channel", "group") else "direct",
                id=channel_id,
                thread_id=event.get("thread_ts", ""),
            ),
            sender=ExternalIdentity(channel_id=self.channel_id, external_id=user),
            text=clean_text if event_type == "message" else "",
            mentions=mentions,
            interaction=interaction,
            raw=payload,
        )

    def webhook_response(self, request: ChannelRequest, envelope) -> WebhookResponse:
        if envelope is not None and envelope.message_id == "url_verification":
            return WebhookResponse(
                status_code=200,
                body=json.dumps({"challenge": envelope.raw.get("challenge", "")}),
                media_type="application/json",
            )
        return WebhookResponse(status_code=200, body="ok", media_type="text/plain")

    async def send(self, message: OutboundMessage) -> SendResult:
        rendered = message.meta.get("rendered") or {}
        card_payload = rendered.get("card_payload")
        data: dict = {"channel": message.conversation.id}
        if message.reply_to:
            data["thread_ts"] = message.reply_to
        if card_payload and card_payload.get("blocks"):
            data["blocks"] = json.dumps(card_payload["blocks"], ensure_ascii=False)
            if rendered.get("text"):
                data["text"] = rendered["text"]
        else:
            data["text"] = rendered.get("text") or message.text
        try:
            async with self._client() as client:
                resp = await client.post(
                    f"{SLACK_BASE}/chat.postMessage",
                    headers={"Authorization": f"Bearer {self.bot_token()}"},
                    data=data,
                )
            payload = resp.json()
            if payload.get("ok"):
                return SendResult(ok=True, provider_message_id=payload.get("ts", ""), raw=payload)
            return SendResult(ok=False, error=f"slack error={payload.get('error')}")
        except httpx.HTTPError as exc:
            return SendResult(ok=False, error=f"slack http error: {exc}")

    async def test_connection(self) -> TestResult:
        try:
            async with self._client() as client:
                resp = await client.post(
                    f"{SLACK_BASE}/auth.test", headers={"Authorization": f"Bearer {self.bot_token()}"}
                )
            payload = resp.json()
            if payload.get("ok"):
                return TestResult(ok=True, detail=f"auth.test ok team={payload.get('team')}")
            return TestResult(ok=False, detail=f"auth.test error={payload.get('error')}")
        except httpx.HTTPError as exc:
            return TestResult(ok=False, detail=f"http error: {exc}")

    async def group_ops(self) -> GroupOps:
        return SlackGroupOps(self)
