"""Runtime HTTP patching for wyzeapy.

wyzeapy creates short-lived aiohttp ClientSession objects inside its request
helpers. In Home Assistant, the integration should use Home Assistant's shared
managed session instead.
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
_PATCHED_HASS: HomeAssistant | None = None


def _session():
    if _PATCHED_HASS is None:
        raise RuntimeError("wyzeapy HTTP transport was used before being patched")

    return async_get_clientsession(_PATCHED_HASS)


def _app_ver() -> str:
    """Return the app_ver value expected by Wyze."""
    return f"{APP_NAME}___{APP_VERSION}"


def patch_wyzeapy_http(hass: HomeAssistant) -> None:
    """Patch wyzeapy to use Home Assistant's shared aiohttp session."""
    global _PATCHED_HASS

    _PATCHED_HASS = hass

    if getattr(WyzeAuthLib, "_ha_wyzeapi_http_patched", False):
        return

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

    async def put(
        self: WyzeAuthLib,
        url: str,
        json: dict[str, Any] | None = None,
        headers: dict[str, str] | None = None,
        data: Any = None,
    ) -> dict[str, Any]:
        return await _request_json(
            self,
            "PUT",
            url,
            json=json,
            headers=headers,
            data=data,
        )

    async def get(
        self: WyzeAuthLib,
        url: str,
        headers: dict[str, str] | None = None,
        params: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        return await _request_json(
            self,
            "GET",
            url,
            headers=headers,
            params=params,
        )

    async def patch(
        self: WyzeAuthLib,
        url: str,
        headers: dict[str, str] | None = None,
        params: dict[str, Any] | None = None,
        json: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        return await _request_json(
            self,
            "PATCH",
            url,
            headers=headers,
            params=params,
            json=json,
        )

    async def delete(
        self: WyzeAuthLib,
        url: str,
        headers: dict[str, str] | None = None,
        json: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        return await _request_json(
            self,
            "DELETE",
            url,
            headers=headers,
            json=json,
        )

    async def refresh(self: WyzeAuthLib) -> None:
        """Refresh Wyze token using Home Assistant's shared session."""
        payload = {
            "phone_id": PHONE_ID,
            "app_name": APP_NAME,
            "app_version": APP_VERSION,
            "app_ver": _app_ver(),
            "sc": SC,
            "sv": SV,
            "phone_system_type": PHONE_SYSTEM_TYPE,
            "ts": int(time.time()),
            "refresh_token": self.token.refresh_token,
            "access_token": self.token.access_token,
        }

        _LOGGER.debug(
            "Refreshing Wyze token with app_name=%s app_version=%s app_ver=%s",
            payload["app_name"],
            payload["app_version"],
            payload["app_ver"],
        )

        response_json = await _request_json(
            self,
            "POST",
            _REFRESH_TOKEN_URL,
            json=payload,
            headers={"X-API-Key": API_KEY},
        )

        check_for_errors_standard(self, response_json)

        token_data = response_json["data"]
        self.token.access_token = token_data["access_token"]
        self.token.refresh_token = token_data["refresh_token"]
        self.token.expired = False

        if self.token_callback is not None:
            await self.token_callback(self.token)

    WyzeAuthLib.post = post
    WyzeAuthLib.put = put
    WyzeAuthLib.get = get
    WyzeAuthLib.patch = patch
    WyzeAuthLib.delete = delete
    WyzeAuthLib.refresh = refresh
    WyzeAuthLib._ha_wyzeapi_http_patched = True

    _LOGGER.debug("Patched wyzeapy HTTP transport for Home Assistant")
