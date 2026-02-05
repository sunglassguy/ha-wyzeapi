"""
This module describes the connection between Home Assistant and Wyze for Binary Sensors.

Design:
- Keep physical Wyze sensors (PIR/contact) using wyzeapy's update registration.
- For CAMERA motion: do NOT rely on wyzeapy camera_service.get_cameras()/last_event_ts
  (doesn't reflect real motion for many users).
- Instead, optionally add ONE motion binary sensor based on Wyze cloud event_list (v4),
  mirroring docker-wyze-bridge behavior.
"""

from __future__ import annotations

import logging
from datetime import timedelta
from typing import Any, Callable, List, Optional

from homeassistant.components.binary_sensor import BinarySensorDeviceClass, BinarySensorEntity
from homeassistant.config_entries import ConfigEntry
from homeassistant.const import ATTR_ATTRIBUTION
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers.event import async_call_later
from homeassistant.helpers.update_coordinator import CoordinatorEntity
from homeassistant.util import dt as dt_util

from wyzeapy import SensorService, Wyzeapy
from wyzeapy.services.sensor_service import Sensor
from wyzeapy.types import DeviceTypes

from .const import (
    CONF_CLIENT,
    CONF_ENABLE_CAMERA_MOTION,
    CONF_MOTION_CAMERA_MAC,
    CONF_MOTION_HOLD_SECONDS,
    CONF_MOTION_POLL_INTERVAL,
    DOMAIN,
)
from .motion_events_coordinator import WyzeMotionEventsCoordinator
from .token_manager import token_exception_handler
from .wyze_cloud_events import WyzeCloudEventsApi

_LOGGER = logging.getLogger(__name__)

ATTRIBUTION = "Data provided by Wyze"


def _normalize_device_id(value: str) -> str:
    """
    Wyze cloud event_list expects device_id values that look like camera MACs but
    without separators, typically uppercase.

    Accept:
      - 7C:78:B2:87:12:50
      - 7c78b2871250
      - 7c-78-b2-87-12-50
    Normalize to:
      - 7C78B2871250
    """
    if not value:
        return ""
    cleaned = "".join(ch for ch in value if ch.isalnum())
    return cleaned.upper()


@token_exception_handler
async def async_setup_entry(
    hass: HomeAssistant,
    config_entry: ConfigEntry,
    async_add_entities: Callable[[List[Any], bool], None],
) -> None:
    """
    Set up Wyze binary sensors.

    - Physical sensors (PIR/contact): register for updates via wyzeapy.
    - Optional: ONE camera motion-event binary sensor using Wyze cloud event_list.
    """
    _LOGGER.debug("Creating new WyzeApi binary sensor component")

    client: Wyzeapy = hass.data[DOMAIN][config_entry.entry_id][CONF_CLIENT]
    sensor_service: SensorService = await client.sensor_service

    # Physical Wyze sensors (PIR/contact) - safe to register for updates
    physical = [WyzeSensor(sensor_service, s) for s in await sensor_service.get_sensors()]
    async_add_entities(physical, True)

    # Optional: ONE camera motion sensor based on cloud events
    options = config_entry.options
    enable = bool(options.get(CONF_ENABLE_CAMERA_MOTION, False))
    camera_mac = options.get(CONF_MOTION_CAMERA_MAC)

    if not (enable and camera_mac):
        return

    interval_s = max(30, int(options.get(CONF_MOTION_POLL_INTERVAL, 90)))
    hold_s = int(options.get(CONF_MOTION_HOLD_SECONDS, 15))

    device_id = _normalize_device_id(camera_mac)
    if not device_id:
        _LOGGER.warning("Camera motion enabled but MAC/device_id is empty")
        return

    api = WyzeCloudEventsApi.from_config_entry(hass, config_entry)

    motion_coord = WyzeMotionEventsCoordinator(
        hass=hass,
        api=api,
        target_device_id=device_id,
        interval_s=interval_s,
        entry_id=config_entry.entry_id,
    )
    await motion_coord.async_config_entry_first_refresh()

    async_add_entities([WyzeCameraMotionEventBinarySensor(motion_coord, device_id, hold_s)], True)


class WyzeSensor(BinarySensorEntity):
    """A representation of a Wyze physical sensor for Home Assistant."""

    _attr_should_poll = False

    def __init__(self, sensor_service: SensorService, sensor: Sensor) -> None:
        self._sensor_service = sensor_service
        self._sensor = sensor

    async def async_added_to_hass(self) -> None:
        await self._sensor_service.register_for_updates(self._sensor, self.process_update)

    async def async_will_remove_from_hass(self) -> None:
        await self._sensor_service.deregister_for_updates(self._sensor)

    @token_exception_handler
    def process_update(self, sensor: Sensor) -> None:
        """Callback invoked by wyzeapy update manager (may be off-thread)."""
        self._sensor = sensor
        # Always bounce to HA loop thread-safely
        if self.hass:
            self.hass.loop.call_soon_threadsafe(self.async_schedule_update_ha_state)

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
        # Physical sensors carry their own availability state
        return bool(getattr(self._sensor, "available", True))

    @property
    def name(self) -> str:
        return self._sensor.nickname

    @property
    def is_on(self) -> bool:
        return bool(self._sensor.detected)

    @property
    def unique_id(self) -> str:
        return f"{self._sensor.mac}-binary"

    @property
    def extra_state_attributes(self):
        return {
            ATTR_ATTRIBUTION: ATTRIBUTION,
            "device_model": self._sensor.product_model,
            "mac": self._sensor.mac,
        }

    @property
    def device_class(self):
        if self._sensor.type is DeviceTypes.MOTION_SENSOR:
            return BinarySensorDeviceClass.MOTION
        if self._sensor.type is DeviceTypes.CONTACT_SENSOR:
            return BinarySensorDeviceClass.DOOR
        raise RuntimeError(f"Unsupported sensor type: {self._sensor.type}")


class WyzeCameraMotionEventBinarySensor(CoordinatorEntity[WyzeMotionEventsCoordinator], BinarySensorEntity):
    """Binary motion sensor driven by Wyze cloud events via coordinator."""

    _attr_device_class = BinarySensorDeviceClass.MOTION

    def __init__(
        self,
        coordinator: WyzeMotionEventsCoordinator,
        camera_device_id: str,
        hold_seconds: int,
    ) -> None:
        super().__init__(coordinator)
        self._device_id = _normalize_device_id(camera_device_id)
        self._hold = timedelta(seconds=int(hold_seconds))
        self._last_seen_event_ms: Optional[int] = None
        self._unsub_off = None

        self._attr_unique_id = f"{self._device_id}-motion-event"
        self._attr_name = f"Wyze {self._device_id} Motion"

    @property
    def available(self) -> bool:
        # CoordinatorEntity already toggles availability via last_update_success,
        # but we also require the coordinator to have "found" the target.
        data = self.coordinator.data or {}
        return bool(self.coordinator.last_update_success) and bool(data.get("found", False))

    @callback
    def _handle_coordinator_update(self) -> None:
        # Proves entity is receiving coordinator updates
        data = self.coordinator.data or {}
        _LOGGER.debug(
            "Binary sensor update: last_event_ts=%s event_id=%s",
            data.get("last_event_ts"),
            data.get("event_id"),
        )
        # Recompute state now
        self.async_write_ha_state()

    @property
    def device_info(self):
        data = self.coordinator.data or {}
        raw = data.get("raw") or {}
        return {
            "identifiers": {(DOMAIN, self._device_id)},
            "name": self._attr_name,
            "manufacturer": "WyzeLabs",
            "model": raw.get("device_model"),
        }

    def _schedule_turn_off(self) -> None:
        """Schedule a state write after hold expires (runs on HA loop)."""
        if self._unsub_off:
            self._unsub_off()
            self._unsub_off = None

        @callback
        def _cb(_now) -> None:
            # This callback is executed in HA's event loop, so it's thread-safe
            self.async_write_ha_state()

        self._unsub_off = async_call_later(self.hass, self._hold.total_seconds(), _cb)

    @property
    def is_on(self) -> bool:
        data = self.coordinator.data or {}
        ts_ms = data.get("last_event_ts")
        if not ts_ms:
            return False

        # Track newest event and ensure we schedule an update when hold expires
        if self._last_seen_event_ms is None or ts_ms > self._last_seen_event_ms:
            self._last_seen_event_ms = int(ts_ms)
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
