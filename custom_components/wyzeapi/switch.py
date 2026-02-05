#!/usr/bin/python3
"""Platform for switch integration."""

from __future__ import annotations

from collections.abc import Callable
from datetime import timedelta
import logging
from typing import Any

from aiohttp.client_exceptions import ClientConnectionError
from wyzeapy import BulbService, CameraService, SwitchService, Wyzeapy
from wyzeapy.exceptions import AccessTokenError, ParameterError, UnknownApiError
from wyzeapy.services.bulb_service import Bulb
from wyzeapy.services.camera_service import Camera
from wyzeapy.services.switch_service import Switch
from wyzeapy.types import Device, DeviceTypes, Event

from homeassistant.components.switch import SwitchEntity
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant, callback
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers import device_registry as dr
from homeassistant.helpers.dispatcher import async_dispatcher_connect, async_dispatcher_send
from homeassistant.helpers.entity import EntityCategory

from .const import (
    CAMERA_UPDATED,
    CONF_CLIENT,
    DOMAIN,
    LIGHT_UPDATED,
    WYZE_CAMERA_EVENT,
    WYZE_NOTIFICATION_TOGGLE,
    CONF_ENABLE_CAMERA_MOTION,
    CONF_MOTION_TRACKING_DEVICES,
)
from .token_manager import token_exception_handler

_LOGGER = logging.getLogger(__name__)
ATTRIBUTION = "Data provided by Wyze"
SCAN_INTERVAL = timedelta(seconds=30)

OUTDOOR_PLUGS = "WLPPO"
OUTDOOR_PLUG_INDIVUAL_OUTLETS = "WLPPO-SUB"

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


# noinspection DuplicatedCode
@token_exception_handler
async def async_setup_entry(
    hass: HomeAssistant,
    config_entry: ConfigEntry,
    async_add_entities: Callable[[list[Any], bool], None],
) -> None:
    """Set up switch entities."""

    _LOGGER.debug("Creating new WyzeApi switch component")
    client: Wyzeapy = hass.data[DOMAIN][config_entry.entry_id][CONF_CLIENT]

    switch_service = await client.switch_service
    wall_switch_service = await client.wall_switch_service
    camera_service = await client.camera_service
    bulb_service = await client.bulb_service

    switches: list[SwitchEntity] = []

    base_switches = await switch_service.get_switches()
    switches.extend(
        WyzeSwitch(switch_service, sw)
        for sw in base_switches
        if sw.product_model not in OUTDOOR_PLUGS or sw.product_model.endswith("-SUB")
    )

    switches.extend(
        WyzeSwitch(wall_switch_service, sw)
        for sw in await wall_switch_service.get_switches()
    )

    camera_devices = await camera_service.get_cameras()
    for cam in camera_devices:
        # Notification toggle switch (Wyze-side)
        if cam.product_model not in NOTIFICATION_SWITCH_UNSUPPORTED:
            switches.append(WyzeCameraNotificationSwitch(camera_service, cam))

        # IoT Power switch (Wyze-side)
        if cam.product_model not in POWER_SWITCH_UNSUPPORTED:
            switches.append(WyzeSwitch(camera_service, cam))

        # Motion detection toggle (Wyze-side)
        if cam.product_model not in MOTION_SWITCH_UNSUPPORTED:
            switches.append(WyzeCameraMotionSwitch(camera_service, cam))

        # HA-only cloud-events tracking toggle
        switches.append(WyzeCameraMotionTrackingSwitch(hass, config_entry, cam))

    # Global notifications toggle (Wyze-side)
    switches.append(WyzeNotifications(client))

    bulb_devices = await bulb_service.get_bulbs()
    switches.extend(
        WzyeLightstripSwitch(bulb_service, bulb)
        for bulb in bulb_devices
        if bulb.type is DeviceTypes.LIGHTSTRIP
    )

    async_add_entities(switches, True)


class WyzeNotifications(SwitchEntity):
    """Class for global notification switch."""

    _attr_should_poll = False
    _attr_name = "Wyze Notifications"

    def __init__(self, client: Wyzeapy) -> None:
        self._client = client
        self._is_on = False
        self._uid = WYZE_NOTIFICATION_TOGGLE
        self._just_updated = False

    @property
    def is_on(self) -> bool:
        return self._is_on

    @property
    def device_info(self):
        return {
            "identifiers": {(DOMAIN, self._uid)},
            "name": "Wyze Notifications",
            "manufacturer": "WyzeLabs",
            "model": "WyzeNotificationToggle",
        }

    @property
    def available(self) -> bool:
        # Do NOT depend on ConfigEntry here; this entity is constructed without it.
        return True

    @property
    def unique_id(self):
        return self._uid

    async def async_turn_on(self, **kwargs: Any) -> None:
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
        if not self._just_updated:
            self._is_on = await self._client.notifications_are_on
        else:
            self._just_updated = False


class WyzeSwitch(SwitchEntity):
    """Representation of a Wyze Switch."""

    _just_updated = False
    _old_event_ts: int = 0  # preload with 0 so that we know when it's been updated
    _attr_should_poll = False

    def __init__(self, service: CameraService | SwitchService, device: Device) -> None:
        self._device = device
        self._service = service

        if type(self._device) is Camera:
            self._device = Camera(self._device.raw_dict)
        elif type(self._device) is Switch:
            self._device = Switch(self._device.raw_dict)

    @property
    def device_info(self):
        if self._device.product_model == OUTDOOR_PLUG_INDIVUAL_OUTLETS:
            mac = self._device.mac.split("-")[0]
            return {
                "identifiers": {(DOMAIN, mac)},
                "connections": {(dr.CONNECTION_NETWORK_MAC, mac)},
                "name": f"Outdoor Plug {mac}",
                "manufacturer": "WyzeLabs",
                "model": self._device.product_model,
            }
        return {
            "identifiers": {(DOMAIN, self._device.mac)},
            "connections": {(dr.CONNECTION_NETWORK_MAC, self._device.mac)},
            "name": self._device.nickname,
            "manufacturer": "WyzeLabs",
            "model": self._device.product_model,
        }

    @token_exception_handler
    async def async_turn_on(self, **kwargs: Any) -> None:
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
        if type(self._device) is Camera:
            return f"{self._device.nickname} Power"
        return self._device.nickname

    @property
    def available(self):
        return self._device.available

    @property
    def is_on(self):
        return self._device.on

    @property
    def unique_id(self):
        return f"{self._device.mac}-switch"

    @property
    def extra_state_attributes(self):
        dev_info: dict[str, Any] = {}

        if self._device.device_params.get("electricity"):
            dev_info["Battery"] = str(self._device.device_params.get("electricity") + "%")
        if self._device.device_params.get("ip"):
            dev_info["IP"] = str(self._device.device_params.get("ip"))
        if self._device.device_params.get("rssi"):
            dev_info["RSSI"] = str(self._device.device_params.get("rssi"))
        if self._device.device_params.get("ssid"):
            dev_info["SSID"] = str(self._device.device_params.get("ssid"))

        return dev_info

    @token_exception_handler
    async def async_update(self):
        if not self._just_updated:
            self._device = await self._service.update(self._device)
        else:
            self._just_updated = False

    @callback
    def async_update_callback(self, switch: Switch):
        self._device = switch
        async_dispatcher_send(self.hass, f"{CAMERA_UPDATED}-{switch.mac}", switch)
        self.async_schedule_update_ha_state()

        if isinstance(switch, Camera):
            if (
                self._old_event_ts > 0
                and self._old_event_ts != switch.last_event_ts
                and switch.last_event is not None
            ):
                event: Event = switch.last_event
                _screenshot_url = None
                _video_url = None
                _ai_tag_list: list[Any] = []

                for resource in event.file_list:
                    _ai_tag_list = _ai_tag_list + resource["ai_tag_list"]
                    if resource["type"] == 1:
                        _screenshot_url = resource["url"]
                    elif resource["type"] == 2:
                        _video_url = resource["url"]

                _LOGGER.debug("Camera: %s has a new event", switch.nickname)
                self.hass.bus.fire(
                    WYZE_CAMERA_EVENT,
                    {
                        "device_name": switch.nickname,
                        "device_mac": switch.mac,
                        "ai_tag_list": _ai_tag_list,
                        "tag_list": event.tag_list,
                        "event_screenshot": _screenshot_url,
                        "event_video": _video_url,
                    },
                )
            self._old_event_ts = switch.last_event_ts

    async def async_added_to_hass(self) -> None:
        self._device.callback_function = self.async_update_callback
        self._service.register_updater(self._device, 30)
        await self._service.start_update_manager()
        return await super().async_added_to_hass()

    async def async_will_remove_from_hass(self) -> None:
        self._service.unregister_updater(self._device)


class WyzeCameraNotificationSwitch(SwitchEntity):
    """Representation of a Wyze Camera Notification Switch."""

    def __init__(self, service: CameraService, device: Camera) -> None:
        self._service = service
        self._device = device

    @property
    def device_info(self):
        return {
            "identifiers": {(DOMAIN, self._device.mac)},
            "name": self._device.nickname,
            "manufacturer": "WyzeLabs",
            "model": self._device.product_model,
        }

    @property
    def should_poll(self) -> bool:
        return False

    async def async_turn_on(self, **kwargs: Any) -> None:
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
        return f"{self._device.nickname} Notifications"

    @property
    def available(self):
        return self._device.available

    @property
    def is_on(self):
        return self._device.notify

    @property
    def unique_id(self):
        return f"{self._device.mac}-notification_switch"

    @callback
    def handle_camera_update(self, camera: Camera) -> None:
        self._device = camera
        self.async_write_ha_state()

    async def async_added_to_hass(self) -> None:
        self.async_on_remove(
            async_dispatcher_connect(
                self.hass,
                f"{CAMERA_UPDATED}-{self._device.mac}",
                self.handle_camera_update,
            )
        )


class WyzeCameraMotionSwitch(SwitchEntity):
    """Representation of a Wyze Camera Motion Detection Switch."""

    def __init__(self, service: CameraService, device: Camera) -> None:
        self._service = service
        self._device = device

    @property
    def device_info(self):
        return {
            "identifiers": {(DOMAIN, self._device.mac)},
            "name": self._device.nickname,
            "manufacturer": "WyzeLabs",
            "model": self._device.product_model,
        }

    @property
    def should_poll(self) -> bool:
        return False

    async def async_turn_on(self, **kwargs: Any) -> None:
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
        return f"{self._device.nickname} Motion Detection"

    @property
    def available(self):
        return self._device.available

    @property
    def is_on(self):
        return self._device.motion

    @property
    def unique_id(self):
        return f"{self._device.mac}-motion_switch"

    @callback
    def handle_camera_update(self, camera: Camera) -> None:
        self._device = camera
        self.async_write_ha_state()

    async def async_added_to_hass(self) -> None:
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

    def __init__(self, hass: HomeAssistant, config_entry: ConfigEntry, camera: Camera) -> None:
        self.hass = hass
        self._entry = config_entry
        self._camera = camera
        self._device_id = _device_id_from_mac(camera.mac)

        self._attr_name = f"{camera.nickname} Motion Tracking (Cloud)"
        self._attr_unique_id = f"{self._device_id}-motion-tracking-cloud"

    @property
    def device_info(self):
        return {
            "identifiers": {(DOMAIN, self._camera.mac)},
            "name": self._camera.nickname,
            "manufacturer": "WyzeLabs",
            "model": self._camera.product_model,
        }

    def _get_enabled_set(self) -> set[str]:
        return self.hass.data[DOMAIN][self._entry.entry_id].setdefault(
            "motion_tracking_enabled", set()
        )

    @property
    def is_on(self) -> bool:
        return self._device_id in self._get_enabled_set()

    @property
    def available(self) -> bool:
        # “Master” toggle: if disabled, hide/unavailable the per-camera toggles.
        return bool(self._entry.options.get(CONF_ENABLE_CAMERA_MOTION, False))

    async def async_turn_on(self, **kwargs: Any) -> None:
        enabled = self._get_enabled_set()
        enabled.add(self._device_id)

        opts = dict(self._entry.options)
        lst = [str(x).upper() for x in (opts.get(CONF_MOTION_TRACKING_DEVICES) or [])]
        if self._device_id not in lst:
            lst.append(self._device_id)
        opts[CONF_MOTION_TRACKING_DEVICES] = sorted(set(lst))
        self.hass.config_entries.async_update_entry(self._entry, options=opts)

        coord = self.hass.data[DOMAIN][self._entry.entry_id].get("motion_events_coordinator")
        if coord:
            await coord.async_request_refresh()

        self.async_write_ha_state()

    async def async_turn_off(self, **kwargs: Any) -> None:
        enabled = self._get_enabled_set()
        enabled.discard(self._device_id)

        opts = dict(self._entry.options)
        lst = [str(x).upper() for x in (opts.get(CONF_MOTION_TRACKING_DEVICES) or [])]
        lst = [x for x in lst if x != self._device_id]
        opts[CONF_MOTION_TRACKING_DEVICES] = sorted(set(lst))
        self.hass.config_entries.async_update_entry(self._entry, options=opts)

        coord = self.hass.data[DOMAIN][self._entry.entry_id].get("motion_events_coordinator")
        if coord:
            await coord.async_request_refresh()

        self.async_write_ha_state()


class WzyeLightstripSwitch(SwitchEntity):
    """Music Mode Switch for Wyze Light Strip."""

    def __init__(self, service: BulbService, device: Device) -> None:
        self._service = service
        self._device = Bulb(device.raw_dict)

    @property
    def device_info(self):
        return {
            "identifiers": {(DOMAIN, self._device.mac)},
            "name": self._device.nickname,
            "manufacturer": "WyzeLabs",
            "model": self._device.product_model,
        }

    @property
    def should_poll(self) -> bool:
        return False

    async def async_turn_on(self, **kwargs: Any) -> None:
        try:
            await self._service.music_mode_on(self._device)
        except (AccessTokenError, ParameterError, UnknownApiError) as err:
            raise HomeAssistantError(f"Wyze returned an error: {err.args}") from err
        except ClientConnectionError as err:
            raise HomeAssistantError(err) from err
        else:
            self._device.music_mode = True
            self.async_schedule_update_ha_state()

    async def async_turn_off(self, **kwargs: Any) -> None:
        try:
            await self._service.music_mode_off(self._device)
        except (AccessTokenError, ParameterError, UnknownApiError) as err:
            raise HomeAssistantError(f"Wyze returned an error: {err.args}") from err
        except ClientConnectionError as err:
            raise HomeAssistantError(err) from err
        else:
            self._device.music_mode = False
            self.async_schedule_update_ha_state()

    @property
    def name(self):
        return f"{self._device.nickname} Music Mode for Effects"

    @property
    def available(self):
        return self._device.available

    @property
    def is_on(self):
        return self._device.music_mode

    @property
    def unique_id(self):
        return f"{self._device.mac}-music_mode"

    @callback
    def handle_light_update(self, bulb: Bulb) -> None:
        self._device = bulb
        self.async_write_ha_state()

    async def async_added_to_hass(self) -> None:
        self.async_on_remove(
            async_dispatcher_connect(
                self.hass,
                f"{LIGHT_UPDATED}-{self._device.mac}",
                self.handle_light_update,
            )
        )
