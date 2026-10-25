from __future__ import annotations

import logging
import re
from dataclasses import dataclass, field
from typing import Any

from fusion_channel.capabilities import ChannelCapabilities
from fusion_channel.models import Card, Interaction

logger = logging.getLogger(__name__)

COMMAND_PATTERN = re.compile(r"^\s*(?P<label>[^\s]+)\s*(?P<code>[\w-]+)?\s*$")


class CardRenderer:
    # Degradation is a rendering problem, not a business problem (PRD §4.2):
    # the consumer always asks for a card; the renderer picks the channel shape.
    # Falls back to plain text when CAN_CARDS == "none".

    def render(self, caps: ChannelCapabilities, adapter_id: str, card: Card, text: str = "") -> RenderedMessage:
        rendered = RenderedMessage()
        base_text = text
        if caps.CAN_CARDS == "none":
            rendered.text = self._join(base_text, card.build_fallback_text())
            rendered.used_fallback = True
            return rendered
        payload = self._render_for(adapter_id, card)
        rendered.text = self._join(base_text, card.build_fallback_text() if caps.CAN_CARDS == "partial" else "")
        rendered.card_payload = payload
        rendered.used_fallback = False
        return rendered

    def _render_for(self, adapter_id: str, card: Card) -> dict[str, Any]:
        if adapter_id == "feishu_app":
            return self._feishu_card(card)
        if adapter_id == "dingtalk_robot":
            return self._dingtalk_card(card)
        if adapter_id == "slack_app":
            return self._slack_card(card)
        if adapter_id == "telegram_bot":
            return self._telegram_keyboard(card)
        if adapter_id == "wecom_app":
            return self._wecom_textcard(card)
        logger.warning("no card renderer for adapter=%s, using fallback", adapter_id)
        return {}

    def _feishu_card(self, card: Card) -> dict:
        elements = [{"tag": "div", "text": {"tag": "lark_md", "content": card.body}}] if card.body else []
        actions = {
            "tag": "action",
            "actions": [
                {
                    "tag": "button",
                    "text": {"tag": "plain_text", "content": action.label},
                    "type": "primary" if action.action == "approve" else "danger",
                    "value": {"action": action.action, "code": card.code},
                }
                for action in card.actions
            ],
        }
        if card.actions:
            elements.append(actions)
        return {
            "msg_type": "interactive",
            "card": {"header": {"title": {"tag": "plain_text", "content": card.title}}, "elements": elements},
        }

    def _dingtalk_card(self, card: Card) -> dict:
        btns = [
            {
                "title": action.label,
                "actionURL": "",
                "value": {"action": action.action, "code": card.code},
            }
            for action in card.actions
        ]
        return {
            "msgtype": "actionCard",
            "actionCard": {"title": card.title, "text": card.body, "btns": btns},
        }

    def _slack_card(self, card: Card) -> dict:
        blocks: list[dict] = [
            {"type": "header", "text": {"type": "plain_text", "text": card.title}},
        ]
        if card.body:
            blocks.append({"type": "section", "text": {"type": "mrkdwn", "text": card.body}})
        if card.actions:
            blocks.append(
                {
                    "type": "actions",
                    "elements": [
                        {
                            "type": "button",
                            "text": {"type": "plain_text", "text": action.label},
                            "action_id": action.action,
                            "value": card.code,
                            "style": "primary" if action.action == "approve" else "danger",
                        }
                        for action in card.actions
                    ],
                }
            )
        return {"blocks": blocks}

    def _telegram_keyboard(self, card: Card) -> dict:
        keyboard = [
            [{"text": action.label, "callback_data": f"{action.action}:{card.code}"} for action in card.actions]
        ]
        return {"reply_markup": {"inline_keyboard": keyboard}}

    def _wecom_textcard(self, card: Card) -> dict:
        # WeCom application message text_card: partial card support.
        if not card.url:
            logger.warning("wecom textcard without click url, using placeholder title=%s", card.title)
        return {
            "msgtype": "textcard",
            "textcard": {
                "title": card.title,
                "description": card.body,
                "url": card.url or "https://example.invalid/card",
                "btntxt": card.actions[0].label if card.actions else "查看",
            },
        }

    def _join(self, left: str, right: str) -> str:
        if left and right:
            return f"{left}\n{right}"
        return left or right


@dataclass
class RenderedMessage:
    text: str = ""
    card_payload: dict[str, Any] | None = None
    used_fallback: bool = False
    extra: dict[str, Any] = field(default_factory=dict)


class CommandParser:
    # Text commands are the cross-channel lowest common denominator
    # (PRD §6.3): "同意 AP-123456" produces the same Interaction a card
    # button produces. Aliases are registered by the consumer.

    def __init__(self):
        self._actions: dict[str, dict[str, str]] = {}

    def register(self, action: str, aliases: list[str], code_required: bool = True) -> None:
        self._actions[action] = {
            "aliases": {alias.lower() for alias in aliases},
            "code_required": code_required,
        }
        logger.info("command registered action=%s aliases=%s", action, aliases)

    def parse(self, text: str, message_id: str = "", actor=None, conversation=None) -> Interaction | None:
        match = COMMAND_PATTERN.match(text or "")
        if not match:
            return None
        label = match.group("label").lower()
        code = match.group("code") or ""
        for action, spec in self._actions.items():
            if label not in spec["aliases"]:
                continue
            if spec["code_required"] and not code:
                return None
            return Interaction(
                kind="command",
                action=action,
                message_id=message_id,
                payload={"code": code, "text": text},
                actor=actor,
                conversation=conversation,
            )
        return None


DEFAULT_RENDERER = CardRenderer()
