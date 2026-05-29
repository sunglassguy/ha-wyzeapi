"""Runtime HTTP patching for wyzeapy.

wyzeapy currently creates a new aiohttp ClientSession inside request helpers.
In Home Assistant, integrations should use HA's managed session instead.
"""

from __future__ import annotations

import logging
import time
from typing import Any

from homeassistant.core import HomeAssistant
from homeassistant.helpers.aiohttp_client import async_get_clientsession

from wyzeapy.const import (
    API_KEY,
    APP_NAME,
    APP_VERSION,
    PHONE_ID,
    PHONE_SYSTEM_TYPE,
    SC,
    SV,
)
from wyzeapy.utils import check_for_errors_standard
from wyzeapy.wyze_auth_lib import WyzeAuthLib

from .http import request_json_with_retries

_LOGGER = logging.getLogger(__name__)

_REFRESH_TOKEN_URL = "https://api.wyzecam.com/app/user/refresh_token"


def patch_wyzeapy_http(hass: HomeAssistant) -> None:
    """Patch wyzeapy to use Home Assistant's shared aiohttp session."""
    if getattr(WyzeAuthLib, "_ha_wyzeapi_http_patched", False):
        return

    def _session():
        return async_get_clientsession(hass)

    async def _request_json(
        self: WyzeAuthLib,
        method: str,
        url: str,
        **kwargs: Any,
    ) -> dict[str, Any]:
        return await request_json_with_retries(
            _session(),
            method,
            url,
            logger=_LOGGER,
            **kwargs,
        )

    async def post(
        self: WyzeAuthLib,
        url: str,
        json: dict[str, Any] | None = None,
        headers: dict[str, str] | None = None,
        data: Any = None,
    ) -> dict[str, Any]:
        return await _request_json(
            self,
            "POST",
            url,
            json=json,
            headers=headers,
            data=data,
        )

    async def get(
        self: WyzeAuthLib,
        url: str,
        headers: dict[str, str] | None = None,
    ) -> dict[str, Any]:
        return await _request_json(
            self,
            "GET",
            url,
            headers=headers,
        )

    async def put(
        self: WyzeAuthLib,
        url: str,
        json: dict[str, Any] | None = None,
        headers: dict[str, str] | None = None,
    ) -> dict[str, Any]:
        return await _request_json(
            self,
            "PUT",
            url,
            json=json,
            headers=headers,
        )

    async def patch(
        self: WyzeAuthLib,
        url: str,
        json: dict[str, Any] | None = None,
        headers: dict[str, str] | None = None,
    ) -> dict[str, Any]:
        return await _request_json(
            self,
            "PATCH",
            url,
            json=json,
            headers=headers,
        )

    async def delete(
        self: WyzeAuthLib,
        url: str,
        json: dict[str, Any] | None = None,
        headers: dict[str, str] | None = None,
    ) -> dict[str, Any]:
        return await _request_json(
            self,
            "DELETE",
            url,
            json=json,
            headers=headers,
        )

    async def refresh(self: WyzeAuthLib) -> None:
        """Refresh Wyze token using HA's managed aiohttp session."""
        payload = {
            "app_name": APP_NAME,
            "app_version": APP_VERSION,
            "phone_system_type": PHONE_SYSTEM_TYPE,
            "sc": SC,
            "sv": SV,
            "ts": int(time.time()),
            "access_token": self.token.access_token,
            "refresh_token": self.token.refresh_token,
            "phone_id": PHONE_ID,
        }

        headers = {"X-API-Key": API_KEY}

        response_json = await _request_json(
            self,
            "POST",
            _REFRESH_TOKEN_URL,
            json=payload,
            headers=headers,
        )

        check_for_errors_standard(self, response_json)

        token_data = response_json["data"]
        self.token.access_token = token_data["access_token"]
        self.token.refresh_token = token_data["refresh_token"]
        self.token.expired = False

        if self.token_callback is not None:
            await self.token_callback(self.token)

    WyzeAuthLib.post = post
    WyzeAuthLib.get = get
    WyzeAuthLib.put = put
    WyzeAuthLib.patch = patch
    WyzeAuthLib.delete = delete
    WyzeAuthLib.refresh = refresh
    WyzeAuthLib._ha_wyzeapi_http_patched = True

    _LOGGER.debug("Patched wyzeapy HTTP transport for Home Assistant")
