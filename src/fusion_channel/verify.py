from __future__ import annotations

import base64
import hashlib
import hmac
import logging
import time

from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes

logger = logging.getLogger(__name__)

SLACK_MAX_AGE_SECONDS = 300
DINGTALK_MAX_AGE_SECONDS = 300
WECOM_MAX_AGE_SECONDS = 300


def _ct_eq(left: str, right: str) -> bool:
    # hmac.compare_digest raises TypeError on non-ASCII str; compare bytes so
    # an attacker-controlled header can never trigger an unhandled exception.
    return hmac.compare_digest(left.encode("utf-8", errors="replace"), right.encode("utf-8", errors="replace"))


def verify_twilio_signature(url: str, params: dict[str, str], signature: str, auth_token: str) -> bool:
    # Ported from ai-family sms/twilio_webhook.py (M1 behavior-equivalent).
    if not auth_token or not signature:
        return False
    sorted_items = sorted(params.items())
    payload = url + "".join(f"{k}{v}" for k, v in sorted_items)
    digest = hmac.new(auth_token.encode(), payload.encode(), hashlib.sha1).digest()
    expected = base64.b64encode(digest).decode()
    return _ct_eq(expected, signature)


def verify_slack_signature(signing_secret: str, timestamp: str, body: str, signature: str) -> bool:
    if not signing_secret or not signature or not timestamp:
        return False
    try:
        ts = int(timestamp)
    except ValueError:
        return False
    if abs(time.time() - ts) > SLACK_MAX_AGE_SECONDS:
        logger.warning("slack signature stale timestamp=%s", timestamp)
        return False
    base = f"v0:{timestamp}:{body}"
    expected = "v0=" + hmac.new(signing_secret.encode(), base.encode(), hashlib.sha256).hexdigest()
    return _ct_eq(expected, signature)


def compute_dingtalk_sign(secret: str, timestamp: int | str) -> str:
    string_to_sign = f"{timestamp}\n{secret}"
    digest = hmac.new(secret.encode(), string_to_sign.encode(), hashlib.sha256).digest()
    return base64.b64encode(digest).decode()


def verify_dingtalk_signature(
    secret: str, timestamp: str, sign: str, max_age_seconds: int = DINGTALK_MAX_AGE_SECONDS
) -> bool:
    # Protocol limitation: the robot sign covers only (timestamp, secret),
    # never the body — a captured sign is replayable with any body inside
    # the freshness window. Keep the window tight (300s).
    if not secret or not sign or not timestamp:
        return False
    try:
        ts = int(timestamp)
    except ValueError:
        return False
    # DingTalk robots sign with millisecond timestamps; normalize to seconds.
    if ts > 10_000_000_000:
        ts = ts // 1000
    if abs(time.time() - ts) > max_age_seconds:
        logger.warning("dingtalk signature stale timestamp=%s", timestamp)
        return False
    expected = compute_dingtalk_sign(secret, timestamp)
    return _ct_eq(expected, sign)


def verify_whatsapp_signature(app_secret: str, raw_body: bytes, x_hub_signature: str) -> bool:
    if not app_secret or not x_hub_signature:
        return False
    expected = "sha256=" + hmac.new(app_secret.encode(), raw_body, hashlib.sha256).hexdigest()
    return _ct_eq(expected, x_hub_signature)


def verify_telegram_secret_token(expected_token: str, received: str) -> bool:
    if not expected_token or not received:
        return False
    return _ct_eq(expected_token, received)


def _pkcs7_unpad(data: bytes) -> bytes:
    if not data:
        return data
    pad = data[-1]
    if pad < 1 or pad > 32:
        raise ValueError("invalid PKCS7 padding")
    return data[:-pad]


def feishu_decrypt(encrypt_key: str, encrypted_b64: str) -> dict:
    # Feishu event callback: AES-256-CBC, key = sha256(encrypt_key), iv = key[:16].
    import json

    key = hashlib.sha256(encrypt_key.encode()).digest()
    blob = base64.b64decode(encrypted_b64)
    if len(blob) < 32 or len(blob) % 16 != 0:
        raise ValueError("feishu encrypted payload has invalid length")
    iv = key[:16]
    decryptor = Cipher(algorithms.AES(key), modes.CBC(iv)).decryptor()
    plain = _pkcs7_unpad(decryptor.update(blob) + decryptor.finalize())
    return json.loads(plain.decode("utf-8"))


def feishu_encrypt(encrypt_key: str, plaintext: str) -> str:
    # Test helper implementing Feishu's documented encryption so tests can
    # produce real encrypted payloads.

    key = hashlib.sha256(encrypt_key.encode()).digest()
    data = plaintext.encode("utf-8")
    pad = 32 - (len(data) % 32)
    data = data + bytes([pad]) * pad
    iv = key[:16]
    encryptor = Cipher(algorithms.AES(key), modes.CBC(iv)).encryptor()
    blob = encryptor.update(data) + encryptor.finalize()
    return base64.b64encode(blob).decode()


def wecom_signature(token: str, timestamp: str, nonce: str, encrypt_xml: str) -> str:
    items = sorted([token, timestamp, nonce, encrypt_xml])
    return hashlib.sha1("".join(items).encode()).hexdigest()


def verify_wecom_signature(
    token: str,
    timestamp: str,
    nonce: str,
    encrypt_xml: str,
    msg_signature: str,
    max_age_seconds: int = WECOM_MAX_AGE_SECONDS,
) -> bool:
    if not token or not msg_signature:
        return False
    try:
        ts = int(timestamp)
    except (TypeError, ValueError):
        return False
    if abs(time.time() - ts) > max_age_seconds:
        logger.warning("wecom signature stale timestamp=%s", timestamp)
        return False
    expected = wecom_signature(token, timestamp, nonce, encrypt_xml)
    return _ct_eq(expected, msg_signature)


def wecom_decrypt(encoding_aes_key: str, encrypted_b64: str, expected_receive_id: str = "") -> bytes:
    # WeCom callback: AES-256-CBC, key = base64decode(encoding_aes_key + "="), iv = key[:16].
    key = base64.b64decode(encoding_aes_key + "=")
    if len(key) != 32:
        raise ValueError("wecom encoding_aes_key must decode to 32 bytes")
    blob = base64.b64decode(encrypted_b64)
    iv = key[:16]
    decryptor = Cipher(algorithms.AES(key), modes.CBC(iv)).decryptor()
    plain = _pkcs7_unpad(decryptor.update(blob) + decryptor.finalize())
    # Layout: 16 random bytes + 4 bytes msg_len (network order) + msg + receiveid
    if len(plain) < 20:
        raise ValueError("wecom plaintext too short")
    msg_len = int.from_bytes(plain[16:20], "big")
    if msg_len < 0 or len(plain) < 20 + msg_len:
        raise ValueError("wecom msg_len exceeds plaintext")
    receive_id = plain[20 + msg_len :].decode("utf-8", errors="replace")
    if expected_receive_id and receive_id != expected_receive_id:
        raise ValueError("wecom receiveid mismatch")
    return plain[20 : 20 + msg_len]


def wecom_encrypt(encoding_aes_key: str, plaintext: str, receive_id: str = "") -> str:
    # Test helper implementing WeCom's documented encryption.
    import os

    key = base64.b64decode(encoding_aes_key + "=")
    msg = plaintext.encode("utf-8")
    data = os.urandom(16) + len(msg).to_bytes(4, "big") + msg + receive_id.encode("utf-8")
    pad = 32 - (len(data) % 32)
    data = data + bytes([pad]) * pad
    iv = key[:16]
    encryptor = Cipher(algorithms.AES(key), modes.CBC(iv)).encryptor()
    return base64.b64encode(encryptor.update(data) + encryptor.finalize()).decode()


def verify_hmac_sha256_hex(secret: str, raw_body: bytes, signature: str) -> bool:
    # Generic HMAC-SHA256 check used by aliyun/tencent callback configs
    # where a shared secret header scheme is in place.
    if not secret or not signature:
        return False
    expected = hmac.new(secret.encode(), raw_body, hashlib.sha256).hexdigest()
    received = signature[7:] if signature.startswith("sha256=") else signature
    return _ct_eq(expected, received.lower())


def aliyun_sign(phones: str, content: str, secret: str, timestamp: int | str | None = None) -> str:
    # Aliyun SMS reply-report callback MAC (documented scheme):
    # hmac_sha256(secret, "phones=...&content=...&timestamp=...")
    if timestamp is None:
        timestamp = int(time.time())
    base = f"phones={phones}&content={content}&timestamp={timestamp}"
    return hmac.new(secret.encode(), base.encode(), hashlib.sha256).hexdigest()


def tencent_sig(secret: str, timestamp: int | str | None = None) -> str:
    if timestamp is None:
        timestamp = int(time.time())
    return hmac.new(secret.encode(), str(timestamp).encode(), hashlib.sha256).hexdigest()
