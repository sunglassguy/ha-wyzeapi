"""The Wyze Home Assistant Integration."""

from __future__ import annotations

import asyncio
import logging

from aiohttp import ClientError
from aiohttp.client_exceptions import ClientConnectorError

from homeassistant.config_entries import (
    ConfigEntry,
    ConfigEntryNotReady,
    SOURCE_IMPORT,
)
from homeassistant.const import CONF_PASSWORD, CONF_USERNAME
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import ConfigEntryAuthFailed
from homeassistant.helpers import device_registry as dr
from homeassistant.helpers.check_config import HomeAssistantConfig

from wyzeapy import Wyzeapy
from wyzeapy.exceptions import AccessTokenError
from wyzeapy.wyze_auth_lib import Token

from .const import (
    ACCESS_TOKEN,
    API_KEY,
    CONF_CLIENT,
    CONF_MOTION_TRACKING_DEVICES,
    DOMAIN,
    KEY_ID,
    REFRESH_TIME,
    REFRESH_TOKEN,
    WYZE_NOTIFICATION_TOGGLE,
)
from .http import is_transient_exception
from .token_manager import TokenManager
from .wyzeapy_patch import patch_wyzeapy_http

_LOGGER = logging.getLogger(__name__)

PLATFORMS = [
    "binary_sensor",  # Camera motion detection
    "switch",  # Camera controls + motion tracking toggles
    # "sensor",  # Camera battery sensors
]


def _norm_mac(value: str) -> str:
    """Normalize MAC-like identifiers for comparison."""
    return (value or "").replace(":", "").replace("-", "").lower()


async def async_setup(
    hass: HomeAssistant,
    config: HomeAssistantConfig,
    discovery_info=None,
):
    """Set up the WyzeApi domain."""
    if hass.config_entries.async_entries(DOMAIN):
        _LOGGER.debug(
            "Nothing to import from configuration.yaml, loading from Integrations"
        )
        return True

    domainconfig = config.get(DOMAIN)
    if not domainconfig:
        return True

    _LOGGER.debug(
        "Importing config information for %s from configuration.yml",
        domainconfig.get(CONF_USERNAME),
    )

    if hass.config_entries.async_entries(DOMAIN):
        for entry in hass.config_entries.async_entries(DOMAIN):
            entry_data = entry.as_dict().get("data")
            hass.config_entries.async_update_entry(entry, data=entry_data)
            break
    else:
        hass.async_create_task(
            hass.config_entries.flow.async_init(
                DOMAIN,
                context={"source": SOURCE_IMPORT},
                data={
                    CONF_USERNAME: domainconfig[CONF_USERNAME],
                    CONF_PASSWORD: domainconfig[CONF_PASSWORD],
                    ACCESS_TOKEN: domainconfig[ACCESS_TOKEN],
                    REFRESH_TOKEN: domainconfig[REFRESH_TOKEN],
                    REFRESH_TIME: domainconfig[REFRESH_TIME],
                    KEY_ID: domainconfig[KEY_ID],
                    API_KEY: domainconfig[API_KEY],
                },
            )
        )

    return True


async def async_setup_entry(hass: HomeAssistant, config_entry: ConfigEntry) -> bool:
    """Set up Wyze Home Assistant Integration from a config entry."""
    patch_wyzeapy_http(hass)
    hass.data.setdefault(DOMAIN, {})

    key_id = config_entry.data.get(KEY_ID)
    api_key = config_entry.data.get(API_KEY)

    client = await Wyzeapy.create()

    token = None
    if config_entry.data.get(ACCESS_TOKEN):
        token = Token(
            config_entry.data.get(ACCESS_TOKEN),
            config_entry.data.get(REFRESH_TOKEN),
            float(config_entry.data.get(REFRESH_TIME)),
        )

    token_manager = TokenManager(hass, config_entry)
    client.register_for_token_callback(token_manager.token_callback)

    try:
        await client.login(
            config_entry.data.get(CONF_USERNAME),
            config_entry.data.get(CONF_PASSWORD),
            key_id,
            api_key,
            token,
        )
    except AccessTokenError:
        _LOGGER.error(
            "Wyzeapi: Could not login. Please re-login through integration "
            "configuration"
        )
        raise ConfigEntryAuthFailed("Unable to login, please re-login.") from None
    except (ClientConnectorError, ClientError, asyncio.TimeoutError, TimeoutError, OSError) as err:
        raise ConfigEntryNotReady("Unable to login due to network issues.") from err

    # Store client + shared integration state.
    hass.data[DOMAIN][config_entry.entry_id] = {
        CONF_CLIENT: client,
        "key_id": key_id,
        "api_key": api_key,
        # Runtime set of enabled device_ids for motion tracking.
        "motion_tracking_enabled": set(),
        # Global coordinator placeholder, created by binary_sensor.py when needed.
        "motion_events_coordinator": None,
    }

    # Set defaults for options.
    options_dict = dict(config_entry.options)
    options_dict.setdefault(CONF_MOTION_TRACKING_DEVICES, [])
    hass.config_entries.async_update_entry(config_entry, options=options_dict)

    # Hydrate enabled set from persisted options list.
    enabled_list = options_dict.get(CONF_MOTION_TRACKING_DEVICES, []) or []
    enabled_set = {str(x).upper() for x in enabled_list if str(x).strip()}
    hass.data[DOMAIN][config_entry.entry_id]["motion_tracking_enabled"] = enabled_set

    # Load platforms before best-effort cleanup so transient Wyze cloud failures do
    # not block entity setup.
    await hass.config_entries.async_forward_entry_setups(config_entry, PLATFORMS)

    try:
        mac_addresses = await client.unique_device_ids
        normalized_macs = {_norm_mac(m) for m in mac_addresses}
        normalized_macs.add(_norm_mac(WYZE_NOTIFICATION_TOGGLE))

        hms_service = await client.hms_service
        hms_id = hms_service.hms_id
        if hms_id:
            normalized_macs.add(_norm_mac(hms_id))
    except Exception as err:
        if is_transient_exception(err):
            _LOGGER.warning(
                "Skipping Wyze stale-device cleanup because Wyze cloud is "
                "temporarily unavailable: %s",
                err,
            )
            return True
        raise

    device_registry = dr.async_get(hass)
    for device in dr.async_entries_for_config_entry(
        device_registry,
        config_entry.entry_id,
    ):
        for identifier in device.identifiers:
            domain, mac = identifier
            if domain != DOMAIN:
                continue

            if _norm_mac(mac) not in normalized_macs:
                _LOGGER.warning(
                    "%s is not in the mac_addresses list, removing the entry",
                    mac,
                )
                device_registry.async_remove_device(device.id)
                break

    return True


async def options_update_listener(hass: HomeAssistant, config_entry: ConfigEntry):
    """Handle options update."""
    _LOGGER.debug("Updated options, reloading entry")
    await hass.config_entries.async_reload(config_entry.entry_id)


async def async_unload_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    """Unload a config entry."""
    unload_ok = await hass.config_entries.async_unload_platforms(entry, PLATFORMS)

    if unload_ok:
        hass.data.get(DOMAIN, {}).pop(entry.entry_id, None)

    return unload_ok
