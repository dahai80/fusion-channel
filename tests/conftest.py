from __future__ import annotations

import base64
import hashlib
import hmac
import json

import pytest

from fusion_channel.credentials import MemoryCredentialProvider
from fusion_channel.registry import ChannelRegistry
from fusion_channel.storage import MemoryStore

TWILIO_TOKEN = "twilio-auth-token-123"
SLACK_SIGNING_SECRET = "slack-signing-secret"
DINGTALK_SECRET = "SEC" + "a" * 40
FEISHU_ENCRYPT_KEY = "feishu-encrypt-key"
FEISHU_VERIFY_TOKEN = "feishu-verification-token"
WECOM_TOKEN = "wecom-callback-token"
WECOM_AES_KEY = "A" * 43
WHATSAPP_APP_SECRET = "whatsapp-app-secret"
TELEGRAM_SECRET = "telegram-secret-token"


def channel_config() -> dict:
    return {
        "channels": [
            {
                "id": "family_twilio",
                "adapter": "twilio_sms",
                "credentials": {
                    "account_sid": "AC" + "0" * 32,
                    "auth_token": TWILIO_TOKEN,
                    "from_number": "+15550000001",
                },
            },
            {
                "id": "family_feishu",
                "adapter": "feishu_app",
                "credentials": {
                    "app_id": "cli_test123",
                    "app_secret": "app-secret",
                    "encrypt_key": FEISHU_ENCRYPT_KEY,
                    "verification_token": FEISHU_VERIFY_TOKEN,
                },
                "groups": ["oc_family"],
                "mentions_required": True,
            },
            {
                "id": "team_slack",
                "adapter": "slack_app",
                "credentials": {"bot_token": "xoxb-test-token", "signing_secret": SLACK_SIGNING_SECRET},
            },
            {
                "id": "home_dingtalk",
                "adapter": "dingtalk_robot",
                "credentials": {"access_token": "dingtalk-access-token", "secret": DINGTALK_SECRET},
            },
            {
                "id": "home_wecom",
                "adapter": "wecom_app",
                "credentials": {
                    "corp_id": "corp-id",
                    "agent_id": "1000001",
                    "corp_secret": "corp-secret",
                    "token": WECOM_TOKEN,
                    "encoding_aes_key": WECOM_AES_KEY,
                },
            },
            {
                "id": "home_whatsapp",
                "adapter": "whatsapp_cloud",
                "credentials": {
                    "phone_number_id": "123456789",
                    "access_token": "wa-token",
                    "app_secret": WHATSAPP_APP_SECRET,
                    "verify_token": "wa-verify",
                },
            },
            {
                "id": "home_telegram",
                "adapter": "telegram_bot",
                "credentials": {"bot_token": "123456:ABC-DEF", "secret_token": TELEGRAM_SECRET},
            },
        ],
        "identity_bindings": [
            {
                "identity": "identity:u_owner",
                "bindings": [
                    {"channel": "family_twilio", "external_id": "+15550000002"},
                    {"channel": "home_whatsapp", "external_id": "8613800000002"},
                ],
            },
        ],
    }


def flat_credentials() -> dict[str, str]:
    config = channel_config()
    flat: dict[str, str] = {}
    for channel in config["channels"]:
        for name, value in channel["credentials"].items():
            flat[f"{channel['id']}:{name}"] = value
    return flat


@pytest.fixture
def memory_store():
    return MemoryStore()


@pytest.fixture
def registry(memory_store):
    provider = MemoryCredentialProvider(flat_credentials())
    registry = ChannelRegistry.from_config(channel_config(), credentials=provider, store=memory_store)
    return registry


def twilio_sign(url: str, params: dict[str, str], token: str = TWILIO_TOKEN) -> str:
    sorted_items = sorted(params.items())
    payload = url + "".join(f"{k}{v}" for k, v in sorted_items)
    digest = hmac.new(token.encode(), payload.encode(), hashlib.sha1).digest()
    return base64.b64encode(digest).decode()


def slack_sign(timestamp: str, body: str, secret: str = SLACK_SIGNING_SECRET) -> str:
    base = f"v0:{timestamp}:{body}"
    return "v0=" + hmac.new(secret.encode(), base.encode(), hashlib.sha256).hexdigest()


def dingtalk_sign(secret: str, timestamp: int) -> str:
    string_to_sign = f"{timestamp}\n{secret}"
    digest = hmac.new(secret.encode(), string_to_sign.encode(), hashlib.sha256).digest()
    return base64.b64encode(digest).decode()


def whatsapp_sign(body: bytes, secret: str = WHATSAPP_APP_SECRET) -> str:
    return "sha256=" + hmac.new(secret.encode(), body, hashlib.sha256).hexdigest()


def feishu_payload(data: dict, encrypt_key: str = FEISHU_ENCRYPT_KEY) -> bytes:
    from fusion_channel.verify import feishu_encrypt

    encrypted = feishu_encrypt(encrypt_key, json.dumps(data, ensure_ascii=False))
    return json.dumps({"encrypt": encrypted}).encode("utf-8")
