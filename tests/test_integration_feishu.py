from __future__ import annotations

import os

import pytest

from fusion_channel.adapters import ADAPTERS

pytestmark = pytest.mark.integration


def _feishu_credentials() -> dict[str, str]:
    return {
        "app_id": os.environ.get("FUSION_CHANNEL_FEISHU_APP_ID", ""),
        "app_secret": os.environ.get("FUSION_CHANNEL_FEISHU_APP_SECRET", ""),
        "encrypt_key": os.environ.get("FUSION_CHANNEL_FEISHU_ENCRYPT_KEY", ""),
        "verification_token": os.environ.get("FUSION_CHANNEL_FEISHU_VERIFY_TOKEN", ""),
    }


@pytest.mark.skipif(
    not os.environ.get("FUSION_CHANNEL_FEISHU_APP_SECRET"),
    reason="no feishu sandbox credentials in env",
)
async def test_feishu_real_tenant_token():
    adapter = ADAPTERS["feishu_app"]("integration_feishu", _feishu_credentials())
    token = await adapter.tenant_access_token()
    assert token, "tenant_access_token fetch failed"
    result = await adapter.test_connection()
    assert result.ok, result.detail
