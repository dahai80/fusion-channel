from __future__ import annotations

import logging
from abc import ABC, abstractmethod
from typing import Any

from fusion_channel.capabilities import ChannelCapabilities
from fusion_channel.models import (
    Card,
    ChannelRequest,
    InboundEnvelope,
    OutboundMessage,
    SendResult,
    TestResult,
    VerifyResult,
)

logger = logging.getLogger(__name__)


class FusionChannelError(Exception):
    pass


class CapabilityNotSupported(FusionChannelError):
    pass


class VerificationError(FusionChannelError):
    pass


class CredentialField:
    def __init__(
        self,
        name: str,
        required: bool = True,
        secret: bool = True,
        pattern: str | None = None,
        label: str = "",
        help: str = "",
    ):
        self.name = name
        self.required = required
        self.secret = secret
        self.pattern = pattern
        self.label = label or name
        self.help = help

    def as_dict(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "required": self.required,
            "secret": self.secret,
            "pattern": self.pattern,
            "label": self.label,
            "help": self.help,
        }


class GroupOps(ABC):
    # Optional capability: only CAN_GROUP adapters expose it.

    @abstractmethod
    async def list(self) -> list: ...

    @abstractmethod
    async def info(self, group_id: str): ...

    @abstractmethod
    async def broadcast(self, conversation, text: str) -> SendResult: ...


class ChannelAdapter(ABC):
    # verify_request is abstract on purpose: an adapter that does not
    # implement request verification cannot be instantiated or registered.

    def __init__(self, channel_id: str, credentials: dict[str, str], config: dict[str, Any] | None = None):
        self.channel_id = channel_id
        self.credentials = credentials
        self.config = config or {}

    @classmethod
    @abstractmethod
    def adapter_id(cls) -> str: ...

    @classmethod
    @abstractmethod
    def capabilities(cls) -> ChannelCapabilities: ...

    @classmethod
    @abstractmethod
    def credential_schema(cls) -> list[CredentialField]: ...

    @abstractmethod
    def verify_request(self, request: ChannelRequest) -> VerifyResult: ...

    @abstractmethod
    def parse_inbound(self, request: ChannelRequest) -> InboundEnvelope: ...

    @abstractmethod
    async def send(self, message: OutboundMessage) -> SendResult: ...

    @abstractmethod
    async def test_connection(self) -> TestResult: ...

    def webhook_response(self, request: ChannelRequest, envelope: InboundEnvelope | None) -> Any:
        return None

    def parse_event(self, request: ChannelRequest) -> InboundEnvelope | None:
        # Secondary callback stream (card actions, receipts). Adapters that
        # receive interactions on a separate endpoint override this.
        return None

    async def group_ops(self) -> GroupOps:
        caps = self.capabilities()
        if not caps.CAN_GROUP:
            raise CapabilityNotSupported(f"adapter={self.adapter_id()} has no group support")
        raise NotImplementedError(f"adapter={self.adapter_id()} does not implement group_ops")

    def render_card(self, card: Card) -> Any:
        caps = self.capabilities()
        if caps.CAN_CARDS == "none":
            raise CapabilityNotSupported(f"adapter={self.adapter_id()} has no card support")
        raise NotImplementedError(f"adapter={self.adapter_id()} does not implement render_card")

    def http_client_kwargs(self) -> dict[str, Any]:
        return {"timeout": 15.0}
