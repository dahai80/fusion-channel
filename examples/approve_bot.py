from __future__ import annotations

import asyncio
import logging

import uvicorn
from fastapi import FastAPI

from fusion_channel import ApprovalCard, CardAction, ChannelRegistry
from fusion_channel.credentials import AESGCMCredentialProvider
from fusion_channel.fastapi import build_channel_router
from fusion_channel.storage import SqliteStore

logging.basicConfig(level=logging.INFO)

CONFIG_PATH = "examples/channels.yaml"
DB_PATH = "channel.db"


async def main() -> None:
    store = SqliteStore(DB_PATH)
    await store.init()
    credentials = AESGCMCredentialProvider("data/secrets")
    registry = ChannelRegistry.from_config(CONFIG_PATH, credentials=credentials, store=store)

    # One approval state machine, channel-agnostic: card buttons on Feishu /
    # Slack / DingTalk / Telegram and text commands ("同意 AP-123456") on any
    # channel all land in the same handler.
    decisions: dict[str, str] = {}

    async def on_interaction(interaction):
        code = interaction.payload.get("code") or interaction.payload.get("value") or ""
        decisions[code] = interaction.action
        logging.info("approval decision code=%s action=%s", code, interaction.action)

    registry.on_interaction(on_interaction)
    registry.register_command("approve", ["同意", "approve"], code_required=True)
    registry.register_command("reject", ["拒绝", "reject"], code_required=True)

    async def on_message(envelope):
        logging.info(
            "message channel=%s from=%s text=%s", envelope.channel_id, envelope.sender.external_id, envelope.text[:40]
        )

    registry.on_message(on_message)

    # Outbound with automatic card degradation: full cards on Feishu,
    # "回复 同意/拒绝 AP-123456" fallback on SMS.
    await registry.send(
        "family_twilio",
        conversation={"type": "direct", "id": "+15550000001->+15550000000"},
        card=ApprovalCard(
            title="审批 AP-123456",
            code="AP-123456",
            body="新增雇员张阿姨",
            actions=[CardAction(action="approve", label="同意"), CardAction(action="reject", label="拒绝")],
        ),
    )

    app = FastAPI()
    app.include_router(build_channel_router(registry))
    outbox_task = asyncio.create_task(registry.outbox.run_loop(interval=5.0))
    try:
        config = uvicorn.Config(app, host="127.0.0.1", port=8090, log_level="info")
        server = uvicorn.Server(config)
        await server.serve()
    finally:
        outbox_task.cancel()
        await registry.close()


if __name__ == "__main__":
    asyncio.run(main())
