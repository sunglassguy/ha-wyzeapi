"""Platform for sensor integration."""

from collections.abc import Callable
import logging
from typing import Any

from wyzeapy import Wyzeapy
from wyzeapy.services.camera_service import Camera

from homeassistant.components.sensor import (
    SensorDeviceClass,
    SensorEntity,
)
from homeassistant.config_entries import ConfigEntry
from homeassistant.const import (
    ATTR_ATTRIBUTION,
    PERCENTAGE,
)
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers import device_registry as dr
from homeassistant.helpers.dispatcher import async_dispatcher_connect

from .const import (
    CAMERA_UPDATED,
    CONF_CLIENT,
    DOMAIN,
)
from .token_manager import token_exception_handler

_LOGGER = logging.getLogger(__name__)
ATTRIBUTION = "Data provided by Wyze"
CAMERAS_WITH_BATTERIES = ["WVOD1", "HL_WCO2", "AN_RSCW", "GW_BE1"]


@token_exception_handler
async def async_setup_entry(
    hass: HomeAssistant,
    config_entry: ConfigEntry,
    async_add_entities: Callable[[list[Any], bool], None],
) -> None:
    """Set up camera battery sensors only."""
    _LOGGER.debug("Creating WyzeApi camera battery sensors")
    client: Wyzeapy = hass.data[DOMAIN][config_entry.entry_id][CONF_CLIENT]
    camera_service = await client.camera_service

    cameras = await camera_service.get_cameras()
    sensors = [
        WyzeCameraBatterySensor(camera)
        for camera in cameras
        if camera.product_model in CAMERAS_WITH_BATTERIES
    ]

    async_add_entities(sensors, True)


class WyzeCameraBatterySensor(SensorEntity):
    """Representation of a Wyze Camera Battery."""

    _attr_device_class = SensorDeviceClass.BATTERY
    _attr_native_unit_of_measurement = PERCENTAGE
    _attr_should_poll = False
    _attr_has_entity_name = True
    _attr_name = "Battery"

    def __init__(self, camera: Camera) -> None:
        """Initialize the sensor."""
        self._camera = camera
        self._attr_unique_id = f"{self._camera.mac}-battery"

    @callback
    def handle_camera_update(self, camera: Camera) -> None:
        """Handle camera updates."""
        self._camera = camera
        self.async_write_ha_state()

    async def async_added_to_hass(self) -> None:
        """Add listener on startup."""
        self.async_on_remove(
            async_dispatcher_connect(
                self.hass,
                f"{CAMERA_UPDATED}-{self._camera.mac}",
                self.handle_camera_update,
            )
        )

    @property
    def device_info(self):
        """Return the device info."""
        return {
            "identifiers": {(DOMAIN, self._camera.mac)},
            "connections": {(dr.CONNECTION_NETWORK_MAC, self._camera.mac)},
            "name": self._camera.nickname,
            "model": self._camera.product_model,
            "manufacturer": "WyzeLabs",
        }

    @property
    def extra_state_attributes(self):
        """Return device attributes of the entity."""
        return {
            ATTR_ATTRIBUTION: ATTRIBUTION,
            "device_model": self._camera.product_model,
        }

    @property
    def native_value(self):
        """Return the value of the sensor."""
        return self._camera.device_params.get("electricity")

    @property
    def available(self) -> bool:
        """Return if the sensor is available."""
        return self._camera.available and self._camera.device_params.get("electricity") is not None
