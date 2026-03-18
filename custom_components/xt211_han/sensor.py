"""Sensor platform for XT211 HAN integration.

Registers three types of entities:
  - Numeric sensors  (power, energy)
  - Text sensors     (serial number, tariff, limiter)
  - Binary sensors   (disconnector, relays)
"""

from __future__ import annotations

import logging
from typing import Any

from homeassistant.components.binary_sensor import (
    BinarySensorDeviceClass,
    BinarySensorEntity,
)
from homeassistant.components.sensor import (
    SensorDeviceClass,
    SensorEntity,
    SensorStateClass,
)
from homeassistant.config_entries import ConfigEntry
from homeassistant.const import (
    CONF_NAME,
    EntityCategory,
    UnitOfEnergy,
    UnitOfPower,
)
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers.device_registry import DeviceInfo
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.helpers.update_coordinator import CoordinatorEntity

from .const import (
    DOMAIN,
    CONF_PHASES,
    CONF_HAS_FVE,
    CONF_TARIFFS,
    CONF_RELAY_COUNT,
    PHASES_3,
    TARIFFS_2,
    RELAYS_4,
)
from .coordinator import XT211Coordinator
from .dlms_parser import OBIS_DESCRIPTIONS

_LOGGER = logging.getLogger(__name__)

# Map OBIS "class" → HA SensorDeviceClass + StateClass + unit
SENSOR_META: dict[str, dict] = {
    "power": {
        "device_class": SensorDeviceClass.POWER,
        "state_class": SensorStateClass.MEASUREMENT,
        "unit": UnitOfPower.WATT,
    },
    "energy": {
        "device_class": SensorDeviceClass.ENERGY,
        "state_class": SensorStateClass.TOTAL_INCREASING,
        "unit": UnitOfEnergy.KILO_WATT_HOUR,
    },
    "sensor": {
        "device_class": None,
        "state_class": SensorStateClass.MEASUREMENT,
        "unit": None,
    },
}

# OBIS codes that send text values (not numeric)
TEXT_OBIS = {
    "0-0:42.0.0.255",   # COSEM logical device name
    "0-0:96.1.0.255",   # Serial number
    "0-0:96.14.0.255",  # Current tariff
    "0-0:96.13.0.255",  # Consumer message
}

# OBIS codes that are binary (on/off)
BINARY_OBIS = {
    "0-0:96.3.10.255",  # Disconnector
    "0-1:96.3.10.255",  # Relay R1
    "0-2:96.3.10.255",  # Relay R2
    "0-3:96.3.10.255",  # Relay R3
    "0-4:96.3.10.255",  # Relay R4
    "0-5:96.3.10.255",  # Relay R5
    "0-6:96.3.10.255",  # Relay R6
}


def _device_info(entry: ConfigEntry) -> DeviceInfo:
    return DeviceInfo(
        identifiers={(DOMAIN, entry.entry_id)},
        name=entry.data.get(CONF_NAME, "XT211 HAN"),
        manufacturer="Sagemcom",
        model="XT211 AMM",
    )


async def async_setup_entry(
    hass: HomeAssistant,
    entry: ConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    """Set up all XT211 HAN entities from a config entry, filtered by meter config."""
    coordinator: XT211Coordinator = hass.data[DOMAIN][entry.entry_id]

    phases       = entry.data.get(CONF_PHASES, PHASES_3)
    has_fve      = entry.data.get(CONF_HAS_FVE, True)
    tariffs      = int(entry.data.get(CONF_TARIFFS, TARIFFS_2))
    relay_count  = int(entry.data.get(CONF_RELAY_COUNT, RELAYS_4))

    # Build set of OBIS codes to include based on user config
    enabled_obis: set[str] = set()

    # Always include: device name, serial, tariff, consumer message, disconnector, limiter
    enabled_obis.update({
        "0-0:42.0.0.255",
        "0-0:96.1.0.255",
        "0-0:96.14.0.255",
        "0-0:96.13.0.255",
        "0-0:96.3.10.255",
        "0-0:17.0.0.255",
    })

    # Relays – according to relay_count
    relay_obis = {
        1: "0-1:96.3.10.255",
        2: "0-2:96.3.10.255",
        3: "0-3:96.3.10.255",
        4: "0-4:96.3.10.255",
        5: "0-5:96.3.10.255",
        6: "0-6:96.3.10.255",
    }
    for i in range(1, relay_count + 1):
        enabled_obis.add(relay_obis[i])

    # Instant power import – total always included
    enabled_obis.add("1-0:1.7.0.255")
    if phases == PHASES_3:
        enabled_obis.update({"1-0:21.7.0.255", "1-0:41.7.0.255", "1-0:61.7.0.255"})

    # Instant power export – only with FVE
    if has_fve:
        enabled_obis.add("1-0:2.7.0.255")
        if phases == PHASES_3:
            enabled_obis.update({"1-0:22.7.0.255", "1-0:42.7.0.255", "1-0:62.7.0.255"})

    # Cumulative energy import – total + tariffs
    enabled_obis.add("1-0:1.8.0.255")
    for t in range(1, tariffs + 1):
        enabled_obis.add(f"1-0:1.8.{t}.255")

    # Cumulative energy export – only with FVE
    if has_fve:
        enabled_obis.add("1-0:2.8.0.255")

    _LOGGER.debug(
        "XT211 config: phases=%s fve=%s tariffs=%d relays=%d → %d entities",
        phases, has_fve, tariffs, relay_count, len(enabled_obis),
    )

    entities: list = []
    registered_obis: set[str] = set()

    for obis, meta in OBIS_DESCRIPTIONS.items():
        if obis not in enabled_obis:
            continue
        registered_obis.add(obis)
        if obis in BINARY_OBIS:
            entities.append(XT211BinarySensorEntity(coordinator, entry, obis, meta))
        elif obis in TEXT_OBIS:
            entities.append(XT211TextSensorEntity(coordinator, entry, obis, meta))
        else:
            entities.append(XT211SensorEntity(coordinator, entry, obis, meta))

    async_add_entities(entities)

    # Dynamically register any unknown OBIS codes that arrive at runtime
    @callback
    def _on_update() -> None:
        if not coordinator.data:
            return
        new: list = []
        for obis, data in coordinator.data.items():
            if obis in registered_obis or obis not in enabled_obis:
                continue
            registered_obis.add(obis)
            _LOGGER.info("XT211: discovered new OBIS code %s – adding entity", obis)
            if obis in BINARY_OBIS:
                new.append(XT211BinarySensorEntity(coordinator, entry, obis, data))
            elif obis in TEXT_OBIS:
                new.append(XT211TextSensorEntity(coordinator, entry, obis, data))
            else:
                new.append(XT211SensorEntity(coordinator, entry, obis, data))
        if new:
            async_add_entities(new)

    coordinator.async_add_listener(_on_update)


# ---------------------------------------------------------------------------
# Numeric sensor
# ---------------------------------------------------------------------------

class XT211SensorEntity(CoordinatorEntity[XT211Coordinator], SensorEntity):
    """Numeric sensor (power / energy / generic)."""

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
        self._entry = entry

        sensor_type = meta.get("class", "sensor")
        sm = SENSOR_META.get(sensor_type, SENSOR_META["sensor"])

        self._attr_unique_id = f"{entry.entry_id}_{obis}"
        self._attr_name = meta.get("name", obis)
        self._attr_device_class = sm["device_class"]
        self._attr_state_class = sm["state_class"]
        self._attr_native_unit_of_measurement = sm["unit"] or meta.get("unit")
        self._wh_to_kwh = (sensor_type == "energy")

    @property
    def device_info(self) -> DeviceInfo:
        return _device_info(self._entry)

    @property
    def native_value(self) -> float | None:
        if not self.coordinator.data:
            return None
        obj = self.coordinator.data.get(self._obis)
        if obj is None:
            return None
        raw = obj.get("value")
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


# ---------------------------------------------------------------------------
# Text sensor
# ---------------------------------------------------------------------------

class XT211TextSensorEntity(CoordinatorEntity[XT211Coordinator], SensorEntity):
    """Text sensor (serial number, tariff)."""

    _attr_has_entity_name = True
    _attr_entity_category = EntityCategory.DIAGNOSTIC

    def __init__(
        self,
        coordinator: XT211Coordinator,
        entry: ConfigEntry,
        obis: str,
        meta: dict,
    ) -> None:
        super().__init__(coordinator)
        self._obis = obis
        self._entry = entry
        self._attr_unique_id = f"{entry.entry_id}_{obis}"
        self._attr_name = meta.get("name", obis)
        self._attr_device_class = None
        self._attr_state_class = None
        self._attr_native_unit_of_measurement = None

    @property
    def device_info(self) -> DeviceInfo:
        return _device_info(self._entry)

    @property
    def native_value(self) -> str | None:
        if not self.coordinator.data:
            return None
        obj = self.coordinator.data.get(self._obis)
        if obj is None:
            return None
        val = obj.get("value")
        return str(val) if val is not None else None

    @property
    def available(self) -> bool:
        return self.coordinator.connected and self.coordinator.data is not None


# ---------------------------------------------------------------------------
# Binary sensor
# ---------------------------------------------------------------------------

class XT211BinarySensorEntity(CoordinatorEntity[XT211Coordinator], BinarySensorEntity):
    """Binary sensor (disconnector / relay status)."""

    _attr_has_entity_name = True
    _attr_device_class = BinarySensorDeviceClass.PLUG

    def __init__(
        self,
        coordinator: XT211Coordinator,
        entry: ConfigEntry,
        obis: str,
        meta: dict,
    ) -> None:
        super().__init__(coordinator)
        self._obis = obis
        self._entry = entry
        self._attr_unique_id = f"{entry.entry_id}_{obis}"
        self._attr_name = meta.get("name", obis)

    @property
    def device_info(self) -> DeviceInfo:
        return _device_info(self._entry)

    @property
    def is_on(self) -> bool | None:
        if not self.coordinator.data:
            return None
        obj = self.coordinator.data.get(self._obis)
        if obj is None:
            return None
        val = obj.get("value")
        if isinstance(val, bool):
            return val
        try:
            return int(val) != 0
        except (TypeError, ValueError):
            return None

    @property
    def available(self) -> bool:
        return self.coordinator.connected and self.coordinator.data is not None
