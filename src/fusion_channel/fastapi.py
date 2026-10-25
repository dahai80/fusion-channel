from __future__ import annotations

import json
import logging
import urllib.parse

from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse, PlainTextResponse

from fusion_channel.models import ChannelRequest, WebhookResponse
from fusion_channel.registry import ChannelRegistry

logger = logging.getLogger(__name__)

# Webhook bodies are provider event payloads (Feishu/WeCom ciphertext included);
# 1 MiB is far above any legitimate event and caps memory-exhaustion DoS.
MAX_BODY_BYTES = 1_048_576


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
        content_length = request.headers.get("content-length", "")
        if content_length.isdigit() and int(content_length) > MAX_BODY_BYTES:
            logger.warning("webhook body too large channel=%s bytes=%s", channel_id, content_length)
            return JSONResponse({"error": "payload too large"}, status_code=413)
        channel_request = await build_channel_request(request)
        if len(channel_request.body) > MAX_BODY_BYTES:
            logger.warning("webhook body too large channel=%s bytes=%d", channel_id, len(channel_request.body))
            return JSONResponse({"error": "payload too large"}, status_code=413)
        try:
            result = await registry.handle_inbound(channel_id, channel_request)
        except Exception as exc:
            # Fail closed (PRD §4.2): no webhook input ever yields a 500 —
            # reject-archive what we have and answer 400 with a traceback in
            # the server log.
            logger.exception("webhook internal error channel=%s err=%s", channel_id, exc)
            try:
                await registry._reject(channel_id, channel_request, f"internal_error:{type(exc).__name__}")
            except Exception as reject_exc:
                logger.error("reject archive failed channel=%s err=%s", channel_id, reject_exc)
            return PlainTextResponse("internal error", status_code=400)
        return _to_response(result.response)

    @router.get(prefix + "/health")
    async def channel_health():
        return JSONResponse({"channels": sorted(registry.channels.keys())})

    @router.get(prefix + "/{channel_id}")
    async def channel_webhook_get(channel_id: str, request: Request):
        # GET handshakes: WeCom URL-verification echostr, WhatsApp
        # hub.challenge. Delegates to the adapter; unknown channels 404.
        adapter = registry.adapters.get(channel_id)
        if adapter is None:
            return PlainTextResponse("unknown channel", status_code=404)
        channel_request = ChannelRequest(
            method="GET",
            url=str(request.url),
            headers={k.lower(): v for k, v in request.headers.items()},
        )
        try:
            response = adapter.handle_verification_get(channel_request)
        except Exception as exc:
            logger.exception("webhook GET handshake error channel=%s err=%s", channel_id, exc)
            return PlainTextResponse("internal error", status_code=400)
        return _to_response(response)

    return router
