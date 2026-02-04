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
import time
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

from wyzeapy import Wyzeapy, SensorService
from wyzeapy.services.sensor_service import Sensor
from wyzeapy.types import DeviceTypes

from .token_manager import token_exception_handler
from .const import (
    DOMAIN,
    CONF_CLIENT,
    CONF_ENABLE_CAMERA_MOTION,
    CONF_MOTION_CAMERA_MAC,
    CONF_MOTION_POLL_INTERVAL,
    CONF_MOTION_HOLD_SECONDS,
)

# NEW (you'll add these files next)
from .wyze_cloud_events import WyzeCloudEventsApi
from .motion_events_coordinator import WyzeMotionEventsCoordinator

_LOGGER = logging.getLogger(__name__)

ATTRIBUTION = "Data provided by Wyze"


def _normalize_device_id(value: str) -> str:
    """
    Wyze bridge passes camera.mac values into device_id_list, typically as
    an uppercase hex string WITHOUT separators.

    Accept user input in any of these forms:
      - 7C:78:B2:87:12:50
      - 7c78b2871250
      - 7c-78-b2-87-12-50
    and normalize to: 7C78B2871250
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
):
    """
    Set up Wyze binary sensors.

    - Physical sensors (PIR/contact): register for updates.
    - Optional: ONE camera motion-event binary sensor using Wyze cloud event_list.
    """

    _LOGGER.debug("Creating new WyzeApi binary sensor component")

    client: Wyzeapy = hass.data[DOMAIN][config_entry.entry_id][CONF_CLIENT]
    sensor_service: SensorService = await client.sensor_service

    # Physical Wyze sensors (PIR/contact) - safe to register for updates
    sensors = [
        WyzeSensor(sensor_service, sensor)
        for sensor in await sensor_service.get_sensors()
    ]
    async_add_entities(sensors, True)

    # Optional: ONE camera motion sensor based on cloud events
    options = config_entry.options
    enable = options.get(CONF_ENABLE_CAMERA_MOTION, False)
    camera_mac = options.get(CONF_MOTION_CAMERA_MAC)

    if enable and camera_mac:
        interval_s = int(options.get(CONF_MOTION_POLL_INTERVAL, 90))
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
        )
        await motion_coord.async_config_entry_first_refresh()

        async_add_entities(
            [WyzeCameraMotionEventBinarySensor(motion_coord, device_id, hold_s)],
            True,
        )


class WyzeSensor(BinarySensorEntity):
    """A representation of a Wyze physical sensor for Home Assistant."""

    def __init__(self, sensor_service: SensorService, sensor: Sensor):
        self._sensor_service = sensor_service
        self._sensor = sensor

    async def async_added_to_hass(self) -> None:
        await self._sensor_service.register_for_updates(
            self._sensor, self.process_update
        )

    async def async_will_remove_from_hass(self) -> None:
        await self._sensor_service.deregister_for_updates(self._sensor)

    def process_update(self, sensor: Sensor):
        self._sensor = sensor
        # Thread-safe: bounce onto HA loop
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
        # Available if the coordinator is successfully updating
        return self.coordinator.last_update_success

    @property
    def name(self):
        return self._sensor.nickname

    @property
    def should_poll(self) -> bool:
        return False

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
    """Binary motion sensor driven by Wyze cloud events via coordinator."""

    _attr_device_class = BinarySensorDeviceClass.MOTION

    def __init__(
        self,
        coordinator: WyzeMotionEventsCoordinator,
        camera_device_id: str,
        hold_seconds: int,
    ):
        super().__init__(coordinator)
        self._device_id = (camera_device_id or "").replace(":", "").replace("-", "").upper()
        self._hold = timedelta(seconds=int(hold_seconds))
        self._last_seen_event_ms: Optional[int] = None
        self._unsub_off = None

        self._attr_unique_id = f"{self._device_id}-motion-event"
        self._attr_name = f"Wyze {self._device_id} Motion"

    @property
    def available(self) -> bool:
        return self.coordinator.last_update_success
      
    def _handle_coordinator_update(self) -> None:
        # 🔍 DEBUG: proves entity is receiving coordinator updates
        _LOGGER.debug(
            "Binary sensor update: last_event_ts=%s event_id=%s",
            self.coordinator.data.get("last_event_ts"),
            self.coordinator.data.get("event_id"),
        )

        # This schedules the entity state to be recalculated
        self.async_schedule_update_ha_state()

    @property
    def device_info(self):
        return {
            "identifiers": {(DOMAIN, self._device_id)},
            "name": self._attr_name,
            "manufacturer": "WyzeLabs",
        }

    def _schedule_turn_off(self) -> None:
        if self._unsub_off:
            self._unsub_off()
            self._unsub_off = None

        def _cb(_now):
            self.hass.loop.call_soon_threadsafe(self.async_write_ha_state)

        self._unsub_off = async_call_later(
            self.hass,
            self._hold.total_seconds(),
            _cb,
        )

    @property
    def is_on(self) -> bool:
        ts_ms = self.coordinator.data.get("last_event_ts")
        if not ts_ms:
            return False

        # Track newest event and ensure we schedule an update when hold expires
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
