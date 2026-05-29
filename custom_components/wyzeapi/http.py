"""HTTP helpers for Wyze cloud calls."""

from __future__ import annotations

import asyncio
import logging
import ssl
from typing import Any

import certifi
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


def wyze_ssl_context() -> ssl.SSLContext:
    """Return an SSL context for Wyze API calls.

    This keeps certificate and hostname verification enabled, but relaxes
    Python 3.13+'s strict X.509 mode for compatibility with older/nonstandard
    chains seen from some cloud endpoints or proxies.
    """
    global _SSL_CONTEXT

    if _SSL_CONTEXT is not None:
        return _SSL_CONTEXT

    ctx = ssl.create_default_context(cafile=certifi.where())

    # Do not disable TLS verification. Only relax Python 3.13+'s strict
    # certificate-chain validation mode for Wyze cloud requests.
    if hasattr(ssl, "VERIFY_X509_STRICT"):
        ctx.verify_flags &= ~ssl.VERIFY_X509_STRICT

    _SSL_CONTEXT = ctx
    return ctx


def _should_retry_status(status: int) -> bool:
    return status == 429 or 500 <= status <= 599


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
    """Request JSON from Wyze with sane timeout, SSL context, and retries."""
    kwargs.setdefault("timeout", WYZE_REQUEST_TIMEOUT)
    kwargs.setdefault("ssl", wyze_ssl_context())

    last_err: BaseException | None = None

    for attempt in range(retries):
        try:
            async with session.request(method, url, **kwargs) as resp:
                try:
                    data = await resp.json(content_type=None)
                except ContentTypeError:
                    body = await resp.text()
                    logger.debug(
                        "Non-JSON Wyze response from %s %s: status=%s body=%s",
                        method.upper(),
                        url,
                        resp.status,
                        body[:500],
                    )
                    resp.raise_for_status()
                    raise

                if _should_retry_status(resp.status):
                    raise ClientResponseError(
                        resp.request_info,
                        resp.history,
                        status=resp.status,
                        message=f"Transient Wyze HTTP status {resp.status}",
                        headers=resp.headers,
                    )

                resp.raise_for_status()
                return data

        except ClientResponseError as err:
            last_err = err

            if not _should_retry_status(err.status) or attempt >= retries - 1:
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
