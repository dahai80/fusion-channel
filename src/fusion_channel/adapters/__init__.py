from __future__ import annotations

import logging

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


def _headers(request_headers: dict[str, str], name: str) -> str:
    return request_headers.get(name.lower(), "")


from fusion_channel.adapters.aliyun_sms import AliyunSmsAdapter  # noqa: E402
from fusion_channel.adapters.dingtalk_robot import DingTalkRobotAdapter  # noqa: E402
from fusion_channel.adapters.feishu_app import FeishuAppAdapter  # noqa: E402
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
    "FeishuAppAdapter",
    "SlackAppAdapter",
    "TelegramBotAdapter",
    "TencentSmsAdapter",
    "TwilioSmsAdapter",
    "WeComAppAdapter",
    "WhatsAppCloudAdapter",
]
