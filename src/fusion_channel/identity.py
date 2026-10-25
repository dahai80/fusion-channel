from __future__ import annotations

import logging

logger = logging.getLogger(__name__)


class IdentityNotBound(Exception):
    pass


class IdentityMapper:
    # External identities (E.164 phone / open_id / user_id) to fusion-identity
    # accounts. One account may bind to multiple channels (PRD §4.1).
    # Bindings persist in the store-agnostic dict passed by the consumer;
    # RBAC stays out (fusion-identity owns it).

    def __init__(self):
        self._by_binding: dict[tuple[str, str], str] = {}
        self._by_identity: dict[str, list[tuple[str, str]]] = {}

    def bind_identity(self, identity_id: str, channel_id: str, external_id: str, tenant: str | None = None) -> None:
        key = (channel_id, external_id)
        self._by_binding[key] = identity_id
        self._by_identity.setdefault(identity_id, []).append(key)
        logger.info("identity bound identity=%s channel=%s external=%s", identity_id, channel_id, _mask(external_id))

    def unbind_identity(self, identity_id: str, channel_id: str, external_id: str) -> None:
        key = (channel_id, external_id)
        if self._by_binding.get(key) == identity_id:
            del self._by_binding[key]
        bindings = self._by_identity.get(identity_id, [])
        self._by_identity[identity_id] = [item for item in bindings if item != key]

    def identify(self, channel_id: str, external_id: str, tenant: str | None = None) -> str | None:
        return self._by_binding.get((channel_id, external_id))

    def bindings_of(self, identity_id: str) -> list[tuple[str, str]]:
        return list(self._by_identity.get(identity_id, []))

    def load(self, bindings: list[dict]) -> None:
        # Two accepted shapes: the documented YAML grouping
        # ({identity, bindings: [{channel, external_id}]}) and a flat
        # [{identity, channel, external_id}] list.
        for item in bindings:
            if "bindings" in item:
                for binding in item["bindings"]:
                    self.bind_identity(item["identity"], binding["channel"], binding["external_id"])
            else:
                self.bind_identity(item["identity"], item["channel"], item["external_id"])

    def assert_bound_identity(self, channel_id: str, external_id: str, identity_id: str | None = None) -> str:
        # Two-factor primitive for sensitive commands (PRD §7.1):
        # the external identity must be bound; optionally to a specific account.
        resolved = self.identify(channel_id, external_id)
        if resolved is None:
            raise IdentityNotBound(f"channel={channel_id} external_id={_mask(external_id)} not bound")
        if identity_id is not None and resolved != identity_id:
            raise IdentityNotBound(f"channel={channel_id} bound to different identity")
        return resolved


def _mask(value: str) -> str:
    if len(value) <= 6:
        return value[:2] + "***"
    return f"{value[:4]}***{value[-2:]}"
