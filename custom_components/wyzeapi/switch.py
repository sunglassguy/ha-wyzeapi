#!/usr/bin/python3

"""Platform for switch integration."""

from __future__ import annotations

from collections.abc import Callable
from datetime import timedelta
import logging
from typing import Any

from aiohttp.client_exceptions import ClientConnectionError
from wyzeapy import CameraService, Wyzeapy
from wyzeapy.exceptions import AccessTokenError, ParameterError, UnknownApiError
from wyzeapy.services.camera_service import Camera
from wyzeapy.types import Event

from homeassistant.components.switch import SwitchEntity
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant, callback
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers import device_registry as dr
from homeassistant.helpers.dispatcher import (
    async_dispatcher_connect,
    async_dispatcher_send,
)
from homeassistant.helpers.entity import EntityCategory

from .const import (
    CAMERA_UPDATED,
    CONF_CLIENT,
    DOMAIN,
    WYZE_CAMERA_EVENT,
    WYZE_NOTIFICATION_TOGGLE,
    CONF_ENABLE_CAMERA_MOTION,
    CONF_MOTION_TRACKING_DEVICES,
)
from .token_manager import token_exception_handler

_LOGGER = logging.getLogger(__name__)
ATTRIBUTION = "Data provided by Wyze"
SCAN_INTERVAL = timedelta(seconds=30)

MOTION_SWITCH_UNSUPPORTED = [
    "GW_BE1",
    "GW_GC1",
    "GW_GC2",
]  # Video doorbell pro, OG, OG 3x Telephoto
POWER_SWITCH_UNSUPPORTED = ["GW_BE1"]  # Video doorbell pro (device has no off function)
NOTIFICATION_SWITCH_UNSUPPORTED = {
    "GW_GC1",
    "GW_GC2",
}  # OG and OG 3x Telephoto models currently unsupported due to InvalidSignature2 error


def _device_id_from_mac(mac: str) -> str:
    """Wyze cloud events 'device_id' uses MAC without separators."""
    return (mac or "").replace(":", "").replace("-", "").upper()


@token_exception_handler
async def async_setup_entry(
    hass: HomeAssistant,
    config_entry: ConfigEntry,
    async_add_entities: Callable[[list[Any], bool], None],
) -> None:
    """Set up switch entities for cameras only."""
    _LOGGER.debug("Creating new WyzeApi switch component (camera-focused)")
    client: Wyzeapy = hass.data[DOMAIN][config_entry.entry_id][CONF_CLIENT]
    camera_service = await client.camera_service

    switches: list[SwitchEntity] = []

    # Camera-related switches only
    camera_devices = await camera_service.get_cameras()
    for cam in camera_devices:
        # Notification toggle switch (Wyze-side)
        if cam.product_model not in NOTIFICATION_SWITCH_UNSUPPORTED:
            switches.append(WyzeCameraNotificationSwitch(camera_service, cam))

        # IoT Power switch (Wyze-side)
        if cam.product_model not in POWER_SWITCH_UNSUPPORTED:
            switches.append(WyzeCameraSwitch(camera_service, cam))

        # Motion detection toggle (Wyze-side)
        if cam.product_model not in MOTION_SWITCH_UNSUPPORTED:
            switches.append(WyzeCameraMotionSwitch(camera_service, cam))

        # HA-only cloud-events tracking toggle
        switches.append(WyzeCameraMotionTrackingSwitch(hass, config_entry, cam))

    # Global notifications toggle
    switches.append(WyzeNotifications(client))

    async_add_entities(switches, True)


class WyzeNotifications(SwitchEntity):
    """Class for global Wyze notification switch."""

    _attr_should_poll = False
    _attr_name = "Wyze Notifications"

    def __init__(self, client: Wyzeapy) -> None:
        """Initialize the global notification switch."""
        self._client = client
        self._is_on = False
        self._uid = WYZE_NOTIFICATION_TOGGLE
        self._just_updated = False

    @property
    def is_on(self) -> bool:
        """Return true if notifications are enabled."""
        return self._is_on

    @property
    def device_info(self):
        """Return device info."""
        return {
            "identifiers": {(DOMAIN, self._uid)},
            "name": "Wyze Notifications",
            "manufacturer": "WyzeLabs",
            "model": "WyzeNotificationToggle",
        }

    @property
    def available(self) -> bool:
        """Always available since this is a global toggle."""
        return True

    @property
    def unique_id(self):
        """Return unique ID."""
        return self._uid

    async def async_turn_on(self, **kwargs: Any) -> None:
        """Turn on global notifications."""
        try:
            await self._client.enable_notifications()
        except (AccessTokenError, ParameterError, UnknownApiError) as err:
            raise HomeAssistantError(f"Wyze returned an error: {err.args}") from err
        except ClientConnectionError as err:
            raise HomeAssistantError(err) from err
        else:
            self._is_on = True
            self._just_updated = True
            self.async_schedule_update_ha_state()

    async def async_turn_off(self, **kwargs: Any) -> None:
        """Turn off global notifications."""
        try:
            await self._client.disable_notifications()
        except (AccessTokenError, ParameterError, UnknownApiError) as err:
            raise HomeAssistantError(f"Wyze returned an error: {err.args}") from err
        except ClientConnectionError as err:
            raise HomeAssistantError(err) from err
        else:
            self._is_on = False
            self._just_updated = True
            self.async_schedule_update_ha_state()

    async def async_update(self):
        """Update notification state."""
        if not self._just_updated:
            self._is_on = await self._client.notifications_are_on
        else:
            self._just_updated = False


class WyzeCameraSwitch(SwitchEntity):
    """Representation of a Wyze Camera Power Switch."""

    _just_updated = False
    _old_event_ts: int = 0
    _attr_should_poll = False

    def __init__(self, service: CameraService, device: Camera) -> None:
        """Initialize a camera power switch."""
        self._device = Camera(device.raw_dict)
        self._service = service

    @property
    def device_info(self):
        """Return device info."""
        return {
            "identifiers": {(DOMAIN, self._device.mac)},
            "connections": {(dr.CONNECTION_NETWORK_MAC, self._device.mac)},
            "name": self._device.nickname,
            "manufacturer": "WyzeLabs",
            "model": self._device.product_model,
        }

    @token_exception_handler
    async def async_turn_on(self, **kwargs: Any) -> None:
        """Turn on the camera."""
        try:
            await self._service.turn_on(self._device)
        except (AccessTokenError, ParameterError, UnknownApiError) as err:
            raise HomeAssistantError(f"Wyze returned an error: {err.args}") from err
        except ClientConnectionError as err:
            raise HomeAssistantError(err) from err
        else:
            self._device.on = True
            self._just_updated = True
            self.async_schedule_update_ha_state()

    @token_exception_handler
    async def async_turn_off(self, **kwargs: Any) -> None:
        """Turn off the camera."""
        try:
            await self._service.turn_off(self._device)
        except (AccessTokenError, ParameterError, UnknownApiError) as err:
            raise HomeAssistantError(f"Wyze returned an error: {err.args}") from err
        except ClientConnectionError as err:
            raise HomeAssistantError(err) from err
        else:
            self._device.on = False
            self._just_updated = True
            self.async_schedule_update_ha_state()

    @property
    def name(self):
        """Return the name."""
        return f"{self._device.nickname} Power"

    @property
    def available(self):
        """Return availability."""
        return self._device.available

    @property
    def is_on(self):
        """Return true if camera is on."""
        return self._device.on

    @property
    def unique_id(self):
        """Return unique ID."""
        return f"{self._device.mac}-switch"

    @property
    def extra_state_attributes(self):
        """Return device attributes."""
        dev_info: dict[str, Any] = {}
        if self._device.device_params.get("electricity"):
            dev_info["Battery"] = str(self._device.device_params.get("electricity")) + "%"
        if self._device.device_params.get("ip"):
            dev_info["IP"] = str(self._device.device_params.get("ip"))
        if self._device.device_params.get("rssi"):
            dev_info["RSSI"] = str(self._device.device_params.get("rssi"))
        if self._device.device_params.get("ssid"):
            dev_info["SSID"] = str(self._device.device_params.get("ssid"))
        return dev_info

    @token_exception_handler
    async def async_update(self):
        """Update the entity."""
        if not self._just_updated:
            self._device = await self._service.update(self._device)
        else:
            self._just_updated = False

    @callback
    def async_update_callback(self, camera: Camera):
        """Update the camera state and fire events."""
        self._device = camera
        async_dispatcher_send(self.hass, f"{CAMERA_UPDATED}-{camera.mac}", camera)
        self.async_schedule_update_ha_state()

        # Fire camera event if new event detected
        if (
            self._old_event_ts > 0
            and self._old_event_ts != camera.last_event_ts
            and camera.last_event is not None
        ):
            event: Event = camera.last_event
            _screenshot_url = None
            _video_url = None
            _ai_tag_list: list[Any] = []

            for resource in event.file_list:
                _ai_tag_list = _ai_tag_list + resource["ai_tag_list"]
                if resource["type"] == 1:
                    _screenshot_url = resource["url"]
                elif resource["type"] == 2:
                    _video_url = resource["url"]

            _LOGGER.debug("Camera: %s has a new event", camera.nickname)
            self.hass.bus.fire(
                WYZE_CAMERA_EVENT,
                {
                    "device_name": camera.nickname,
                    "device_mac": camera.mac,
                    "ai_tag_list": _ai_tag_list,
                    "tag_list": event.tag_list,
                    "event_screenshot": _screenshot_url,
                    "event_video": _video_url,
                },
            )
        self._old_event_ts = camera.last_event_ts

    async def async_added_to_hass(self) -> None:
        """Subscribe to updates."""
        self._device.callback_function = self.async_update_callback
        self._service.register_updater(self._device, 30)
        await self._service.start_update_manager()
        return await super().async_added_to_hass()

    async def async_will_remove_from_hass(self) -> None:
        """Unregister updater."""
        self._service.unregister_updater(self._device)


class WyzeCameraNotificationSwitch(SwitchEntity):
    """Representation of a Wyze Camera Notification Switch."""

    _attr_should_poll = False

    def __init__(self, service: CameraService, device: Camera) -> None:
        """Initialize notification switch."""
        self._service = service
        self._device = device

    @property
    def device_info(self):
        """Return device info."""
        return {
            "identifiers": {(DOMAIN, self._device.mac)},
            "name": self._device.nickname,
            "manufacturer": "WyzeLabs",
            "model": self._device.product_model,
        }

    async def async_turn_on(self, **kwargs: Any) -> None:
        """Turn on notifications."""
        try:
            await self._service.turn_on_notifications(self._device)
        except (AccessTokenError, ParameterError, UnknownApiError) as err:
            raise HomeAssistantError(f"Wyze returned an error: {err.args}") from err
        except ClientConnectionError as err:
            raise HomeAssistantError(err) from err
        else:
            self._device.notify = True
            self.async_write_ha_state()

    async def async_turn_off(self, **kwargs: Any) -> None:
        """Turn off notifications."""
        try:
            await self._service.turn_off_notifications(self._device)
        except (AccessTokenError, ParameterError, UnknownApiError) as err:
            raise HomeAssistantError(f"Wyze returned an error: {err.args}") from err
        except ClientConnectionError as err:
            raise HomeAssistantError(err) from err
        else:
            self._device.notify = False
            self.async_write_ha_state()

    @property
    def name(self):
        """Return name."""
        return f"{self._device.nickname} Notifications"

    @property
    def available(self):
        """Return availability."""
        return self._device.available

    @property
    def is_on(self):
        """Return true if notifications are on."""
        return self._device.notify

    @property
    def unique_id(self):
        """Return unique ID."""
        return f"{self._device.mac}-notification_switch"

    @callback
    def handle_camera_update(self, camera: Camera) -> None:
        """Update state from camera update."""
        self._device = camera
        self.async_write_ha_state()

    async def async_added_to_hass(self) -> None:
        """Subscribe to camera updates."""
        self.async_on_remove(
            async_dispatcher_connect(
                self.hass,
                f"{CAMERA_UPDATED}-{self._device.mac}",
                self.handle_camera_update,
            )
        )


class WyzeCameraMotionSwitch(SwitchEntity):
    """Representation of a Wyze Camera Motion Detection Switch."""

    _attr_should_poll = False

    def __init__(self, service: CameraService, device: Camera) -> None:
        """Initialize motion detection switch."""
        self._service = service
        self._device = device

    @property
    def device_info(self):
        """Return device info."""
        return {
            "identifiers": {(DOMAIN, self._device.mac)},
            "name": self._device.nickname,
            "manufacturer": "WyzeLabs",
            "model": self._device.product_model,
        }

    async def async_turn_on(self, **kwargs: Any) -> None:
        """Turn on motion detection."""
        try:
            await self._service.turn_on_motion_detection(self._device)
        except (AccessTokenError, ParameterError, UnknownApiError) as err:
            raise HomeAssistantError(f"Wyze returned an error: {err.args}") from err
        except ClientConnectionError as err:
            raise HomeAssistantError(err) from err
        else:
            self._device.motion = True
            self.async_write_ha_state()

    async def async_turn_off(self, **kwargs: Any) -> None:
        """Turn off motion detection."""
        try:
            await self._service.turn_off_motion_detection(self._device)
        except (AccessTokenError, ParameterError, UnknownApiError) as err:
            raise HomeAssistantError(f"Wyze returned an error: {err.args}") from err
        except ClientConnectionError as err:
            raise HomeAssistantError(err) from err
        else:
            self._device.motion = False
            self.async_write_ha_state()

    @property
    def name(self):
        """Return name."""
        return f"{self._device.nickname} Motion Detection"

    @property
    def available(self):
        """Return availability."""
        return self._device.available

    @property
    def is_on(self):
        """Return true if motion detection is on."""
        return self._device.motion

    @property
    def unique_id(self):
        """Return unique ID."""
        return f"{self._device.mac}-motion_switch"

    @callback
    def handle_camera_update(self, camera: Camera) -> None:
        """Update state from camera update."""
        self._device = camera
        self.async_write_ha_state()

    async def async_added_to_hass(self) -> None:
        """Subscribe to camera updates."""
        self.async_on_remove(
            async_dispatcher_connect(
                self.hass,
                f"{CAMERA_UPDATED}-{self._device.mac}",
                self.handle_camera_update,
            )
        )


class WyzeCameraMotionTrackingSwitch(SwitchEntity):
    """Enable/disable cloud-event motion tracking for a specific camera (HA-local toggle)."""

    _attr_should_poll = False
    _attr_entity_category = EntityCategory.CONFIG

    def __init__(
        self, hass: HomeAssistant, config_entry: ConfigEntry, camera: Camera
    ) -> None:
        """Initialize motion tracking toggle."""
        self.hass = hass
        self._entry = config_entry
        self._camera = camera
        self._device_id = _device_id_from_mac(camera.mac)
        self._attr_name = f"{camera.nickname} Motion Tracking (Cloud)"
        self._attr_unique_id = f"{self._device_id}-motion-tracking-cloud"

    @property
    def device_info(self):
        """Return device info."""
        return {
            "identifiers": {(DOMAIN, self._camera.mac)},
            "name": self._camera.nickname,
            "manufacturer": "WyzeLabs",
            "model": self._camera.product_model,
        }

    def _get_enabled_set(self) -> set[str]:
        """Get the set of enabled device IDs from runtime data."""
        return self.hass.data[DOMAIN][self._entry.entry_id].setdefault(
            "motion_tracking_enabled", set()
        )

    @property
    def is_on(self) -> bool:
        """Return true if motion tracking is enabled for this camera."""
        return self._device_id in self._get_enabled_set()

    @property
    def available(self) -> bool:
        """Available only if master camera motion toggle is enabled."""
        return bool(self._entry.options.get(CONF_ENABLE_CAMERA_MOTION, False))

    async def async_turn_on(self, **kwargs: Any) -> None:
        """Enable cloud motion tracking for this camera."""
        enabled = self._get_enabled_set()
        enabled.add(self._device_id)

        # Persist to config entry options
        opts = dict(self._entry.options)
        lst = [str(x).upper() for x in (opts.get(CONF_MOTION_TRACKING_DEVICES) or [])]
        if self._device_id not in lst:
            lst.append(self._device_id)
        opts[CONF_MOTION_TRACKING_DEVICES] = sorted(set(lst))
        self.hass.config_entries.async_update_entry(self._entry, options=opts)

        # Refresh coordinator
        coord = self.hass.data[DOMAIN][self._entry.entry_id].get(
            "motion_events_coordinator"
        )
        if coord:
            await coord.async_request_refresh()

        self.async_write_ha_state()

    async def async_turn_off(self, **kwargs: Any) -> None:
        """Disable cloud motion tracking for this camera."""
        enabled = self._get_enabled_set()
        enabled.discard(self._device_id)

        # Persist to config entry options
        opts = dict(self._entry.options)
        lst = [str(x).upper() for x in (opts.get(CONF_MOTION_TRACKING_DEVICES) or [])]
        lst = [x for x in lst if x != self._device_id]
        opts[CONF_MOTION_TRACKING_DEVICES] = sorted(set(lst))
        self.hass.config_entries.async_update_entry(self._entry, options=opts)

        # Refresh coordinator
        coord = self.hass.data[DOMAIN][self._entry.entry_id].get(
            "motion_events_coordinator"
        )
        if coord:
            await coord.async_request_refresh()

        self.async_write_ha_state()
