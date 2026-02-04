from __future__ import annotations

import logging
from collections import deque
from datetime import timedelta
from typing import Any, Deque, Optional

from homeassistant.core import HomeAssistant
from homeassistant.helpers.update_coordinator import DataUpdateCoordinator, UpdateFailed

_LOGGER = logging.getLogger(__name__)


class WyzeMotionEventsCoordinator(DataUpdateCoordinator[dict[str, Any]]):
    def __init__(self, hass: HomeAssistant, api, target_device_id: str, interval_s: int):
        super().__init__(
            hass,
            _LOGGER,
            name="Wyze motion events",
            update_interval=timedelta(seconds=int(interval_s)),
        )
        self._api = api
        self._target = target_device_id
        self._seen: Deque[str] = deque(maxlen=50)
        self._last_ts_s: int = 0

    async def _async_update_data(self) -> dict[str, Any]:
        try:
            _, events = await self._api.get_events([self._target], self._last_ts_s)
        except Exception as err:
            raise UpdateFailed(f"Failed fetching Wyze events: {err}") from err

        newest_ts_s = self._last_ts_s
        newest_event: Optional[dict[str, Any]] = None

        for ev in events or []:
            ev_id = ev.get("event_id")
            if ev_id and ev_id in self._seen:
                continue
            if ev_id:
                self._seen.append(ev_id)

            ev_ts_ms = ev.get("event_ts")
            if isinstance(ev_ts_ms, (int, float)) and ev_ts_ms > 0:
                ts_s = int(ev_ts_ms / 1000)
                if ts_s > newest_ts_s:
                    newest_ts_s = ts_s
                    newest_event = ev

        self._last_ts_s = newest_ts_s

        _LOGGER.debug(
            "Motion events data: target=%s last_ts_s=%s events=%d newest_ms=%s event_id=%s",
            self._target,
            self._last_ts_s,
            len(events or []),
            (newest_ts_s * 1000) if newest_ts_s else None,
            newest_event.get("event_id") if newest_event else None,
        )
        payload = {
            "found": True,
            "last_event_ts": (newest_ts_s * 1000) if newest_ts_s else None,
            "event_id": newest_event.get("event_id") if newest_event else None,
            "raw": newest_event,
        }
        _LOGGER.debug("Coordinator return payload: %s", payload)
        _LOGGER.debug("Polling events for device_id=%s", self._target)

        return payload
