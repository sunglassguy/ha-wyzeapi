from __future__ import annotations

import logging
from datetime import timedelta

from homeassistant.core import HomeAssistant
from homeassistant.helpers.update_coordinator import DataUpdateCoordinator, UpdateFailed

_LOGGER = logging.getLogger(__name__)


def _norm(value: str) -> str:
    return (value or "").strip().lower()


def _norm_mac(value: str) -> str:
    """Normalize MAC-like identifiers for comparison (strip : and -)."""
    return _norm(value).replace(":", "").replace("-", "")


class WyzeCameraMotionCoordinator(DataUpdateCoordinator[dict]):
    def __init__(self, hass: HomeAssistant, camera_service, camera_mac: str, interval_s: int):
        super().__init__(
            hass,
            _LOGGER,
            name="Wyze camera motion events",
            update_interval=timedelta(seconds=int(interval_s)),
        )
        self._camera_service = camera_service
        # Allow either MAC (with/without separators) OR a WYZE_* device id.
        self._target_raw = camera_mac or ""
        self._target = _norm_mac(camera_mac)  # for WYZE_* this is just lowercase

    async def _async_update_data(self) -> dict:
        try:
            cameras = await self._camera_service.get_cameras()
        except Exception as err:
            raise UpdateFailed(f"Failed fetching cameras: {err}") from err

        # Debug: log identifiers for all cameras returned by wyzeapy.
        for c in cameras:
            _LOGGER.debug(
                "Camera debug: nickname=%s mac=%s device_id=%s last_event_ts=%s available=%s product_model=%s",
                getattr(c, "nickname", None),
                getattr(c, "mac", None),
                getattr(c, "device_id", None) or getattr(c, "deviceId", None),
                getattr(c, "last_event_ts", None),
                getattr(c, "available", None),
                getattr(c, "product_model", None),
            )

        def matches(cam) -> bool:
            # Candidate identifiers that may exist depending on wyzeapy version/model
            candidates = []
            for attr in ("mac", "device_id", "deviceId", "product_mac", "productMac", "p2p_id", "p2pId"):
                val = getattr(cam, attr, None)
                if isinstance(val, str) and val:
                    candidates.append(val)

            # Compare as MAC-normalized and as plain-lowercased
            for v in candidates:
                if _norm_mac(v) == self._target:
                    return True
                if _norm(v) == _norm(self._target_raw):
                    return True
            return False

        cam = next((c for c in cameras if matches(c)), None)
        if not cam:
            return {"found": False, "last_event_ts": None, "available": False}

        return {
            "found": True,
            "mac": getattr(cam, "mac", None),
            "device_id": getattr(cam, "device_id", None) or getattr(cam, "deviceId", None),
            "last_event_ts": getattr(cam, "last_event_ts", None),
            "available": getattr(cam, "available", True),
            "nickname": getattr(cam, "nickname", self._target_raw),
            "product_model": getattr(cam, "product_model", None),
        }
