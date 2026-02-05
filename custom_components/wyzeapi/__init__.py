"""The Wyze Home Assistant Integration."""

from __future__ import annotations

import logging

from aiohttp.client_exceptions import ClientConnectorError
from homeassistant.config_entries import (
    ConfigEntry,
    ConfigEntryNotReady,
    SOURCE_IMPORT,
)
from homeassistant.const import CONF_USERNAME, CONF_PASSWORD
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import ConfigEntryAuthFailed
from homeassistant.helpers import device_registry as dr
from homeassistant.helpers.check_config import HomeAssistantConfig
from homeassistant.components import bluetooth
from wyzeapy import Wyzeapy
from wyzeapy.exceptions import AccessTokenError
from wyzeapy.wyze_auth_lib import Token

from .const import (
    DOMAIN,
    CONF_CLIENT,
    ACCESS_TOKEN,
    REFRESH_TOKEN,
    REFRESH_TIME,
    WYZE_NOTIFICATION_TOGGLE,
    BULB_LOCAL_CONTROL,
    DEFAULT_LOCAL_CONTROL,
    KEY_ID,
    API_KEY,
)

from .coordinator import WyzeLockBoltCoordinator
from .token_manager import TokenManager

_LOGGER = logging.getLogger(__name__)

# IMPORTANT: binary_sensor must be included
PLATFORMS = [
    "light",
    "switch",
    "lock",
    "climate",
    "alarm_control_panel",
    "sensor",
    "binary_sensor",
    "siren",
    "cover",
    "number",
    "button",
]  # Fixme: Re-add scene

# ---- Motion tracking (per-camera) shared state keys ----
DATA_MOTION_TRACKING_ENABLED = "motion_tracking_enabled"  # set[str] of device_ids
DATA_MOTION_TRACKING_DEFAULT = "motion_tracking_default"  # bool default if switch not created yet


def _norm_mac(value: str) -> str:
    """Normalize MAC-like identifiers for comparison."""
    return (value or "").replace(":", "").replace("-", "").lower()


async def async_setup(
    hass: HomeAssistant, config: HomeAssistantConfig, discovery_info=None
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

    hass.data.setdefault(DOMAIN, {})
    hass.data[DOMAIN].setdefault(config_entry.entry_id, {})

    # Per-entry shared state for motion tracking switches/coordinators/binary sensors.
    # We'll store camera "device_id"/MAC-without-colons (e.g. D03F274B131D) in this set.
    hass.data[DOMAIN][config_entry.entry_id].setdefault(DATA_MOTION_TRACKING_ENABLED, set())
    hass.data[DOMAIN][config_entry.entry_id].setdefault(DATA_MOTION_TRACKING_DEFAULT, False)

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
    except ClientConnectorError as err:
        raise ConfigEntryNotReady("Unable to login due to network issues.") from err
    except AccessTokenError:
        _LOGGER.error(
            "Wyzeapi: Could not login. Please re-login through integration configuration"
        )
        raise ConfigEntryAuthFailed("Unable to login, please re-login.") from None

    hass.data[DOMAIN][config_entry.entry_id].update(
        {
            CONF_CLIENT: client,
            "key_id": key_id,
            "api_key": api_key,
        }
    )

    await setup_coordinators(hass, config_entry, client)

    # IMPORTANT: preserve ALL options (do not wipe motion options)
    options_dict = dict(config_entry.options)
    options_dict.setdefault(BULB_LOCAL_CONTROL, DEFAULT_LOCAL_CONTROL)
    hass.config_entries.async_update_entry(config_entry, options=options_dict)

    # Load platforms
    await hass.config_entries.async_forward_entry_setups(config_entry, PLATFORMS)

    # ---- Device registry cleanup (normalized MACs) ----
    mac_addresses = await client.unique_device_ids
    normalized_macs = {_norm_mac(m) for m in mac_addresses}

    normalized_macs.add(_norm_mac(WYZE_NOTIFICATION_TOGGLE))

    hms_service = await client.hms_service
    hms_id = hms_service.hms_id
    if hms_id:
        normalized_macs.add(_norm_mac(hms_id))

    device_registry = dr.async_get(hass)
    for device in dr.async_entries_for_config_entry(device_registry, config_entry.entry_id):
        for identifier in device.identifiers:
            domain, mac = identifier
            if domain != DOMAIN:
                continue

            if _norm_mac(mac) not in normalized_macs:
                _LOGGER.warning(
                    "%s is not in the mac_addresses list, removing the entry", mac
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
    return await hass.config_entries.async_unload_platforms(entry, PLATFORMS)


async def setup_coordinators(hass: HomeAssistant, config_entry: ConfigEntry, client: Wyzeapy):
    """Set up coordinators for Wyze devices that require Bluetooth."""
    if bluetooth.async_scanner_count(hass, connectable=True) == 0:
        _LOGGER.info(
            "Bluetooth is not active or no scanners available. Skipping WyzeLockBoltCoordinator setup."
        )
        return

    lock_service = await client.lock_service
    for lock in await lock_service.get_locks():
        if lock.product_model == "YD_BT1":
            coordinators = hass.data[DOMAIN][config_entry.entry_id].setdefault("coordinators", {})
            coordinators[lock.mac] = WyzeLockBoltCoordinator(hass, lock_service, lock)
            await coordinators[lock.mac].update_lock_info()
