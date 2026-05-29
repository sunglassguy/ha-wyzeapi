from __future__ import annotations

import asyncio
import hmac
import json
import logging
import time
from datetime import datetime
from hashlib import md5
from os import getenv
from typing import Any, Dict, List, Optional

from aiohttp import ClientResponseError, ContentTypeError
from aiohttp.client import ClientSession

from homeassistant.config_entries import ConfigEntry
from homeassistant.const import CONF_PASSWORD, CONF_USERNAME
from homeassistant.core import HomeAssistant
from homeassistant.helpers.aiohttp_client import async_get_clientsession

from .const import ACCESS_TOKEN, API_KEY, KEY_ID, REFRESH_TOKEN
from .http import (
    TRANSIENT_EXCEPTIONS,
    WYZE_REQUEST_TIMEOUT,
    is_transient_exception,
    is_transient_status,
    wyze_ssl_context,
)
from .wyze_cloud_models import WyzeCredential

_LOGGER = logging.getLogger(__name__)

# Keep these minimal; exact values are not critical for get_event_list as long
# as they are consistent.
APP_VERSION = "2.50.0"
IOS_VERSION = "17.0"
AUTH_API = "https://auth-prod.api.wyze.com"
WYZE_API = "https://api.wyzecam.com/app"
CLOUD_API = "https://app-core.cloud.wyze.com/app"

SC_SV = {
    "default": {
        "sc": "9f275790cab94a72bd206c8876429f3c",
        "sv": "e1fe392906d54888a9b99b88de4162d7",
    },
    "get_event_list": {
        "sc": "9f275790cab94a72bd206c8876429f3c",
        "sv": "782ced6909a44d92a1f70d582bbe88be",
    },
}

APP_KEY = {"9319141212m2ik": "wyze_app_secret_key_132"}


class AccessTokenError(Exception):
    """Raised when the Wyze access token is missing or invalid."""


class RateLimitError(Exception):
    """Raised when Wyze rate-limit headers indicate a cooldown is needed."""

    def __init__(self, remaining: int, reset_by: int, reset_header: str = ""):
        self.remaining = remaining
        self.reset_by = reset_by
        super().__init__(f"{remaining} requests remaining until {reset_header}")


class WyzeAPIError(Exception):
    """Raised for a semantic Wyze API error response."""

    def __init__(self, code: str, msg: str, method: str, path: str):
        self.code = code
        self.msg = msg
        super().__init__(f"code={code} msg={msg} method={method} path={path}")


def _headers_for_login(key_id: str, api_key: str) -> dict[str, str]:
    return {
        "apikey": api_key,
        "keyid": key_id,
        "user-agent": f"wyze_ios_{APP_VERSION}",
    }


def _headers_default() -> dict[str, str]:
    return {
        "user-agent": f"Wyze/{APP_VERSION} (iPhone; iOS {IOS_VERSION}; Scale/3.00)",
        "appversion": APP_VERSION,
        "env": "prod",
    }


def hash_password(password: str) -> str:
    encoded = password.strip()

    for ex in ("hashed:", "md5:"):
        if encoded.lower().startswith(ex):
            return encoded[len(ex) :]

    for _ in range(3):
        encoded = md5(encoded.encode("ascii")).hexdigest()  # nosec

    return encoded


def sort_dict(payload: dict) -> str:
    return json.dumps(payload, separators=(",", ":"), sort_keys=True)


def sign_msg(app_id: str, msg: str | dict, token: str = "") -> str:
    secret = getenv(app_id, APP_KEY.get(app_id, app_id))
    key = md5((token + secret).encode()).hexdigest().encode()  # nosec

    if isinstance(msg, dict):
        msg = sort_dict(msg)

    return hmac.new(key, msg.encode(), md5).hexdigest()  # nosec


def sign_payload(auth: WyzeCredential, app_id: str, payload: str) -> dict[str, str]:
    if not auth.access_token:
        raise AccessTokenError()

    return {
        "content-type": "application/json",
        "phoneid": auth.phone_id,
        "user-agent": f"wyze_ios_{APP_VERSION}",
        "appinfo": f"wyze_ios_{APP_VERSION}",
        "appversion": APP_VERSION,
        "access_token": auth.access_token,
        "appid": app_id,
        "env": "prod",
        "signature2": sign_msg(app_id, payload, auth.access_token),
    }


def _payload(auth: WyzeCredential, endpoint: str = "default") -> dict[str, Any]:
    values = SC_SV.get(endpoint, SC_SV["default"])

    return {
        "sc": values["sc"],
        "sv": values["sv"],
        "app_ver": f"com.hualai.WyzeCam___{APP_VERSION}",
        "app_version": APP_VERSION,
        "app_name": "com.hualai.WyzeCam",
        "phone_system_type": 1,
        "ts": int(time.time() * 1000),
        "access_token": auth.access_token,
        "phone_id": auth.phone_id,
    }


def _parse_reset_by(reset_by: str) -> int:
    ts_format = "%a %b %d %H:%M:%S %Z %Y"

    try:
        return int(datetime.strptime(reset_by, ts_format).timestamp())
    except Exception:
        return 0


def _check_rate_limit(headers) -> None:
    try:
        remaining = int(headers.get("X-RateLimit-Remaining", "100"))
    except Exception:
        remaining = 100

    if remaining <= 10:
        reset_header = headers.get("X-RateLimit-Reset-By", "")
        raise RateLimitError(remaining, _parse_reset_by(reset_header), reset_header)


async def _validate_resp(resp) -> dict:
    if is_transient_status(resp.status):
        body = await resp.text()
        raise ClientResponseError(
            resp.request_info,
            resp.history,
            status=resp.status,
            message=f"Transient Wyze HTTP status {resp.status}: {body[:300]}",
            headers=resp.headers,
        )

    resp.raise_for_status()
    _check_rate_limit(resp.headers)

    try:
        data = await resp.json(content_type=None)
    except ContentTypeError as err:
        body = await resp.text()
        raise WyzeAPIError(
            "non_json_response",
            f"Wyze returned non-JSON response: status={resp.status}, body={body[:300]}",
            resp.request_info.method,
            str(resp.request_info.url),
        ) from err
    except (json.JSONDecodeError, ValueError) as err:
        body = await resp.text()
        raise WyzeAPIError(
            "invalid_json_response",
            f"Wyze returned invalid JSON: status={resp.status}, body={body[:300]}",
            resp.request_info.method,
            str(resp.request_info.url),
        ) from err

    code = str(data.get("code", data.get("errorCode", 0)))

    if code == "2001":
        raise AccessTokenError()

    if code not in {"1", "0"}:
        msg = data.get("msg", data.get("description", code))
        raise WyzeAPIError(
            code,
            msg,
            resp.request_info.method,
            str(resp.request_info.url),
        )

    return data.get("data", data)


class WyzeCloudEventsApi:
    """Async Wyze cloud client for get_event_list v4 only."""

    def __init__(
        self,
        hass: HomeAssistant,
        auth: Optional[WyzeCredential],
        email: str,
        password: str,
        key_id: str,
        api_key: str,
    ):
        self._hass = hass
        self._session: ClientSession = async_get_clientsession(hass)
        self._email = email
        self._password = password
        self._key_id = key_id
        self._api_key = api_key
        self.auth: Optional[WyzeCredential] = auth

    @classmethod
    def from_config_entry(cls, hass: HomeAssistant, entry: ConfigEntry) -> "WyzeCloudEventsApi":
        data = entry.data

        # Prefer stored tokens, especially when 2FA was used.
        auth = WyzeCredential(
            access_token=data.get(ACCESS_TOKEN),
            refresh_token=data.get(REFRESH_TOKEN),
        )

        return cls(
            hass=hass,
            auth=auth,
            email=data.get(CONF_USERNAME, ""),
            password=data.get(CONF_PASSWORD, ""),
            key_id=data.get(KEY_ID, ""),
            api_key=data.get(API_KEY, ""),
        )

    async def _post_json(
        self,
        url: str,
        *,
        json_payload: dict | None = None,
        data: str | bytes | None = None,
        headers: dict[str, str] | None = None,
    ) -> dict:
        """POST to Wyze and validate JSON response with retries."""
        last_err: BaseException | None = None

        for attempt in range(3):
            try:
                async with self._session.post(
                    url,
                    json=json_payload,
                    data=data,
                    headers=headers,
                    timeout=WYZE_REQUEST_TIMEOUT,
                    ssl=wyze_ssl_context(),
                ) as resp:
                    return await _validate_resp(resp)

            except ClientResponseError as err:
                last_err = err

                if not is_transient_status(err.status) or attempt >= 2:
                    raise

            except TRANSIENT_EXCEPTIONS as err:
                last_err = err

                if attempt >= 2:
                    raise

            await asyncio.sleep(min(0.75 * (2**attempt), 5.0))

        if last_err is not None:
            raise last_err

        raise WyzeAPIError("request_failed", f"Wyze request failed: {url}", "POST", url)

    async def _login(self) -> bool:
        if not (self._email and self._password and self._key_id and self._api_key):
            _LOGGER.error(
                "Missing Wyze credentials for cloud login "
                "(email/password/key_id/api_key)."
            )
            return False

        payload = {"email": self._email.strip(), "password": hash_password(self._password)}
        headers = _headers_for_login(self._key_id, self._api_key)

        data = await self._post_json(
            f"{AUTH_API}/api/user/login",
            json_payload=payload,
            headers=headers,
        )

        phone_id = data.get("phone_id") or (self.auth.phone_id if self.auth else None)
        self.auth = WyzeCredential(
            access_token=data.get("access_token"),
            refresh_token=data.get("refresh_token"),
            user_id=data.get("user_id"),
            mfa_options=data.get("mfa_options"),
            mfa_details=data.get("mfa_details"),
            sms_session_id=data.get("sms_session_id"),
            email_session_id=data.get("email_session_id"),
            phone_id=phone_id
            or (self.auth.phone_id if self.auth else None)
            or WyzeCredential().phone_id,
        )

        return bool(self.auth and self.auth.access_token)

    async def _refresh(self) -> bool:
        if not self.auth or not self.auth.refresh_token:
            return False

        payload = _payload(self.auth)
        payload["refresh_token"] = self.auth.refresh_token

        data = await self._post_json(
            f"{WYZE_API}/user/refresh_token",
            json_payload=payload,
            headers=_headers_default(),
        )

        # Preserve phone_id/user_id.
        self.auth.access_token = data.get("access_token") or self.auth.access_token
        self.auth.refresh_token = data.get("refresh_token") or self.auth.refresh_token

        return bool(self.auth.access_token)

    async def post_device_v4(self, endpoint: str, params: dict) -> dict:
        if not self.auth or not self.auth.access_token:
            if not (await self._refresh()):
                ok = await self._login()
                if not ok:
                    raise AccessTokenError()

        assert self.auth is not None

        device_url = f"{CLOUD_API}/v4/device/{endpoint}"
        payload = sort_dict(params)
        headers = sign_payload(self.auth, "9319141212m2ik", payload)

        return await self._post_json(
            device_url,
            data=payload,
            headers=headers,
        )

    async def get_events(
        self,
        device_ids: Optional[list[str]] = None,
        last_ts_s: int = 0,
    ) -> List[Dict[str, Any]]:
        current_ms = int(time.time() + 60) * 1000
        params = {
            "count": 20,
            "order_by": 1,
            "begin_time": max((last_ts_s + 1) * 1_000, (current_ms - 1_000_000)),
            "end_time": current_ms,
            "nonce": str(int(time.time() * 1000)),
            "device_id_list": list(set(device_ids or [])),
            "event_value_list": [],
            "event_tag_list": [],
        }

        try:
            resp = await self.post_device_v4("get_event_list", params)
            return resp.get("event_list", []) or []
        except AccessTokenError:
            if await self._refresh() or await self._login():
                resp = await self.post_device_v4("get_event_list", params)
                return resp.get("event_list", []) or []

            return []
        except RateLimitError as ex:
            _LOGGER.warning("Wyze events rate-limited; cooling down until %s", ex.reset_by)
            return []
        except Exception as ex:
            if is_transient_exception(ex):
                raise

            _LOGGER.warning("Wyze events error: %s", ex)
            return []
