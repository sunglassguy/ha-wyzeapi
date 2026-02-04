from __future__ import annotations

import logging
import time
from datetime import timedelta
from typing import Any, Optional

from homeassistant.core import HomeAssistant
from homeassistant.helpers.update_coordinator import DataUpdateCoordinator, UpdateFailed

from .wyze_cloud_events import WyzeCloudEventsApi

_LOGGER = logging.getLogger(__name__)


class WyzeMotionEventsCoordinator(DataUpdateCoordinator[dict[str, Any]]):
    """
    Poll Wyze cloud event list (v4 get_event_list) for ONE device_id (camera MAC without colons),
    mirroring docker-wyze-bridge behavior.

    coordinator.data payload:
      - found: bool (polling is working)
      - last_event_ts: Optional[int]  (ms since epoch from Wyze event_ts)
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
        # IMPORTANT: start at 0 so we don't accidentally "skip past" events
        self._last_ts_s: int = 0

    async def _async_update_data(self) -> dict[str, Any]:
        start = time.perf_counter()
        _LOGGER.debug("Polling events for device_id=%s", self._target)

        try:
            # returns a list of events (possibly empty)
            events = await self._api.get_events([self._target], self._last_ts_s)
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
        newest_ts_s: Optional[int] = None

        if events:
            # Pick newest by event_ts (ms)
            newest_event = max(events, key=lambda e: int(e.get("event_ts", 0) or 0))
            newest_ts_ms = int(newest_event.get("event_ts", 0) or 0)
            newest_ts_s = newest_ts_ms // 1000

            # Only advance the cursor when we actually saw an event
            if newest_ts_s > self._last_ts_s:
                self._last_ts_s = newest_ts_s

        payload = {
            "found": True,
            # Only set last_event_ts if there was an actual event
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
