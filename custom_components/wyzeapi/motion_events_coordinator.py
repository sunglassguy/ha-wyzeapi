from __future__ import annotations

import logging
import time
from collections import deque
from datetime import timedelta
from typing import Any, Deque, Optional

from homeassistant.core import HomeAssistant
from homeassistant.helpers.update_coordinator import DataUpdateCoordinator, UpdateFailed

_LOGGER = logging.getLogger(__name__)


def _norm_id(value: str) -> str:
    """Normalize device identifiers (MAC or device_id)."""
    return (value or "").replace(":", "").replace("-", "").strip().lower()


class WyzeMotionEventsCoordinator(DataUpdateCoordinator[dict[str, Any]]):
    """
    Poll Wyze "get_event_list" (api v4) and surface latest motion event timestamp.

    This mirrors docker-wyze-bridge logic:
      - pass device id list (bridge passes camera.mac)
      - use begin_time/end_time window
      - track last_ts (seconds)
      - de-dupe by event_id
    """

    def __init__(
        self,
        hass: HomeAssistant,
        wyze_events_api: Any,
        target_device_id_or_mac: str,
        interval_s: int,
    ) -> None:
        super().__init__(
            hass,
            _LOGGER,
            name="Wyze motion events",
            update_interval=timedelta(seconds=int(interval_s)),
        )
        self._api = wyze_events_api
        self._target_raw = target_device_id_or_mac or ""
        self._target_norm = _norm_id(self._target_raw)

        self._seen: Deque[str] = deque(maxlen=50)
        self._last_check: float = 0.0
        self._last_ts: int = 0  # seconds since epoch, as used in bridge

    async def _async_update_data(self) -> dict[str, Any]:
        # Call the sync API in an executor if needed (safe either way)
        try:
            last_check, events = await self.hass.async_add_executor_job(
                self._api.get_events,
                [self._target_raw],  # bridge passes macs as device_id_list
                self._last_ts,
            )
        except Exception as err:
            raise UpdateFailed(f"Failed fetching Wyze events: {err}") from err

        self._last_check = float(last_check) if last_check else time.time()
        events = events or []

        newest_event: Optional[dict[str, Any]] = None
        newest_ts_s: int = self._last_ts

        for ev in events:
            ev_id = ev.get("event_id")
            if ev_id and ev_id in self._seen:
                continue

            if ev_id:
                self._seen.append(ev_id)

            # Wyze returns event_ts in ms; bridge stores seconds
            ev_ts_ms = ev.get("event_ts")
            if isinstance(ev_ts_ms, (int, float)) and ev_ts_ms > 0:
                ts_s = int(ev_ts_ms / 1000)
                if ts_s > newest_ts_s:
                    newest_ts_s = ts_s
                    newest_event = ev

        # Advance last_ts so next poll only asks for newer events
        self._last_ts = newest_ts_s

        # Basic debug
        if newest_event:
            _LOGGER.debug(
                "New Wyze motion event: id=%s ts_s=%s device_id=%s",
                newest_event.get("event_id"),
                newest_ts_s,
                newest_event.get("device_id") or newest_event.get("device_mac"),
            )

        return {
            "found": True,
            "target": self._target_raw,
            "last_event_ts": (newest_ts_s * 1000) if newest_ts_s else None,  # ms for your binary sensor
            "event_id": newest_event.get("event_id") if newest_event else None,
            "raw": newest_event,
        }

