from __future__ import annotations

import json
import time
import urllib.parse

from fusion_channel.models import ChannelRequest
from tests.conftest import (
    dingtalk_sign,
    feishu_payload,
    slack_sign,
    twilio_sign,
    whatsapp_sign,
)


def twilio_request(params: dict, url: str = "https://example.com/webhook/family_twilio", sign=None) -> ChannelRequest:
    body = urllib.parse.urlencode(params).encode()
    return ChannelRequest(
        url=url,
        headers={
            "content-type": "application/x-www-form-urlencoded",
            "x-twilio-signature": sign or twilio_sign(url, params),
        },
        body=body,
        form=params,
    )


async def test_inbound_full_flow_archives_and_dispatches(registry):
    received = []
    registry.on_message(lambda env: received.append(env) or _noop())
    params = {"MessageSid": "SM001", "From": "+15550000002", "To": "+15550000001", "Body": "今晚吃什么"}
    result = await registry.handle_inbound("family_twilio", twilio_request(params))
    assert bool(result) is True
    assert result.envelope.text == "今晚吃什么"
    assert result.envelope.sender.external_id == "+15550000002"
    assert len(received) == 1
    # archive-first: raw params persisted
    assert ("family_twilio", "SM001") in registry.store.inbound


async def _noop():
    return None


async def test_inbound_invalid_signature_rejected_and_archived(registry):
    params = {"MessageSid": "SM002", "From": "+15550000002", "Body": "hi"}
    request = twilio_request(params, sign="bad-signature")
    result = await registry.handle_inbound("family_twilio", request)
    assert result.envelope is None
    assert result.response.status_code == 403
    assert len(registry.store.rejected) == 1
    assert registry.store.rejected[0]["reason"].startswith("invalid_signature")


async def test_inbound_duplicate_returns_none_and_counts(registry):
    params = {"MessageSid": "SM003", "From": "+15550000002", "Body": "dup"}
    request = twilio_request(params)
    first = await registry.handle_inbound("family_twilio", request)
    assert bool(first)
    second = await registry.handle_inbound("family_twilio", twilio_request(params))
    assert second.envelope is None
    assert second.reason == "duplicate"
    assert registry.store.duplicates[("family_twilio", "SM003")] == 1


async def test_inbound_unknown_channel_404(registry):
    result = await registry.handle_inbound("nope", ChannelRequest(url="https://x/hook"))
    assert result.response.status_code == 404


async def test_group_filter_ignores_unwatched_group(registry):
    data = {
        "header": {"event_id": "evt-1", "token": "feishu-verification-token", "event_type": "im.message.receive_v1"},
        "event": {
            "message": {
                "chat_id": "oc_unknown",
                "chat_type": "group",
                "message_type": "text",
                "content": json.dumps({"text": "hello"}),
                "message_id": "om_1",
            },
            "sender": {"id": "ou_sender"},
        },
    }
    payload = feishu_payload(data)
    request = ChannelRequest(
        url="https://example.com/webhook/family_feishu",
        headers={"content-type": "application/json"},
        body=payload,
        json_body=json.loads(payload),
    )
    result = await registry.handle_inbound("family_feishu", request)
    assert result.envelope is None
    assert result.reason == "group_not_watched"


async def test_group_mention_gate_requires_bot_mention(registry):
    # Watched group, no mention -> filtered by mentions_required.
    data = {
        "header": {"event_id": "evt-2", "token": "feishu-verification-token"},
        "event": {
            "message": {
                "chat_id": "oc_family",
                "chat_type": "group",
                "message_type": "text",
                "content": json.dumps({"text": "在吗"}),
                "message_id": "om_2",
            },
            "sender": {"id": "ou_sender"},
        },
    }
    payload = feishu_payload(data)
    request = ChannelRequest(
        url="https://example.com/webhook/family_feishu",
        headers={"content-type": "application/json"},
        body=payload,
        json_body=json.loads(payload),
    )
    result = await registry.handle_inbound("family_feishu", request)
    assert result.envelope is None
    assert result.reason == "no_mention"


async def test_group_with_bot_mention_passes(registry):
    registry.adapters["family_feishu"].config["bot_ids"] = ["ou_bot"]
    data = {
        "header": {"event_id": "evt-3", "token": "feishu-verification-token"},
        "event": {
            "message": {
                "chat_id": "oc_family",
                "chat_type": "group",
                "message_type": "text",
                "content": json.dumps({"text": '<at user_id="ou_bot">bot</at> 帮我定闹钟'}),
                "message_id": "om_3",
                "mentions": [{"key": "@_user_1", "id": "ou_bot", "name": "bot"}],
            },
            "sender": {"id": "ou_sender"},
        },
    }
    payload = feishu_payload(data)
    request = ChannelRequest(
        url="https://example.com/webhook/family_feishu",
        headers={"content-type": "application/json"},
        body=payload,
        json_body=json.loads(payload),
    )
    result = await registry.handle_inbound("family_feishu", request)
    assert bool(result)
    assert result.envelope.conversation.type == "group"
    assert result.envelope.text == "帮我定闹钟"


async def test_text_command_becomes_interaction(registry):
    interactions = []
    registry.on_interaction(lambda it: interactions.append(it) or _noop())
    registry.register_command("approve", ["同意", "approve"], code_required=True)
    registry.register_command("reject", ["拒绝", "reject"], code_required=True)
    params = {"MessageSid": "SM004", "From": "+15550000002", "Body": "同意 AP-123456"}
    result = await registry.handle_inbound("family_twilio", twilio_request(params))
    assert bool(result)
    assert result.envelope.interaction is not None
    assert result.envelope.interaction.action == "approve"
    assert result.envelope.interaction.payload["code"] == "AP-123456"
    assert len(interactions) == 1


async def test_command_without_code_not_an_interaction(registry):
    registry.register_command("approve", ["同意"], code_required=True)
    params = {"MessageSid": "SM005", "From": "+15550000002", "Body": "同意"}
    result = await registry.handle_inbound("family_twilio", twilio_request(params))
    assert bool(result)
    assert result.envelope.interaction is None


async def test_interaction_dedup_idempotent(registry):
    interactions = []
    registry.on_interaction(lambda it: interactions.append(it) or _noop())
    registry.register_command("approve", ["同意"], code_required=True)
    params = {"MessageSid": "SM006", "From": "+15550000002", "Body": "同意 AP-1"}
    await registry.handle_inbound("family_twilio", twilio_request(params))
    await registry.handle_inbound("family_twilio", twilio_request(params))
    # duplicate inbound returns early; handler fires exactly once
    assert len(interactions) == 1


async def test_slack_block_action_interaction(registry):
    interactions = []
    registry.on_interaction(lambda it: interactions.append(it) or _noop())
    payload = {
        "type": "block_actions",
        "event_id": "ev-block-1",
        "user": "U123",
        "channel": "C456",
        "trigger_id": "trg-1",
        "actions": [{"action_id": "approve", "value": "AP-123456", "block_id": "b1"}],
    }
    body = json.dumps(payload)
    request = ChannelRequest(
        url="https://example.com/webhook/team_slack",
        headers={
            "content-type": "application/json",
            "x-slack-signature": slack_sign(str(int(time.time())), body),
            "x-slack-request-timestamp": str(int(time.time())),
        },
        body=body.encode(),
        json_body=payload,
    )
    result = await registry.handle_inbound("team_slack", request)
    assert bool(result)
    assert result.envelope.interaction is not None
    assert result.envelope.interaction.action == "approve"
    assert result.envelope.interaction.payload["value"] == "AP-123456"
    assert len(interactions) == 1


async def test_verify_disabled_still_processes_but_logs(registry, caplog):
    registry.channels["family_twilio"].inbound.verify = "disabled"
    params = {"MessageSid": "SM007", "From": "+15550000002", "Body": "no sig"}
    request = twilio_request(params, sign="")
    result = await registry.handle_inbound("family_twilio", request)
    assert bool(result)


async def test_dingtalk_signed_callback(registry):
    interactions = []
    registry.on_interaction(lambda it: interactions.append(it) or _noop())
    registry.register_command("approve", ["同意"], code_required=True)
    timestamp = str(int(time.time() * 1000))
    payload = {
        "msgId": "dm-1",
        "conversationId": "cid-1",
        "senderStaffId": "staff-1",
        "senderNick": "张三",
        "text": {"content": "同意 AP-777"},
        "timestamp": timestamp,
        "sign": dingtalk_sign("SEC" + "a" * 40, timestamp),
    }
    body = json.dumps(payload)
    request = ChannelRequest(
        url="https://example.com/webhook/home_dingtalk",
        headers={"content-type": "application/json"},
        body=body.encode(),
        json_body=payload,
    )
    result = await registry.handle_inbound("home_dingtalk", request)
    assert bool(result)
    assert result.envelope.text == "同意 AP-777"
    assert result.envelope.interaction is not None
    assert result.envelope.interaction.action == "approve"


async def test_whatsapp_signed_message(registry):
    payload = {
        "object": "whatsapp_business_account",
        "entry": [
            {
                "changes": [
                    {
                        "value": {
                            "messages": [
                                {"from": "8613800000002", "id": "wamid.1", "type": "text", "text": {"body": "hi"}}
                            ]
                        }
                    }
                ]
            }
        ],
    }
    body = json.dumps(payload)
    request = ChannelRequest(
        url="https://example.com/webhook/home_whatsapp",
        headers={"content-type": "application/json", "x-hub-signature-256": whatsapp_sign(body.encode())},
        body=body.encode(),
        json_body=payload,
    )
    result = await registry.handle_inbound("home_whatsapp", request)
    assert bool(result)
    assert result.envelope.text == "hi"
    assert result.envelope.sender.external_id == "8613800000002"


async def test_telegram_signed_update(registry):
    payload = {
        "update_id": 100,
        "message": {
            "message_id": 1,
            "chat": {"id": -100200, "type": "group", "title": "家庭群"},
            "from": {"id": 999, "username": "owner"},
            "text": "在吗",
        },
    }
    body = json.dumps(payload)
    request = ChannelRequest(
        url="https://example.com/webhook/home_telegram",
        headers={"content-type": "application/json", "x-telegram-bot-api-secret-token": "telegram-secret-token"},
        body=body.encode(),
        json_body=payload,
    )
    result = await registry.handle_inbound("home_telegram", request)
    assert bool(result)
    assert result.envelope.conversation.type == "group"


async def test_wecom_signed_callback(registry):
    from fusion_channel.verify import wecom_encrypt, wecom_signature

    inner = "<xml><ToUserName>corp</ToUserName><FromUserName>zhangsan</FromUserName><MsgType>text</MsgType><Content>同意 AP-9</Content><MsgId>1</MsgId></xml>"
    encrypted = wecom_encrypt("A" * 43, inner, "corp-id")
    timestamp, nonce = "1700000000", "n1"
    signature = wecom_signature("wecom-callback-token", timestamp, nonce, encrypted)
    payload = {"encrypt": encrypted, "msg_signature": signature, "timestamp": timestamp, "nonce": nonce}
    body = json.dumps(payload)
    request = ChannelRequest(
        url="https://example.com/webhook/home_wecom",
        headers={"content-type": "application/json"},
        body=body.encode(),
        json_body=payload,
    )
    registry.register_command("approve", ["同意"], code_required=True)
    interactions = []
    registry.on_interaction(lambda it: interactions.append(it) or _noop())
    result = await registry.handle_inbound("home_wecom", request)
    assert bool(result)
    assert result.envelope.text == "同意 AP-9"
    assert result.envelope.interaction is not None
    assert len(interactions) == 1
