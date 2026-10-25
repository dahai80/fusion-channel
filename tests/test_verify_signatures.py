from __future__ import annotations

import json
import time

import pytest

from fusion_channel.verify import (
    compute_dingtalk_sign,
    feishu_decrypt,
    feishu_encrypt,
    tencent_sig,
    verify_dingtalk_signature,
    verify_hmac_sha256_hex,
    verify_slack_signature,
    verify_telegram_secret_token,
    verify_twilio_signature,
    verify_wecom_signature,
    verify_whatsapp_signature,
    wecom_decrypt,
    wecom_encrypt,
    wecom_signature,
)
from tests.conftest import dingtalk_sign, slack_sign, twilio_sign


def test_twilio_signature_roundtrip():
    params = {"MessageSid": "SM123", "From": "+15550000002", "Body": "同意 AP-123456"}
    url = "https://example.com/webhook/family_twilio"
    signature = twilio_sign(url, params)
    assert verify_twilio_signature(url, params, signature, "twilio-auth-token-123")
    assert not verify_twilio_signature(url, params, signature, "wrong-token")
    assert not verify_twilio_signature(url, params, "", "twilio-auth-token-123")


def test_twilio_signature_param_order_independent():
    params = {"b": "2", "a": "1"}
    url = "https://example.com/hook"
    signature = twilio_sign(url, params)
    assert verify_twilio_signature(url, {"a": "1", "b": "2"}, signature, "twilio-auth-token-123")


def test_slack_signature_roundtrip():
    body = '{"type":"event_callback"}'
    timestamp = str(int(time.time()))
    signature = slack_sign(timestamp, body)
    assert verify_slack_signature("slack-signing-secret", timestamp, body, signature)
    assert not verify_slack_signature("slack-signing-secret", timestamp, body + "x", signature)


def test_slack_signature_rejects_stale_timestamp():
    body = "{}"
    timestamp = str(int(time.time()) - 3600)
    signature = slack_sign(timestamp, body)
    assert not verify_slack_signature("slack-signing-secret", timestamp, body, signature)


def test_dingtalk_sign_roundtrip():
    timestamp = str(int(time.time() * 1000))
    sign = dingtalk_sign("SEC" + "a" * 40, timestamp)
    assert verify_dingtalk_signature("SEC" + "a" * 40, timestamp, sign)
    assert compute_dingtalk_sign("secret", 1000) == compute_dingtalk_sign("secret", "1000")


def test_dingtalk_sign_rejects_stale():
    timestamp = str(int(time.time() * 1000) - 7200_000)
    sign = dingtalk_sign("SEC" + "a" * 40, timestamp)
    assert not verify_dingtalk_signature("SEC" + "a" * 40, timestamp, sign)


def test_whatsapp_signature_roundtrip():
    body = b'{"entry":[]}'
    signature = (
        "sha256=" + __import__("hmac").new(b"whatsapp-app-secret", body, __import__("hashlib").sha256).hexdigest()
    )
    assert verify_whatsapp_signature("whatsapp-app-secret", body, signature)
    assert not verify_whatsapp_signature("whatsapp-app-secret", body + b"x", signature)


def test_telegram_secret_token():
    assert verify_telegram_secret_token("expected", "expected")
    assert not verify_telegram_secret_token("expected", "other")
    assert not verify_telegram_secret_token("", "x")


def test_feishu_encrypt_decrypt_roundtrip():
    data = {"challenge": "abc", "token": "t", "type": "url_verification"}
    encrypted = feishu_encrypt("feishu-encrypt-key", json.dumps(data))
    assert feishu_decrypt("feishu-encrypt-key", encrypted) == data
    from cryptography.exceptions import InvalidTag

    with pytest.raises((InvalidTag, ValueError)):
        feishu_decrypt("wrong-key", encrypted)


def test_wecom_signature_and_decrypt_roundtrip():
    encrypt_xml = wecom_encrypt("A" * 43, "<xml><Content>hello</Content></xml>", "corp-id")
    timestamp, nonce = "1234567890", "nonce1"
    signature = wecom_signature("wecom-callback-token", timestamp, nonce, encrypt_xml)
    assert verify_wecom_signature("wecom-callback-token", timestamp, nonce, encrypt_xml, signature)
    assert not verify_wecom_signature("wrong-token", timestamp, nonce, encrypt_xml, signature)
    plain = wecom_decrypt("A" * 43, encrypt_xml)
    assert plain.decode("utf-8") == "<xml><Content>hello</Content></xml>"


def test_generic_hmac_hex_roundtrip():
    import hashlib
    import hmac as hmac_mod

    body = b"raw-body"
    signature = hmac_mod.new(b"secret", body, hashlib.sha256).hexdigest()
    assert verify_hmac_sha256_hex("secret", body, signature)
    assert verify_hmac_sha256_hex("secret", body, "sha256=" + signature)
    assert not verify_hmac_sha256_hex("secret", body, "0" * 64)


def test_tencent_sig_deterministic():
    assert tencent_sig("secret", 1000) == tencent_sig("secret", "1000")
    assert tencent_sig("secret", 1000) != tencent_sig("secret", 1001)
