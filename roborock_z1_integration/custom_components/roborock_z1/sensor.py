"""Sensor platform for the Roborock Z1."""
from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass

from homeassistant.components.sensor import (
    RestoreSensor,
    SensorDeviceClass,
    SensorEntityDescription,
    SensorStateClass,
)
from homeassistant.config_entries import ConfigEntry
from homeassistant.const import EntityCategory, PERCENTAGE
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers.device_registry import DeviceInfo
from homeassistant.helpers.dispatcher import async_dispatcher_connect
from homeassistant.helpers.entity_platform import AddEntitiesCallback

from roborock.data.mower import MowerStatus

from . import RockMowZ1Device
from .const import DOMAIN


@dataclass(frozen=True, kw_only=True)
class Z1SensorDescription(SensorEntityDescription):
    value_fn: Callable[[MowerStatus], int | None]


SENSORS: tuple[Z1SensorDescription, ...] = (
    Z1SensorDescription(
        key="battery",
        device_class=SensorDeviceClass.BATTERY,
        state_class=SensorStateClass.MEASUREMENT,
        native_unit_of_measurement=PERCENTAGE,
        value_fn=lambda s: s.battery,
    ),
    Z1SensorDescription(
        key="mow_progress",
        translation_key="mow_progress",
        state_class=SensorStateClass.MEASUREMENT,
        native_unit_of_measurement=PERCENTAGE,
        value_fn=lambda s: s.mow_progress,
    ),
    Z1SensorDescription(
        key="blade_lifespan",
        translation_key="blade_lifespan",
        state_class=SensorStateClass.MEASUREMENT,
        native_unit_of_measurement=PERCENTAGE,
        entity_category=EntityCategory.DIAGNOSTIC,
        value_fn=lambda s: s.blade_lifespan,
    ),
    # Raw mow_state code as reported by the mower. Its recorded history is
    # the easiest way to map the Z1's undocumented state codes: run a mowing
    # session, then read the values off this sensor's history graph and
    # adjust MOW_STATE_TO_ACTIVITY in const.py.
    Z1SensorDescription(
        key="mow_state",
        translation_key="mow_state_raw",
        entity_category=EntityCategory.DIAGNOSTIC,
        value_fn=lambda s: s.mow_state,
    ),
)


async def async_setup_entry(
    hass: HomeAssistant,
    entry: ConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    devices: list[RockMowZ1Device] = hass.data[DOMAIN][entry.entry_id]["devices"]
    async_add_entities(
        RoborockZ1Sensor(dev, desc) for dev in devices for desc in SENSORS
    )


class RoborockZ1Sensor(RestoreSensor):
    """A sensor backed by a field of the mower status; restores its last
    value across restarts since the mower only pushes data sporadically."""

    _attr_has_entity_name = True
    _attr_should_poll = False
    entity_description: Z1SensorDescription

    def __init__(self, device: RockMowZ1Device, description: Z1SensorDescription) -> None:
        self._device = device
        self.entity_description = description
        self._attr_unique_id = f"{device.duid}_{description.key}"
        self._attr_device_info = DeviceInfo(identifiers={(DOMAIN, device.duid)})

    async def async_added_to_hass(self) -> None:
        await super().async_added_to_hass()
        last = await self.async_get_last_sensor_data()
        if (
            last is not None
            and last.native_value is not None
            and self.entity_description.value_fn(self._device.status) is None
        ):
            # seed the in-memory status with the pre-restart value
            setattr(
                self._device.status,
                self.entity_description.key,
                last.native_value,
            )
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
        # Stay available while the cloud channel is up; a value that hasn't
        # arrived yet shows as "unknown" rather than hiding the entity.
        return self._device.available

    @property
    def native_value(self) -> int | None:
        return self.entity_description.value_fn(self._device.status)
