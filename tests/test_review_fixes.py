from __future__ import annotations

import json
import time
import urllib.parse

import httpx
import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from fusion_channel.adapters.tencent_sms import TencentSmsAdapter
from fusion_channel.fastapi import build_channel_router
from fusion_channel.identity import IdentityMapper
from fusion_channel.models import ChannelRequest, Conversation, OutboundMessage
from fusion_channel.outbox import OutboxSender
from fusion_channel.storage import MemoryStore
from tests.conftest import (
    DINGTALK_SECRET,
    FEISHU_VERIFY_TOKEN,
    TWILIO_TOKEN,
    WECOM_AES_KEY,
    WECOM_TOKEN,
    dingtalk_sign,
    feishu_payload,
    slack_sign,
    twilio_sign,
)


async def _noop():
    return None


@pytest.fixture
def client(registry):
    app = FastAPI()
    app.include_router(build_channel_router(registry))
    return TestClient(app)


# ---- slack: real block_actions shape (user object, no event_id) ----


async def test_slack_block_actions_user_object_and_trigger_id(registry):
    payload = {
        "type": "block_actions",
        "trigger_id": "trigger-77",
        "user": {"id": "U42", "name": "owner"},
        "channel": {"id": "C7"},
        "actions": [{"action_id": "approve", "value": "AP-123456", "block_id": "b1"}],
    }
    body = json.dumps(payload)
    ts = str(int(time.time()))
    request = ChannelRequest(
        url="https://example.com/webhook/team_slack",
        headers={
            "content-type": "application/json",
            "x-slack-signature": slack_sign(ts, body),
            "x-slack-request-timestamp": ts,
        },
        body=body.encode(),
        json_body=payload,
    )
    interactions = []
    registry.on_interaction(lambda it: interactions.append(it) or _noop())
    result = await registry.handle_inbound("team_slack", request)
    assert bool(result)
    assert result.envelope.message_id == "trigger-77"
    assert result.envelope.sender.external_id == "U42"
    assert len(interactions) == 1
    assert interactions[0].action == "approve"
    assert interactions[0].payload["code"] == "AP-123456"


async def test_slack_messages_without_event_id_get_distinct_synthetic_ids(registry):
    # Two message events lacking event_id must not collapse into one dedup hit.
    received = []
    registry.on_message(lambda env: received.append(env) or _noop())
    for ts in ("111.0001", "111.0002"):
        payload = {
            "type": "event_callback",
            "event": {"type": "message", "text": f"m{ts}", "user": "U1", "channel": "C1", "ts": ts},
        }
        body = json.dumps(payload)
        ts = str(int(time.time()))
        request = ChannelRequest(
            url="https://example.com/webhook/team_slack",
            headers={
                "content-type": "application/json",
                "x-slack-signature": slack_sign(ts, body),
                "x-slack-request-timestamp": ts,
            },
            body=body.encode(),
            json_body=payload,
        )
        result = await registry.handle_inbound("team_slack", request)
        assert bool(result)
        assert result.reason != "duplicate"
    assert len(received) == 2
    assert received[0].message_id != received[1].message_id


# ---- dingtalk: no msgId, sessionId must not mute the group ----


async def test_dingtalk_missing_msg_id_does_not_mute_session(registry):
    received = []
    registry.on_message(lambda env: received.append(env) or _noop())
    for content in ("第一条", "第二条"):
        payload = {
            "text": {"content": content},
            "senderStaffId": "staff-1",
            "conversationId": "cid-same",
            "sessionId": "cid-same",
            "timestamp": str(int(time.time() * 1000)),
            "sign": dingtalk_sign(DINGTALK_SECRET, int(time.time() * 1000)),
        }
        body = json.dumps(payload)
        request = ChannelRequest(
            url="https://example.com/webhook/home_dingtalk",
            headers={"content-type": "application/json"},
            body=body.encode(),
            json_body=payload,
        )
        result = await registry.handle_inbound("home_dingtalk", request)
        assert result.reason != "duplicate"
    assert [env.text for env in received] == ["第一条", "第二条"]


async def test_dingtalk_test_connection_is_static():
    from fusion_channel.adapters.dingtalk_robot import DingTalkRobotAdapter

    adapter = DingTalkRobotAdapter(
        "home_dingtalk",
        {"access_token": "tok", "secret": "SEC" + "a" * 40},
        {"http_transport": httpx.MockTransport(lambda request: httpx.Response(200, json={"errcode": 0}))},
    )
    result = await adapter.test_connection()
    assert result.ok
    # No HTTP call was made — the transport would have recorded one otherwise.
    assert "static check" in result.detail


# ---- wecom: XML body + query params, MsgId-only, GET echostr ----


def _wecom_callback(inner: str, msg_id: str = "1") -> tuple[str, dict]:
    from fusion_channel.verify import wecom_encrypt, wecom_signature

    xml = f"<xml><ToUserName>corp</ToUserName><FromUserName>zhangsan</FromUserName><MsgType>text</MsgType><Content>同意 AP-9</Content><MsgId>{msg_id}</MsgId></xml>"
    encrypted = wecom_encrypt(WECOM_AES_KEY, xml, "corp-id")
    timestamp, nonce = str(int(time.time())), "n1"
    signature = wecom_signature(WECOM_TOKEN, timestamp, nonce, encrypted)
    query = urllib.parse.urlencode({"msg_signature": signature, "timestamp": timestamp, "nonce": nonce})
    return encrypted, {"msg_signature": signature, "timestamp": timestamp, "nonce": nonce, "query": query}


async def test_wecom_real_xml_callback_with_query_params(registry):
    encrypted, ctx = _wecom_callback("text")
    request = ChannelRequest(
        url=f"https://example.com/webhook/home_wecom?{ctx['query']}",
        headers={"content-type": "text/xml"},
        body=f"<xml><Encrypt>{encrypted}</Encrypt></xml>".encode(),
    )
    received = []
    registry.on_message(lambda env: received.append(env) or _noop())
    result = await registry.handle_inbound("home_wecom", request)
    assert bool(result)
    assert result.envelope.text == "同意 AP-9"


async def test_wecom_event_without_msg_id_distinct_synthetic_ids(registry):
    # Event callbacks (subscribe etc.) carry no MsgId; CreateTime-second
    # fallback used to collide — synthetic ids must stay distinct.
    from fusion_channel.verify import wecom_encrypt, wecom_signature

    received = []
    registry.on_message(lambda env: received.append(env) or _noop())
    for event in ("subscribe", "enter_agent"):
        xml = f"<xml><ToUserName>corp</ToUserName><FromUserName>u1</FromUserName><MsgType>event</MsgType><Event>{event}</Event><CreateTime>1700000000</CreateTime></xml>"
        encrypted = wecom_encrypt(WECOM_AES_KEY, xml, "corp-id")
        timestamp, nonce = str(int(time.time())), "n2"
        signature = wecom_signature(WECOM_TOKEN, timestamp, nonce, encrypted)
        query = urllib.parse.urlencode({"msg_signature": signature, "timestamp": timestamp, "nonce": nonce})
        request = ChannelRequest(
            url=f"https://example.com/webhook/home_wecom?{query}",
            headers={"content-type": "text/xml"},
            body=f"<xml><Encrypt>{encrypted}</Encrypt></xml>".encode(),
        )
        result = await registry.handle_inbound("home_wecom", request)
        assert result.reason != "duplicate"
    assert len(received) == 2


def test_wecom_get_echostr_handshake(client):
    from fusion_channel.verify import wecom_decrypt, wecom_encrypt, wecom_signature

    plain = "echo-plain-42"
    echostr = wecom_encrypt(WECOM_AES_KEY, plain, "corp-id")
    timestamp, nonce = str(int(time.time())), "n3"
    signature = wecom_signature(WECOM_TOKEN, timestamp, nonce, echostr)
    resp = client.get(
        f"/webhook/home_wecom?msg_signature={urllib.parse.quote(signature)}&timestamp={timestamp}&nonce={nonce}&echostr={urllib.parse.quote(echostr)}"
    )
    assert resp.status_code == 200
    assert wecom_decrypt(WECOM_AES_KEY, echostr, expected_receive_id="corp-id").decode() == plain
    assert resp.text == plain


def test_wecom_get_echostr_bad_signature_rejected(client):
    resp = client.get("/webhook/home_wecom?msg_signature=bad&timestamp=1&nonce=2&echostr=zz")
    assert resp.status_code == 403


def test_wecom_get_default_adapter_405():
    # Adapters without a GET handshake return the SPI default 405.
    from fusion_channel.adapters.feishu_app import FeishuAppAdapter

    adapter = FeishuAppAdapter("f", {"verification_token": "t"}, {})
    response = adapter.handle_verification_get(ChannelRequest(url="https://x/webhook/f"))
    assert response.status_code == 405


# ---- whatsapp: GET hub.challenge + status receipts ----


def test_whatsapp_get_hub_challenge(client):
    resp = client.get("/webhook/home_whatsapp?hub.mode=subscribe&hub.verify_token=wa-verify&hub.challenge=ch-99")
    assert resp.status_code == 200
    assert resp.text == "ch-99"


def test_whatsapp_get_hub_challenge_bad_token(client):
    resp = client.get("/webhook/home_whatsapp?hub.mode=subscribe&hub.verify_token=wrong&hub.challenge=ch-99")
    assert resp.status_code == 403


def test_get_unknown_channel_404(client):
    resp = client.get("/webhook/nope?hub.challenge=1")
    assert resp.status_code == 404


async def test_whatsapp_status_webhook_is_receipt_not_message(registry):
    payload = {
        "entry": [
            {"changes": [{"value": {"statuses": [{"id": "wamid.X", "status": "delivered"}]}, "field": "messages"}]}
        ]
    }
    body = json.dumps(payload)
    from tests.conftest import whatsapp_sign

    request = ChannelRequest(
        url="https://example.com/webhook/home_whatsapp",
        headers={"content-type": "application/json", "x-hub-signature-256": whatsapp_sign(body.encode())},
        body=body.encode(),
        json_body=payload,
    )
    received = []
    registry.on_message(lambda env: received.append(env) or _noop())
    result = await registry.handle_inbound("home_whatsapp", request)
    assert result.reason == "receipt"
    assert received == []
    assert registry.store.receipts and registry.store.receipts[-1]["status"] == "delivered"


# ---- twilio: MessageStatus receipt + external_url override ----


async def test_twilio_status_callback_is_receipt(registry):
    params = {"MessageSid": "SM9", "From": "+15550000002", "To": "+15550000001", "MessageStatus": "delivered"}
    request = ChannelRequest(
        url="https://example.com/webhook/family_twilio",
        headers={
            "content-type": "application/x-www-form-urlencoded",
            "X-Twilio-Signature": twilio_sign("https://example.com/webhook/family_twilio", params),
        },
        body=urllib.parse.urlencode(params).encode(),
        form=params,
    )
    received = []
    registry.on_message(lambda env: received.append(env) or _noop())
    result = await registry.handle_inbound("family_twilio", request)
    assert result.reason == "receipt"
    assert received == []
    assert registry.store.receipts[-1]["message_id"] == "SM9"


async def test_twilio_external_url_override():
    from fusion_channel.adapters.twilio_sms import TwilioSmsAdapter

    public_url = "https://public.example.com/webhook/family_twilio"
    adapter = TwilioSmsAdapter(
        "family_twilio",
        {"account_sid": "AC" + "0" * 32, "auth_token": TWILIO_TOKEN, "from_number": "+15550000001"},
        {"external_url": public_url},
    )
    params = {"MessageSid": "SM1", "From": "+15550000002", "To": "+15550000001", "Body": "hi"}
    request = ChannelRequest(
        url="http://internal:8000/webhook/family_twilio",
        headers={"X-Twilio-Signature": twilio_sign(public_url, params)},
        form=params,
    )
    assert adapter.verify_request(request).ok


# ---- feishu: challenge retries bypass dedup + card.action.trigger ----


async def test_feishu_challenge_echoes_on_every_retry(registry):
    challenge = {"type": "url_verification", "token": FEISHU_VERIFY_TOKEN, "challenge": "ch-1"}
    body = feishu_payload(challenge)
    responses = []
    for _ in range(2):
        request = ChannelRequest(
            url="https://example.com/webhook/family_feishu",
            headers={"content-type": "application/json"},
            body=body,
            json_body=json.loads(body),
        )
        result = await registry.handle_inbound("family_feishu", request)
        assert result.reason == "challenge"
        responses.append(result.response)
    assert json.loads(responses[0].body)["challenge"] == "ch-1"
    assert json.loads(responses[1].body)["challenge"] == "ch-1"


async def test_feishu_card_action_trigger_interaction(registry):
    event = {
        "header": {"event_id": "evt-card-1", "event_type": "card.action.trigger", "token": FEISHU_VERIFY_TOKEN},
        "event": {
            "operator": {"operator_id": {"open_id": "ou_owner"}},
            "action": {"value": {"action": "approve", "code": "AP-123456"}, "tag": "button"},
            "delivery_id": "dlv-1",
        },
    }
    body = feishu_payload(event)
    interactions = []
    registry.on_interaction(lambda it: interactions.append(it) or _noop())
    request = ChannelRequest(
        url="https://example.com/webhook/family_feishu",
        headers={"content-type": "application/json"},
        body=body,
        json_body=json.loads(body),
    )
    result = await registry.handle_inbound("family_feishu", request)
    assert bool(result)
    assert result.reason != "duplicate"
    assert len(interactions) == 1
    assert interactions[0].action == "approve"
    assert interactions[0].payload["code"] == "AP-123456"
    assert interactions[0].actor.external_id == "ou_owner"


# ---- fastapi catch-all: internal error never 500 ----


def test_fastapi_internal_error_reject_archives_and_400(client, registry, monkeypatch):
    params = {"MessageSid": "SM2", "From": "+15550000002", "To": "+15550000001", "Body": "boom"}
    url = "http://testserver/webhook/family_twilio"

    async def exploding(envelope):
        raise RuntimeError("archive exploded")

    monkeypatch.setattr(registry.archive, "on_inbound_dedup", exploding)
    resp = client.post("/webhook/family_twilio", data=params, headers={"X-Twilio-Signature": twilio_sign(url, params)})
    assert resp.status_code == 400
    assert registry.store.rejected and registry.store.rejected[-1]["reason"].startswith("internal_error")


# ---- outbox: CAS claim, deferral bump, aging, unknown channel hook ----


async def test_outbox_claim_prevents_double_send():
    store = MemoryStore()
    sent = []

    async def send_fn(message):
        sent.append(message)
        from fusion_channel.models import SendResult

        return SendResult(ok=True, provider_message_id="p1")

    outbox = OutboxSender(store, adapters={})
    message = OutboundMessage(channel_id="c", conversation=Conversation(type="direct", id="x"), text="hi")
    record_id = await outbox.enqueue(message)
    # Another worker claims it first; flush_once must skip, not resend.
    assert await store.claim_outbound(record_id, "2999-01-01T00:00:00+00:00")
    assert await outbox.flush_once(send_fn) == 0
    assert sent == []


async def test_outbox_stale_sending_requeued_after_lease():
    store = MemoryStore()
    sent = []

    async def send_fn(message):
        sent.append(message)
        from fusion_channel.models import SendResult

        return SendResult(ok=True, provider_message_id="p1")

    outbox = OutboxSender(store, adapters={})
    message = OutboundMessage(channel_id="c", conversation=Conversation(type="direct", id="x"), text="hi")
    record_id = await outbox.enqueue(message)
    # Crashed mid-send: lease long expired — next flush recovers it.
    row = store.outbox[record_id]
    row["status"] = "sending"
    row["next_retry_at"] = "2000-01-01T00:00:00+00:00"
    assert await outbox.flush_once(send_fn) == 1
    assert len(sent) == 1
    assert store.outbox[record_id]["status"] == "sent"


async def test_outbox_rate_limit_deferral_bumps_next_retry():
    store = MemoryStore()
    outbox = OutboxSender(store, adapters={}, rate_limits={"c": 1})
    from fusion_channel.models import SendResult

    calls = []

    async def send_fn(message):
        calls.append(message)
        return SendResult(ok=True, provider_message_id="p")

    for i in range(2):
        await outbox.enqueue(
            OutboundMessage(channel_id="c", conversation=Conversation(type="direct", id=f"x{i}"), text="hi")
        )
    await outbox.flush_once(send_fn)
    assert len(calls) == 1
    deferred_row = store.outbox[2]
    assert deferred_row["status"] == "pending"
    # Bumped into the future, not parked at the head of the due queue.
    assert deferred_row["next_retry_at"] > "2000-01-01T00:00:00+00:00"


async def test_outbox_aged_out_pending_fires_permanent_failure():
    store = MemoryStore()
    outbox = OutboxSender(store, adapters={})
    failures = []

    async def on_fail(message, error):
        failures.append((message, error))

    outbox.on_permanent_failure = on_fail
    message = OutboundMessage(channel_id="c", conversation=Conversation(type="direct", id="x"), text="hi")
    record_id = await outbox.enqueue(message)
    store.outbox[record_id]["created_at"] = "2020-01-01T00:00:00+00:00"

    async def send_fn(msg):
        raise AssertionError("aged-out record must never send")

    assert await outbox.flush_once(send_fn) == 0
    assert store.outbox[record_id]["status"] == "failed"
    assert failures and failures[0][1] == "max age exceeded while pending"


async def test_outbox_unknown_channel_fires_permanent_failure():
    store = MemoryStore()
    outbox = OutboxSender(store, adapters={})
    failures = []

    async def on_fail(message, error):
        failures.append((message, error))

    outbox.on_permanent_failure = on_fail
    await outbox.enqueue(
        OutboundMessage(channel_id="ghost", conversation=Conversation(type="direct", id="x"), text="hi")
    )
    assert await outbox.flush_once() == 0
    assert failures and "unknown channel ghost" in failures[0][1]


# ---- provider protocol details ----


async def test_tencent_phone_normalization():
    captured = {}

    def handler(request: httpx.Request) -> httpx.Response:
        captured["body"] = json.loads(request.content)
        return httpx.Response(200, json={"Response": {"SendStatusSet": [{"Code": "Ok", "SerialNo": "s1"}]}})

    creds = {
        "secret_id": "id",
        "secret_key": "key",
        "sdk_app_id": "1400000000",
        "sign_name": "s",
        "template_id": "1",
    }
    adapter = TencentSmsAdapter("sms_tencent", creds, {"http_transport": httpx.MockTransport(handler)})
    await adapter.send(
        OutboundMessage(
            channel_id="sms_tencent", conversation=Conversation(type="direct", id="+8613800000002"), text="hi"
        )
    )
    assert captured["body"]["PhoneNumberSet"] == ["+8613800000002"]
    await adapter.send(
        OutboundMessage(channel_id="sms_tencent", conversation=Conversation(type="direct", id="13800000002"), text="hi")
    )
    assert captured["body"]["PhoneNumberSet"] == ["+8613800000002"]


async def test_telegram_mention_utf16_offsets():
    from fusion_channel.adapters.telegram_bot import TelegramBotAdapter

    adapter = TelegramBotAdapter("tg", {"bot_token": "1:A", "secret_token": "s"}, {})
    # Emoji before the mention: UTF-16 offset != str index.
    text = "😀 @mybot hello"
    payload = {
        "update_id": 1,
        "message": {
            "message_id": 5,
            "chat": {"id": 10, "type": "group"},
            "from": {"id": 20},
            "text": text,
            "entities": [{"type": "mention", "offset": 3, "length": 6}],
        },
    }
    envelope = adapter.parse_inbound(
        ChannelRequest(url="https://x", body=json.dumps(payload).encode(), json_body=payload)
    )
    assert envelope.mentions[0].external_id == "mybot"


def test_identity_rebind_drops_stale_reverse_entry():
    mapper = IdentityMapper()
    mapper.bind_identity("identity:a", "team_slack", "U1")
    mapper.bind_identity("identity:b", "team_slack", "U1")
    assert mapper.bindings_of("identity:a") == []
    assert mapper.identify("team_slack", "U1") == "identity:b"
    assert mapper.bindings_of("identity:b") == [("team_slack", "U1")]


def test_credentials_put_leaves_no_tmp_files(tmp_path):
    from fusion_channel.credentials import AESGCMCredentialProvider

    provider = AESGCMCredentialProvider(tmp_path)
    provider.put("secret_a", "value-1")
    provider.put("secret_a", "value-2")
    provider.put("secret_b", "value-3")
    leftovers = [p.name for p in (tmp_path / "secrets").iterdir() if p.name.endswith(".tmp") or ".tmp" in p.name]
    assert leftovers == []
    assert provider.get("secret_a") == "value-2"
    assert provider.get("secret_b") == "value-3"
