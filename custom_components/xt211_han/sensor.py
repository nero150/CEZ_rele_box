"""Sensor platform for XT211 HAN integration."""

from __future__ import annotations

from homeassistant.components.sensor import SensorDeviceClass, SensorEntity, SensorStateClass
from homeassistant.config_entries import ConfigEntry
from homeassistant.const import CONF_NAME, EntityCategory, UnitOfEnergy, UnitOfPower
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers.device_registry import DeviceInfo
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.helpers.update_coordinator import CoordinatorEntity

from .const import (
    CONF_HAS_FVE,
    CONF_PHASES,
    CONF_RELAY_COUNT,
    CONF_TARIFFS,
    DOMAIN,
    PHASES_3,
    RELAYS_4,
    TARIFFS_2,
)
from .coordinator import XT211Coordinator
from .dlms_parser import OBIS_DESCRIPTIONS

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

TEXT_OBIS = {
    "0-0:42.0.0.255",
    "0-0:96.1.0.255",
    "0-0:96.1.1.255",
    "0-0:96.14.0.255",
    "0-0:96.13.0.255",
}

BINARY_OBIS = {
    "0-0:96.3.10.255",
    "0-1:96.3.10.255",
    "0-2:96.3.10.255",
    "0-3:96.3.10.255",
    "0-4:96.3.10.255",
    "0-5:96.3.10.255",
    "0-6:96.3.10.255",
}


def _device_info(entry: ConfigEntry) -> DeviceInfo:
    return DeviceInfo(
        identifiers={(DOMAIN, entry.entry_id)},
        name=entry.data.get(CONF_NAME, "XT211 HAN"),
        manufacturer="Sagemcom",
        model="XT211 AMM",
    )


def build_enabled_obis(entry: ConfigEntry) -> set[str]:
    phases = entry.data.get(CONF_PHASES, PHASES_3)
    has_fve = entry.data.get(CONF_HAS_FVE, True)
    tariffs = int(entry.data.get(CONF_TARIFFS, TARIFFS_2))
    relay_count = int(entry.data.get(CONF_RELAY_COUNT, RELAYS_4))

    enabled_obis: set[str] = {
        "0-0:42.0.0.255",
        "0-0:96.1.0.255",
        "0-0:96.1.1.255",
        "0-0:96.14.0.255",
        "0-0:96.13.0.255",
        "0-0:96.3.10.255",
        "0-0:17.0.0.255",
        "1-0:1.7.0.255",
        "1-0:1.8.0.255",
    }

    relay_obis = {
        1: "0-1:96.3.10.255",
        2: "0-2:96.3.10.255",
        3: "0-3:96.3.10.255",
        4: "0-4:96.3.10.255",
        5: "0-5:96.3.10.255",
        6: "0-6:96.3.10.255",
    }
    for idx in range(1, relay_count + 1):
        enabled_obis.add(relay_obis[idx])

    if phases == PHASES_3:
        enabled_obis.update({"1-0:21.7.0.255", "1-0:41.7.0.255", "1-0:61.7.0.255"})

    if has_fve:
        enabled_obis.add("1-0:2.7.0.255")
        enabled_obis.add("1-0:2.8.0.255")
        if phases == PHASES_3:
            enabled_obis.update({"1-0:22.7.0.255", "1-0:42.7.0.255", "1-0:62.7.0.255"})

    for tariff in range(1, tariffs + 1):
        enabled_obis.add(f"1-0:1.8.{tariff}.255")

    return enabled_obis


async def async_setup_entry(
    hass: HomeAssistant,
    entry: ConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    coordinator: XT211Coordinator = hass.data[DOMAIN][entry.entry_id]
    enabled_obis = build_enabled_obis(entry)

    entities = [
        XT211SensorEntity(coordinator, entry, obis, meta)
        for obis, meta in OBIS_DESCRIPTIONS.items()
        if obis in enabled_obis and obis not in BINARY_OBIS
    ]
    async_add_entities(entities)

    registered_obis = {entity._obis for entity in entities}

    @callback
    def _on_update() -> None:
        if not coordinator.data:
            return
        new_entities = []
        for obis, data in coordinator.data.items():
            if obis in registered_obis or obis not in enabled_obis or obis in BINARY_OBIS:
                continue
            registered_obis.add(obis)
            new_entities.append(XT211SensorEntity(coordinator, entry, obis, data))
        if new_entities:
            async_add_entities(new_entities)

    coordinator.async_add_listener(_on_update)


class XT211SensorEntity(CoordinatorEntity[XT211Coordinator], SensorEntity):
    _attr_has_entity_name = True

    def __init__(self, coordinator: XT211Coordinator, entry: ConfigEntry, obis: str, meta: dict) -> None:
        super().__init__(coordinator)
        sensor_type = meta.get("class", "sensor")
        sensor_meta = SENSOR_META.get(sensor_type, SENSOR_META["sensor"])
        self._entry = entry
        self._obis = obis
        self._wh_to_kwh = sensor_type == "energy"
        self._text = obis in TEXT_OBIS
        self._attr_unique_id = f"{entry.entry_id}_{obis}"
        self._attr_name = meta.get("name", obis)
        self._attr_device_class = None if self._text else sensor_meta["device_class"]
        self._attr_state_class = None if self._text else sensor_meta["state_class"]
        self._attr_native_unit_of_measurement = None if self._text else (sensor_meta["unit"] or meta.get("unit"))
        if self._text:
            self._attr_entity_category = EntityCategory.DIAGNOSTIC

    @property
    def device_info(self) -> DeviceInfo:
        return _device_info(self._entry)

    @property
    def native_value(self):
        obj = (self.coordinator.data or {}).get(self._obis)
        if obj is None:
            return None
        value = obj.get("value")
        if self._text:
            return None if value is None else str(value)
        try:
            number = float(value)
        except (TypeError, ValueError):
            return None
        if self._wh_to_kwh:
            number /= 1000.0
        return round(number, 3)

    @property
    def available(self) -> bool:
        return self.coordinator.data is not None
