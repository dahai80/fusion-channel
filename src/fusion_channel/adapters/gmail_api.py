from __future__ import annotations

import base64
import json
import logging
import time
from email.utils import parseaddr
from urllib.parse import quote

import httpx

from fusion_channel.adapters import HttpAdapterMixin, redact_secrets, register
from fusion_channel.adapters.email_imap import _build_mime
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

GMAIL_BASE = "https://gmail.googleapis.com/gmail/v1"
TOKEN_URL = "https://oauth2.googleapis.com/token"
DEFAULT_POLL_LIMIT = 25
DEFAULT_LABEL_IDS = ("INBOX", "UNREAD")
MAX_TEXT = 20000


def _b64url_decode(data: str) -> bytes:
    return base64.urlsafe_b64decode(data + "=" * (-len(data) % 4))


def _payload_text(part: dict) -> str:
    # Thin walk of a Gmail format=full payload: text/plain bodies only,
    # consistent with the email_imap MIME handling (no HTML fallback).
    mime_type = str(part.get("mimeType", ""))
    if mime_type == "text/plain" and part.get("body", {}).get("data"):
        return _b64url_decode(part["body"]["data"]).decode("utf-8", "replace")
    chunks = [_payload_text(child) for child in part.get("parts", [])]
    return "\n".join(chunk for chunk in chunks if chunk)


def _payload_headers(payload: dict) -> dict[str, str]:
    return {str(h.get("name", "")).lower(): str(h.get("value", "")) for h in payload.get("headers", [])}


def _payload_attachments(message_id: str, part: dict, out: list[MediaRef]) -> None:
    filename = str(part.get("filename", "") or "")
    body = part.get("body", {})
    attachment_id = str(body.get("attachmentId", "") or "")
    if filename and attachment_id:
        out.append(
            MediaRef(
                media_id=f"{message_id}:{attachment_id}",
                mime_type=part.get("mimeType"),
                size=int(body.get("size", 0) or 0),
                url=f"gmail://messages/{quote(message_id)}/attachments/{quote(attachment_id)}",
            )
        )
    for child in part.get("parts", []):
        _payload_attachments(message_id, child, out)


@register
class GmailApiAdapter(HttpAdapterMixin, ChannelAdapter):
    # Pull-mode Gmail / Google Workspace ingestion via the Gmail API with
    # OAuth2 (refresh-token grant; service-account + domain-wide delegation
    # works through the same refresh-token contract when the consumer issues
    # one). Sibling of graph_mail (#2): same envelope/poll/attachment
    # semantics, different provider. Gmail has no "isRead" — read-marking is
    # a modifyLabels call that strips UNREAD, so label-based polls do not
    # re-deliver; if that call fails, Message-ID dedup still guards the
    # dispatch. historyId incremental sync is used when a previous poll (or
    # config seed) recorded one, with full-list fallback when the history
    # window has expired (Gmail 404s on too-old historyIds). OAuth2 + TLS is
    # the trust boundary; verify_request always passes (logged). 429 quota
    # responses never crash the poll loop: the list call backs off by
    # returning empty and the next cycle retries.

    @classmethod
    def adapter_id(cls) -> str:
        return "gmail_api"

    @classmethod
    def capabilities(cls):
        return matrix_capabilities(cls.adapter_id())

    @classmethod
    def credential_schema(cls) -> list[CredentialField]:
        return [
            CredentialField("client_id", required=True, secret=False),
            CredentialField("client_secret", required=True, secret=True),
            CredentialField("refresh_token", required=True, secret=True),
            CredentialField("user_id", required=True, secret=False),
        ]

    def verify_request(self, request: ChannelRequest) -> VerifyResult:
        logger.debug("gmail verify bypassed channel=%s (transport auth is the boundary)", self.channel_id)
        return VerifyResult(ok=True)

    def parse_inbound(self, request: ChannelRequest) -> InboundEnvelope:
        resource = request.json_body if isinstance(request.json_body, dict) else {}
        headers = _payload_headers(resource.get("payload", {}))
        message_id = headers.get("message-id", "").strip() or str(resource.get("id", ""))
        from_name, from_addr = parseaddr(headers.get("from", ""))
        subject = headers.get("subject", "").strip()
        in_reply_to = headers.get("in-reply-to", "").strip()
        references = headers.get("references", "").strip()
        gmail_thread_id = str(resource.get("threadId", ""))
        text = _payload_text(resource.get("payload", {}))[:MAX_TEXT]
        media: list[MediaRef] = []
        _payload_attachments(str(resource.get("id", "")), resource.get("payload", {}), media)
        return InboundEnvelope(
            channel_id=self.channel_id,
            message_id=message_id,
            # In-Reply-To keeps header-based threading consistent with the
            # email adapters; Gmail's own threadId is the fallback (first
            # message of a thread carries no In-Reply-To) and is always in raw.
            conversation=Conversation(type="direct", id=from_addr, thread_id=in_reply_to or gmail_thread_id or None),
            sender=ExternalIdentity(channel_id=self.channel_id, external_id=from_addr, display_name=from_name or None),
            text=text,
            media=media,
            raw={
                "message_id": message_id,
                "subject": subject,
                "from": headers.get("from", ""),
                "to": headers.get("to", ""),
                "date": headers.get("date", ""),
                "in_reply_to": in_reply_to,
                "references": references,
                "gmail_message_id": str(resource.get("id", "")),
                "gmail_thread_id": gmail_thread_id,
                "history_id": str(resource.get("historyId", "")),
                "label_ids": resource.get("labelIds", []),
                "text": text[:4000],
                "attachments": [
                    {"media_id": m.media_id, "mime_type": m.mime_type or "", "size": m.size, "url": m.url}
                    for m in media
                ],
            },
        )

    async def _access_token(self, force_refresh: bool = False) -> str:
        cached = getattr(self, "_token_cache", None)
        if cached is not None and not force_refresh and cached[1] > time.monotonic():
            return cached[0]
        data = {
            "client_id": self.credentials.get("client_id", ""),
            "client_secret": self.credentials.get("client_secret", ""),
            "refresh_token": self.credentials.get("refresh_token", ""),
            "grant_type": "refresh_token",
        }
        async with self._client() as client:
            resp = await client.post(TOKEN_URL, data=data)
        payload = resp.json()
        token = payload.get("access_token", "")
        if not token:
            raise RuntimeError(f"gmail token fetch failed: {payload.get('error', 'unknown')}")
        expires_in = int(payload.get("expires_in", 3600) or 3600)
        self._token_cache = (token, time.monotonic() + max(60, expires_in - 120))
        logger.info("gmail token acquired channel=%s expires_in=%ds", self.channel_id, expires_in)
        return token

    async def _gmail_request(self, client: httpx.AsyncClient, method: str, path: str, **kwargs) -> httpx.Response:
        headers = kwargs.pop("headers", {})
        headers["Authorization"] = f"Bearer {await self._access_token()}"
        resp = await client.request(method, f"{GMAIL_BASE}{path}", headers=headers, **kwargs)
        if resp.status_code == 401:
            # Token revoked server-side before local expiry: refresh once.
            headers["Authorization"] = f"Bearer {await self._access_token(force_refresh=True)}"
            resp = await client.request(method, f"{GMAIL_BASE}{path}", headers=headers, **kwargs)
        return resp

    def _user_path(self) -> str:
        return f"/users/{quote(self.credentials.get('user_id', 'me'), safe='')}"

    async def _message_ids(self, client: httpx.AsyncClient, limit: int) -> tuple[list[str], str]:
        # Returns (message ids, latest historyId). historyId incremental sync
        # when we have one recorded (previous poll or config seed); full
        # label/q list otherwise, and as fallback when the history window
        # has expired (Gmail answers 404 for too-old startHistoryId).
        last = getattr(self, "_last_history_id", "") or str(self.config.get("history_id", ""))
        if last:
            resp = await self._gmail_request(
                client, "GET", f"{self._user_path()}/history?startHistoryId={quote(last)}&maxResults={limit}"
            )
            if resp.status_code == 200:
                ids: list[str] = []
                for record in resp.json().get("history", []):
                    for added in record.get("messagesAdded", []):
                        ids.append(str(added.get("message", {}).get("id", "")))
                history_id = str(resp.json().get("historyId", last))
                logger.info("gmail incremental sync channel=%s since=%s added=%d", self.channel_id, last, len(ids))
                return [mid for mid in ids if mid], history_id
            if resp.status_code != 404:
                raise RuntimeError(f"gmail history sync failed: {resp.status_code} {redact_secrets(resp.text[:200])}")
            logger.warning(
                "gmail history gap detected channel=%s since=%s, falling back to full list", self.channel_id, last
            )
        params = [f"maxResults={limit}"]
        for label in self.config.get("label_ids", list(DEFAULT_LABEL_IDS)):
            params.append(f"labelIds={quote(str(label))}")
        query = str(self.config.get("query", "")).strip()
        if query:
            params.append(f"q={quote(query)}")
        resp = await self._gmail_request(client, "GET", f"{self._user_path()}/messages?{'&'.join(params)}")
        if resp.status_code == 429:
            # Quota: never crash the poll loop — skip this cycle, next one retries.
            logger.warning("gmail list rate-limited (429) channel=%s, backing off to next poll", self.channel_id)
            return [], ""
        if resp.status_code != 200:
            raise RuntimeError(f"gmail list failed: {resp.status_code} {redact_secrets(resp.text[:200])}")
        payload = resp.json()
        ids = [str(item.get("id", "")) for item in payload.get("messages", [])]
        return ids, str(payload.get("historyId", ""))

    async def poll(self) -> list[ChannelRequest]:
        missing = [
            name
            for name in ("client_id", "client_secret", "refresh_token", "user_id")
            if not self.credentials.get(name)
        ]
        if missing:
            logger.warning(
                "gmail poll skipped, credentials missing channel=%s missing=%s", self.channel_id, ",".join(missing)
            )
            return []
        limit = int(self.config.get("poll_batch_limit", DEFAULT_POLL_LIMIT))
        requests: list[ChannelRequest] = []
        async with self._client() as client:
            message_ids, history_id = await self._message_ids(client, limit)
            for mid in message_ids:
                try:
                    resp = await self._gmail_request(
                        client, "GET", f"{self._user_path()}/messages/{quote(mid)}?format=full"
                    )
                    if resp.status_code == 429:
                        logger.warning(
                            "gmail message fetch rate-limited (429) channel=%s id=%s skipped", self.channel_id, mid
                        )
                        continue
                    if resp.status_code != 200:
                        logger.warning(
                            "gmail message fetch failed channel=%s id=%s status=%d",
                            self.channel_id,
                            mid,
                            resp.status_code,
                        )
                        continue
                    resource = resp.json()
                    mark = await self._gmail_request(
                        client,
                        "POST",
                        f"{self._user_path()}/messages/{quote(mid)}/modifyLabels",
                        json={"removeLabelIds": ["UNREAD"]},
                    )
                    if mark.status_code not in (200, 201, 204):
                        # Unmarked message will be re-listed next poll; the
                        # registry dedup (Message-ID) keeps it single-dispatch.
                        logger.warning(
                            "gmail label-strip failed channel=%s id=%s status=%d",
                            self.channel_id,
                            mid,
                            mark.status_code,
                        )
                    requests.append(
                        ChannelRequest(
                            method="POST",
                            url=f"gmail://{self.credentials.get('user_id', 'me')}",
                            body=json.dumps(resource).encode("utf-8"),
                            json_body=resource,
                        )
                    )
                except httpx.HTTPError as exc:
                    logger.warning(
                        "gmail message fetch failed channel=%s id=%s err=%s",
                        self.channel_id,
                        mid,
                        redact_secrets(str(exc)),
                    )
            if history_id:
                self._last_history_id = history_id
        logger.info("gmail polled channel=%s fetched=%d", self.channel_id, len(requests))
        return requests

    async def download_attachment(self, message_id: str, attachment_id: str) -> bytes:
        # Consumer entry point for real attachment bytes; Gmail returns the
        # Attachment resource with base64url data (25 MB per attachment cap
        # is the provider's, size is surfaced on the MediaRef).
        async with self._client() as client:
            resp = await self._gmail_request(
                client, "GET", f"{self._user_path()}/messages/{quote(message_id)}/attachments/{quote(attachment_id)}"
            )
        if resp.status_code != 200:
            raise RuntimeError(f"gmail attachment download failed: {resp.status_code}")
        return _b64url_decode(resp.json().get("data", ""))

    async def send(self, message: OutboundMessage) -> SendResult:
        rendered = message.meta.get("rendered") or {}
        text = rendered.get("text") or message.text
        missing = [
            name
            for name in ("client_id", "client_secret", "refresh_token", "user_id")
            if not self.credentials.get(name)
        ]
        if missing:
            return SendResult(ok=False, error=f"gmail credentials not configured: {','.join(missing)}")
        to_addr = message.conversation.id
        subject = str(
            message.meta.get("subject") or text.strip().splitlines()[0][:78] if text.strip() else "(no subject)"
        )
        if message.reply_to and not subject.lower().startswith("re:"):
            subject = f"Re: {subject}"
        from_addr = self.config.get("from_address", "") or self.credentials.get("user_id", "me")
        raw = base64.urlsafe_b64encode(
            _build_mime(from_addr, to_addr, subject, text, reply_to=message.reply_to)
        ).decode("ascii")
        draft_mode = bool(self.config.get("draft_mode", False))
        path = f"{self._user_path()}/drafts/create" if draft_mode else f"{self._user_path()}/messages/send"
        body = {"message": {"raw": raw}} if draft_mode else {"raw": raw}
        try:
            async with self._client() as client:
                resp = await self._gmail_request(client, "POST", path, json=body)
        except httpx.HTTPError as exc:
            return SendResult(ok=False, error=f"gmail http error: {redact_secrets(str(exc))}")
        if resp.status_code not in (200, 201):
            return SendResult(
                ok=False,
                error=f"gmail {'drafts.create' if draft_mode else 'messages.send'} failed: {resp.status_code} {redact_secrets(resp.text[:200])}",
            )
        logger.info(
            "gmail %s channel=%s to=%s subject=%s",
            "draft created" if draft_mode else "mail sent",
            self.channel_id,
            to_addr,
            subject[:40],
        )
        return SendResult(ok=True)

    async def test_connection(self) -> TestResult:
        # Static check (email/graph_mail precedent): a live poll would consume
        # unread state and race the poll loop.
        missing = [
            name
            for name in ("client_id", "client_secret", "refresh_token", "user_id")
            if not self.credentials.get(name)
        ]
        if missing:
            return TestResult(ok=False, detail="missing: " + ",".join(missing))
        return TestResult(ok=True, detail="static check ok (no live connect)")
