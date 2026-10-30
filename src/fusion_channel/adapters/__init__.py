from __future__ import annotations

import logging
import re

import httpx

from fusion_channel.spi import ChannelAdapter

logger = logging.getLogger(__name__)

ADAPTERS: dict[str, type[ChannelAdapter]] = {}


def register(cls: type[ChannelAdapter]) -> type[ChannelAdapter]:
    adapter_id = cls.adapter_id()
    if adapter_id in ADAPTERS:
        raise RuntimeError(f"adapter {adapter_id} already registered")
    ADAPTERS[adapter_id] = cls
    logger.debug("adapter registered id=%s", adapter_id)
    return cls


class HttpAdapterMixin:
    # One AsyncClient per call; tests inject httpx.MockTransport through
    # config["http_transport"] so no live network is needed.

    def _client(self) -> httpx.AsyncClient:
        transport = self.config.get("http_transport")
        kwargs = self.http_client_kwargs()
        if transport is not None:
            kwargs["transport"] = transport
        return httpx.AsyncClient(**kwargs)


_SECRET_QUERY_RE = re.compile(r"(access_token|sign)=[^&\\s]+")
_BOT_TOKEN_RE = re.compile(r"bot[0-9]+:[A-Za-z0-9_-]+")


def redact_secrets(text: str) -> str:
    # httpx error strings embed request URLs; provider tokens/signs in those
    # URLs are replayable credentials and must never reach logs or outbox
    # last_error fields.
    text = _SECRET_QUERY_RE.sub(r"\1=***", text)
    return _BOT_TOKEN_RE.sub("bot***", text)


from fusion_channel.adapters.aliyun_sms import AliyunSmsAdapter  # noqa: E402
from fusion_channel.adapters.dingtalk_robot import DingTalkRobotAdapter  # noqa: E402
from fusion_channel.adapters.email_imap import EmailImapAdapter  # noqa: E402
from fusion_channel.adapters.feishu_app import FeishuAppAdapter  # noqa: E402
from fusion_channel.adapters.graph_mail import GraphMailAdapter  # noqa: E402
from fusion_channel.adapters.slack_app import SlackAppAdapter  # noqa: E402
from fusion_channel.adapters.telegram_bot import TelegramBotAdapter  # noqa: E402
from fusion_channel.adapters.tencent_sms import TencentSmsAdapter  # noqa: E402
from fusion_channel.adapters.twilio_sms import TwilioSmsAdapter  # noqa: E402
from fusion_channel.adapters.wecom_app import WeComAppAdapter  # noqa: E402
from fusion_channel.adapters.whatsapp_cloud import WhatsAppCloudAdapter  # noqa: E402

__all__ = [
    "ADAPTERS",
    "register",
    "AliyunSmsAdapter",
    "DingTalkRobotAdapter",
    "EmailImapAdapter",
    "FeishuAppAdapter",
    "GraphMailAdapter",
    "SlackAppAdapter",
    "TelegramBotAdapter",
    "TencentSmsAdapter",
    "TwilioSmsAdapter",
    "WeComAppAdapter",
    "WhatsAppCloudAdapter",
]
