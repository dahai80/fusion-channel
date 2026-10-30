from __future__ import annotations

import json
from email.message import EmailMessage

import httpx
import pytest

from fusion_channel.adapters.graph_mail import GraphMailAdapter
from fusion_channel.config import ConfigError
from fusion_channel.models import OutboundMessage
from fusion_channel.registry import ChannelRegistry
from fusion_channel.storage import MemoryStore

CREDS = {
    "tenant_id": "tenant-1",
    "client_id": "client-1",
    "client_secret": "secret-1",
    "mailbox": "shared-orders@corp.com",
}

MAILBOX = "shared-orders@corp.com"
TOKEN_PATH = "/tenant-1/oauth2/v2.0/token"


def build_mime(message_id="<m-1@supplier.com>", subject="Purchase order PO-42", body="PO-42 attached", reply_to=None):
    msg = EmailMessage()
    msg["Message-ID"] = message_id
    msg["From"] = "Supplier <orders@supplier.com>"
    msg["To"] = "shared-orders@corp.com"
    msg["Subject"] = subject
    if reply_to:
        msg["In-Reply-To"] = reply_to
    msg.set_content(body)
    return msg.as_bytes()


class FakeGraph:
    # httpx.MockTransport handler emulating the Graph surface this adapter
    # touches: token endpoint, unread list, MIME $value, attachments,
    # mark-read PATCH, attachment $value, sendMail.

    def __init__(self, messages: dict[str, dict]):
        # messages: graph message id -> {"mime": bytes, "read": False,
        # "attachments": [ {...Graph attachment dicts...} ]}
        self.messages = messages
        self.token_hits = 0
        self.graph_calls: list[str] = []
        self.marked_read: list[str] = []
        self.sent: list[dict] = []
        self.downloaded: list[tuple[str, str]] = []
        self.expire_token_once = False

    def transport(self):
        return httpx.MockTransport(self.handler)

    def handler(self, request: httpx.Request) -> httpx.Response:
        path = request.url.path.replace("%40", "@")
        if path.endswith(TOKEN_PATH):
            self.token_hits += 1
            return httpx.Response(200, json={"access_token": f"tok-{self.token_hits}", "expires_in": 3600})
        if "login.microsoftonline.com" in request.url.host:
            return httpx.Response(400, json={"error": "bad tenant"})
        auth = request.headers.get("Authorization", "")
        if self.expire_token_once and auth == "Bearer tok-1":
            self.expire_token_once = False
            return httpx.Response(401, json={"error": "InvalidAuthenticationToken"})
        if not auth.startswith("Bearer "):
            return httpx.Response(401, json={"error": "missing token"})
        self.graph_calls.append(f"{request.method} {request.url.path.replace('%40', '@')}")
        for mid, item in self.messages.items():
            if path == f"/v1.0/users/{MAILBOX}/mailFolders/inbox/messages/{mid}/$value":
                return httpx.Response(200, content=item["mime"])
            if path == f"/v1.0/users/{MAILBOX}/mailFolders/inbox/messages/{mid}/attachments":
                return httpx.Response(200, json={"value": item["attachments"]})
            if path == f"/v1.0/users/{MAILBOX}/mailFolders/inbox/messages/{mid}/attachments/ATT-1/$value":
                self.downloaded.append((mid, "ATT-1"))
                return httpx.Response(200, content=item["attachment_bytes"])
            if path == f"/v1.0/users/{MAILBOX}/mailFolders/inbox/messages/{mid}" and request.method == "PATCH":
                item["read"] = True
                self.marked_read.append(mid)
                return httpx.Response(200, json={})
        if path == f"/v1.0/users/{MAILBOX}/mailFolders/inbox/messages" and request.method == "GET":
            unread = [{"id": mid} for mid, item in self.messages.items() if not item["read"]]
            return httpx.Response(200, json={"value": unread})
        if path == f"/v1.0/users/{MAILBOX}/sendMail" and request.method == "POST":
            self.sent.append(json.loads(request.content))
            return httpx.Response(202)
        return httpx.Response(404, json={"error": "not found"})


def graph_message(mid="AAMk-1", attachments=None, attachment_bytes=b"%PDF-1.4 binary", reply_to=None):
    return {
        mid: {
            "mime": build_mime(reply_to=reply_to),
            "read": False,
            "attachments": attachments
            or [
                {
                    "id": "ATT-1",
                    "name": "po-42.pdf",
                    "contentType": "application/pdf",
                    "size": len(attachment_bytes),
                    "@microsoft.graph.downloadUrl": "https://graph.microsoft.com/v1.0/$value",
                }
            ],
            "attachment_bytes": attachment_bytes,
        }
    }


def graph_registry(fake: FakeGraph) -> ChannelRegistry:
    config = {
        "channels": [
            {
                "id": "ops_graph",
                "adapter": "graph_mail",
                "credentials": dict(CREDS),
                "folder": "INBOX",
                "poll_batch_limit": 20,
                "http_transport": fake.transport(),
            }
        ]
    }
    return ChannelRegistry.from_config(config, store=MemoryStore())


def graph_adapter(fake: FakeGraph) -> GraphMailAdapter:
    return GraphMailAdapter("ops_graph", dict(CREDS), {"folder": "INBOX", "http_transport": fake.transport()})


def test_adapter_registered_with_capabilities():
    from fusion_channel.adapters import ADAPTERS

    assert "graph_mail" in ADAPTERS
    caps = ADAPTERS["graph_mail"].capabilities()
    assert caps.CAN_RECEIVE and caps.CAN_SEND and caps.CAN_MEDIA and caps.CAN_THREADS
    assert caps.CAN_CARDS == "none"


def test_verify_always_ok():
    adapter = graph_adapter(FakeGraph({}))
    from fusion_channel.models import ChannelRequest

    assert adapter.verify_request(ChannelRequest(method="POST", url="graph://x", body=b"")).ok


async def test_poll_fetches_mime_marks_read_and_caches_token():
    fake = FakeGraph(graph_message())
    adapter = graph_adapter(fake)
    requests = await adapter.poll()
    assert len(requests) == 1
    assert requests[0].body == build_mime()
    assert requests[0].json_body["graph_message_id"] == "AAMk-1"
    assert requests[0].json_body["attachments"][0]["id"] == "ATT-1"
    # token fetched once, reused across token+list+mime+attachments+patch calls
    assert fake.token_hits == 1
    # mark-read PATCH verified
    assert fake.marked_read == ["AAMk-1"]
    # unread list went to the inbox folder with the filter and batch limit
    assert "GET /v1.0/users/shared-orders@corp.com/mailFolders/inbox/messages" in fake.graph_calls


async def test_poll_token_refresh_on_401():
    fake = FakeGraph(graph_message())
    fake.expire_token_once = True
    adapter = graph_adapter(fake)
    requests = await adapter.poll()
    assert len(requests) == 1
    assert fake.token_hits == 2  # first token expired server-side, refreshed once


async def test_parse_inbound_graph_attachments():
    fake = FakeGraph(graph_message(reply_to="<orig@supplier.com>"))
    adapter = graph_adapter(fake)
    requests = await adapter.poll()
    envelope = adapter.parse_inbound(requests[0])
    assert envelope.message_id == "<m-1@supplier.com>"
    assert envelope.sender.external_id == "orders@supplier.com"
    assert envelope.conversation.thread_id == "<orig@supplier.com>"
    assert envelope.raw["subject"] == "Purchase order PO-42"
    assert envelope.raw["graph_message_id"] == "AAMk-1"
    assert envelope.raw["attachments"][0]["name"] == "po-42.pdf"
    # Graph attachment refs replace the MIME walk: provider id + download URL
    assert len(envelope.media) == 1
    assert envelope.media[0].media_id == "ATT-1"
    assert envelope.media[0].mime_type == "application/pdf"
    assert envelope.media[0].url.startswith("https://graph.microsoft.com/")


async def test_download_attachment_returns_bytes():
    fake = FakeGraph(graph_message())
    adapter = graph_adapter(fake)
    data = await adapter.download_attachment("AAMk-1", "ATT-1")
    assert data == b"%PDF-1.4 binary"
    assert fake.downloaded == [("AAMk-1", "ATT-1")]


async def test_registry_poll_dispatch_and_dedup_on_repoll():
    fake = FakeGraph(graph_message())
    registry = graph_registry(fake)
    got = []
    registry.on_message(lambda env: got.append(env))
    results = await registry.poll()
    assert len(results) == 1 and results[0]
    assert len(got) == 1
    assert got[0].sender.external_id == "orders@supplier.com"
    # second poll: message was marked read, unread list is empty
    results = await registry.poll()
    assert all(not result for result in results)
    assert len(got) == 1
    assert fake.marked_read == ["AAMk-1"]


async def test_registry_dedup_when_mark_read_fails():
    fake = FakeGraph(graph_message())
    real_handler = fake.handler

    def handler(request):
        if request.method == "PATCH":
            # mark-read never sticks: message stays unread for the next list
            return httpx.Response(500, json={"error": "boom"})
        return real_handler(request)

    fake.transport = lambda: httpx.MockTransport(handler)
    registry = graph_registry(fake)
    got = []
    registry.on_message(lambda env: got.append(env))
    results = await registry.poll()
    assert len(results) == 1 and results[0]
    assert len(got) == 1
    # re-poll re-lists the unmarked message; Message-ID dedup keeps it single-dispatch
    results = await registry.poll()
    assert all(not result for result in results)
    assert len(got) == 1


def test_registry_missing_env_credentials_raises_config_error():
    # issue #2 acceptance: env: credential refs that resolve empty must fail
    # at load with a clear ConfigError, not surface later at poll time
    config = {
        "channels": [
            {
                "id": "ops_graph",
                "adapter": "graph_mail",
                "credentials": {
                    "tenant_id": "env:GRAPH_TENANT_ID",
                    "client_id": "env:GRAPH_CLIENT_ID",
                    "client_secret": "env:GRAPH_CLIENT_SECRET",
                    "mailbox": "shared-orders@corp.com",
                },
            }
        ]
    }
    with pytest.raises(ConfigError, match="credential validation failed"):
        ChannelRegistry.from_config(config, store=MemoryStore())


async def test_poll_missing_credentials_returns_empty():
    adapter = GraphMailAdapter("ops_graph", {"tenant_id": "", "client_id": "", "client_secret": "", "mailbox": ""}, {})
    assert await adapter.poll() == []
    result = await adapter.test_connection()
    assert not result.ok
    assert "missing" in result.detail


async def test_poll_list_failure_isolated_by_registry():
    fake = FakeGraph(graph_message())

    def handler(request):
        if request.url.path.endswith("/messages") and request.method == "GET":
            return httpx.Response(403, json={"error": "AccessDenied"})
        return fake.handler(request)

    fake.transport = lambda: httpx.MockTransport(handler)
    registry = graph_registry(fake)
    results = await registry.poll()
    assert results == []


async def test_send_via_sendmail():
    fake = FakeGraph(graph_message())
    adapter = graph_adapter(fake)
    message = OutboundMessage(
        channel_id="ops_graph",
        conversation={"type": "direct", "id": "orders@supplier.com"},
        text="PO-42 received and processed",
        meta={"subject": "Re: Purchase order PO-42", "rendered": {"text": "PO-42 received and processed"}},
        reply_to="<m-1@supplier.com>",
    )
    result = await adapter.send(message)
    assert result.ok
    assert len(fake.sent) == 1
    payload = fake.sent[0]
    assert payload["saveToSentItems"] is False
    assert payload["message"]["subject"] == "Re: Purchase order PO-42"
    assert payload["message"]["body"]["content"] == "PO-42 received and processed"
    assert payload["message"]["toRecipients"][0]["emailAddress"]["address"] == "orders@supplier.com"


async def test_send_reply_prefix_and_default_subject():
    fake = FakeGraph(graph_message())
    adapter = graph_adapter(fake)
    message = OutboundMessage(
        channel_id="ops_graph",
        conversation={"type": "direct", "id": "orders@supplier.com"},
        text="first line subject\nbody",
        reply_to="<m-1@supplier.com>",
    )
    result = await adapter.send(message)
    assert result.ok
    assert fake.sent[0]["message"]["subject"] == "Re: first line subject"


async def test_send_missing_credentials():
    adapter = GraphMailAdapter("ops_graph", {"tenant_id": "", "client_id": "", "client_secret": "", "mailbox": ""}, {})
    message = OutboundMessage(channel_id="ops_graph", conversation={"type": "direct", "id": "x@y.com"}, text="hi")
    result = await adapter.send(message)
    assert not result.ok
    assert "credentials not configured" in result.error


async def test_send_provider_error_surface():
    fake = FakeGraph(graph_message())

    def handler(request):
        if request.url.path.endswith("/sendMail"):
            return httpx.Response(400, json={"error": "EmailAddressUnavailable"})
        return fake.handler(request)

    fake.transport = lambda: httpx.MockTransport(handler)
    adapter = graph_adapter(fake)
    message = OutboundMessage(channel_id="ops_graph", conversation={"type": "direct", "id": "x@y.com"}, text="hi")
    result = await adapter.send(message)
    assert not result.ok
    assert "sendMail failed" in result.error


@pytest.mark.parametrize(
    "folder,expected",
    [("INBOX", "inbox"), ("Junk Email", "Junk%20Email")],
)
def test_folder_path_mapping(folder, expected):
    adapter = graph_adapter(FakeGraph({}))
    adapter.config["folder"] = folder
    assert adapter._folder_path(folder).endswith(f"/mailFolders/{expected}")
