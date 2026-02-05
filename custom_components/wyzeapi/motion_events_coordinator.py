from __future__ import annotations

import logging
import time
from datetime import timedelta
from typing import Any, Optional, Tuple, List

from homeassistant.core import HomeAssistant
from homeassistant.helpers.update_coordinator import DataUpdateCoordinator, UpdateFailed

from .wyze_cloud_events import WyzeCloudEventsApi

_LOGGER = logging.getLogger(__name__)


class WyzeMotionEventsCoordinator(DataUpdateCoordinator[dict[str, Any]]):
    """
    Poll Wyze cloud event list for ONE device_id.

    Supports WyzeCloudEventsApi.get_events returning either:
      - List[dict] events
      - Tuple[next_check: float, List[dict]] (bridge-style)

    coordinator.data payload:
      - found: bool
      - last_event_ts: Optional[int] (ms since epoch from Wyze event_ts)
      - event_id: Optional[str]
      - raw: Optional[dict]
    """

    def __init__(
        self,
        hass: HomeAssistant,
        api: WyzeCloudEventsApi,
        target_device_id: str,
        interval_s: int,
    ):
        super().__init__(
            hass,
            _LOGGER,
            name="Wyze motion events",
            update_interval=timedelta(seconds=int(interval_s)),
        )
        self._api = api
        self._target = target_device_id
        self._last_ts_s: int = 0

    async def _async_update_data(self) -> dict[str, Any]:
        start = time.perf_counter()
        _LOGGER.debug("Polling events for device_id=%s", self._target)

        enabled_ids = set(self.hass.data[DOMAIN][self.config_entry_id]["motion_tracking_enabled"])
        if self._target not in enabled_ids:
            return {"found": True, "last_event_ts": None, "event_id": None, "raw": None}

        try:
            result = await self._api.get_events([self._target], self._last_ts_s)

            # Handle either (next_check, events) or events
            if isinstance(result, tuple) and len(result) == 2:
                _next_check, events = result
            else:
                events = result

            if events is None:
                events = []

            # Filter to dict-like events only (defensive)
            events = [e for e in events if isinstance(e, dict)]

        except Exception as err:
            raise UpdateFailed(f"Failed fetching Wyze motion events: {err}") from err
        finally:
            elapsed = time.perf_counter() - start
            _LOGGER.debug(
                "Finished fetching Wyze motion events data in %.3f seconds (success: %s)",
                elapsed,
                True,
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
        _LOGGER.debug("Coordinator return payload: %s", payload)

        return payload
