from __future__ import annotations

import pytest

from fusion_channel.adapters import ADAPTERS
from fusion_channel.capabilities import CAPABILITY_MATRIX, ChannelCapabilities
from fusion_channel.cards import CardRenderer, CommandParser
from fusion_channel.identity import IdentityMapper, IdentityNotBound
from fusion_channel.models import ApprovalCard, CardAction
from fusion_channel.spi import CapabilityNotSupported
from fusion_channel.storage import SqliteStore


def approval_card() -> ApprovalCard:
    return ApprovalCard(
        title="审批 AP-123456",
        code="AP-123456",
        body="新增雇员张阿姨",
        actions=[CardAction(action="approve", label="同意"), CardAction(action="reject", label="拒绝")],
    )


def test_render_sms_falls_back_to_text():
    renderer = CardRenderer()
    caps = ChannelCapabilities(CAN_CARDS="none")
    rendered = renderer.render(caps, "twilio_sms", approval_card())
    assert rendered.used_fallback is True
    assert rendered.card_payload is None
    assert "同意 AP-123456" in rendered.text
    assert "拒绝 AP-123456" in rendered.text


def test_render_feishu_full_card():
    renderer = CardRenderer()
    rendered = renderer.render(ChannelCapabilities(CAN_CARDS="full"), "feishu_app", approval_card())
    assert rendered.used_fallback is False
    assert rendered.card_payload["msg_type"] == "interactive"
    elements = rendered.card_payload["card"]["elements"]
    actions = [element for element in elements if element["tag"] == "action"][0]["actions"]
    assert actions[0]["value"]["action"] == "approve"
    assert actions[0]["value"]["code"] == "AP-123456"


def test_render_slack_block_kit():
    renderer = CardRenderer()
    rendered = renderer.render(ChannelCapabilities(CAN_CARDS="full"), "slack_app", approval_card())
    blocks = rendered.card_payload["blocks"]
    assert blocks[0]["type"] == "header"
    actions = [block for block in blocks if block["type"] == "actions"][0]["elements"]
    assert actions[0]["action_id"] == "approve"
    assert actions[0]["value"] == "AP-123456"


def test_render_dingtalk_action_card():
    renderer = CardRenderer()
    rendered = renderer.render(ChannelCapabilities(CAN_CARDS="full"), "dingtalk_robot", approval_card())
    assert rendered.card_payload["msgtype"] == "actionCard"
    assert len(rendered.card_payload["actionCard"]["btns"]) == 2


def test_render_telegram_inline_keyboard():
    renderer = CardRenderer()
    rendered = renderer.render(ChannelCapabilities(CAN_CARDS="full"), "telegram_bot", approval_card())
    keyboard = rendered.card_payload["reply_markup"]["inline_keyboard"]
    assert keyboard[0][0]["callback_data"] == "approve:AP-123456"


def test_render_wecom_partial_card_plus_text():
    renderer = CardRenderer()
    card = approval_card()
    card.url = "https://ops.example.com/approvals/AP-123456"
    rendered = renderer.render(ChannelCapabilities(CAN_CARDS="partial"), "wecom_app", card)
    # partial: real card AND text fallback rendered together (PRD §6.3)
    assert rendered.card_payload["msgtype"] == "textcard"
    assert rendered.card_payload["textcard"]["url"] == card.url
    assert "example.invalid" not in str(rendered.card_payload)
    assert rendered.used_fallback is False
    assert "同意" in rendered.text


def test_render_wecom_card_without_url_degrades_to_text():
    renderer = CardRenderer()
    rendered = renderer.render(ChannelCapabilities(CAN_CARDS="partial"), "wecom_app", approval_card())
    # No click url means no valid textcard — plain text, no placeholder URL.
    assert rendered.card_payload == {}
    assert "同意" in rendered.text


def test_render_telegram_card_without_actions_no_empty_keyboard():
    renderer = CardRenderer()
    card = ApprovalCard(title="通知", body="无按钮")
    rendered = renderer.render(ChannelCapabilities(CAN_CARDS="full"), "telegram_bot", card)
    # Empty inline_keyboard is a Telegram API 400 — actionless cards degrade.
    assert rendered.card_payload == {}


def test_command_parser_converges_with_button():
    parser = CommandParser()
    parser.register("approve", ["同意", "approve"], code_required=True)
    parser.register("reject", ["拒绝", "reject"], code_required=True)
    approve = parser.parse("同意 AP-123456", message_id="SM1")
    reject = parser.parse("拒绝 AP-123456", message_id="SM2")
    assert approve.action == "approve" and approve.payload["code"] == "AP-123456"
    assert reject.action == "reject" and reject.kind == "command"
    assert parser.parse("随机闲聊") is None
    assert parser.parse("同意") is None


def test_command_parser_english_alias():
    parser = CommandParser()
    parser.register("approve", ["Approve", "同意"], code_required=True)
    interaction = parser.parse("approve AP-1", message_id="m")
    assert interaction.action == "approve"


def test_identity_bind_and_identify():
    mapper = IdentityMapper()
    mapper.bind_identity("identity:u_1", "family_twilio", "+15550000002")
    mapper.bind_identity("identity:u_1", "home_feishu", "ou_xxx")
    assert mapper.identify("family_twilio", "+15550000002") == "identity:u_1"
    assert mapper.identify("home_feishu", "ou_xxx") == "identity:u_1"
    assert mapper.identify("family_twilio", "+15550000003") is None
    assert len(mapper.bindings_of("identity:u_1")) == 2


def test_identity_assert_bound():
    mapper = IdentityMapper()
    mapper.bind_identity("identity:u_1", "family_twilio", "+15550000002")
    assert mapper.assert_bound_identity("family_twilio", "+15550000002") == "identity:u_1"
    with pytest.raises(IdentityNotBound):
        mapper.assert_bound_identity("family_twilio", "+19999999999")
    with pytest.raises(IdentityNotBound):
        mapper.assert_bound_identity("family_twilio", "+15550000002", identity_id="identity:other")


def test_capability_matrix_covers_all_adapters():
    for adapter_id in ADAPTERS:
        assert adapter_id in CAPABILITY_MATRIX, f"adapter {adapter_id} missing from capability matrix"
        caps = ADAPTERS[adapter_id].capabilities()
        assert caps.CAN_SEND is True
        assert caps.CAN_CARDS in ("full", "partial", "none")


def test_group_channels_declared():
    group_adapters = {"wecom_app", "feishu_app", "dingtalk_robot", "slack_app", "telegram_bot"}
    for adapter_id, adapter_cls in ADAPTERS.items():
        can_group = adapter_cls.capabilities().CAN_GROUP
        assert can_group == (adapter_id in group_adapters), adapter_id


async def test_group_ops_unsupported_raises():
    adapter = ADAPTERS["twilio_sms"]("family_twilio", {})
    with pytest.raises(CapabilityNotSupported):
        await adapter.group_ops()
    # Base render_card degrades instead of raising: CAN_CARDS=none channels
    # get the fallback text render (PRD: degradation is rendering, not business).
    rendered = adapter.render_card(approval_card())
    assert rendered.used_fallback is True
    assert rendered.card_payload is None
    assert "AP-123456" in rendered.text


async def test_sqlite_store_parity(tmp_path):
    store = SqliteStore(tmp_path / "channel.db")
    await store.init()
    assert await store.save_inbound("ch", "m1", {"a": 1}) is True
    assert await store.save_inbound("ch", "m1", {"a": 1}) is False
    assert await store.bump_duplicate("ch", "m1") == 1
    await store.save_rejected("ch", "bad_sig", {"raw": True})
    record_id = await store.save_outbound("ch", "dk1", {"message": {}}, "2020-01-01T00:00:00+00:00")
    assert record_id is not None
    assert await store.save_outbound("ch", "dk1", {"message": {}}, "2020-01-01T00:00:00+00:00") is None
    due = await store.due_outbound("2099-01-01T00:00:00+00:00")
    assert len(due) == 1
    record = due[0]
    record.status = "sent"
    record.provider_message_id = "PM1"
    await store.update_outbound(record)
    due_after = await store.due_outbound("2099-01-01T00:00:00+00:00")
    assert due_after == []
    await store.save_receipt("ch", "m1", "delivered", {})
    await store.close()


async def test_sqlite_store_rejects_bad_duplicate_count(tmp_path):
    store = SqliteStore(tmp_path / "channel.db")
    await store.init()
    await store.save_inbound("ch", "m1", {"a": 1})
    assert await store.bump_duplicate("ch", "m1") == 1
    assert await store.bump_duplicate("ch", "m1") == 2
    await store.close()
