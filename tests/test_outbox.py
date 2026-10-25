from __future__ import annotations

from fusion_channel.models import ApprovalCard, Conversation, OutboundMessage, SendResult
from fusion_channel.outbox import RETRY_BACKOFF, OutboxSender, TokenBucket
from fusion_channel.storage import MemoryStore


def make_message(channel_id="family_twilio", text="hello", dedup_key=None) -> OutboundMessage:
    return OutboundMessage(
        channel_id=channel_id,
        conversation=Conversation(type="direct", id="+15550000002->+15550000001"),
        text=text,
        dedup_key=dedup_key,
    )


class FlakySender:
    def __init__(self, fail_times: int):
        self.fail_times = fail_times
        self.calls = 0

    async def __call__(self, message):
        self.calls += 1
        if self.calls <= self.fail_times:
            return SendResult(ok=False, error="simulated failure")
        return SendResult(ok=True, provider_message_id="SM-OK")


async def test_enqueue_dedup_same_window():
    store = MemoryStore()
    sender = OutboxSender(store, adapters={})
    first = await sender.enqueue(make_message())
    second = await sender.enqueue(make_message())
    assert first is not None
    assert second is None


async def test_flush_sends_pending():
    store = MemoryStore()
    sender = OutboxSender(store, adapters={})
    await sender.enqueue(make_message())
    flaky = FlakySender(fail_times=0)
    sent = await sender.flush_once(send_fn=flaky)
    assert sent == 1
    due = await store.due_outbound("9999-01-01T00:00:00+00:00")
    assert all(record.status == "sent" for record in due)


async def test_flush_retries_with_backoff():
    store = MemoryStore()
    sender = OutboxSender(store, adapters={})
    await sender.enqueue(make_message())
    flaky = FlakySender(fail_times=1)
    sent = await sender.flush_once(send_fn=flaky)
    assert sent == 0
    due = await store.due_outbound("9999-01-01T00:00:00+00:00")
    assert len(due) == 1
    record = due[0]
    assert record.attempts == 1
    assert record.status == "pending"
    # next_retry_at pushed into the future by first backoff step
    assert record.next_retry_at > record.created_at


async def test_permanent_failure_after_max_attempts():
    store = MemoryStore()
    sender = OutboxSender(store, adapters={})
    failures = []

    async def on_fail(message, error):
        failures.append((message, error))

    sender.on_permanent_failure = on_fail
    await sender.enqueue(make_message())
    flaky = FlakySender(fail_times=99)
    for _ in range(3):
        await sender.flush_once(send_fn=flaky)
        # backoff pushes next_retry_at into the future; rewind so the next
        # flush cycle picks the record up again (simulates time passing)
        for record in store.outbox.values():
            if record["status"] == "pending":
                record["next_retry_at"] = "2000-01-01T00:00:00+00:00"
    record = store.outbox[1]
    assert record["status"] == "failed"
    assert record["attempts"] == 3
    assert len(failures) == 1
    assert failures[0][1].startswith("simulated")


async def test_rate_limit_defers_send():
    store = MemoryStore()
    bucket = TokenBucket(per_minute=1)
    sender = OutboxSender(store, adapters={})
    sender._buckets["family_twilio"] = bucket
    await sender.enqueue(make_message(text="m1"))
    await sender.enqueue(make_message(text="m2"))
    flaky = FlakySender(fail_times=0)
    sent = await sender.flush_once(send_fn=flaky)
    assert sent == 1  # second message deferred by bucket


def test_token_bucket_burst_limit():
    bucket = TokenBucket(per_minute=3)
    results = [bucket.take() for _ in range(5)]
    assert results == [True, True, True, False, False]


def test_retry_backoff_schedule_matches_ai_family():
    assert RETRY_BACKOFF == [30, 120, 600]


async def test_registry_send_routes_through_outbox(registry):

    seen = []

    async def fake_send(message):
        seen.append(message)
        return SendResult(ok=True, provider_message_id="X1")

    registry.adapters["family_twilio"].send = fake_send
    outbox_id = await registry.send(
        "family_twilio",
        Conversation(type="direct", id="+15550000002->+15550000001"),
        text="提醒：明早 9 点有会",
    )
    assert outbox_id is not None
    sent = await registry.outbox.flush_once()
    assert sent == 1
    assert seen[0].text == "提醒：明早 9 点有会"


async def test_registry_send_accepts_dict_conversation(registry):
    seen = []

    async def fake_send(message):
        seen.append(message)
        return SendResult(ok=True, provider_message_id="X1")

    registry.adapters["family_twilio"].send = fake_send
    outbox_id = await registry.send(
        "family_twilio",
        {"type": "direct", "id": "+15550000002->+15550000001"},
        text="dict conversation",
    )
    assert outbox_id is not None
    sent = await registry.outbox.flush_once()
    assert sent == 1
    assert seen[0].conversation.id == "+15550000002->+15550000001"


async def test_registry_send_card_degrades_on_sms(registry):
    card = ApprovalCard(
        title="审批 AP-123456",
        code="AP-123456",
        body="新增雇员张阿姨",
    )
    outbox_id = await registry.send(
        "family_twilio",
        Conversation(type="direct", id="+15550000002->+15550000001"),
        card=card,
    )
    assert outbox_id is not None
    record = (await registry.store.due_outbound("0000-01-01T00:00:00+00:00"))[0]
    rendered = record.payload["message"]["meta"]["rendered"]
    assert rendered["used_fallback"] is True
    assert "AP-123456" in rendered["text"]
    assert rendered["card_payload"] is None
