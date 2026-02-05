from homeassistant.components.switch import SwitchEntity
from homeassistant.helpers.restore_state import RestoreEntity

from .const import DOMAIN

class WyzeMotionTrackingSwitch(SwitchEntity, RestoreEntity):
    _attr_icon = "mdi:motion-sensor"

    def __init__(self, hass, camera_id: str, camera_name: str):
        self.hass = hass
        self._camera_id = camera_id
        self._attr_unique_id = f"{camera_id}_motion_tracking"
        self._attr_name = f"{camera_name} Motion Tracking"
        self._is_on = False

    async def async_added_to_hass(self):
        last = await self.async_get_last_state()
        if last:
            self._is_on = last.state == "on"

    @property
    def is_on(self) -> bool:
        return self._is_on

    async def async_turn_on(self, **kwargs):
        self._is_on = True
        self.async_write_ha_state()

        # notify integration
        self.hass.bus.async_fire(
            f"{DOMAIN}_motion_tracking_enabled",
            {"camera_id": self._camera_id},
        )

    async def async_turn_off(self, **kwargs):
        self._is_on = False
        self.async_write_ha_state()

        self.hass.bus.async_fire(
            f"{DOMAIN}_motion_tracking_disabled",
            {"camera_id": self._camera_id},
        )

