from __future__ import annotations

import asyncio
from email import policy
from email.message import EmailMessage
from email.parser import BytesParser

from fusion_channel.adapters.email_imap import EmailImapAdapter
from fusion_channel.models import ApprovalCard, CardAction, ChannelRequest, OutboundMessage
from fusion_channel.registry import ChannelRegistry
from fusion_channel.storage import MemoryStore

CREDS = {"host": "mail.corp.com", "username": "bot@corp.com", "password": "bot-password"}


def request_from(raw: bytes) -> ChannelRequest:
    return ChannelRequest(method="POST", url="imap://mail.corp.com/INBOX", body=raw)


def parse_mime(mime_bytes: bytes) -> EmailMessage:
    return BytesParser(policy=policy.default).parsebytes(mime_bytes)


def build_email(
    message_id: str = "<m-1@example.com>",
    from_addr: str = "Boss <boss@example.com>",
    to: str = "bot@corp.com",
    subject: str = "审批请求",
    body: str = "同意 AP-123456",
    reply_to: str | None = None,
    attach: tuple[str, bytes] | None = None,
) -> bytes:
    msg = EmailMessage()
    msg["Message-ID"] = message_id
    msg["From"] = from_addr
    msg["To"] = to
    msg["Subject"] = subject
    if reply_to:
        msg["In-Reply-To"] = reply_to
    msg.set_content(body)
    if attach:
        msg.add_attachment(attach[1], maintype="application", subtype="octet-stream", filename=attach[0])
    return msg.as_bytes()


def email_registry(imap_fetch=None, smtp_send=None) -> ChannelRegistry:
    config: dict = {
        "channels": [
            {
                "id": "ops_email",
                "adapter": "email_imap",
                "credentials": dict(CREDS),
            }
        ]
    }
    extra = {}
    if imap_fetch is not None:
        extra["imap_fetch"] = imap_fetch
    if smtp_send is not None:
        extra["smtp_send"] = smtp_send
    config["channels"][0].update(extra)
    return ChannelRegistry.from_config(config, store=MemoryStore())


def test_parse_inbound_basic():
    adapter = EmailImapAdapter("ops_email", dict(CREDS), {})
    envelope = adapter.parse_inbound(request_from(build_email()))
    assert envelope.message_id == "<m-1@example.com>"
    assert envelope.sender.external_id == "boss@example.com"
    assert envelope.sender.display_name == "Boss"
    assert envelope.text.strip() == "同意 AP-123456"
    assert envelope.conversation.type == "direct"
    assert envelope.conversation.id == "boss@example.com"
    assert envelope.raw["subject"] == "审批请求"


def test_parse_inbound_attachment_and_thread():
    adapter = EmailImapAdapter("ops_email", dict(CREDS), {})
    raw = build_email(message_id="<m-2@example.com>", reply_to="<orig@example.com>", attach=("report.pdf", b"%PDF-1.4"))
    envelope = adapter.parse_inbound(request_from(raw))
    assert envelope.conversation.thread_id == "<orig@example.com>"
    assert len(envelope.media) == 1
    assert envelope.media[0].media_id == "report.pdf"
    assert envelope.media[0].mime_type == "application/octet-stream"
    assert envelope.media[0].size == len(b"%PDF-1.4")


def test_parse_inbound_missing_message_id():
    adapter = EmailImapAdapter("ops_email", dict(CREDS), {})
    raw = build_email(message_id="")
    envelope = adapter.parse_inbound(request_from(raw))
    assert envelope.message_id == ""


def test_verify_always_ok():
    adapter = EmailImapAdapter("ops_email", dict(CREDS), {})
    assert adapter.verify_request(request_from(b"")).ok


async def test_send_via_smtp_fake():
    sent: list[tuple[str, str, bytes]] = []

    def fake_smtp(from_addr, to_addr, mime_bytes):
        sent.append((from_addr, to_addr, mime_bytes))

    adapter = EmailImapAdapter("ops_email", dict(CREDS), {"smtp_send": fake_smtp})
    message = OutboundMessage(
        channel_id="ops_email",
        conversation={"type": "direct", "id": "boss@example.com"},
        text="审批 AP-123456 已通过",
        meta={"subject": "审批结果 AP-123456", "rendered": {"text": "审批 AP-123456 已通过"}},
        reply_to="<m-1@example.com>",
    )
    result = await adapter.send(message)
    assert result.ok
    assert len(sent) == 1
    from_addr, to_addr, mime = sent[0]
    assert from_addr == "bot@corp.com"
    assert to_addr == "boss@example.com"
    parsed = parse_mime(mime)
    assert parsed["Subject"] == "审批结果 AP-123456"
    assert parsed["In-Reply-To"] == "<m-1@example.com>"
    assert parsed["References"] == "<m-1@example.com>"
    assert "审批 AP-123456 已通过" in parsed.get_content()


async def test_send_subject_defaults_to_first_line():
    sent: list[bytes] = []

    def fake_smtp(from_addr, to_addr, mime_bytes):
        sent.append(mime_bytes)

    adapter = EmailImapAdapter("ops_email", dict(CREDS), {"smtp_send": fake_smtp})
    message = OutboundMessage(
        channel_id="ops_email",
        conversation={"type": "direct", "id": "boss@example.com"},
        text="第一行当标题\n第二行内容",
    )
    result = await adapter.send(message)
    assert result.ok
    assert parse_mime(sent[0])["Subject"] == "第一行当标题"


async def test_send_missing_credentials():
    adapter = EmailImapAdapter("ops_email", {"host": "", "username": "", "password": ""}, {})
    message = OutboundMessage(channel_id="ops_email", conversation={"type": "direct", "id": "x@y.com"}, text="hi")
    result = await adapter.send(message)
    assert not result.ok
    assert "credentials not configured" in result.error


async def test_smtp_error_surface():
    def broken_smtp(from_addr, to_addr, mime_bytes):
        raise RuntimeError("connection refused")

    adapter = EmailImapAdapter("ops_email", dict(CREDS), {"smtp_send": broken_smtp})
    message = OutboundMessage(channel_id="ops_email", conversation={"type": "direct", "id": "x@y.com"}, text="hi")
    result = await adapter.send(message)
    assert not result.ok
    assert "connection refused" in result.error


async def test_connection_static():
    adapter = EmailImapAdapter("ops_email", dict(CREDS), {})
    result = await adapter.test_connection()
    assert result.ok
    broken = EmailImapAdapter("ops_email", {"host": "", "username": "u", "password": "p"}, {})
    assert not (await broken.test_connection()).ok


async def test_registry_poll_dispatch_and_dedup():
    raws = [build_email()]
    fetch_count = {"n": 0}

    def fake_fetch():
        fetch_count["n"] += 1
        return raws

    registry = email_registry(imap_fetch=fake_fetch)
    got_messages, got_interactions = [], []

    async def on_message(envelope):
        got_messages.append(envelope)

    async def on_interaction(interaction):
        got_interactions.append(interaction)

    registry.on_message(on_message)
    registry.on_interaction(on_interaction)
    registry.register_command("approve", ["同意"], code_required=True)

    results = await registry.poll()
    assert len(results) == 1 and results[0]
    assert len(got_messages) == 1
    assert got_messages[0].sender.external_id == "boss@example.com"
    assert len(got_interactions) == 1
    assert got_interactions[0].action == "approve"
    assert got_interactions[0].payload["code"] == "AP-123456"

    got_messages.clear()
    got_interactions.clear()
    results = await registry.poll()
    assert fetch_count["n"] == 2
    assert all(not result for result in results)
    assert got_messages == [] and got_interactions == []


async def test_registry_poll_fetch_error_isolated():
    def broken_fetch():
        raise RuntimeError("imap connection refused")

    registry = email_registry(imap_fetch=broken_fetch)
    results = await registry.poll()
    assert results == []


async def test_registry_poll_skips_webhook_adapters():
    config = {
        "channels": [
            {
                "id": "family_twilio",
                "adapter": "twilio_sms",
                "credentials": {"account_sid": "AC" + "0" * 32, "auth_token": "t", "from_number": "+15550000001"},
            }
        ]
    }
    registry = ChannelRegistry.from_config(config, store=MemoryStore())
    assert await registry.poll() == []


async def test_registry_send_card_fallback_then_smtp():
    sent: list[tuple[str, str, bytes]] = []

    def fake_smtp(from_addr, to_addr, mime_bytes):
        sent.append((from_addr, to_addr, mime_bytes))

    registry = email_registry(smtp_send=fake_smtp)
    await registry.send(
        "ops_email",
        {"type": "direct", "id": "boss@example.com"},
        card=ApprovalCard(
            title="审批 AP-123456",
            code="AP-123456",
            body="新增雇员张阿姨",
            actions=[CardAction(action="approve", label="同意"), CardAction(action="reject", label="拒绝")],
        ),
        meta={"subject": "审批请求 AP-123456"},
    )
    sent_count = await registry.outbox.flush_once()
    assert sent_count == 1
    assert len(sent) == 1
    content = parse_mime(sent[0][2]).get_content()
    assert "AP-123456" in content
    assert "同意 AP-123456" in content  # fallback text carries the command hint


async def test_start_polling_lifecycle():
    raws = [build_email(message_id="<m-3@example.com>")]

    def fake_fetch():
        return list(raws)

    registry = email_registry(imap_fetch=fake_fetch)
    got = []

    async def on_message(envelope):
        got.append(envelope)

    registry.on_message(on_message)
    registry.start_polling(interval=0.05)
    await asyncio.sleep(0.15)
    await registry.close()
    assert len(got) >= 1
