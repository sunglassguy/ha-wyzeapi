"""
This module describes the connection between Home Assistant and Wyze for Binary Sensors
"""
import asyncio
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

from wyzeapy import Wyzeapy, CameraService, SensorService
from wyzeapy.services.camera_service import Camera
from wyzeapy.services.sensor_service import Sensor
from wyzeapy.types import DeviceTypes

from .token_manager import token_exception_handler
from .motion_coordinator import WyzeCameraMotionCoordinator
from .motion_events_coordinator import WyzeMotionEventsCoordinator
from .wyze_events_api import WyzeEventsApi

from .const import (
    DOMAIN,
    CONF_CLIENT,
    CONF_ENABLE_CAMERA_MOTION,
    CONF_MOTION_CAMERA_MAC,
    CONF_MOTION_POLL_INTERVAL,
    CONF_MOTION_HOLD_SECONDS,
)

_LOGGER = logging.getLogger(__name__)

ATTRIBUTION = "Data provided by Wyze"


@token_exception_handler
async def async_setup_entry(
    hass: HomeAssistant,
    config_entry: ConfigEntry,
    async_add_entities: Callable[[List[Any], bool], None],
):
    """
    Set up Wyze binary sensors.

    IMPORTANT:
    - We do NOT register per-camera update workers for motion anymore (API-heavy).
    - We optionally add ONE camera motion-event binary sensor using a slow poll coordinator.
    """

    _LOGGER.debug("Creating new WyzeApi binary sensor component")

    client: Wyzeapy = hass.data[DOMAIN][config_entry.entry_id][CONF_CLIENT]

    _LOGGER.debug(
        "Wyzeapy event-ish attrs: %s",
        sorted([a for a in dir(client) if "event" in a.lower()])
    )

    sensor_service: SensorService = await client.sensor_service
    camera_service: CameraService = await client.camera_service

    # Physical Wyze sensors (PIR/contact) - these are fine to register for updates.
    sensors = [
        WyzeSensor(sensor_service, sensor)
        for sensor in await sensor_service.get_sensors()
    ]
    async_add_entities(sensors, True)

    # OPTIONAL: One camera motion-event sensor using a polling coordinator (low-impact)
    options = config_entry.options
    enable = options.get(CONF_ENABLE_CAMERA_MOTION, False)
    camera_mac = options.get(CONF_MOTION_CAMERA_MAC)

    if enable and camera_mac:
        interval_s = int(options.get(CONF_MOTION_POLL_INTERVAL, 90))
        hold_s = int(options.get(CONF_MOTION_HOLD_SECONDS, 15))
    
        # Build an events-capable API client
        wyze_events_api = WyzeEventsApi.from_config_entry(hass, config_entry)
    
        motion_coord = WyzeMotionEventsCoordinator(
            hass=hass,
            wyze_events_api=wyze_events_api,
            target_device_id_or_mac=camera_mac,
            interval_s=interval_s,
        )
        await motion_coord.async_config_entry_first_refresh()
    
        async_add_entities(
            [WyzeCameraMotionEventBinarySensor(motion_coord, camera_mac, hold_s)],
            True,
        )


class WyzeSensor(BinarySensorEntity):
    """A representation of the Wyze (hardware) Sensor for use in Home Assistant."""

    def __init__(self, sensor_service: SensorService, sensor: Sensor):
        self._sensor_service = sensor_service
        self._sensor = sensor

    async def async_added_to_hass(self) -> None:
        """Registers for updates when the entity is added to Home Assistant."""
        await self._sensor_service.register_for_updates(self._sensor, self.process_update)

    async def async_will_remove_from_hass(self) -> None:
        await self._sensor_service.deregister_for_updates(self._sensor)

    def process_update(self, sensor: Sensor):
        """Process an update for the Wyze Sensor."""
        self._sensor = sensor
        self.schedule_update_ha_state()

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
        return bool(self.coordinator.data.get("found"))

    @property
    def name(self):
        return self._sensor.nickname

    @property
    def should_poll(self) -> bool:
        return False

    @property
    def is_on(self):
        """Return true if sensor detects motion/contact."""
        return self._sensor.detected

    @property
    def unique_id(self):
        # Avoid collisions with camera motion sensors
        return f"{self._sensor.mac}-sensor"

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
        raise RuntimeError(f"The device type {self._sensor.type} is not supported by this class")


class WyzeCameraMotionEventBinarySensor(CoordinatorEntity, BinarySensorEntity):
    """
    A low-impact motion binary sensor for ONE camera, driven by polling camera metadata
    (e.g., last_event_ts) via WyzeCameraMotionCoordinator.
    """

    _attr_device_class = BinarySensorDeviceClass.MOTION

    def __init__(
        self,
        coordinator: WyzeCameraMotionCoordinator,
        camera_mac: str,
        hold_seconds: int,
    ):
        super().__init__(coordinator)
        self._camera_mac = (camera_mac or "").lower()
        self._hold = timedelta(seconds=int(hold_seconds))
        self._last_seen_event: Optional[int] = None
        self._unsub_off = None

        self._attr_unique_id = f"{self._camera_mac}-motion-event"
        self._attr_name = f"Wyze {self._camera_mac} Motion"

    async def async_added_to_hass(self) -> None:
        await super().async_added_to_hass()
        self._unsub_off = None

    async def async_will_remove_from_hass(self) -> None:
        if self._unsub_off:
            self._unsub_off()
            self._unsub_off = None
        await super().async_will_remove_from_hass()

    @property
    def device_info(self):
        # Attach to the camera device record in HA by MAC
        nickname = self.coordinator.data.get("nickname") or self._attr_name
        model = self.coordinator.data.get("product_model")
        return {
            "identifiers": {(DOMAIN, self._camera_mac)},
            "name": nickname,
            "manufacturer": "WyzeLabs",
            "model": model,
        }

    @property
    def available(self) -> bool:
        return bool(self.coordinator.data.get("found"))

    def _schedule_turn_off(self) -> None:
        if self._unsub_off:
            self._unsub_off()
            self._unsub_off = None

        def _cb(_now):
            # Ensure we run on the HA event loop thread
            self.hass.loop.call_soon_threadsafe(self.async_schedule_update_ha_state)
    
        self._unsub_off = async_call_later(
            self.hass,
            self._hold.total_seconds(),
            _cb,
        )

    @property
    def is_on(self) -> bool:
        ts = self.coordinator.data.get("last_event_ts")
        if not ts:
            return False

        # Track newest event and schedule a state refresh when hold expires
        if self._last_seen_event is None or ts > self._last_seen_event:
            self._last_seen_event = ts
            self._schedule_turn_off()

        try:
            event_dt = dt_util.utc_from_timestamp(self._last_seen_event / 1000.0)
        except Exception:
            return False

        return (dt_util.utcnow() - event_dt) <= self._hold

    @property
    def extra_state_attributes(self):
        attrs = {
            ATTR_ATTRIBUTION: ATTRIBUTION,
            "camera_mac": self._camera_mac,
            "hold_seconds": int(self._hold.total_seconds()),
        }
        last_ts = self.coordinator.data.get("last_event_ts")
        if last_ts:
            attrs["last_event_ts"] = last_ts
        return attrs


# NOTE:
# We intentionally removed/disabled the old WyzeCameraMotion entity from setup to avoid
# per-camera register_for_updates workers (API-heavy). If you keep the class for reference,
# do not instantiate it for all cameras.
#
# If you want it for debugging, only create ONE instance behind an option and for a single MAC.
class WyzeCameraMotion(BinarySensorEntity):
    """Legacy API-heavy camera motion sensor (do not enable broadly)."""

    _is_on = False
    _last_event = time.time() * 1000

    def __init__(self, camera_service: CameraService, camera: Camera):
        self._camera_service = camera_service
        self._camera = camera

    @property
    def device_info(self):
        return {
            "identifiers": {(DOMAIN, self._camera.mac)},
            "name": self.name,
            "manufacturer": "WyzeLabs",
            "model": self._camera.product_model,
        }

    @property
    def available(self) -> bool:
        return self._camera.available

    @property
    def name(self):
        return self._camera.nickname

    @property
    def should_poll(self) -> bool:
        return False

    @property
    def is_on(self):
        return self._is_on

    @property
    def unique_id(self):
        return f"{self._camera.mac}-camera_motion_legacy"

    @property
    def extra_state_attributes(self):
        return {
            ATTR_ATTRIBUTION: ATTRIBUTION,
            "device model": self._camera.product_model,
            "mac": self._camera.mac,
        }

    @property
    def device_class(self):
        return BinarySensorDeviceClass.MOTION

    async def async_added_to_hass(self) -> None:
        await self._camera_service.register_for_updates(self._camera, self.process_update)

    async def async_will_remove_from_hass(self) -> None:
        await self._camera_service.deregister_for_updates(self._camera)

    def process_update(self, camera: Camera) -> None:
        self._camera = camera

        if camera.last_event_ts > self._last_event:
            self._is_on = True
            self._last_event = camera.last_event_ts
        else:
            self._is_on = False
            self._last_event = camera.last_event_ts

        self.schedule_update_ha_state()
