from __future__ import annotations

import json
import logging
import urllib.parse

from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse, PlainTextResponse

from fusion_channel.models import ChannelRequest, WebhookResponse
from fusion_channel.registry import ChannelRegistry

logger = logging.getLogger(__name__)


async def build_channel_request(request: Request) -> ChannelRequest:
    raw_body = await request.body()
    headers = {k.lower(): v for k, v in request.headers.items()}
    form: dict[str, str] = {}
    json_body = None
    content_type = headers.get("content-type", "")
    if "application/x-www-form-urlencoded" in content_type:
        form = {k: v[-1] for k, v in urllib.parse.parse_qs(raw_body.decode("utf-8"), keep_blank_values=True).items()}
    elif "application/json" in content_type and raw_body:
        try:
            json_body = json.loads(raw_body.decode("utf-8"))
        except ValueError:
            logger.warning("webhook json body unparsable channel_path=%s", request.url.path)
    return ChannelRequest(
        method=request.method,
        url=str(request.url),
        headers=headers,
        body=raw_body,
        form=form,
        json_body=json_body,
    )


def _to_response(response: WebhookResponse):
    if response.media_type == "application/json":
        try:
            payload = json.loads(response.body)
        except ValueError:
            payload = {"ok": True}
        return JSONResponse(content=payload, status_code=response.status_code)
    return PlainTextResponse(content=response.body, status_code=response.status_code, media_type=response.media_type)


def build_channel_router(registry: ChannelRegistry, prefix: str = "/webhook") -> APIRouter:
    # One line of consumer code: app.include_router(build_channel_router(registry)).
    # Endpoints stay generic; per-channel response shaping lives in adapters.
    router = APIRouter(tags=["fusion-channel"])

    @router.post(prefix + "/{channel_id}")
    async def channel_webhook(channel_id: str, request: Request):
        channel_request = await build_channel_request(request)
        result = await registry.handle_inbound(channel_id, channel_request)
        return _to_response(result.response)

    @router.get(prefix + "/health")
    async def channel_health():
        return JSONResponse({"channels": sorted(registry.channels.keys())})

    return router
