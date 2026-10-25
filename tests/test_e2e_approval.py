from __future__ import annotations

import json
import time
import urllib.parse

import httpx
import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from fusion_channel.fastapi import build_channel_router
from fusion_channel.models import ApprovalCard, CardAction, Conversation
from tests.conftest import (
    dingtalk_sign,
    feishu_payload,
    slack_sign,
    twilio_sign,
    whatsapp_sign,
)

pytestmark = pytest.mark.usefixtures("registry")


@pytest.fixture
def client(registry):
    app = FastAPI()
    app.include_router(build_channel_router(registry))
    return TestClient(app)


def post_twilio(client, params, sign=None):
    return client.post(
        "/webhook/family_twilio",
        data=params,
        headers={"X-Twilio-Signature": sign or twilio_sign("http://testserver/webhook/family_twilio", params)},
    )


def test_twilio_e2e_signed_webhook_to_handler(client, registry):
    received = []

    async def handler(envelope):
        received.append(envelope)

    registry.on_message(handler)
    params = {"MessageSid": "SM-E2E-1", "From": "+15550000002", "To": "+15550000001", "Body": "帮我订机票"}
    response = post_twilio(client, params)
    assert response.status_code == 200
    assert response.text == "ok"
    assert len(received) == 1
    assert received[0].text == "帮我订机票"


def test_twilio_e2e_bad_signature_403_and_archived(client, registry):
    params = {"MessageSid": "SM-E2E-2", "From": "+15550000002", "Body": "forged"}
    response = post_twilio(client, params, sign="forged-signature")
    assert response.status_code == 403
    assert len(registry.store.rejected) == 1


def test_twilio_e2e_duplicate_200_without_redispatch(client, registry):
    received = []

    async def handler(envelope):
        received.append(envelope)

    registry.on_message(handler)
    params = {"MessageSid": "SM-E2E-3", "From": "+15550000002", "Body": "dup"}
    assert post_twilio(client, params).status_code == 200
    assert post_twilio(client, params).status_code == 200
    assert len(received) == 1


def test_feishu_e2e_challenge_handshake(client, registry):
    data = {"type": "url_verification", "token": "feishu-verification-token", "challenge": "ch-abc-123"}
    payload = feishu_payload(data)
    response = client.post("/webhook/family_feishu", content=payload, headers={"Content-Type": "application/json"})
    assert response.status_code == 200
    assert response.json() == {"challenge": "ch-abc-123"}


def test_slack_e2e_url_verification(client, registry):
    payload = {"type": "url_verification", "challenge": "slack-ch-1"}
    body = json.dumps(payload)
    timestamp = str(int(time.time()))
    response = client.post(
        "/webhook/team_slack",
        content=body,
        headers={
            "Content-Type": "application/json",
            "X-Slack-Signature": slack_sign(timestamp, body),
            "X-Slack-Request-Timestamp": timestamp,
        },
    )
    assert response.status_code == 200
    assert response.json() == {"challenge": "slack-ch-1"}


def test_unknown_channel_404(client):
    response = client.post("/webhook/does_not_exist", json={})
    assert response.status_code == 404


def test_health_lists_channels(client):
    response = client.get("/webhook/health")
    assert response.status_code == 200
    assert "family_twilio" in response.json()["channels"]


# ---- cross-channel approval acceptance (PRD M2): same approval state
# machine handler receives the same Interaction from card clicks on four
# different channels and from SMS text commands. ----


class ApprovalStateMachine:
    # Consumer-side: written ONCE, channel-agnostic.

    def __init__(self):
        self.decisions: dict[str, list[str]] = {}

    def register(self, registry):
        async def on_interaction(interaction):
            code = interaction.payload.get("code") or interaction.payload.get("value") or ""
            self.decisions.setdefault(code, []).append(interaction.action)

        registry.on_interaction(on_interaction)
        registry.register_command("approve", ["同意", "Approve"], code_required=True)
        registry.register_command("reject", ["拒绝", "Reject"], code_required=True)


def test_approval_converges_across_channels(client, registry):
    state = ApprovalStateMachine()
    state.register(registry)

    # 1. SMS text command
    params = {"MessageSid": "AP-SMS", "From": "+15550000002", "Body": "同意 AP-123456"}
    assert post_twilio(client, params).status_code == 200

    # 2. Slack Block Kit button
    payload = {
        "type": "block_actions",
        "event_id": "AP-SLACK",
        "user": "U1",
        "channel": "C1",
        "actions": [{"action_id": "approve", "value": "AP-123456", "block_id": "b"}],
    }
    body = json.dumps(payload)
    timestamp = str(int(time.time()))
    assert (
        client.post(
            "/webhook/team_slack",
            content=body,
            headers={
                "Content-Type": "application/json",
                "X-Slack-Signature": slack_sign(timestamp, body),
                "X-Slack-Request-Timestamp": timestamp,
            },
        ).status_code
        == 200
    )

    # 3. DingTalk signed text command
    ts = str(int(time.time() * 1000))
    ding_payload = {
        "msgId": "AP-DT",
        "conversationId": "cid-1",
        "senderStaffId": "s1",
        "text": {"content": "拒绝 AP-123456"},
        "timestamp": ts,
        "sign": dingtalk_sign("SEC" + "a" * 40, ts),
    }
    assert (
        client.post(
            "/webhook/home_dingtalk",
            content=json.dumps(ding_payload),
            headers={"Content-Type": "application/json"},
        ).status_code
        == 200
    )

    # 4. Telegram inline keyboard callback
    tg_payload = {
        "update_id": 1,
        "callback_query": {
            "id": "cb1",
            "from": {"id": 99},
            "message": {"chat": {"id": 555, "type": "private"}},
            "data": "approve:AP-123456",
        },
    }
    assert (
        client.post(
            "/webhook/home_telegram",
            content=json.dumps(tg_payload),
            headers={
                "Content-Type": "application/json",
                "X-Telegram-Bot-Api-Secret-Token": "telegram-secret-token",
            },
        ).status_code
        == 200
    )

    # 5. WhatsApp text command (fallback path, CAN_CARDS=none)
    wa_payload = {
        "entry": [
            {
                "changes": [
                    {
                        "value": {
                            "messages": [
                                {
                                    "from": "8613800000002",
                                    "id": "AP-WA",
                                    "type": "text",
                                    "text": {"body": "同意 AP-123456"},
                                }
                            ]
                        }
                    }
                ]
            }
        ]
    }
    wa_body = json.dumps(wa_payload)
    assert (
        client.post(
            "/webhook/home_whatsapp",
            content=wa_body,
            headers={"Content-Type": "application/json", "X-Hub-Signature-256": whatsapp_sign(wa_body.encode())},
        ).status_code
        == 200
    )

    # 6. WeCom encrypted text command
    from fusion_channel.verify import wecom_encrypt, wecom_signature

    inner = "<xml><ToUserName>corp</ToUserName><FromUserName>boss</FromUserName><MsgType>text</MsgType><Content>同意 AP-123456</Content><MsgId>9</MsgId></xml>"
    encrypted = wecom_encrypt("A" * 43, inner, "corp-id")
    signature = wecom_signature("wecom-callback-token", "1700000000", "n1", encrypted)
    assert (
        client.post(
            "/webhook/home_wecom",
            content=json.dumps(
                {"encrypt": encrypted, "msg_signature": signature, "timestamp": "1700000000", "nonce": "n1"}
            ),
            headers={"Content-Type": "application/json"},
        ).status_code
        == 200
    )

    # 7. Feishu group mention command
    feishu_event = {
        "header": {"event_id": "AP-FS", "token": "feishu-verification-token"},
        "event": {
            "message": {
                "chat_id": "oc_family",
                "chat_type": "group",
                "message_type": "text",
                "content": json.dumps({"text": "同意 AP-123456"}),
                "message_id": "om_ap",
                "mentions": [{"key": "@_user_1", "id": "ou_bot", "name": "bot"}],
            },
            "sender": {"id": "ou_owner"},
        },
    }
    registry.adapters["family_feishu"].config["bot_ids"] = ["ou_bot"]
    assert (
        client.post(
            "/webhook/family_feishu",
            content=feishu_payload(feishu_event),
            headers={"Content-Type": "application/json"},
        ).status_code
        == 200
    )

    decisions = state.decisions["AP-123456"]
    assert len(decisions) == 7
    assert decisions.count("approve") == 6
    assert decisions.count("reject") == 1


# ---- outbound E2E through outbox with mock transport ----


def approval_card() -> ApprovalCard:
    return ApprovalCard(
        title="审批 AP-123456",
        code="AP-123456",
        body="新增雇员张阿姨",
        actions=[CardAction(action="approve", label="同意"), CardAction(action="reject", label="拒绝")],
    )


async def test_outbound_twilio_full_path(registry):
    captured = {}

    def handler(request: httpx.Request) -> httpx.Response:
        captured["url"] = str(request.url)
        captured["body"] = urllib.parse.parse_qs(request.content.decode())
        return httpx.Response(200, json={"sid": "SM-SENT-2"})

    registry.adapters["family_twilio"].config["http_transport"] = httpx.MockTransport(handler)
    outbox_id = await registry.send(
        "family_twilio",
        Conversation(type="direct", id="+15550000002->+15550000001"),
        card=approval_card(),
    )
    assert outbox_id is not None
    sent = await registry.outbox.flush_once()
    assert sent == 1
    assert captured["url"].startswith("https://api.twilio.com/2010-04-01/Accounts/")
    assert "AP-123456" in captured["body"]["Body"][0]
    assert "同意 AP-123456" in captured["body"]["Body"][0]


async def test_outbound_feishu_sends_interactive_card(registry):
    captured = {}

    def handler(request: httpx.Request) -> httpx.Response:
        captured["json"] = json.loads(request.content.decode())
        return httpx.Response(200, json={"code": 0, "data": {"message_id": "om-SENT"}})

    def token_handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"code": 0, "tenant_access_token": "t-xyz", "expire": 7200})

    def router(request: httpx.Request) -> httpx.Response:
        if "tenant_access_token" in str(request.url):
            return token_handler(request)
        captured["url"] = str(request.url)
        return handler(request)

    registry.adapters["family_feishu"].config["http_transport"] = httpx.MockTransport(router)
    outbox_id = await registry.send(
        "family_feishu",
        Conversation(type="group", id="oc_family"),
        card=approval_card(),
        text="请审批",
    )
    assert outbox_id is not None
    sent = await registry.outbox.flush_once()
    assert sent == 1
    assert captured["json"]["msg_type"] == "interactive"
    assert captured["json"]["content"]["header"]["title"]["content"] == "审批 AP-123456"


async def test_outbound_slack_sends_block_kit(registry):
    captured = {}

    def handler(request: httpx.Request) -> httpx.Response:
        captured["data"] = urllib.parse.parse_qs(request.content.decode())
        return httpx.Response(200, json={"ok": True, "ts": "1234.5678"})

    registry.adapters["team_slack"].config["http_transport"] = httpx.MockTransport(handler)
    outbox_id = await registry.send(
        "team_slack",
        Conversation(type="group", id="C123"),
        card=approval_card(),
    )
    assert outbox_id is not None
    sent = await registry.outbox.flush_once()
    assert sent == 1
    blocks = json.loads(captured["data"]["blocks"][0])
    assert blocks[0]["type"] == "header"
    actions = [block for block in blocks if block["type"] == "actions"][0]
    assert actions["elements"][0]["action_id"] == "approve"


async def test_test_connection_mocked(registry):
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith(".json"):
            return httpx.Response(200, json={"friendly_name": "test"})
        return httpx.Response(200, json={"ok": True, "team": "TestTeam"})

    registry.adapters["family_twilio"].config["http_transport"] = httpx.MockTransport(handler)
    result = await registry.test_connection("family_twilio")
    assert result.ok is True

    registry.adapters["team_slack"].config["http_transport"] = httpx.MockTransport(handler)
    result = await registry.test_connection("team_slack")
    assert result.ok is True


async def test_group_ops_feishu_mocked(registry):
    def router(request: httpx.Request) -> httpx.Response:
        if "tenant_access_token" in str(request.url):
            return httpx.Response(200, json={"code": 0, "tenant_access_token": "t-1", "expire": 7200})
        if "chats/" in str(request.url) and request.url.params.get("page_size"):
            return httpx.Response(200, json={"code": 0, "data": {"items": [{"chat_id": "oc_1", "name": "家庭群"}]}})
        return httpx.Response(200, json={"code": 0, "data": {"name": "家庭群"}})

    registry.adapters["family_feishu"].config["http_transport"] = httpx.MockTransport(router)
    ops = await registry.adapter("family_feishu").group_ops()
    groups = await ops.list()
    assert groups[0].group_id == "oc_1"
    assert groups[0].name == "家庭群"
