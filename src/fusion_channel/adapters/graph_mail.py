from __future__ import annotations

import logging
import time
from email import policy
from email.parser import BytesParser
from email.utils import parseaddr
from urllib.parse import quote

import httpx

from fusion_channel.adapters import HttpAdapterMixin, redact_secrets, register
from fusion_channel.adapters.email_imap import _extract_body
from fusion_channel.capabilities import matrix_capabilities
from fusion_channel.models import (
    ChannelRequest,
    Conversation,
    ExternalIdentity,
    InboundEnvelope,
    MediaRef,
    OutboundMessage,
    SendResult,
    TestResult,
    VerifyResult,
)
from fusion_channel.spi import ChannelAdapter, CredentialField

logger = logging.getLogger(__name__)

GRAPH_BASE = "https://graph.microsoft.com/v1.0"
TOKEN_URL_TEMPLATE = "https://login.microsoftonline.com/{tenant}/oauth2/v2.0/token"
GRAPH_SCOPE = "https://graph.microsoft.com/.default"
DEFAULT_POLL_LIMIT = 20
MAX_TEXT = 20000


@register
class GraphMailAdapter(HttpAdapterMixin, ChannelAdapter):
    # Pull-mode Microsoft 365 / Exchange Online mailbox via Graph API.
    # Exchange Online has basic auth disabled, so email_imap (IMAP password)
    # cannot connect there; this adapter uses OAuth2 client credentials
    # (application permission Mail.ReadWrite on a shared mailbox). Like
    # email_imap there is no webhook to verify: OAuth2 + TLS are the trust
    # boundary, verify_request always passes (logged). Poll lists unread
    # messages, fetches the full MIME, then marks read so the UNSEEN-style
    # poll/dedup semantics match email_imap. Attachment bytes are fetched on
    # demand by consumers via download_attachment().

    @classmethod
    def adapter_id(cls) -> str:
        return "graph_mail"

    @classmethod
    def capabilities(cls):
        return matrix_capabilities(cls.adapter_id())

    @classmethod
    def credential_schema(cls) -> list[CredentialField]:
        return [
            CredentialField("tenant_id", required=True, secret=False),
            CredentialField("client_id", required=True, secret=False),
            CredentialField("client_secret", required=True, secret=True),
            CredentialField("mailbox", required=True, secret=False),
        ]

    def verify_request(self, request: ChannelRequest) -> VerifyResult:
        # Transport-level trust: Graph API calls are authenticated with an
        # OAuth2 token fetched by poll(); nothing to verify per request.
        logger.debug("graph verify bypassed channel=%s (transport auth is the boundary)", self.channel_id)
        return VerifyResult(ok=True)

    def parse_inbound(self, request: ChannelRequest) -> InboundEnvelope:
        msg = BytesParser(policy=policy.default).parsebytes(request.body)
        meta = request.json_body if isinstance(request.json_body, dict) else {}
        graph_message_id = str(meta.get("graph_message_id", ""))
        graph_attachments = meta.get("attachments") or []
        message_id = str(msg.get("Message-ID", "")).strip()
        from_name, from_addr = parseaddr(str(msg.get("From", "")))
        subject = str(msg.get("Subject", "") or "").strip()
        in_reply_to = str(msg.get("In-Reply-To", "") or "").strip()
        text, media = _extract_body(msg)
        if graph_attachments:
            # Graph attachment refs carry the provider id + download URL —
            # richer than the MIME walk (consumers can pull real bytes).
            media = [
                MediaRef(
                    media_id=str(att.get("id", "")),
                    mime_type=att.get("contentType"),
                    size=int(att.get("size", 0) or 0),
                    url=att.get("@microsoft.graph.downloadUrl", ""),
                )
                for att in graph_attachments
            ]
        return InboundEnvelope(
            channel_id=self.channel_id,
            message_id=message_id,
            conversation=Conversation(type="direct", id=from_addr, thread_id=in_reply_to or None),
            sender=ExternalIdentity(channel_id=self.channel_id, external_id=from_addr, display_name=from_name or None),
            text=text,
            media=media,
            raw={
                "message_id": message_id,
                "subject": subject,
                "from": str(msg.get("From", "")),
                "to": str(msg.get("To", "")),
                "in_reply_to": in_reply_to,
                "text": text[:4000],
                "graph_message_id": graph_message_id,
                "attachments": [
                    {
                        "id": str(att.get("id", "")),
                        "name": att.get("name", ""),
                        "content_type": att.get("contentType", ""),
                        "size": int(att.get("size", 0) or 0),
                        "download_url": att.get("@microsoft.graph.downloadUrl", ""),
                    }
                    for att in graph_attachments
                ],
            },
        )

    async def _access_token(self, force_refresh: bool = False) -> str:
        cached = getattr(self, "_token_cache", None)
        if cached is not None and not force_refresh and cached[1] > time.monotonic():
            return cached[0]
        tenant = self.credentials.get("tenant_id", "")
        data = {
            "client_id": self.credentials.get("client_id", ""),
            "client_secret": self.credentials.get("client_secret", ""),
            "grant_type": "client_credentials",
            "scope": GRAPH_SCOPE,
        }
        async with self._client() as client:
            resp = await client.post(TOKEN_URL_TEMPLATE.format(tenant=quote(tenant)), data=data)
        payload = resp.json()
        token = payload.get("access_token", "")
        if not token:
            raise RuntimeError(f"graph token fetch failed: {payload.get('error', 'unknown')}")
        expires_in = int(payload.get("expires_in", 3600) or 3600)
        self._token_cache = (token, time.monotonic() + max(60, expires_in - 120))
        logger.info("graph token acquired channel=%s expires_in=%ds", self.channel_id, expires_in)
        return token

    async def _graph_get(self, client: httpx.AsyncClient, path: str) -> httpx.Response:
        resp = await client.get(
            f"{GRAPH_BASE}{path}", headers={"Authorization": f"Bearer {await self._access_token()}"}
        )
        if resp.status_code == 401:
            # Token revoked server-side before local expiry: refresh once.
            resp = await client.get(
                f"{GRAPH_BASE}{path}",
                headers={"Authorization": f"Bearer {await self._access_token(force_refresh=True)}"},
            )
        return resp

    def _folder_path(self, folder: str) -> str:
        name = "inbox" if folder.strip().upper() == "INBOX" else folder.strip()
        return f"/users/{quote(self.credentials.get('mailbox', ''), safe='@')}/mailFolders/{quote(name)}"

    async def poll(self) -> list[ChannelRequest]:
        missing = [
            name for name in ("tenant_id", "client_id", "client_secret", "mailbox") if not self.credentials.get(name)
        ]
        if missing:
            logger.warning(
                "graph poll skipped, credentials missing channel=%s missing=%s", self.channel_id, ",".join(missing)
            )
            return []
        folder = self.config.get("folder", "INBOX")
        limit = int(self.config.get("poll_batch_limit", DEFAULT_POLL_LIMIT))
        base = self._folder_path(folder)
        requests: list[ChannelRequest] = []
        async with self._client() as client:
            resp = await self._graph_get(client, f"{base}/messages?$filter=isRead eq false&$top={limit}&$select=id")
            if resp.status_code != 200:
                raise RuntimeError(f"graph list unread failed: {resp.status_code} {redact_secrets(resp.text[:200])}")
            messages = resp.json().get("value", [])
            for item in messages:
                graph_id = str(item.get("id", ""))
                try:
                    mime_resp = await self._graph_get(client, f"{base}/messages/{quote(graph_id)}/$value")
                    if mime_resp.status_code != 200:
                        logger.warning(
                            "graph mime fetch failed channel=%s id=%s status=%d",
                            self.channel_id,
                            graph_id,
                            mime_resp.status_code,
                        )
                        continue
                    att_resp = await self._graph_get(client, f"{base}/messages/{quote(graph_id)}/attachments")
                    attachments = att_resp.json().get("value", []) if att_resp.status_code == 200 else []
                    mark = await client.patch(
                        f"{GRAPH_BASE}{base}/messages/{quote(graph_id)}",
                        headers={"Authorization": f"Bearer {await self._access_token()}"},
                        json={"isRead": True},
                    )
                    if mark.status_code not in (200, 204):
                        # Unmarked message will be re-listed next poll; the
                        # registry dedup (Message-ID) keeps it single-dispatch.
                        logger.warning(
                            "graph mark-read failed channel=%s id=%s status=%d",
                            self.channel_id,
                            graph_id,
                            mark.status_code,
                        )
                    requests.append(
                        ChannelRequest(
                            method="POST",
                            url=f"graph://{self.credentials.get('mailbox', '')}/{folder}",
                            body=mime_resp.content,
                            json_body={"graph_message_id": graph_id, "attachments": attachments},
                        )
                    )
                except httpx.HTTPError as exc:
                    logger.warning(
                        "graph message fetch failed channel=%s id=%s err=%s",
                        self.channel_id,
                        graph_id,
                        redact_secrets(str(exc)),
                    )
        logger.info("graph polled channel=%s fetched=%d", self.channel_id, len(requests))
        return requests

    async def download_attachment(self, graph_message_id: str, attachment_id: str) -> bytes:
        # Consumer entry point for real attachment bytes (document pipelines
        # need content, not just filename/size refs).
        base = self._folder_path(self.config.get("folder", "INBOX"))
        async with self._client() as client:
            resp = await self._graph_get(
                client, f"{base}/messages/{quote(graph_message_id)}/attachments/{quote(attachment_id)}/$value"
            )
        if resp.status_code != 200:
            raise RuntimeError(f"graph attachment download failed: {resp.status_code}")
        return resp.content

    async def send(self, message: OutboundMessage) -> SendResult:
        rendered = message.meta.get("rendered") or {}
        text = rendered.get("text") or message.text
        missing = [
            name for name in ("tenant_id", "client_id", "client_secret", "mailbox") if not self.credentials.get(name)
        ]
        if missing:
            return SendResult(ok=False, error=f"graph credentials not configured: {','.join(missing)}")
        to_addr = message.conversation.id
        subject = str(
            message.meta.get("subject") or text.strip().splitlines()[0][:78] if text.strip() else "(no subject)"
        )
        if message.reply_to and not subject.lower().startswith("re:"):
            subject = f"Re: {subject}"
        body = {
            "message": {
                "subject": subject,
                "body": {"contentType": "Text", "content": text},
                "toRecipients": [{"emailAddress": {"address": to_addr}}],
            },
            "saveToSentItems": False,
        }
        try:
            async with self._client() as client:
                resp = await client.post(
                    f"{GRAPH_BASE}/users/{quote(self.credentials.get('mailbox', ''), safe='@')}/sendMail",
                    headers={"Authorization": f"Bearer {await self._access_token()}"},
                    json=body,
                )
        except httpx.HTTPError as exc:
            return SendResult(ok=False, error=f"graph http error: {redact_secrets(str(exc))}")
        if resp.status_code not in (200, 201, 202):
            return SendResult(
                ok=False, error=f"graph sendMail failed: {resp.status_code} {redact_secrets(resp.text[:200])}"
            )
        logger.info("graph mail sent channel=%s to=%s subject=%s", self.channel_id, to_addr, subject[:40])
        return SendResult(ok=True)

    async def test_connection(self) -> TestResult:
        # Static check (email_imap precedent): a live poll would consume
        # unread state and race the poll loop.
        missing = [
            name for name in ("tenant_id", "client_id", "client_secret", "mailbox") if not self.credentials.get(name)
        ]
        if missing:
            return TestResult(ok=False, detail="missing: " + ",".join(missing))
        return TestResult(ok=True, detail="static check ok (no live connect)")
