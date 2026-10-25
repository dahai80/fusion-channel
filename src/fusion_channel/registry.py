from __future__ import annotations

import logging
from collections.abc import Awaitable, Callable
from typing import Any

from fusion_channel.cards import DEFAULT_RENDERER, CommandParser
from fusion_channel.config import ChannelConfig, ConfigError, RootConfig, load_config
from fusion_channel.credentials import CredentialProvider, MemoryCredentialProvider
from fusion_channel.hooks import ArchiveHook, AuditHook
from fusion_channel.identity import IdentityMapper
from fusion_channel.models import (
    ChannelRequest,
    InboundEnvelope,
    Interaction,
    OutboundMessage,
    RejectedEnvelope,
    WebhookResponse,
)
from fusion_channel.outbox import OutboxSender
from fusion_channel.spi import ChannelAdapter, FusionChannelError
from fusion_channel.storage import ChannelStore, MemoryStore

logger = logging.getLogger(__name__)

MessageHandler = Callable[[InboundEnvelope], Awaitable[None]]
InteractionHandler = Callable[[Interaction], Awaitable[None]]


class HandleInboundResult:
    def __init__(self, envelope: InboundEnvelope | None, response: WebhookResponse, reason: str = ""):
        self.envelope = envelope
        self.response = response
        self.reason = reason

    def __bool__(self) -> bool:
        return self.envelope is not None


class ChannelRegistry:
    # Facade (PRD §5.2): consumers only touch handle_inbound / send /
    # on_interaction / on_message / identify / bind_identity.

    def __init__(
        self,
        store: ChannelStore | None = None,
        credentials: CredentialProvider | None = None,
    ):
        self.store = store or MemoryStore()
        self.credentials = credentials or MemoryCredentialProvider()
        self.channels: dict[str, ChannelConfig] = {}
        self.adapters: dict[str, ChannelAdapter] = {}
        self.identity = IdentityMapper()
        self.archive = ArchiveHook(self.store)
        self.audit = AuditHook()
        self.outbox = OutboxSender(self.store, self.adapters)
        self.command_parser = CommandParser()
        self._message_handlers: list[MessageHandler] = []
        self._interaction_handlers: list[InteractionHandler] = []

    @classmethod
    def from_config(
        cls,
        config: str | Any,
        credentials: CredentialProvider | None = None,
        store: ChannelStore | None = None,
        adapter_classes: dict[str, type[ChannelAdapter]] | None = None,
    ) -> ChannelRegistry:
        from fusion_channel.adapters import ADAPTERS

        root: RootConfig = load_config(config)
        registry = cls(store=store, credentials=credentials)
        classes = dict(adapter_classes or ADAPTERS)
        for channel_config in root.channels:
            adapter_cls = classes.get(channel_config.adapter)
            if adapter_cls is None:
                raise ConfigError(f"unknown adapter {channel_config.adapter} (channel {channel_config.id})")
            _assert_verify_implemented(adapter_cls)
            resolved = {
                name: registry.credentials.get(_resolve_credential_ref(value) or f"{channel_config.id}:{name}", "")
                for name, value in channel_config.credentials.items()
            }
            adapter = adapter_cls(channel_config.id, resolved, channel_config.extra)
            registry.channels[channel_config.id] = channel_config
            registry.adapters[channel_config.id] = adapter
            logger.info("channel registered id=%s adapter=%s", channel_config.id, channel_config.adapter)
        registry.identity.load(root.identity_bindings)
        registry.outbox = OutboxSender(registry.store, registry.adapters, rate_limits=merged_rate_limits(root))
        return registry

    # ---- inbound ----

    async def handle_inbound(self, channel_id: str, request: ChannelRequest) -> HandleInboundResult:
        # verify -> reject-archive -> parse -> dedup -> archive -> dispatch
        channel_config = self.channels.get(channel_id)
        adapter = self.adapters.get(channel_id)
        if adapter is None or channel_config is None:
            logger.warning("inbound on unknown channel=%s", channel_id)
            return HandleInboundResult(
                None, WebhookResponse(status_code=404, body="unknown channel"), "unknown_channel"
            )

        if channel_config.inbound.verify == "required":
            result = adapter.verify_request(request)
            if not result.ok:
                await self._reject(channel_id, request, f"invalid_signature:{result.reason or 'verify_failed'}")
                return HandleInboundResult(
                    None,
                    WebhookResponse(status_code=403, body="invalid signature", media_type="text/plain"),
                    "invalid_signature",
                )
        elif channel_config.inbound.verify == "disabled":
            logger.warning("channel %s has verify=disabled — INSECURE, test only", channel_id)

        envelope = adapter.parse_inbound(request)

        conversation = envelope.conversation
        if (
            conversation.type == "group"
            and channel_config.groups
            and conversation.title not in channel_config.groups
            and conversation.id not in channel_config.groups
        ):
            await self.archive.on_inbound(envelope)
            logger.info("group not in allowlist channel=%s conversation=%s", channel_id, conversation.id[:24])
            return HandleInboundResult(None, WebhookResponse(), "group_not_watched")

        if channel_config.mentions_required and conversation.type == "group":
            bot_mentioned = any(m.external_id in adapter.config.get("bot_ids", []) for m in envelope.mentions)
            if not bot_mentioned and not _mentions_bot_text(envelope.text, adapter.config.get("bot_names", [])):
                await self.archive.on_inbound(envelope)
                return HandleInboundResult(None, WebhookResponse(), "no_mention")

        if channel_config.inbound.archive != "off":
            saved = await self.archive.on_inbound_dedup(envelope)
            if not saved:
                await self.store.bump_duplicate(channel_id, envelope.message_id)
                logger.info("duplicate inbound channel=%s message_id=%s", channel_id, envelope.message_id[:24])
                return HandleInboundResult(None, WebhookResponse(), "duplicate")
        else:
            if not await self.store.save_inbound(channel_id, envelope.message_id, {"kind": "dedup_only"}):
                return HandleInboundResult(None, WebhookResponse(), "duplicate")

        if envelope.interaction is None:
            command = self.command_parser.parse(
                envelope.text,
                message_id=envelope.message_id,
                actor=envelope.sender,
                conversation=envelope.conversation,
            )
            if command is not None:
                envelope.interaction = command

        if envelope.interaction is not None and await self._dedup_interaction(channel_id, envelope.interaction):
            for handler in self._interaction_handlers:
                try:
                    await handler(envelope.interaction)
                except Exception as exc:
                    logger.error("interaction handler failed action=%s err=%s", envelope.interaction.action, exc)

        for handler in self._message_handlers:
            try:
                await handler(envelope)
            except Exception as exc:
                logger.error("message handler failed channel=%s err=%s", channel_id, exc)

        response = adapter.webhook_response(request, envelope) or WebhookResponse()
        return HandleInboundResult(envelope, response)

    async def _reject(self, channel_id: str, request: ChannelRequest, reason: str) -> None:
        rejected = RejectedEnvelope(
            channel_id=channel_id,
            reason=reason,
            raw={
                "method": request.method,
                "url": request.url,
                "headers": {k: v for k, v in request.headers.items() if k.lower() not in ("authorization", "cookie")},
                "form": request.form,
                "body": request.body.decode("utf-8", errors="replace")[:4000],
                "json": request.json_body,
            },
        )
        await self.archive.on_rejected(rejected)
        await self.audit.emit(
            actor="system",
            action="channel.rejected",
            target=f"channel:{channel_id}",
            detail={"reason": reason},
        )

    async def _dedup_interaction(self, channel_id: str, interaction: Interaction) -> bool:
        key = f"interaction:{interaction.action}:{interaction.payload.get('code', '')}:{interaction.message_id}"
        return await self.store.save_inbound(
            channel_id, key, {"kind": "interaction", "interaction": interaction.model_dump(mode="json")}
        )

    # ---- outbound ----

    async def send(self, channel_id: str, conversation, spec: dict | None = None, **kwargs) -> int | None:
        # Outbox semantics: returns the outbox id, or None on dedup hit.
        data = dict(spec or {})
        data.update(kwargs)
        data.setdefault("channel_id", channel_id)
        data.setdefault("conversation", conversation if not isinstance(conversation, dict) else None)
        if isinstance(data.get("conversation"), dict):
            from fusion_channel.models import Conversation

            data["conversation"] = Conversation.model_validate(data["conversation"])
        message = OutboundMessage.model_validate(data)
        adapter = self.adapters.get(channel_id)
        if adapter is None:
            raise FusionChannelError(f"unknown channel {channel_id}")
        if not adapter.capabilities().CAN_SEND:
            raise FusionChannelError(f"channel {channel_id} cannot send")
        rendered = self.render_outgoing(adapter, message)
        message.meta["rendered"] = rendered
        await self.archive.on_outbound(message)
        return await self.outbox.enqueue(message)

    def render_outgoing(self, adapter: ChannelAdapter, message: OutboundMessage) -> dict:
        caps = _effective_capabilities(adapter, self.channels.get(message.channel_id))
        if message.card is not None:
            rendered = DEFAULT_RENDERER.render(caps, adapter.adapter_id(), message.card, message.text)
            return {
                "text": rendered.text,
                "card_payload": rendered.card_payload,
                "used_fallback": rendered.used_fallback,
            }
        return {"text": message.text, "card_payload": None, "used_fallback": False}

    # ---- handlers / identity ----

    def on_message(self, handler: MessageHandler) -> None:
        self._message_handlers.append(handler)

    def on_interaction(self, handler: InteractionHandler) -> None:
        self._interaction_handlers.append(handler)

    def register_command(self, action: str, aliases: list[str], code_required: bool = True) -> None:
        self.command_parser.register(action, aliases, code_required=code_required)

    def identify(self, channel_id: str, external_id: str) -> str | None:
        return self.identity.identify(channel_id, external_id)

    def bind_identity(self, identity_id: str, channel_id: str, external_id: str) -> None:
        self.identity.bind_identity(identity_id, channel_id, external_id)

    def adapter(self, channel_id: str) -> ChannelAdapter:
        adapter = self.adapters.get(channel_id)
        if adapter is None:
            raise FusionChannelError(f"unknown channel {channel_id}")
        return adapter

    async def test_connection(self, channel_id: str) -> Any:
        return await self.adapter(channel_id).test_connection()

    async def init(self) -> None:
        await self.store.init()

    async def close(self) -> None:
        await self.store.close()


def merged_rate_limits(root: RootConfig) -> dict[str, int]:
    limits: dict[str, int] = {}
    for channel in root.channels:
        if channel.rate_limit.get("per_minute"):
            limits[channel.id] = int(channel.rate_limit["per_minute"])
    return limits


def _resolve_credential_ref(value: Any) -> str:
    if isinstance(value, str):
        return value
    return ""


def _assert_verify_implemented(adapter_cls: type[ChannelAdapter]) -> None:
    # SPI-level rule (PRD §4.2): no verify_request, no registration.
    verify = adapter_cls.__dict__.get("verify_request")
    if verify is None or not callable(verify):
        raise ConfigError(f"adapter {adapter_cls.__name__} does not implement verify_request — cannot register")


def _effective_capabilities(adapter: ChannelAdapter, channel_config: ChannelConfig | None):
    caps = adapter.capabilities()
    if channel_config and channel_config.capabilities_override:
        return caps.__class__(**{**caps.as_dict(), **channel_config.capabilities_override})
    return caps


def _mentions_bot_text(text: str, bot_names: list[str]) -> bool:
    return any(name and name in text for name in bot_names)
