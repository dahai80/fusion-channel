from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class ChannelCapabilities:
    CAN_RECEIVE: bool = True
    CAN_SEND: bool = True
    CAN_GROUP: bool = False
    CAN_BOT: bool = False
    CAN_MENTION: bool = False
    CAN_CARDS: str = "none"  # "full" | "partial" | "none"
    CAN_MEDIA: bool = False
    CAN_RECEIPT: bool = False
    CAN_THREADS: bool = False

    def as_dict(self) -> dict:
        return {
            "CAN_RECEIVE": self.CAN_RECEIVE,
            "CAN_SEND": self.CAN_SEND,
            "CAN_GROUP": self.CAN_GROUP,
            "CAN_BOT": self.CAN_BOT,
            "CAN_MENTION": self.CAN_MENTION,
            "CAN_CARDS": self.CAN_CARDS,
            "CAN_MEDIA": self.CAN_MEDIA,
            "CAN_RECEIPT": self.CAN_RECEIPT,
            "CAN_THREADS": self.CAN_THREADS,
        }


# First-batch adapter defaults, mirroring PRD §3.3 capability matrix.
# Overrides come from channel config (capabilities_override).
CAPABILITY_MATRIX: dict[str, dict] = {
    "twilio_sms": {
        "CAN_RECEIVE": True,
        "CAN_SEND": True,
        "CAN_GROUP": False,
        "CAN_BOT": False,
        "CAN_MENTION": False,
        "CAN_CARDS": "none",
        "CAN_MEDIA": True,
        "CAN_RECEIPT": True,
        "CAN_THREADS": False,
    },
    "aliyun_sms": {
        "CAN_RECEIVE": False,
        "CAN_SEND": True,
        "CAN_GROUP": False,
        "CAN_BOT": False,
        "CAN_MENTION": False,
        "CAN_CARDS": "none",
        "CAN_MEDIA": False,
        "CAN_RECEIPT": False,
        "CAN_THREADS": False,
    },
    "tencent_sms": {
        "CAN_RECEIVE": False,
        "CAN_SEND": True,
        "CAN_GROUP": False,
        "CAN_BOT": False,
        "CAN_MENTION": False,
        "CAN_CARDS": "none",
        "CAN_MEDIA": False,
        "CAN_RECEIPT": True,
        "CAN_THREADS": False,
    },
    "wecom_app": {
        "CAN_RECEIVE": True,
        "CAN_SEND": True,
        "CAN_GROUP": True,
        "CAN_BOT": True,
        "CAN_MENTION": True,
        "CAN_CARDS": "partial",
        "CAN_MEDIA": True,
        "CAN_RECEIPT": False,
        "CAN_THREADS": True,
    },
    "feishu_app": {
        "CAN_RECEIVE": True,
        "CAN_SEND": True,
        "CAN_GROUP": True,
        "CAN_BOT": True,
        "CAN_MENTION": True,
        "CAN_CARDS": "full",
        "CAN_MEDIA": True,
        "CAN_RECEIPT": True,
        "CAN_THREADS": True,
    },
    "dingtalk_robot": {
        "CAN_RECEIVE": True,
        "CAN_SEND": True,
        "CAN_GROUP": True,
        "CAN_BOT": True,
        "CAN_MENTION": True,
        "CAN_CARDS": "full",
        "CAN_MEDIA": True,
        "CAN_RECEIPT": False,
        "CAN_THREADS": True,
    },
    "slack_app": {
        "CAN_RECEIVE": True,
        "CAN_SEND": True,
        "CAN_GROUP": True,
        "CAN_BOT": True,
        "CAN_MENTION": True,
        "CAN_CARDS": "full",
        "CAN_MEDIA": True,
        "CAN_RECEIPT": True,
        "CAN_THREADS": True,
    },
    "whatsapp_cloud": {
        "CAN_RECEIVE": True,
        "CAN_SEND": True,
        "CAN_GROUP": False,
        "CAN_BOT": True,
        "CAN_MENTION": False,
        "CAN_CARDS": "none",
        "CAN_MEDIA": True,
        "CAN_RECEIPT": True,
        "CAN_THREADS": False,
    },
    "telegram_bot": {
        "CAN_RECEIVE": True,
        "CAN_SEND": True,
        "CAN_GROUP": True,
        "CAN_BOT": True,
        "CAN_MENTION": True,
        "CAN_CARDS": "full",
        "CAN_MEDIA": True,
        "CAN_RECEIPT": True,
        "CAN_THREADS": True,
    },
    "email_imap": {
        "CAN_RECEIVE": True,
        "CAN_SEND": True,
        "CAN_GROUP": False,
        "CAN_BOT": False,
        "CAN_MENTION": False,
        "CAN_CARDS": "none",
        "CAN_MEDIA": True,
        "CAN_RECEIPT": False,
        "CAN_THREADS": True,
    },
    "graph_mail": {
        "CAN_RECEIVE": True,
        "CAN_SEND": True,
        "CAN_GROUP": False,
        "CAN_BOT": False,
        "CAN_MENTION": False,
        "CAN_CARDS": "none",
        "CAN_MEDIA": True,
        "CAN_RECEIPT": False,
        "CAN_THREADS": True,
    },
    "gmail_api": {
        "CAN_RECEIVE": True,
        "CAN_SEND": True,
        "CAN_GROUP": False,
        "CAN_BOT": False,
        "CAN_MENTION": False,
        "CAN_CARDS": "none",
        "CAN_MEDIA": True,
        "CAN_RECEIPT": False,
        "CAN_THREADS": True,
    },
}


def matrix_capabilities(adapter_id: str) -> ChannelCapabilities:
    return ChannelCapabilities(**CAPABILITY_MATRIX[adapter_id])
