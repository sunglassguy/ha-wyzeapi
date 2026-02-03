from __future__ import annotations

import logging
from datetime import timedelta

from homeassistant.core import HomeAssistant
from homeassistant.helpers.update_coordinator import DataUpdateCoordinator, UpdateFailed

_LOGGER = logging.getLogger(__name__)


class WyzeCameraMotionCoordinator(DataUpdateCoordinator[dict]):
    def __init__(self, hass: HomeAssistant, camera_service, camera_mac: str, interval_s: int):
        super().__init__(
            hass,
            _LOGGER,
            name="Wyze camera motion events",
            update_interval=timedelta(seconds=int(interval_s)),
        )
        self._camera_service = camera_service
        self._camera_mac = (camera_mac or "").lower()

    async def _async_update_data(self) -> dict:
        try:
            cameras = await self._camera_service.get_cameras()
        except Exception as err:
            raise UpdateFailed(f"Failed fetching cameras: {err}") from err

        cam = next((c for c in cameras if (getattr(c, "mac", "") or "").lower() == self._camera_mac), None)
        if not cam:
            return {"found": False, "last_event_ts": None, "available": False}

        return {
            "found": True,
            "mac": getattr(cam, "mac", self._camera_mac),
            "last_event_ts": getattr(cam, "last_event_ts", None),
            "available": getattr(cam, "available", True),
            "nickname": getattr(cam, "nickname", self._camera_mac),
            "product_model": getattr(cam, "product_model", None),
        }
