"""Lawn mower platform for the Roborock Z1."""
from __future__ import annotations

from homeassistant.components.lawn_mower import (
    LawnMowerActivity,
    LawnMowerEntity,
    LawnMowerEntityFeature,
)
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers.device_registry import DeviceInfo
from homeassistant.helpers.dispatcher import async_dispatcher_connect
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.helpers.restore_state import RestoreEntity

from . import RockMowZ1Device
from .const import DOMAIN, MOW_STATE_DESCRIPTIONS, MOW_STATE_TO_ACTIVITY

ACTIVITY_MAP = {
    "docked": LawnMowerActivity.DOCKED,
    "mowing": LawnMowerActivity.MOWING,
    "paused": LawnMowerActivity.PAUSED,
    "returning": LawnMowerActivity.RETURNING,
    "error": LawnMowerActivity.ERROR,
}


async def async_setup_entry(
    hass: HomeAssistant,
    entry: ConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    devices: list[RockMowZ1Device] = hass.data[DOMAIN][entry.entry_id]["devices"]
    async_add_entities(RoborockZ1Mower(dev) for dev in devices)


class RoborockZ1Mower(LawnMowerEntity, RestoreEntity):
    """The Z1 as a native lawn_mower entity."""

    _attr_has_entity_name = True
    _attr_name = None
    _attr_translation_key = "mower"
    _attr_should_poll = False
    _attr_supported_features = (
        LawnMowerEntityFeature.START_MOWING
        | LawnMowerEntityFeature.PAUSE
        | LawnMowerEntityFeature.DOCK
    )

    def __init__(self, device: RockMowZ1Device) -> None:
        self._device = device
        self._attr_unique_id = f"{device.duid}_mower"
        self._attr_device_info = DeviceInfo(
            identifiers={(DOMAIN, device.duid)},
            name=device.device.name,
            manufacturer="Roborock",
            model=device.product.model,
            sw_version=device.device.fv,
        )

    async def async_added_to_hass(self) -> None:
        await super().async_added_to_hass()
        last = await self.async_get_last_state()
        if last is not None and self._device.status.mow_state is None:
            raw = last.attributes.get("mow_state_raw")
            if raw is not None:
                self._device.status.mow_state = raw
        self.async_on_remove(
            async_dispatcher_connect(
                self.hass, self._device.signal, self._handle_update
            )
        )

    @callback
    def _handle_update(self) -> None:
        self.async_write_ha_state()

    @property
    def available(self) -> bool:
        return self._device.available

    @property
    def activity(self) -> LawnMowerActivity | None:
        status = self._device.status
        if status.error_code:
            return LawnMowerActivity.ERROR
        if status.mow_state is None:
            return None
        name = MOW_STATE_TO_ACTIVITY.get(status.mow_state)
        return ACTIVITY_MAP.get(name) if name else None

    @property
    def extra_state_attributes(self) -> dict:
        status = self._device.status
        return {
            k: v
            for k, v in {
                "error_code": status.error_code,
                "mow_state_raw": status.mow_state,
                "state_description": MOW_STATE_DESCRIPTIONS.get(status.mow_state),
                "mow_progress": status.mow_progress,
                "mow_height": status.mow_height,
                "blade_lifespan": status.blade_lifespan,
                "charge_state": status.charge_state,
                "dock_state": status.dock_state,
            }.items()
            if v is not None
        }

    async def async_start_mowing(self) -> None:
        if self.activity == LawnMowerActivity.PAUSED:
            await self._device.async_resume()
        else:
            await self._device.async_start_mowing()

    async def async_pause(self) -> None:
        await self._device.async_pause()

    async def async_dock(self) -> None:
        await self._device.async_dock()
