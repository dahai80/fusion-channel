from __future__ import annotations

import pytest

from fusion_channel.adapters import ADAPTERS
from fusion_channel.config import ConfigError, load_config, validate_credentials
from fusion_channel.credentials import (
    AESGCMCredentialProvider,
    ChainedCredentialProvider,
    CredentialError,
    EnvCredentialProvider,
    MemoryCredentialProvider,
    mask,
)
from fusion_channel.registry import ChannelRegistry
from fusion_channel.storage import MemoryStore
from tests.conftest import channel_config


def test_mask_hides_middle():
    assert mask("abcdefghijklmnop") == "abcd***op"
    assert mask("short") == "sh***"
    assert mask("") == ""


def test_memory_provider_roundtrip():
    provider = MemoryCredentialProvider({"a": "1"})
    assert provider.get("a") == "1"
    provider.put("b", "2")
    assert provider.get("b") == "2"
    assert provider.mask_value("a") == "1***"


def test_env_provider_reads_prefix(monkeypatch):
    monkeypatch.setenv("FUSION_CHANNEL_TWILIO_TOKEN", "env-value")
    provider = EnvCredentialProvider()
    assert provider.get("twilio_token") == "env-value"
    with pytest.raises(CredentialError):
        provider.put("x", "y")


def test_aesgcm_provider_roundtrip(tmp_path):
    provider = AESGCMCredentialProvider(tmp_path)
    provider.put("twilio_auth_token", "secret-value")
    assert provider.get("twilio_auth_token") == "secret-value"
    assert provider.mask_value("twilio_auth_token") == "secr***ue"
    token = provider.encrypt_text("plain")
    assert token.startswith("enc:v1:")
    assert provider.decrypt_text(token) == "plain"


def test_aesgcm_provider_json_roundtrip(tmp_path):
    provider = AESGCMCredentialProvider(tmp_path)
    provider.put_json("integrations", {"account_sid": "AC123", "nested": {"x": 1}})
    assert provider.get_json("integrations") == {"account_sid": "AC123", "nested": {"x": 1}}


def test_aesgcm_provider_key_file_permissions(tmp_path):
    provider = AESGCMCredentialProvider(tmp_path)
    provider.put("k", "v")
    import os
    import stat

    mode = stat.S_IMODE(os.stat(tmp_path / ".secret_key").st_mode)
    assert mode == 0o600


def test_chained_provider_first_wins():
    chain = ChainedCredentialProvider(
        MemoryCredentialProvider({"a": "first"}),
        MemoryCredentialProvider({"a": "second", "b": "second-b"}),
    )
    assert chain.get("a") == "first"
    assert chain.get("b") == "second-b"
    assert chain.get("missing", "default") == "default"


def test_chained_provider_write_goes_to_first_writable():
    readonly_env = EnvCredentialProvider()
    writable = MemoryCredentialProvider()
    chain = ChainedCredentialProvider(readonly_env, writable)
    chain.put("name", "value")
    assert writable.get("name") == "value"


def test_config_requires_id_and_adapter():
    with pytest.raises(ConfigError):
        load_config({"channels": [{"id": "", "adapter": ""}]})


def test_config_rejects_duplicate_channel_ids():
    with pytest.raises(ConfigError):
        load_config({"channels": [{"id": "a", "adapter": "twilio_sms"}, {"id": "a", "adapter": "slack_app"}]})


def test_config_rejects_unknown_adapter():
    with pytest.raises(ConfigError):
        ChannelRegistry.from_config(
            {"channels": [{"id": "x", "adapter": "nope"}]},
            credentials=MemoryCredentialProvider(),
            store=MemoryStore(),
        )


def test_config_defaults():
    config = load_config({"channels": [{"id": "x", "adapter": "twilio_sms"}]})
    channel = config.channels[0]
    assert channel.inbound.verify == "required"
    assert channel.inbound.archive == "full"
    assert channel.groups == []
    assert channel.mentions_required is False


def test_registry_loads_identity_bindings(registry):
    assert registry.identify("family_twilio", "+15550000002") == "identity:u_owner"
    assert registry.identify("home_whatsapp", "8613800000002") == "identity:u_owner"
    assert registry.identify("family_twilio", "+15550000009") is None


def test_registry_bind_identity_roundtrip(registry):
    registry.bind_identity("identity:u_helper", "home_telegram", "999001")
    assert registry.identify("home_telegram", "999001") == "identity:u_helper"


def test_no_verify_request_no_registration():
    # SPI rule: an adapter class without its own verify_request cannot register.
    class BareAdapter(ADAPTERS["twilio_sms"]):
        @classmethod
        def adapter_id(cls):
            return "bare_test"

        verify_request = None

    with pytest.raises(ConfigError):
        ChannelRegistry.from_config(
            {"channels": [{"id": "bare", "adapter": "twilio_sms"}]},
            credentials=MemoryCredentialProvider(),
            store=MemoryStore(),
            adapter_classes={"twilio_sms": BareAdapter},
        )


def test_validate_credentials_field_errors():
    adapter_cls = ADAPTERS["twilio_sms"]
    errors = validate_credentials(
        adapter_cls,
        channel_config()["channels"][0],
        {"account_sid": "WRONG", "auth_token": "", "from_number": "1555"},
    )
    fields = {error.split(":")[0] for error in errors}
    assert "account_sid" in fields
    assert "auth_token" in fields
    assert "from_number" in fields
