\"""HTTP helpers for Wyze cloud calls."""

from __future__ import annotations

import asyncio
import json
import logging
import ssl
from typing import Any

from aiohttp import (
    ClientConnectionError,
    ClientConnectorError,
    ClientOSError,
    ClientPayloadError,
    ClientResponseError,
    ClientSession,
    ClientTimeout,
    ContentTypeError,
    ServerDisconnectedError,
)

from homeassistant.core import HomeAssistant

_LOGGER = logging.getLogger(__name__)

WYZE_HTTP_RETRIES = 3

WYZE_REQUEST_TIMEOUT = ClientTimeout(
    total=35,
    connect=10,
    sock_connect=10,
    sock_read=25,
)

TRANSIENT_EXCEPTIONS = (
    asyncio.TimeoutError,
    TimeoutError,
    ClientConnectionError,
    ClientConnectorError,
    ClientOSError,
    ClientPayloadError,
    ServerDisconnectedError,
    OSError,
    ssl.SSLError,
)

_SSL_CONTEXT: ssl.SSLContext | None = None


def _build_wyze_ssl_context() -> ssl.SSLContext:
    """Build the Wyze SSL context.

    Wyze's API certificate chain is failing validation in some Home Assistant
    Python 3.14 environments. Use an unverified TLS context for Wyze API calls
    only, instead of disabling SSL globally.
    """
    ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
    ctx.check_hostname = False
    ctx.verify_mode = ssl.CERT_NONE
    return ctx


async def async_setup_wyze_http(hass: HomeAssistant) -> None:
    """Preload SSL context outside the event loop."""
    global _SSL_CONTEXT

    if _SSL_CONTEXT is None:
        _SSL_CONTEXT = await hass.async_add_executor_job(_build_wyze_ssl_context)

        _LOGGER.warning(
            "Wyze API TLS certificate verification is disabled for this integration "
            "only. check_hostname=%s verify_mode=%s",
            _SSL_CONTEXT.check_hostname,
            _SSL_CONTEXT.verify_mode,
        )


def wyze_ssl_context() -> ssl.SSLContext:
    """Return the preloaded Wyze SSL context."""
    if _SSL_CONTEXT is None:
        raise RuntimeError(
            "Wyze SSL context was used before async_setup_wyze_http() completed"
        )

    return _SSL_CONTEXT


def is_transient_status(status: int) -> bool:
    """Return True for HTTP statuses that should be retried."""
    return status == 429 or 500 <= status <= 599


def is_transient_exception(err: BaseException) -> bool:
    """Return True for network/HTTP failures that are usually temporary."""
    if isinstance(err, ClientResponseError):
        return is_transient_status(err.status)

    return isinstance(err, TRANSIENT_EXCEPTIONS)


async def _sleep_before_retry(attempt: int) -> None:
    await asyncio.sleep(min(0.75 * (2**attempt), 5.0))


async def request_json_with_retries(
    session: ClientSession,
    method: str,
    url: str,
    *,
    retries: int = WYZE_HTTP_RETRIES,
    logger: logging.Logger = _LOGGER,
    **kwargs: Any,
) -> dict[str, Any]:
    """Request JSON from Wyze with timeout, SSL context, and retries."""
    kwargs["timeout"] = kwargs.get("timeout", WYZE_REQUEST_TIMEOUT)

    # Important: force our Wyze SSL context. Do not use setdefault here.
    # Some callers or session defaults may already provide ssl=True.
    kwargs["ssl"] = wyze_ssl_context()

    last_err: BaseException | None = None

    for attempt in range(retries):
        try:
            logger.debug(
                "Wyze request %s %s using ssl check_hostname=%s verify_mode=%s",
                method.upper(),
                url,
                kwargs["ssl"].check_hostname,
                kwargs["ssl"].verify_mode,
            )

            async with session.request(method, url, **kwargs) as resp:
                if is_transient_status(resp.status):
                    body = await resp.text()
                    raise ClientResponseError(
                        resp.request_info,
                        resp.history,
                        status=resp.status,
                        message=(
                            f"Transient Wyze HTTP status {resp.status}: "
                            f"{body[:300]}"
                        ),
                        headers=resp.headers,
                    )

                resp.raise_for_status()

                try:
                    return await resp.json(content_type=None)
                except (ContentTypeError, json.JSONDecodeError, ValueError) as err:
                    body = await resp.text()
                    logger.debug(
                        "Non-JSON Wyze response from %s %s: status=%s body=%s",
                        method.upper(),
                        url,
                        resp.status,
                        body[:500],
                    )
                    raise err

        except ClientResponseError as err:
            last_err = err

            if not is_transient_status(err.status) or attempt >= retries - 1:
                raise

            logger.debug(
                "Transient Wyze HTTP status %s for %s %s; retrying",
                err.status,
                method.upper(),
                url,
            )
            await _sleep_before_retry(attempt)

        except TRANSIENT_EXCEPTIONS as err:
            last_err = err

            if attempt >= retries - 1:
                raise

            logger.debug(
                "Transient Wyze request failure for %s %s: %s; retrying",
                method.upper(),
                url,
                err,
            )
            await _sleep_before_retry(attempt)

    if last_err is not None:
        raise last_err

    raise RuntimeError(f"Wyze request failed without an exception: {method} {url}")
