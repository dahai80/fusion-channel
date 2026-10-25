from __future__ import annotations

import base64
import hashlib
import hmac
import json
import urllib.parse

import httpx

from fusion_channel.adapters.aliyun_sms import ALIYUN_SMS_ENDPOINT, AliyunSmsAdapter
from fusion_channel.adapters.tencent_sms import TENCENT_SMS_HOST, TencentSmsAdapter
from fusion_channel.models import ChannelRequest, Conversation, OutboundMessage

ALIYUN_CREDS = {
    "access_key_id": "LTAI-test-key-id",
    "access_key_secret": "aliyun-secret",
    "sign_name": "fusion",
    "template_code": "SMS_1000001",
    "callback_secret": "callback-secret",
}
TENCENT_CREDS = {
    "secret_id": "AKIDtest",
    "secret_key": "tencent-secret",
    "sdk_app_id": "1400000001",
    "sign_name": "fusion",
    "template_id": "10001",
    "callback_secret": "callback-secret",
}


def _outbound(text: str = "hello") -> OutboundMessage:
    return OutboundMessage(
        channel_id="sms",
        conversation=Conversation(type="direct", id="+8613800000002"),
        text=text,
    )


def _callback_request(payload: dict, secret: str, header: str = "X-Fusion-Signature") -> ChannelRequest:
    body = json.dumps(payload).encode()
    signature = hmac.new(secret.encode(), body, hashlib.sha256).hexdigest()
    return ChannelRequest(
        url="https://example.com/webhook/sms",
        headers={"content-type": "application/json", header: f"sha256={signature}"},
        body=body,
        json_body=payload,
    )


# ---- aliyun ----


def test_aliyun_rpc_signature_v1():
    adapter = AliyunSmsAdapter("sms_aliyun", dict(ALIYUN_CREDS), {})
    params = adapter._rpc_params("+8613800000002", {"content": "hello"})
    signed = adapter._signed(params)
    assert "Signature" in signed

    def _percent_encode(value) -> str:
        return urllib.parse.quote(str(value), safe="-_.~")

    # Independent recomputation of the documented scheme: RFC3986 strict
    # percent-encoding (space -> %20, NOT "+"), HMAC-SHA1, key = secret + "&".
    assert _percent_encode("a b") == "a%20b"
    query = "&".join(f"{_percent_encode(k)}={_percent_encode(v)}" for k, v in sorted(params.items()))
    string_to_sign = "GET&%2F&" + _percent_encode(query)
    expected = base64.b64encode(hmac.new(b"aliyun-secret&", string_to_sign.encode(), hashlib.sha1).digest()).decode()
    assert signed["Signature"] == expected


async def test_aliyun_send_ok():
    captured = {}

    def handler(request: httpx.Request) -> httpx.Response:
        captured["url"] = str(request.url)
        captured["params"] = dict(request.url.params)
        return httpx.Response(200, json={"Code": "OK", "BizId": "biz-1"})

    adapter = AliyunSmsAdapter("sms_aliyun", dict(ALIYUN_CREDS), {"http_transport": httpx.MockTransport(handler)})
    result = await adapter.send(_outbound())
    assert result.ok
    assert result.provider_message_id == "biz-1"
    assert captured["url"].startswith(ALIYUN_SMS_ENDPOINT)
    assert captured["params"]["PhoneNumbers"] == "+8613800000002"
    assert captured["params"]["Action"] == "SendSms"
    assert "Signature" in captured["params"]


async def test_aliyun_send_provider_error():
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"Code": "isv.SMS_SIGNATURE_ILLEGAL", "Message": "bad sign"})

    adapter = AliyunSmsAdapter("sms_aliyun", dict(ALIYUN_CREDS), {"http_transport": httpx.MockTransport(handler)})
    result = await adapter.send(_outbound())
    assert not result.ok
    assert "isv.SMS_SIGNATURE_ILLEGAL" in (result.error or "")


async def test_aliyun_send_http_error():
    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("boom")

    adapter = AliyunSmsAdapter("sms_aliyun", dict(ALIYUN_CREDS), {"http_transport": httpx.MockTransport(handler)})
    result = await adapter.send(_outbound())
    assert not result.ok
    assert "aliyun http error" in (result.error or "")


def test_aliyun_verify_without_callback_secret():
    creds = dict(ALIYUN_CREDS)
    creds.pop("callback_secret")
    adapter = AliyunSmsAdapter("sms_aliyun", creds, {})
    result = adapter.verify_request(_callback_request({"phone_number": "1"}, "callback-secret"))
    assert not result.ok
    assert result.reason == "callback_secret_not_configured"


def test_aliyun_verify_roundtrip_and_reject():
    adapter = AliyunSmsAdapter("sms_aliyun", dict(ALIYUN_CREDS), {})
    request = _callback_request({"phone_number": "+8613800000002", "content": "reply"}, "callback-secret")
    assert adapter.verify_request(request).ok
    # Tamper the body after signing: same signature, different payload.
    tampered = {"phone_number": "+8613800000002", "content": "tampered"}
    body = json.dumps(tampered).encode()
    bad = ChannelRequest(
        url="https://example.com/webhook/sms",
        headers={"content-type": "application/json", "X-Fusion-Signature": request.header("X-Fusion-Signature")},
        body=body,
        json_body=tampered,
    )
    assert not adapter.verify_request(bad).ok


def test_aliyun_parse_inbound():
    adapter = AliyunSmsAdapter("sms_aliyun", dict(ALIYUN_CREDS), {})
    request = _callback_request(
        {"phone_number": "+8613800000002", "content": "reply text", "message_id": "msg-9"}, "callback-secret"
    )
    envelope = adapter.parse_inbound(request)
    assert envelope.message_id == "msg-9"
    assert envelope.text == "reply text"
    assert envelope.sender.external_id == "+8613800000002"
    assert envelope.conversation.type == "direct"


async def test_aliyun_test_connection():
    adapter = AliyunSmsAdapter("sms_aliyun", dict(ALIYUN_CREDS), {})
    assert (await adapter.test_connection()).ok
    empty = AliyunSmsAdapter("sms_aliyun", {}, {})
    assert not (await empty.test_connection()).ok


# ---- tencent ----


def test_tencent_tc3_headers_structure_and_signature():
    adapter = TencentSmsAdapter("sms_tencent", dict(TENCENT_CREDS), {})
    payload = json.dumps({"PhoneNumberSet": ["+8613800000002"]}).encode()
    headers = adapter._tc3_headers(payload)
    auth = headers["Authorization"]
    assert auth.startswith("TC3-HMAC-SHA256 Credential=AKIDtest/")
    assert "SignedHeaders=content-type;host" in auth
    assert headers["X-TC-Action"] == "SendSms"
    # Independent recomputation of the TC3 derivation chain.
    timestamp = int(headers["X-TC-Timestamp"])
    date = headers["Authorization"].split(" Credential=AKIDtest/")[1].split("/")[0]
    hashed = hashlib.sha256(payload).hexdigest()
    canonical = (
        "POST\n/\n\ncontent-type:application/json\nhost:" + TENCENT_SMS_HOST + f"\n\ncontent-type;host\n{hashed}"
    )
    string_to_sign = (
        f"TC3-HMAC-SHA256\n{timestamp}\n{date}/sms/tc3_request\n" + hashlib.sha256(canonical.encode()).hexdigest()
    )

    def _h(key: bytes, msg: str) -> bytes:
        return hmac.new(key, msg.encode(), hashlib.sha256).digest()

    key = _h(_h(_h(b"TC3tencent-secret", date), "sms"), "tc3_request")
    expected = hmac.new(key, string_to_sign.encode(), hashlib.sha256).hexdigest()
    assert auth.endswith(f"Signature={expected}")


async def test_tencent_send_ok():
    captured = {}

    def handler(request: httpx.Request) -> httpx.Response:
        captured["body"] = json.loads(request.content)
        captured["action"] = request.headers.get("X-TC-Action")
        return httpx.Response(
            200,
            json={"Response": {"SendStatusSet": [{"Code": "Ok", "SerialNo": "sn-1", "Message": "send success"}]}},
        )

    adapter = TencentSmsAdapter("sms_tencent", dict(TENCENT_CREDS), {"http_transport": httpx.MockTransport(handler)})
    result = await adapter.send(_outbound())
    assert result.ok
    assert result.provider_message_id == "sn-1"
    assert captured["action"] == "SendSms"
    assert captured["body"]["PhoneNumberSet"] == ["+8613800000002"]
    assert captured["body"]["SmsSdkAppId"] == "1400000001"


async def test_tencent_send_provider_error():
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"Response": {"Error": {"Code": "AuthFailure", "Message": "bad key"}}})

    adapter = TencentSmsAdapter("sms_tencent", dict(TENCENT_CREDS), {"http_transport": httpx.MockTransport(handler)})
    result = await adapter.send(_outbound())
    assert not result.ok
    assert "AuthFailure" in (result.error or "")


async def test_tencent_send_status_not_ok():
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            json={"Response": {"SendStatusSet": [{"Code": "LimitExceeded", "SerialNo": "", "Message": "rate"}]}},
        )

    adapter = TencentSmsAdapter("sms_tencent", dict(TENCENT_CREDS), {"http_transport": httpx.MockTransport(handler)})
    result = await adapter.send(_outbound())
    assert not result.ok


def test_tencent_verify_roundtrip_and_reject():
    adapter = TencentSmsAdapter("sms_tencent", dict(TENCENT_CREDS), {})
    request = _callback_request({"phone_number": "+8613800000002", "content": "reply"}, "callback-secret")
    assert adapter.verify_request(request).ok
    tampered = {"phone_number": "+8613800000002", "content": "other"}
    body = json.dumps(tampered).encode()
    bad = ChannelRequest(
        url="https://example.com/webhook/sms",
        headers={"content-type": "application/json", "X-Fusion-Signature": request.header("X-Fusion-Signature")},
        body=body,
        json_body=tampered,
    )
    assert not adapter.verify_request(bad).ok


def test_tencent_verify_missing_header():
    adapter = TencentSmsAdapter("sms_tencent", dict(TENCENT_CREDS), {})
    body = json.dumps({"phone_number": "1"}).encode()
    request = ChannelRequest(
        url="https://example.com/webhook/sms",
        headers={"content-type": "application/json"},
        body=body,
        json_body={"phone_number": "1"},
    )
    assert not adapter.verify_request(request).ok


def test_tencent_parse_inbound():
    adapter = TencentSmsAdapter("sms_tencent", dict(TENCENT_CREDS), {})
    request = _callback_request(
        {"phone_number": "+8613800000002", "content": "reply", "serial_no": "sn-9"}, "callback-secret"
    )
    envelope = adapter.parse_inbound(request)
    assert envelope.message_id == "sn-9"
    assert envelope.text == "reply"
    assert envelope.conversation.id == "+8613800000002"


async def test_tencent_test_connection():
    adapter = TencentSmsAdapter("sms_tencent", dict(TENCENT_CREDS), {})
    assert (await adapter.test_connection()).ok
    empty = TencentSmsAdapter("sms_tencent", {}, {})
    assert not (await empty.test_connection()).ok


def test_tencent_sdk_app_id_pattern_enforced():
    from fusion_channel.config import ChannelConfig, validate_credentials

    channel = ChannelConfig(
        {
            "id": "sms_tencent",
            "adapter": "tencent_sms",
            "credentials": {**TENCENT_CREDS, "sdk_app_id": "not-numeric"},
        }
    )
    errors = validate_credentials(TencentSmsAdapter, channel, dict(channel.credentials))
    assert errors
    assert any("sdk_app_id" in e for e in errors)
