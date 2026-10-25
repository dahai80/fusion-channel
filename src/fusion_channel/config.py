from __future__ import annotations

import logging
import os
from pathlib import Path
from typing import Any

import yaml

logger = logging.getLogger(__name__)

DEFAULT_RATE_LIMITS: dict[str, int] = {}


class ConfigError(Exception):
    pass


class InboundConfig:
    def __init__(self, data: dict[str, Any] | None):
        data = data or {}
        self.verify = data.get("verify", "required")  # required | disabled
        self.archive = data.get("archive", "full")  # full | metadata-only | off

    def as_dict(self) -> dict:
        return {"verify": self.verify, "archive": self.archive}


class ChannelConfig:
    def __init__(self, data: dict[str, Any]):
        self.id: str = data.get("id", "")
        self.adapter: str = data.get("adapter", "")
        if not self.id or not self.adapter:
            raise ConfigError(f"channel entry needs id and adapter, got id={self.id!r} adapter={self.adapter!r}")
        self.credentials: dict[str, Any] = data.get("credentials", {})
        self.capabilities_override: dict[str, Any] = data.get("capabilities_override", {})
        self.rate_limit: dict[str, int] = data.get("rate_limit", {})
        self.inbound = InboundConfig(data.get("inbound"))
        self.groups: list[str] = data.get("groups", [])
        self.mentions_required: bool = bool(data.get("mentions_required", False))
        self.extra: dict[str, Any] = {
            k: v
            for k, v in data.items()
            if k
            not in {
                "id",
                "adapter",
                "credentials",
                "capabilities_override",
                "rate_limit",
                "inbound",
                "groups",
                "mentions_required",
            }
        }

    def as_dict(self) -> dict:
        return {
            "id": self.id,
            "adapter": self.adapter,
            "credentials": {k: "***" for k in self.credentials},
            "capabilities_override": self.capabilities_override,
            "rate_limit": self.rate_limit,
            "inbound": self.inbound.as_dict(),
            "groups": self.groups,
            "mentions_required": self.mentions_required,
        }


class RootConfig:
    def __init__(self, data: dict[str, Any]):
        self.channels: list[ChannelConfig] = [ChannelConfig(item) for item in data.get("channels", [])]
        seen: set[str] = set()
        for channel in self.channels:
            if channel.id in seen:
                raise ConfigError(f"duplicate channel id {channel.id}")
            seen.add(channel.id)
        self.identity_bindings: list[dict] = data.get("identity_bindings", [])
        self.raw = data

    def channel_by_id(self, channel_id: str) -> ChannelConfig | None:
        for channel in self.channels:
            if channel.id == channel_id:
                return channel
        return None


def _resolve_env(value: str) -> str:
    # Credential sources: "env:VAR_NAME" pulls from environment; raw string
    # is treated as a CredentialProvider key.
    if isinstance(value, str) and value.startswith("env:"):
        return os.environ.get(value[4:], "")
    return value


def load_config(source: str | Path | dict[str, Any]) -> RootConfig:
    if isinstance(source, dict):
        data = source
    else:
        path = Path(source)
        if not path.exists():
            raise ConfigError(f"config file not found: {path}")
        with open(path, encoding="utf-8") as handle:
            data = yaml.safe_load(handle) or {}
    config = RootConfig(data)
    logger.info("config loaded channels=%d bindings=%d", len(config.channels), len(config.identity_bindings))
    return config


def validate_credentials(adapter_cls, channel_config: ChannelConfig, credentials: dict[str, str]) -> list[str]:
    # Field-level validation at save time (PRD §7.1): pair/prefix/format
    # checks come from the adapter's credential_schema.
    errors: list[str] = []
    import re

    for field in adapter_cls.credential_schema():
        value = credentials.get(field.name, "")
        if field.required and not value:
            errors.append(f"{field.name}: required")
            continue
        if value and field.pattern and not re.match(field.pattern, value):
            errors.append(f"{field.name}: format mismatch (expected {field.pattern})")
    return errors
