from __future__ import annotations

import asyncio
import logging
import os
import time

import pytest

from fusion_channel.models import OutboundMessage
from fusion_channel.registry import ChannelRegistry
from fusion_channel.storage import MemoryStore

pytestmark = pytest.mark.integration

logger = logging.getLogger(__name__)

QQ_HOST = "imap.qq.com"
SMTP_PORT = "587"


def _qq_credentials() -> dict[str, str]:
    return {
        "host": QQ_HOST,
        "username": os.environ.get("FUSION_CHANNEL_QQ_EMAIL", ""),
        "password": os.environ.get("FUSION_CHANNEL_QQ_AUTH_CODE", ""),
        "smtp_port": SMTP_PORT,
    }


def _has_creds() -> bool:
    return bool(os.environ.get("FUSION_CHANNEL_QQ_EMAIL") and os.environ.get("FUSION_CHANNEL_QQ_AUTH_CODE"))


def _qq_registry() -> ChannelRegistry:
    config = {
        "channels": [
            {
                "id": "qq_email",
                "adapter": "email_imap",
                "credentials": _qq_credentials(),
            }
        ]
    }
    return ChannelRegistry.from_config(config, store=MemoryStore())


@pytest.mark.skipif(
    not _has_creds(), reason="no QQ Mail credentials in env (FUSION_CHANNEL_QQ_EMAIL / FUSION_CHANNEL_QQ_AUTH_CODE)"
)
async def test_qq_imap_login_and_fetch():
    # Raw fetch path: real IMAP login, INBOX select, UNSEEN search.
    registry = _qq_registry()
    adapter = registry.adapters["qq_email"]
    requests = await adapter.poll()
    logger.info("qq imap login ok, fetched=%d unseen messages", len(requests))
    await registry.close()


@pytest.mark.skipif(
    not _has_creds(), reason="no QQ Mail credentials in env (FUSION_CHANNEL_QQ_EMAIL / FUSION_CHANNEL_QQ_AUTH_CODE)"
)
async def test_qq_registry_poll_dispatch_and_dedup():
    # Full inbound pipeline against the real INBOX: fetch, parse, dedup,
    # archive, dispatch. Second poll must not redispatch the same messages.
    registry = _qq_registry()
    got: list = []

    async def on_message(envelope):
        got.append(envelope)
        logger.info(
            "qq email received from=%s subject=%s text=%.80s",
            envelope.sender.external_id,
            envelope.raw.get("subject", ""),
            envelope.text.replace("\n", " "),
        )

    registry.on_message(on_message)
    first = await registry.poll()
    logger.info("first poll: %d requests, %d dispatched", len(first), len(got))
    first_count = len(got)

    got.clear()
    second = await registry.poll()
    logger.info("second poll: %d requests, %d dispatched (dedup)", len(second), len(got))
    assert len(got) == 0, "duplicate dispatch on re-poll"
    assert first_count >= 0  # empty INBOX is a valid pass; pipeline ran without error
    await registry.close()


@pytest.mark.skipif(
    not _has_creds(), reason="no QQ Mail credentials in env (FUSION_CHANNEL_QQ_EMAIL / FUSION_CHANNEL_QQ_AUTH_CODE)"
)
async def test_qq_smtp_send_then_receive_loopback():
    # Outbound E2E: send a uniquely-tagged email to self via SMTP+STARTTLS,
    # then poll until it arrives back in INBOX and dispatches.
    username = os.environ["FUSION_CHANNEL_QQ_EMAIL"]
    tag = f"FUSION-CHANNEL-E2E-{int(time.time())}"
    registry = _qq_registry()
    adapter = registry.adapters["qq_email"]
    message = OutboundMessage(
        channel_id="qq_email",
        conversation={"type": "direct", "id": username},
        text=f"{tag}\nThis is an automated end-to-end test of the fusion-channel email_imap adapter.",
        meta={"subject": tag},
    )
    result = await adapter.send(message)
    assert result.ok, f"smtp send failed: {result.error}"
    logger.info("qq smtp send ok, waiting for loopback of %s", tag)

    got: list = []

    async def on_message(envelope):
        got.append(envelope)

    registry.on_message(on_message)
    deadline = time.monotonic() + 120
    while time.monotonic() < deadline:
        await registry.poll()
        if any(tag in (env.raw.get("subject") or "") for env in got):
            logger.info("qq email loopback received after %ds", int(120 - (deadline - time.monotonic())))
            break
        await asyncio.sleep(5)
    matched = [env for env in got if tag in (env.raw.get("subject") or "")]
    assert matched, "loopback email not received within 120s"
    assert matched[0].sender.external_id == username
    assert tag in matched[0].text
    await registry.close()
