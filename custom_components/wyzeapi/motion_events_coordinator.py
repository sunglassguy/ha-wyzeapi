from __future__ import annotations

import logging
import time
from datetime import timedelta
from typing import Any, Optional

from homeassistant.core import HomeAssistant
from homeassistant.helpers.update_coordinator import DataUpdateCoordinator, UpdateFailed

from .const import CONF_ENABLE_CAMERA_MOTION, DOMAIN
from .http import is_transient_exception
from .wyze_cloud_events import WyzeCloudEventsApi

_LOGGER = logging.getLogger(__name__)


class WyzeMotionEventsCoordinator(DataUpdateCoordinator[dict[str, Any]]):
    """
    Poll Wyze cloud event list for all enabled device_ids.

    coordinator.data payload:
    {
        "found": True,
        "devices": {
            "<DEVICE_ID>": {
                "last_event_ts": Optional[int],
                "event_id": Optional[str],
                "raw": Optional[dict],
            },
        },
    }
    """

    def __init__(
        self,
        hass: HomeAssistant,
        api: WyzeCloudEventsApi,
        config_entry_id: str,
        interval_s: int,
    ):
        super().__init__(
            hass,
            _LOGGER,
            name="Wyze motion events",
            update_interval=timedelta(seconds=int(interval_s)),
        )
        self._api = api
        self._entry_id = config_entry_id
        # Bridge-style global last_ts in seconds.
        self._last_ts_s: int = 0

    def _enabled_ids(self) -> set[str]:
        entry = self.hass.data.get(DOMAIN, {}).get(self._entry_id, {})
        enabled = entry.get("motion_tracking_enabled") or set()
        return {str(x).upper() for x in enabled if str(x).strip()}

    async def _async_update_data(self) -> dict[str, Any]:
        start = time.perf_counter()
        success = False

        config_entry = self.hass.config_entries.async_get_entry(self._entry_id)
        opts = config_entry.options if config_entry else {}

        # Master enable gate.
        if not opts.get(CONF_ENABLE_CAMERA_MOTION, False):
            success = True
            return {"found": True, "devices": {}}

        enabled_ids = sorted(self._enabled_ids())
        if not enabled_ids:
            _LOGGER.debug("Motion events: no enabled cameras")
            success = True
            return {"found": True, "devices": {}}

        _LOGGER.debug("Polling events for %d device_id(s)", len(enabled_ids))

        try:
            result = await self._api.get_events(enabled_ids, self._last_ts_s)

            # Supports either events or (next_check, events).
            if isinstance(result, tuple) and len(result) == 2:
                _next_check, events = result
            else:
                events = result

            if events is None:
                events = []

            # Defensive: only process dict events.
            events = [e for e in events if isinstance(e, dict)]

        except Exception as err:
            if is_transient_exception(err) and self.data is not None:
                _LOGGER.warning(
                    "Wyze motion events temporarily unavailable; keeping previous "
                    "data: %s",
                    err,
                )
                return self.data

            raise UpdateFailed(f"Failed fetching Wyze motion events: {err}") from err
        finally:
            elapsed = time.perf_counter() - start
            _LOGGER.debug(
                "Finished fetching Wyze motion events data in %.3f seconds "
                "(success: %s)",
                elapsed,
                success,
            )

        # Track newest per device.
        devices: dict[str, dict[str, Any]] = {}
        newest_ts_ms_seen: Optional[int] = None

        for event in events:
            dev = str(event.get("device_id") or "").upper()
            if not dev or dev not in enabled_ids:
                continue

            ts = int(event.get("event_ts", 0) or 0)
            if ts <= 0:
                continue

            prev = devices.get(dev)
            if prev is None or ts > int(prev.get("last_event_ts") or 0):
                devices[dev] = {
                    "last_event_ts": ts,
                    "event_id": event.get("event_id"),
                    "raw": event,
                }

            if newest_ts_ms_seen is None or ts > newest_ts_ms_seen:
                newest_ts_ms_seen = ts

        # Move global last_ts forward.
        if newest_ts_ms_seen:
            newest_ts_s = newest_ts_ms_seen // 1000
            if newest_ts_s > self._last_ts_s:
                self._last_ts_s = newest_ts_s

        _LOGGER.debug(
            "Motion events: enabled=%d events=%d devices_with_events=%d last_ts_s=%d",
            len(enabled_ids),
            len(events),
            len(devices),
            self._last_ts_s,
        )

        success = True
        return {"found": True, "devices": devices}
