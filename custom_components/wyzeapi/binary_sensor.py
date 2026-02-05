"""
This module describes the connection between Home Assistant and Wyze for Binary Sensors.

Design:
- Keep physical Wyze sensors (PIR/contact) using wyzeapy's update registration.
- For CAMERA motion: do NOT rely on wyzeapy camera_service.get_cameras()/last_event_ts
  (doesn't reflect real motion for many users).
- Instead, optionally add per-camera motion binary sensors backed by ONE shared
  Wyze cloud event_list (v4) polling coordinator (bridge-like).
"""

from __future__ import annotations

import logging
from datetime import timedelta
from typing import Callable, List, Any, Optional

from homeassistant.components.binary_sensor import (
    BinarySensorEntity,
    BinarySensorDeviceClass,
)
from homeassistant.config_entries import ConfigEntry
from homeassistant.const import ATTR_ATTRIBUTION
from homeassistant.core import HomeAssistant
from homeassistant.helpers.event import async_call_later
from homeassistant.helpers.update_coordinator import CoordinatorEntity
from homeassistant.util import dt as dt_util

from wyzeapy import Wyzeapy, SensorService, CameraService
from wyzeapy.services.sensor_service import Sensor
from wyzeapy.services.camera_service import Camera
from wyzeapy.types import DeviceTypes

from .token_manager import token_exception_handler
from .const import (
    DOMAIN,
    CONF_CLIENT,
    CONF_ENABLE_CAMERA_MOTION,
    CONF_MOTION_POLL_INTERVAL,
    CONF_MOTION_HOLD_SECONDS,
)
from .wyze_cloud_events import WyzeCloudEventsApi
from .motion_events_coordinator import WyzeMotionEventsCoordinator

_LOGGER = logging.getLogger(__name__)
ATTRIBUTION = "Data provided by Wyze"


def _normalize_device_id(value: str) -> str:
    """Normalize MAC-like string to Wyze device_id format (uppercase, no separators)."""
    if not value:
        return ""
    cleaned = "".join(ch for ch in value if ch.isalnum())
    return cleaned.upper()


@token_exception_handler
async def async_setup_entry(
    hass: HomeAssistant,
    config_entry: ConfigEntry,
    async_add_entities: Callable[[List[Any], bool], None],
):
    """
    Set up Wyze binary sensors.

    - Physical sensors (PIR/contact): register for updates.
    - Camera motion (optional): create per-camera entities driven by ONE shared cloud-events coordinator.
    """
    _LOGGER.debug("Creating new WyzeApi binary sensor component")

    client: Wyzeapy = hass.data[DOMAIN][config_entry.entry_id][CONF_CLIENT]
    sensor_service: SensorService = await client.sensor_service

    # --- Physical Wyze sensors (PIR/contact) ---
    physical = [
        WyzeSensor(sensor_service, s) for s in await sensor_service.get_sensors()
    ]
    async_add_entities(physical, True)

    # --- Camera motion via cloud events (optional) ---
    opts = config_entry.options
    if not opts.get(CONF_ENABLE_CAMERA_MOTION, False):
        return

    interval_s = int(opts.get(CONF_MOTION_POLL_INTERVAL, 90))
    hold_s = int(opts.get(CONF_MOTION_HOLD_SECONDS, 15))

    camera_service: CameraService = await client.camera_service
    cameras: list[Camera] = await camera_service.get_cameras()

    # Build / reuse ONE coordinator for this entry
    entry_data = hass.data[DOMAIN][config_entry.entry_id]
    coord: Optional[WyzeMotionEventsCoordinator] = entry_data.get("motion_events_coordinator")

    if coord is None:
        api = WyzeCloudEventsApi.from_config_entry(hass, config_entry)
        coord = WyzeMotionEventsCoordinator(
            hass=hass,
            api=api,
            config_entry_id=config_entry.entry_id,
            interval_s=interval_s,
        )
        await coord.async_config_entry_first_refresh()
        entry_data["motion_events_coordinator"] = coord
        _LOGGER.debug("Created global motion events coordinator interval_s=%s", interval_s)

    motion_entities = [
        WyzeCameraMotionEventBinarySensor(
            coordinator=coord,
            config_entry_id=config_entry.entry_id,
            camera_device_id=_normalize_device_id(cam.mac),
            camera_name=cam.nickname or cam.mac,
            camera_model=cam.product_model,
            hold_seconds=hold_s,
        )
        for cam in cameras
        if cam and cam.mac
    ]

    async_add_entities(motion_entities, True)


class WyzeSensor(BinarySensorEntity):
    """A representation of a Wyze physical sensor for Home Assistant."""

    _attr_should_poll = False

    def __init__(self, sensor_service: SensorService, sensor: Sensor):
        self._sensor_service = sensor_service
        self._sensor = sensor

    async def async_added_to_hass(self) -> None:
        await self._sensor_service.register_for_updates(self._sensor, self.process_update)

    async def async_will_remove_from_hass(self) -> None:
        await self._sensor_service.deregister_for_updates(self._sensor)

    def process_update(self, sensor: Sensor):
        self._sensor = sensor
        # register_for_updates callbacks can arrive off-loop; bounce to loop safely
        if self.hass:
            self.hass.loop.call_soon_threadsafe(self.async_write_ha_state)

    @property
    def device_info(self):
        return {
            "identifiers": {(DOMAIN, self._sensor.mac)},
            "name": self.name,
            "manufacturer": "WyzeLabs",
            "model": self._sensor.product_model,
        }

    @property
    def available(self) -> bool:
        # physical sensors report their own availability
        return bool(getattr(self._sensor, "available", True))

    @property
    def name(self):
        return self._sensor.nickname

    @property
    def is_on(self):
        return self._sensor.detected

    @property
    def unique_id(self):
        return f"{self._sensor.mac}-binary"

    @property
    def extra_state_attributes(self):
        return {
            ATTR_ATTRIBUTION: ATTRIBUTION,
            "device model": self._sensor.product_model,
            "mac": self._sensor.mac,
        }

    @property
    def device_class(self):
        if self._sensor.type is DeviceTypes.MOTION_SENSOR:
            return BinarySensorDeviceClass.MOTION
        if self._sensor.type is DeviceTypes.CONTACT_SENSOR:
            return BinarySensorDeviceClass.DOOR
        raise RuntimeError(f"Unsupported sensor type: {self._sensor.type}")


class WyzeCameraMotionEventBinarySensor(CoordinatorEntity, BinarySensorEntity):
    """Per-camera motion binary sensor driven by shared Wyze cloud events coordinator."""

    _attr_device_class = BinarySensorDeviceClass.MOTION

    def __init__(
        self,
        coordinator: WyzeMotionEventsCoordinator,
        config_entry_id: str,
        camera_device_id: str,
        camera_name: str,
        camera_model: Optional[str],
        hold_seconds: int,
    ):
        super().__init__(coordinator)
        self._entry_id = config_entry_id
        self._device_id = _normalize_device_id(camera_device_id)
        self._camera_name = camera_name
        self._camera_model = camera_model

        self._hold = timedelta(seconds=int(hold_seconds))
        self._last_seen_event_ms: Optional[int] = None
        self._unsub_off = None

        self._attr_unique_id = f"{self._device_id}-motion-event"
        self._attr_name = f"{self._camera_name} Motion (Cloud)"

    @property
    def device_info(self):
        return {
            "identifiers": {(DOMAIN, self._device_id)},
            "name": self._camera_name,
            "manufacturer": "WyzeLabs",
            "model": self._camera_model,
        }

    @property
    def available(self) -> bool:
        # available if coordinator is healthy AND this device is enabled for tracking
        enabled = self.hass.data[DOMAIN][self._entry_id].get("motion_tracking_enabled") or set()
        return self.coordinator.last_update_success and (self._device_id in enabled)

    def _handle_coordinator_update(self) -> None:
        data = self.coordinator.data or {}
        devs = data.get("devices") or {}
        d = devs.get(self._device_id) or {}
        _LOGGER.debug(
            "Binary sensor update [%s]: last_event_ts=%s event_id=%s",
            self._device_id,
            d.get("last_event_ts"),
            d.get("event_id"),
        )
        self.async_write_ha_state()

    def _schedule_turn_off(self) -> None:
        if self._unsub_off:
            self._unsub_off()
            self._unsub_off = None

        def _cb(_now):
            # async_call_later callback runs on the event loop -> safe
            self.async_write_ha_state()

        self._unsub_off = async_call_later(self.hass, self._hold.total_seconds(), _cb)

    @property
    def is_on(self) -> bool:
        if not self.available:
            return False

        data = self.coordinator.data or {}
        devs = data.get("devices") or {}
        d = devs.get(self._device_id) or {}
        ts_ms = d.get("last_event_ts")

        if not ts_ms:
            return False

        # Track newest event and schedule a refresh when hold expires
        if self._last_seen_event_ms is None or ts_ms > self._last_seen_event_ms:
            self._last_seen_event_ms = ts_ms
            self._schedule_turn_off()

        try:
            event_dt = dt_util.utc_from_timestamp(self._last_seen_event_ms / 1000.0)
        except Exception:
            return False

        return (dt_util.utcnow() - event_dt) <= self._hold

    async def async_will_remove_from_hass(self) -> None:
        if self._unsub_off:
            self._unsub_off()
            self._unsub_off = None
        await super().async_will_remove_from_hass()
