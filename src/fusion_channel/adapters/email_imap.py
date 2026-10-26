from __future__ import annotations

import asyncio
import contextlib
import logging
from email import policy
from email.message import EmailMessage
from email.parser import BytesParser
from email.utils import parseaddr

from fusion_channel.adapters import redact_secrets, register
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

MAX_TEXT = 20000
MAX_SUBJECT = 78
DEFAULT_POLL_LIMIT = 20


@register
class EmailImapAdapter(ChannelAdapter):
    # Pull-mode channel: IMAP fetch for inbound, SMTP for outbound. There is
    # no webhook to sign — TLS + account auth are the trust boundary, so
    # verify_request always passes (logged). MIME handling stays thin:
    # text/plain extraction + attachment refs, no rich rendering.

    @classmethod
    def adapter_id(cls) -> str:
        return "email_imap"

    @classmethod
    def capabilities(cls):
        return matrix_capabilities(cls.adapter_id())

    @classmethod
    def credential_schema(cls) -> list[CredentialField]:
        return [
            CredentialField("host", required=True, secret=False),
            CredentialField("username", required=True, secret=False),
            CredentialField("password", required=True, secret=True),
            CredentialField("from_address", required=False, secret=False),
            CredentialField("imap_port", required=False, secret=False, pattern=r"^[0-9]+$"),
            CredentialField("smtp_port", required=False, secret=False, pattern=r"^[0-9]+$"),
        ]

    def verify_request(self, request: ChannelRequest) -> VerifyResult:
        # Transport-level trust: IMAP login over TLS already authenticated
        # the source. Nothing to verify at the request layer.
        logger.debug("email verify bypassed channel=%s (transport auth is the boundary)", self.channel_id)
        return VerifyResult(ok=True)

    def parse_inbound(self, request: ChannelRequest) -> InboundEnvelope:
        msg = BytesParser(policy=policy.default).parsebytes(request.body)
        message_id = str(msg.get("Message-ID", "")).strip()
        from_name, from_addr = parseaddr(str(msg.get("From", "")))
        subject = str(msg.get("Subject", "") or "").strip()
        in_reply_to = str(msg.get("In-Reply-To", "") or "").strip()
        text, media = _extract_body(msg)
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
            },
        )

    async def poll(self) -> list[ChannelRequest]:
        host = self.credentials.get("host", "")
        username = self.credentials.get("username", "")
        if not host or not username:
            logger.warning("email poll skipped, credentials missing channel=%s", self.channel_id)
            return []
        folder = self.config.get("folder", "INBOX")
        limit = int(self.config.get("poll_batch_limit", DEFAULT_POLL_LIMIT))
        fetch = self.config.get("imap_fetch") or _default_imap_fetch(
            host,
            int(self.credentials.get("imap_port", "993")),
            username,
            self.credentials.get("password", ""),
            folder,
            limit,
        )
        try:
            raws = await asyncio.to_thread(fetch)
        except Exception as exc:
            logger.error("email imap fetch failed channel=%s err=%s", self.channel_id, exc)
            return []
        logger.info("email polled channel=%s fetched=%d", self.channel_id, len(raws))
        return [ChannelRequest(method="POST", url=f"imap://{host}/{folder}", body=raw) for raw in raws]

    async def send(self, message: OutboundMessage) -> SendResult:
        rendered = message.meta.get("rendered") or {}
        text = rendered.get("text") or message.text
        host = self.credentials.get("host", "")
        username = self.credentials.get("username", "")
        password = self.credentials.get("password", "")
        if not host or not username or not password:
            return SendResult(ok=False, error="email credentials not configured")
        to_addr = message.conversation.id
        from_addr = self.credentials.get("from_address") or username
        subject = str(message.meta.get("subject") or _subject_from_text(text))
        mime = _build_mime(from_addr, to_addr, subject, text, reply_to=message.reply_to)
        smtp_send = self.config.get("smtp_send") or _default_smtp_send(
            host, int(self.credentials.get("smtp_port", "587")), username, password
        )
        try:
            await asyncio.to_thread(smtp_send, from_addr, to_addr, mime)
        except Exception as exc:
            return SendResult(ok=False, error=f"email smtp error: {redact_secrets(str(exc))}")
        logger.info("email sent channel=%s to=%s subject=%s", self.channel_id, to_addr, subject[:40])
        return SendResult(ok=True)

    async def test_connection(self) -> TestResult:
        # Static check (dingtalk_robot precedent): a live IMAP login would
        # consume UNSEEN state and race the poll loop.
        missing = [name for name in ("host", "username", "password") if not self.credentials.get(name)]
        if missing:
            return TestResult(ok=False, detail="missing: " + ",".join(missing))
        return TestResult(ok=True, detail="static check ok (no live connect)")


def _extract_body(msg: EmailMessage) -> tuple[str, list[MediaRef]]:
    text = ""
    body = msg.get_body(preferencelist=("plain",))
    if body is not None:
        try:
            text = body.get_content()
        except Exception as exc:
            logger.warning("email body decode failed err=%s", exc)
    media: list[MediaRef] = []
    for part in msg.walk():
        filename = part.get_filename()
        if not filename:
            continue
        payload = part.get_payload(decode=True) or b""
        media.append(MediaRef(media_id=filename, mime_type=part.get_content_type(), size=len(payload)))
    return text[:MAX_TEXT], media


def _subject_from_text(text: str) -> str:
    first_line = text.strip().splitlines()[0] if text.strip() else "(no subject)"
    return first_line[:MAX_SUBJECT]


def _build_mime(from_addr: str, to_addr: str, subject: str, text: str, reply_to: str | None = None) -> bytes:
    mime = EmailMessage()
    mime["From"] = from_addr
    mime["To"] = to_addr
    mime["Subject"] = subject
    if reply_to:
        mime["In-Reply-To"] = reply_to
        mime["References"] = reply_to
    mime.set_content(text, subtype="plain", charset="utf-8")
    return mime.as_bytes()


def _default_imap_fetch(host: str, port: int, username: str, password: str, folder: str, limit: int):
    import imaplib

    def fetch() -> list[bytes]:
        conn = imaplib.IMAP4_SSL(host, port)
        try:
            conn.login(username, password)
            conn.select(folder, readonly=True)
            status, data = conn.search(None, "UNSEEN")
            if status != "OK":
                return []
            ids = data[0].split()[-limit:]
            out: list[bytes] = []
            for num in ids:
                status, msg_data = conn.fetch(num, "(RFC822)")
                if status == "OK" and msg_data and msg_data[0] is not None:
                    out.append(msg_data[0][1])
            return out
        finally:
            with contextlib.suppress(Exception):
                conn.logout()

    return fetch


def _default_smtp_send(host: str, port: int, username: str, password: str):
    import smtplib

    def send_mime(from_addr: str, to_addr: str, mime_bytes: bytes) -> str:
        with smtplib.SMTP(host, port, timeout=15) as conn:
            conn.starttls()
            conn.login(username, password)
            conn.sendmail(from_addr, [to_addr], mime_bytes)
        return ""

    return send_mime
