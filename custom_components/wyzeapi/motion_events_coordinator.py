from __future__ import annotations

import logging
import time
from datetime import timedelta
from typing import Any, Optional

from homeassistant.core import HomeAssistant
from homeassistant.helpers.update_coordinator import DataUpdateCoordinator, UpdateFailed

from .const import DOMAIN  # <-- add this
from .wyze_cloud_events import WyzeCloudEventsApi

_LOGGER = logging.getLogger(__name__)


class WyzeMotionEventsCoordinator(DataUpdateCoordinator[dict[str, Any]]):
    """Poll Wyze cloud event list for ONE device_id."""

    def __init__(
        self,
        hass: HomeAssistant,
        api: WyzeCloudEventsApi,
        target_device_id: str,
        interval_s: int,
        entry_id: str,  # <-- add this
    ):
        super().__init__(
            hass,
            _LOGGER,
            name="Wyze motion events",
            update_interval=timedelta(seconds=int(interval_s)),
        )
        self._api = api
        self._target = (target_device_id or "").upper()
        self._last_ts_s: int = 0
        self._entry_id = entry_id  # <-- store it

    async def _async_update_data(self) -> dict[str, Any]:
        start = time.perf_counter()
        _LOGGER.debug("Polling events for device_id=%s", self._target)

        try:
            # HA-only gating: if not enabled, do not hit the Wyze API.
            enabled_ids = set(
                self.hass.data.get(DOMAIN, {})
                .get(self._entry_id, {})
                .get("motion_tracking_enabled", set())
            )
            if self._target not in enabled_ids:
                _LOGGER.debug("Motion tracking disabled for %s; skipping poll", self._target)
                return {"found": True, "last_event_ts": None, "event_id": None, "raw": None}

            result = await self._api.get_events([self._target], self._last_ts_s)

            # Handle either (next_check, events) or events
            if isinstance(result, tuple) and len(result) == 2:
                _next_check, events = result
            else:
                events = result

            if not events:
                events = []

            # defensive: dict-only
            events = [e for e in events if isinstance(e, dict)]

        except Exception as err:
            raise UpdateFailed(f"Failed fetching Wyze motion events: {err}") from err
        finally:
            elapsed = time.perf_counter() - start
            _LOGGER.debug(
                "Finished fetching Wyze motion events data in %.3f seconds",
                elapsed,
            )

        newest_event: Optional[dict[str, Any]] = None

        if events:
            newest_event = max(events, key=lambda e: int(e.get("event_ts", 0) or 0))
            newest_ts_ms = int(newest_event.get("event_ts", 0) or 0)
            newest_ts_s = newest_ts_ms // 1000
            if newest_ts_s > self._last_ts_s:
                self._last_ts_s = newest_ts_s

        payload = {
            "found": True,
            "last_event_ts": int(newest_event["event_ts"]) if newest_event else None,
            "event_id": newest_event.get("event_id") if newest_event else None,
            "raw": newest_event,
        }

        _LOGGER.debug(
            "Motion events data: target=%s last_ts_s=%s events=%d last_event_ts=%s event_id=%s",
            self._target,
            self._last_ts_s,
            len(events),
            payload["last_event_ts"],
            payload["event_id"],
        )
        return payload
