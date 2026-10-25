from __future__ import annotations

import datetime as dt
import json

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from fusion_channel.adapters import redact_secrets
from fusion_channel.config import load_config
from fusion_channel.fastapi import MAX_BODY_BYTES, build_channel_router
from fusion_channel.hooks import AuditHook
from fusion_channel.models import ApprovalCard, CardAction, Conversation, OutboundMessage, SendResult
from fusion_channel.outbox import OutboxSender, TokenBucket
from fusion_channel.registry import ChannelRegistry, merged_rate_limits
from fusion_channel.storage import SqliteStore
from tests.conftest import twilio_sign


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


# ---- webhook hardening (negative paths) ----


def test_webhook_oversize_body_413(client):
    params = {"MessageSid": "SM-BIG", "From": "+15550000002", "Body": "x" * (MAX_BODY_BYTES + 1)}
    response = post_twilio(client, params)
    assert response.status_code == 413
    assert response.json() == {"error": "payload too large"}


def test_webhook_empty_body_unsigned_403(client, registry):
    response = client.post("/webhook/family_twilio", data={})
    assert response.status_code == 403
    assert len(registry.store.rejected) == 1


def test_webhook_invalid_json_verified_channel_rejected(client, registry):
    # Feishu: valid envelope shape but undecryptable payload — verify must
    # reject (403) and archive the rejected envelope, never 500.
    response = client.post(
        "/webhook/family_feishu",
        content=json.dumps({"encrypt": "!!!not-base64!!!"}),
        headers={"Content-Type": "application/json"},
    )
    assert response.status_code == 403
    assert registry.store.rejected
    assert registry.store.rejected[0]["reason"].startswith("invalid_signature")


def test_webhook_parse_failure_400_and_archived(client, registry):
    # verify disabled, garbage encrypted payload: parse raises — the hard
    # rule is reject-archive + 4xx, not a 500.
    registry.channels["family_feishu"].inbound.verify = "disabled"
    response = client.post(
        "/webhook/family_feishu",
        content=json.dumps({"encrypt": "!!!not-base64!!!"}),
        headers={"Content-Type": "application/json"},
    )
    assert response.status_code == 400
    assert registry.store.rejected
    assert registry.store.rejected[0]["reason"].startswith("parse_failed")


# ---- identity gate (PRD §7.1 require_bound_identity) ----


async def test_identity_gate_bound_sender_dispatches(registry):
    interactions = []
    registry.on_interaction(lambda it: interactions.append(it) or _noop())
    registry.register_command("approve", ["同意"], code_required=True)
    registry.channels["family_twilio"].require_bound_identity = True
    params = {"MessageSid": "SM-ID1", "From": "+15550000002", "Body": "同意 AP-1"}
    result = await registry.handle_inbound("family_twilio", _twilio_request(params))
    assert bool(result)
    assert len(interactions) == 1
    assert interactions[0].payload["identity_id"] == "identity:u_owner"


async def test_identity_gate_unbound_sender_403(registry):
    interactions = []
    events = []
    registry.on_interaction(lambda it: interactions.append(it) or _noop())
    registry.register_command("approve", ["同意"], code_required=True)
    registry.channels["family_twilio"].require_bound_identity = True

    async def sink(event):
        events.append(event)

    registry.audit.add_sink(sink)
    params = {"MessageSid": "SM-ID2", "From": "+15550000009", "Body": "同意 AP-1"}
    result = await registry.handle_inbound("family_twilio", _twilio_request(params))
    assert result.response.status_code == 403
    assert result.reason == "identity_not_bound"
    assert interactions == []
    assert events and events[0]["action"] == "channel.interaction_rejected"


async def _noop():
    return None


def _twilio_request(params: dict):
    import urllib.parse

    from fusion_channel.models import ChannelRequest

    body = urllib.parse.urlencode(params).encode()
    return ChannelRequest(
        url="https://example.com/webhook/family_twilio",
        headers={"content-type": "application/x-www-form-urlencoded", "x-twilio-signature": twilio_sign_url(params)},
        body=body,
        form=params,
    )


def twilio_sign_url(params: dict) -> str:
    return twilio_sign("https://example.com/webhook/family_twilio", params)


# ---- archive modes (PRD §4.3 inbound.archive) ----


async def test_archive_metadata_only_strips_content(registry):
    registry.channels["family_twilio"].inbound.archive = "metadata-only"
    params = {"MessageSid": "SM-ARC1", "From": "+15550000002", "Body": "secret content"}
    result = await registry.handle_inbound("family_twilio", _twilio_request(params))
    assert bool(result)
    archived = registry.store.inbound[("family_twilio", "SM-ARC1")]["envelope"]
    assert archived["raw"] == {}
    assert archived["text"] == ""
    assert archived["interaction"] is None
    # live envelope still carries content for handlers
    assert result.envelope.text == "secret content"


async def test_archive_off_keeps_only_dedup_record(registry):
    registry.channels["family_twilio"].inbound.archive = "off"
    params = {"MessageSid": "SM-ARC2", "From": "+15550000002", "Body": "hello"}
    first = await registry.handle_inbound("family_twilio", _twilio_request(params))
    assert bool(first)
    assert registry.store.inbound[("family_twilio", "SM-ARC2")] == {"kind": "dedup_only"}
    second = await registry.handle_inbound("family_twilio", _twilio_request(params))
    assert not bool(second)
    assert second.reason == "duplicate"


# ---- capabilities_override + rate limit wiring ----


async def test_capabilities_override_forces_fallback_render(registry):
    registry.channels["family_feishu"].capabilities_override = {"CAN_CARDS": "none"}
    card = ApprovalCard(
        title="审批 AP-123456",
        code="AP-123456",
        body="content",
        actions=[CardAction(action="approve", label="同意")],
    )
    outbox_id = await registry.send("family_feishu", Conversation(type="group", id="oc_family"), card=card)
    assert outbox_id is not None
    rendered = registry.store.outbox[outbox_id]["payload"]["message"]["meta"]["rendered"]
    assert rendered["used_fallback"] is True
    assert rendered["card_payload"] is None
    assert "AP-123456" in rendered["text"]


async def test_rate_limit_config_wires_into_outbox(memory_store):
    from fusion_channel.credentials import MemoryCredentialProvider

    creds = {
        "account_sid": "AC" + "0" * 32,
        "auth_token": "twilio-auth-token-123",
        "from_number": "+15550000001",
    }
    config = {
        "channels": [
            {
                "id": "family_twilio",
                "adapter": "twilio_sms",
                "credentials": creds,
                "rate_limit": {"per_minute": 42},
            }
        ]
    }
    root = load_config(config)
    assert merged_rate_limits(root) == {"family_twilio": 42}
    provider = MemoryCredentialProvider({f"family_twilio:{name}": value for name, value in creds.items()})
    registry = ChannelRegistry.from_config(config, credentials=provider, store=memory_store)
    assert registry.outbox.bucket_for("family_twilio").capacity == 42


def test_token_bucket_refill_proportional():
    bucket = TokenBucket(120)
    assert bucket.refill_rate == 2.0
    while bucket.take():
        pass
    assert not bucket.take()
    # Rewind the clock 15s: 2 tokens/s refill restores 30 takes.
    bucket.last_refill -= 15.0
    taken = 0
    while bucket.take():
        taken += 1
    assert taken == 30


# ---- audit hook ----


async def test_audit_hook_dispatches_and_swallows_sink_errors():
    hook = AuditHook()
    events = []

    async def good_sink(event):
        events.append(event)

    async def bad_sink(event):
        raise RuntimeError("sink down")

    hook.add_sink(good_sink)
    hook.add_sink(bad_sink)
    await hook.emit(actor="u1", action="test.action", target="x", detail={"k": "v"})
    assert events == [{"actor": "u1", "action": "test.action", "target": "x", "detail": {"k": "v"}}]


# ---- SqliteStore outbox roundtrip (real DB, not memory dict) ----


async def test_sqlite_outbox_retry_and_permanent_failure(tmp_path):
    store = SqliteStore(tmp_path / "channel.db")
    await store.init()
    try:
        outbox = OutboxSender(store, adapters={})
        failures = []
        permanent = []

        async def on_permanent(message, error):
            permanent.append((message.channel_id, error))

        outbox.on_permanent_failure = on_permanent

        async def flaky(message):
            failures.append(message.text)
            if len(failures) < 3:
                raise RuntimeError("provider 503")
            return SendResult(ok=True, provider_message_id="pm-1")

        message = OutboundMessage(
            channel_id="family_twilio",
            conversation=Conversation(type="direct", id="+15550000002"),
            text="retry me",
        )
        record_id = await outbox.enqueue(message)
        assert record_id is not None
        assert await outbox.enqueue(message) is None  # dedup on same key

        # Attempt 1: fails, schedules retry in 30s.
        assert await outbox.flush_once(send_fn=flaky) == 0
        record = (await store.due_outbound(_future_iso(3600)))[0]
        assert record.attempts == 1
        assert record.status == "pending"
        assert "provider 503" in record.last_error

        # Attempt 2: fails again (backoff expired — force due).
        await _force_due(store)
        assert await outbox.flush_once(send_fn=flaky) == 0
        record = (await store.due_outbound(_future_iso(3600)))[0]
        assert record.attempts == 2

        # Attempt 3: succeeds — record flips to sent.
        await _force_due(store)
        assert await outbox.flush_once(send_fn=flaky) == 1
        record = (await _fetch_outbox(store))[0]
        assert record.status == "sent"
        assert record.provider_message_id == "pm-1"
        assert permanent == []
    finally:
        await store.close()


async def test_sqlite_outbox_permanent_failure_hook(tmp_path):
    store = SqliteStore(tmp_path / "channel.db")
    await store.init()
    try:
        outbox = OutboxSender(store, adapters={})
        permanent = []

        async def on_permanent(message, error):
            permanent.append((message.text, error))

        outbox.on_permanent_failure = on_permanent

        async def always_fail(message):
            raise RuntimeError("hard down")

        message = OutboundMessage(
            channel_id="family_twilio",
            conversation=Conversation(type="direct", id="+15550000002"),
            text="doomed",
        )
        await outbox.enqueue(message)
        for _ in range(3):
            await _force_due(store)
            await outbox.flush_once(send_fn=always_fail)
        record = (await _fetch_outbox(store))[0]
        assert record.status == "failed"
        assert record.attempts == 3
        assert permanent and permanent[0][0] == "doomed"
        assert "hard down" in permanent[0][1]
    finally:
        await store.close()


async def test_sqlite_inbound_dedup_and_duplicate_count(tmp_path):
    store = SqliteStore(tmp_path / "channel.db")
    await store.init()
    try:
        assert await store.save_inbound("c1", "m1", {"kind": "inbound"}) is True
        assert await store.save_inbound("c1", "m1", {"kind": "inbound"}) is False
        assert await store.bump_duplicate("c1", "m1") == 1
        assert await store.bump_duplicate("c1", "m1") == 2
    finally:
        await store.close()


def _future_iso(seconds: int) -> str:
    return (dt.datetime.now(dt.UTC) + dt.timedelta(seconds=seconds)).isoformat()


async def _force_due(store: SqliteStore) -> None:
    # Rewind pending records' next_retry_at so flush_once picks them up
    # without waiting out the real backoff.
    for record in await store.due_outbound(_future_iso(3600)):
        if record.status == "pending":
            record.next_retry_at = dt.datetime.now(dt.UTC).isoformat()
            await store.update_outbound(record)


async def _fetch_outbox(store: SqliteStore) -> list:
    # due_outbound only returns pending rows; read every row directly.
    from fusion_channel.storage import OutboxRecord

    cursor = await store.db.execute("SELECT * FROM fc_outbound ORDER BY id")
    rows = await cursor.fetchall()
    return [OutboxRecord(dict(row)) for row in rows]


# ---- secret redaction ----


def test_redact_secrets_masks_tokens_and_bot_keys():
    raw = "request to https://api.telegram.org/bot123456:ABC-DEF_x/send failed; also https://oapi.dingtalk.com/robot/send?access_token=abc123xyz timed out"
    masked = redact_secrets(raw)
    assert "ABC-DEF_x" not in masked
    assert "abc123xyz" not in masked
    assert "bot***/send" in masked
    assert "access_token=***" in masked


def test_redact_secrets_noop_on_clean_text():
    assert redact_secrets("plain error message") == "plain error message"
