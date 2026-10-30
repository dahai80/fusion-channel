from __future__ import annotations

import base64
import json

import httpx
import pytest

from fusion_channel.adapters.gmail_api import GmailApiAdapter
from fusion_channel.config import ConfigError
from fusion_channel.models import OutboundMessage
from fusion_channel.registry import ChannelRegistry
from fusion_channel.storage import MemoryStore

CREDS = {
    "client_id": "client-1",
    "client_secret": "secret-1",
    "refresh_token": "refresh-1",
    "user_id": "me",
}

BASE = "/gmail/v1/users/me"
TOKEN_HOST = "oauth2.googleapis.com"


def b64url(data: bytes | str) -> str:
    if isinstance(data, str):
        data = data.encode("utf-8")
    return base64.urlsafe_b64encode(data).decode("ascii").rstrip("=")


def gmail_message(
    mid="gm-1",
    thread_id="th-1",
    body="Invoice INV-9 attached",
    reply_to=None,
    with_attachment=True,
):
    headers = [
        {"name": "Message-ID", "value": "<m-1@supplier.com>"},
        {"name": "From", "value": "Supplier <orders@supplier.com>"},
        {"name": "To", "value": "orders@corp.com"},
        {"name": "Subject", "value": "Invoice INV-9"},
    ]
    if reply_to:
        headers.append({"name": "In-Reply-To", "value": reply_to})
    parts = [{"mimeType": "text/plain", "body": {"data": b64url(body)}}]
    if with_attachment:
        parts.append(
            {
                "filename": "inv-9.pdf",
                "mimeType": "application/pdf",
                "body": {"attachmentId": "ATT-9", "size": 15},
            }
        )
    return {
        "id": mid,
        "threadId": thread_id,
        "historyId": "5000",
        "labelIds": ["INBOX", "UNREAD"],
        "payload": {"headers": headers, "mimeType": "multipart/mixed", "parts": parts},
    }


class FakeGmail:
    # httpx.MockTransport handler emulating the Gmail surface this adapter
    # touches: token endpoint, messages.list, format=full get, modifyLabels,
    # attachments.get, history, messages.send, drafts.create.

    def __init__(self, messages: list[dict]):
        self.messages = {m["id"]: m for m in messages}
        self.token_hits = 0
        self.calls: list[str] = []
        self.unmarked: list[str] = []
        self.sent: list[dict] = []
        self.drafts: list[dict] = []
        self.list_status = 200
        self.message_status: dict[str, int] = {}
        self.history_response = None  # None = no history route; else (status, json)
        self.expire_token_once = False

    def transport(self):
        return httpx.MockTransport(self.handler)

    def handler(self, request: httpx.Request) -> httpx.Response:
        if request.url.host == TOKEN_HOST:
            self.token_hits += 1
            return httpx.Response(200, json={"access_token": f"tok-{self.token_hits}", "expires_in": 3600})
        auth = request.headers.get("Authorization", "")
        if self.expire_token_once and auth == "Bearer tok-1":
            self.expire_token_once = False
            return httpx.Response(401, json={"error": "invalid_grant"})
        if not auth.startswith("Bearer "):
            return httpx.Response(401, json={"error": "missing token"})
        path = request.url.path
        self.calls.append(f"{request.method} {path}")
        if path == f"{BASE}/messages" and request.method == "GET":
            if self.list_status != 200:
                return httpx.Response(self.list_status, json={"error": "quota"})
            unread = [
                {"id": mid, "threadId": m["threadId"]} for mid, m in self.messages.items() if "UNREAD" in m["labelIds"]
            ]
            return httpx.Response(200, json={"messages": unread, "historyId": "6000"})
        if path == f"{BASE}/history" and request.method == "GET":
            if self.history_response is None:
                return httpx.Response(404, json={"error": "notFound"})
            status, payload = self.history_response
            return httpx.Response(status, json=payload)
        for mid, m in self.messages.items():
            if path == f"{BASE}/messages/{mid}/modifyLabels" and request.method == "POST":
                remove = json.loads(request.content).get("removeLabelIds", [])
                if self.unmarked and mid in self.unmarked:
                    return httpx.Response(500, json={"error": "boom"})
                m["labelIds"] = [label for label in m["labelIds"] if label not in remove]
                return httpx.Response(200, json=m)
            if path == f"{BASE}/messages/{mid}" and request.method == "GET":
                if mid in self.message_status:
                    return httpx.Response(self.message_status[mid], json={"error": "quota"})
                return httpx.Response(200, json=m)
            if path == f"{BASE}/messages/{mid}/attachments/ATT-9" and request.method == "GET":
                return httpx.Response(200, json={"data": b64url(b"%PDF-1.4 binary"), "size": 15})
        if path == f"{BASE}/messages/send" and request.method == "POST":
            self.sent.append(json.loads(request.content))
            return httpx.Response(200, json={"id": "sent-1"})
        if path == f"{BASE}/drafts/create" and request.method == "POST":
            self.drafts.append(json.loads(request.content))
            return httpx.Response(200, json={"id": "draft-1"})
        return httpx.Response(404, json={"error": "not found"})


def gmail_registry(fake: FakeGmail, extra: dict | None = None) -> ChannelRegistry:
    channel = {
        "id": "ops_gmail",
        "adapter": "gmail_api",
        "credentials": dict(CREDS),
        "http_transport": fake.transport(),
    }
    channel.update(extra or {})
    return ChannelRegistry.from_config({"channels": [channel]}, store=MemoryStore())


def gmail_adapter(fake: FakeGmail, config: dict | None = None) -> GmailApiAdapter:
    cfg = {"http_transport": fake.transport()}
    cfg.update(config or {})
    return GmailApiAdapter("ops_gmail", dict(CREDS), cfg)


def test_adapter_registered_with_capabilities():
    from fusion_channel.adapters import ADAPTERS

    assert "gmail_api" in ADAPTERS
    caps = ADAPTERS["gmail_api"].capabilities()
    assert caps.CAN_RECEIVE and caps.CAN_SEND and caps.CAN_MEDIA and caps.CAN_THREADS
    assert caps.CAN_CARDS == "none"


def test_verify_always_ok():
    adapter = gmail_adapter(FakeGmail([]))
    from fusion_channel.models import ChannelRequest

    assert adapter.verify_request(ChannelRequest(method="POST", url="gmail://x", body=b"")).ok


async def test_poll_fetches_full_marks_read_and_caches_token():
    fake = FakeGmail([gmail_message()])
    adapter = gmail_adapter(fake)
    requests = await adapter.poll()
    assert len(requests) == 1
    assert requests[0].json_body["id"] == "gm-1"
    assert requests[0].json_body["payload"]["parts"][0]["body"]["data"] == b64url("Invoice INV-9 attached")
    # token fetched once, reused across list+get+modifyLabels calls
    assert fake.token_hits == 1
    # UNREAD stripped via modifyLabels
    assert "UNREAD" not in fake.messages["gm-1"]["labelIds"]
    assert "POST /gmail/v1/users/me/messages/gm-1/modifyLabels" in fake.calls


async def test_poll_token_refresh_on_401():
    fake = FakeGmail([gmail_message()])
    fake.expire_token_once = True
    adapter = gmail_adapter(fake)
    requests = await adapter.poll()
    assert len(requests) == 1
    assert fake.token_hits == 2


async def test_parse_inbound_headers_text_thread_attachments():
    fake = FakeGmail([gmail_message(reply_to="<orig@supplier.com>")])
    adapter = gmail_adapter(fake)
    requests = await adapter.poll()
    envelope = adapter.parse_inbound(requests[0])
    assert envelope.message_id == "<m-1@supplier.com>"
    assert envelope.sender.external_id == "orders@supplier.com"
    assert envelope.text == "Invoice INV-9 attached"
    assert envelope.conversation.thread_id == "<orig@supplier.com>"
    assert envelope.raw["gmail_thread_id"] == "th-1"
    assert envelope.raw["history_id"] == "5000"
    assert len(envelope.media) == 1
    assert envelope.media[0].media_id == "gm-1:ATT-9"
    assert envelope.media[0].mime_type == "application/pdf"
    assert envelope.media[0].url.startswith("gmail://messages/gm-1/attachments/ATT-9")


async def test_parse_inbound_thread_falls_back_to_gmail_thread_id():
    fake = FakeGmail([gmail_message()])
    adapter = gmail_adapter(fake)
    requests = await adapter.poll()
    envelope = adapter.parse_inbound(requests[0])
    assert envelope.conversation.thread_id == "th-1"


async def test_download_attachment_returns_bytes():
    fake = FakeGmail([gmail_message()])
    adapter = gmail_adapter(fake)
    data = await adapter.download_attachment("gm-1", "ATT-9")
    assert data == b"%PDF-1.4 binary"


async def test_registry_poll_dispatch_and_dedup_on_repoll():
    fake = FakeGmail([gmail_message()])
    registry = gmail_registry(fake)
    got = []
    registry.on_message(lambda env: got.append(env))
    results = await registry.poll()
    assert len(results) == 1 and results[0]
    assert len(got) == 1
    assert got[0].sender.external_id == "orders@supplier.com"
    # second poll: UNREAD was stripped, list is empty
    results = await registry.poll()
    assert all(not result for result in results)
    assert len(got) == 1


async def test_registry_dedup_when_label_strip_fails():
    fake = FakeGmail([gmail_message()])
    fake.unmarked = ["gm-1"]  # modifyLabels 500s; message stays unread
    registry = gmail_registry(fake)
    got = []
    registry.on_message(lambda env: got.append(env))
    results = await registry.poll()
    assert len(results) == 1 and results[0]
    assert len(got) == 1
    # re-poll re-lists the unmarked message; Message-ID dedup keeps it single-dispatch
    results = await registry.poll()
    assert all(not result for result in results)
    assert len(got) == 1


async def test_history_incremental_sync_then_gap_fallback():
    fake = FakeGmail([gmail_message()])
    fake.history_response = (
        200,
        {"historyId": "6500", "history": [{"messagesAdded": [{"message": {"id": "gm-1"}}]}]},
    )
    adapter = gmail_adapter(fake, {"history_id": "5000"})
    requests = await adapter.poll()
    # history route used, not the label list
    assert any("GET /gmail/v1/users/me/history" in call for call in fake.calls)
    assert not any(call.endswith("/messages") and "GET" in call for call in fake.calls)
    assert len(requests) == 1
    assert adapter._last_history_id == "6500"
    # second poll: 404 on too-old historyId falls back to the full list
    fake.history_response = (404, {"error": "notFound"})
    requests = await adapter.poll()
    assert any(call.endswith("/messages") and "GET" in call for call in fake.calls)
    # gm-1 was marked read, so the full list finds nothing new
    assert requests == []


async def test_poll_list_429_backs_off_non_fatal():
    fake = FakeGmail([gmail_message()])
    fake.list_status = 429
    adapter = gmail_adapter(fake)
    assert await adapter.poll() == []


async def test_poll_message_429_skipped_others_delivered():
    m1 = gmail_message(mid="gm-1")
    m2 = gmail_message(mid="gm-2")
    m2["payload"]["headers"] = [h for h in m1["payload"]["headers"] if h["name"] != "Message-ID"]
    m2["payload"]["headers"].append({"name": "Message-ID", "value": "<m-2@supplier.com>"})
    fake = FakeGmail([m1, m2])
    fake.message_status = {"gm-1": 429}
    adapter = gmail_adapter(fake)
    requests = await adapter.poll()
    assert len(requests) == 1
    assert requests[0].json_body["id"] == "gm-2"


def test_registry_missing_env_credentials_raises_config_error():
    # issue #3 acceptance: env: credential refs that resolve empty must fail
    # at load with a clear ConfigError, not surface later at poll time
    config = {
        "channels": [
            {
                "id": "ops_gmail",
                "adapter": "gmail_api",
                "credentials": {
                    "client_id": "env:GMAIL_CLIENT_ID",
                    "client_secret": "env:GMAIL_CLIENT_SECRET",
                    "refresh_token": "env:GMAIL_REFRESH_TOKEN",
                    "user_id": "me",
                },
            }
        ]
    }
    with pytest.raises(ConfigError, match="credential validation failed"):
        ChannelRegistry.from_config(config, store=MemoryStore())


async def test_poll_missing_credentials_returns_empty():
    adapter = GmailApiAdapter(
        "ops_gmail", {"client_id": "", "client_secret": "", "refresh_token": "", "user_id": ""}, {}
    )
    assert await adapter.poll() == []
    result = await adapter.test_connection()
    assert not result.ok
    assert "missing" in result.detail


async def test_poll_list_failure_isolated_by_registry():
    fake = FakeGmail([gmail_message()])
    fake.list_status = 403
    registry = gmail_registry(fake)
    results = await registry.poll()
    assert results == []


async def test_send_via_messages_send():
    fake = FakeGmail([gmail_message()])
    adapter = gmail_adapter(fake)
    message = OutboundMessage(
        channel_id="ops_gmail",
        conversation={"type": "direct", "id": "orders@supplier.com"},
        text="INV-9 processed",
        meta={"subject": "Re: Invoice INV-9", "rendered": {"text": "INV-9 processed"}},
        reply_to="<m-1@supplier.com>",
    )
    result = await adapter.send(message)
    assert result.ok
    assert len(fake.sent) == 1
    raw = base64.urlsafe_b64decode(fake.sent[0]["raw"] + "=" * (-len(fake.sent[0]["raw"]) % 4))
    assert b"To: orders@supplier.com" in raw
    assert b"Subject: Re: Invoice INV-9" in raw
    assert b"INV-9 processed" in raw
    assert b"In-Reply-To: <m-1@supplier.com>" in raw


async def test_send_draft_mode_uses_drafts_create():
    fake = FakeGmail([gmail_message()])
    adapter = gmail_adapter(fake, {"draft_mode": True})
    message = OutboundMessage(
        channel_id="ops_gmail",
        conversation={"type": "direct", "id": "orders@supplier.com"},
        text="draft body",
    )
    result = await adapter.send(message)
    assert result.ok
    assert len(fake.drafts) == 1 and not fake.sent
    assert "raw" in fake.drafts[0]["message"]


async def test_send_reply_prefix_and_default_subject():
    fake = FakeGmail([gmail_message()])
    adapter = gmail_adapter(fake)
    message = OutboundMessage(
        channel_id="ops_gmail",
        conversation={"type": "direct", "id": "orders@supplier.com"},
        text="first line subject\nbody",
        reply_to="<m-1@supplier.com>",
    )
    result = await adapter.send(message)
    assert result.ok
    raw = base64.urlsafe_b64decode(fake.sent[0]["raw"] + "=" * (-len(fake.sent[0]["raw"]) % 4))
    assert b"Subject: Re: first line subject" in raw


async def test_send_missing_credentials():
    adapter = GmailApiAdapter(
        "ops_gmail", {"client_id": "", "client_secret": "", "refresh_token": "", "user_id": ""}, {}
    )
    message = OutboundMessage(channel_id="ops_gmail", conversation={"type": "direct", "id": "x@y.com"}, text="hi")
    result = await adapter.send(message)
    assert not result.ok
    assert "credentials not configured" in result.error


async def test_send_provider_error_surface():
    fake = FakeGmail([])

    def handler(request):
        if request.url.path.endswith("/messages/send"):
            return httpx.Response(400, json={"error": "invalidArgument"})
        return fake.handler(request)

    fake.transport = lambda: httpx.MockTransport(handler)
    adapter = gmail_adapter(fake)
    message = OutboundMessage(channel_id="ops_gmail", conversation={"type": "direct", "id": "x@y.com"}, text="hi")
    result = await adapter.send(message)
    assert not result.ok
    assert "messages.send failed" in result.error
