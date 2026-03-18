"""Sensor platform for XT211 HAN integration."""

from __future__ import annotations

import logging
from typing import Any

from homeassistant.components.sensor import (
    SensorDeviceClass,
    SensorEntity,
    SensorStateClass,
)
from homeassistant.config_entries import ConfigEntry
from homeassistant.const import (
    CONF_NAME,
    UnitOfEnergy,
    UnitOfPower,
)
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers.device_registry import DeviceInfo
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.helpers.update_coordinator import CoordinatorEntity

from .const import DOMAIN
from .coordinator import XT211Coordinator
from .dlms_parser import OBIS_DESCRIPTIONS

_LOGGER = logging.getLogger(__name__)

# Map OBIS "class" strings → HA SensorDeviceClass + StateClass + unit
SENSOR_META: dict[str, dict] = {
    "power": {
        "device_class": SensorDeviceClass.POWER,
        "state_class": SensorStateClass.MEASUREMENT,
        "unit": UnitOfPower.WATT,
    },
    "energy": {
        "device_class": SensorDeviceClass.ENERGY,
        "state_class": SensorStateClass.TOTAL_INCREASING,
        "unit": UnitOfEnergy.WATT_HOUR,
    },
    "sensor": {
        "device_class": None,
        "state_class": SensorStateClass.MEASUREMENT,
        "unit": None,
    },
}

# OBIS codes that are NOT numeric sensors (text / binary) – handled separately
NON_SENSOR_CLASSES = {"text", "binary"}


async def async_setup_entry(
    hass: HomeAssistant,
    entry: ConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    """Set up XT211 HAN sensors from a config entry."""
    coordinator: XT211Coordinator = hass.data[DOMAIN][entry.entry_id]

    # We create entities for all known OBIS codes upfront.
    # Unknown codes that arrive later will be added dynamically.
    entities: list[XT211SensorEntity] = []
    registered_obis: set[str] = set()

    for obis, meta in OBIS_DESCRIPTIONS.items():
        if meta.get("class") in NON_SENSOR_CLASSES:
            continue
        entities.append(
            XT211SensorEntity(coordinator, entry, obis, meta)
        )
        registered_obis.add(obis)

    async_add_entities(entities)

    @callback
    def _handle_new_obis(obis: str, data: dict) -> None:
        """Dynamically add sensor for a previously unknown OBIS code."""
        if obis in registered_obis:
            return
        if data.get("class") in NON_SENSOR_CLASSES:
            return
        registered_obis.add(obis)
        async_add_entities([XT211SensorEntity(coordinator, entry, obis, data)])

    # Subscribe to coordinator updates to detect new OBIS codes
    @callback
    def _on_update() -> None:
        if coordinator.data:
            for obis, data in coordinator.data.items():
                _handle_new_obis(obis, data)

    coordinator.async_add_listener(_on_update)


class XT211SensorEntity(CoordinatorEntity[XT211Coordinator], SensorEntity):
    """A single numeric sensor entity backed by an OBIS code."""

    _attr_has_entity_name = True

    def __init__(
        self,
        coordinator: XT211Coordinator,
        entry: ConfigEntry,
        obis: str,
        meta: dict,
    ) -> None:
        super().__init__(coordinator)
        self._obis = obis
        self._meta = meta
        self._entry = entry

        sensor_type = meta.get("class", "sensor")
        sm = SENSOR_META.get(sensor_type, SENSOR_META["sensor"])

        self._attr_unique_id = f"{entry.entry_id}_{obis}"
        self._attr_name = meta.get("name", obis)
        self._attr_device_class = sm["device_class"]
        self._attr_state_class = sm["state_class"]
        self._attr_native_unit_of_measurement = sm["unit"] or meta.get("unit")

        # Energy sensors: convert Wh → kWh for HA Energy dashboard
        if sensor_type == "energy":
            self._attr_native_unit_of_measurement = UnitOfEnergy.KILO_WATT_HOUR
            self._wh_to_kwh = True
        else:
            self._wh_to_kwh = False

    @property
    def device_info(self) -> DeviceInfo:
        return DeviceInfo(
            identifiers={(DOMAIN, self._entry.entry_id)},
            name=self._entry.data.get(CONF_NAME, "XT211 HAN"),
            manufacturer="Sagemcom",
            model="XT211 AMM",
        )

    @property
    def native_value(self) -> float | None:
        if self.coordinator.data is None:
            return None
        obj = self.coordinator.data.get(self._obis)
        if obj is None:
            return None
        raw = obj.get("value")
        if raw is None:
            return None
        try:
            val = float(raw)
            if self._wh_to_kwh:
                val = val / 1000.0
            return round(val, 3)
        except (TypeError, ValueError):
            return None

    @property
    def available(self) -> bool:
        return self.coordinator.connected and self.coordinator.data is not None
