"""Binary sensor platform for XT211 HAN integration."""

from __future__ import annotations

from homeassistant.components.binary_sensor import BinarySensorDeviceClass, BinarySensorEntity
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers.device_registry import DeviceInfo
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.helpers.update_coordinator import CoordinatorEntity

from .const import DOMAIN
from .coordinator import XT211Coordinator
from .sensor import BINARY_OBIS, build_enabled_obis, _device_info
from .dlms_parser import OBIS_DESCRIPTIONS


async def async_setup_entry(
    hass: HomeAssistant,
    entry: ConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    coordinator: XT211Coordinator = hass.data[DOMAIN][entry.entry_id]
    enabled_obis = build_enabled_obis(entry)

    entities = [
        XT211BinarySensorEntity(coordinator, entry, obis, meta)
        for obis, meta in OBIS_DESCRIPTIONS.items()
        if obis in enabled_obis and obis in BINARY_OBIS
    ]
    async_add_entities(entities)

    registered_obis = {entity._obis for entity in entities}

    @callback
    def _on_update() -> None:
        if not coordinator.data:
            return
        new_entities = []
        for obis, data in coordinator.data.items():
            if obis in registered_obis or obis not in enabled_obis or obis not in BINARY_OBIS:
                continue
            registered_obis.add(obis)
            new_entities.append(XT211BinarySensorEntity(coordinator, entry, obis, data))
        if new_entities:
            async_add_entities(new_entities)

    coordinator.async_add_listener(_on_update)


class XT211BinarySensorEntity(CoordinatorEntity[XT211Coordinator], BinarySensorEntity):
    _attr_has_entity_name = True
    _attr_device_class = BinarySensorDeviceClass.POWER

    def __init__(self, coordinator: XT211Coordinator, entry: ConfigEntry, obis: str, meta: dict) -> None:
        super().__init__(coordinator)
        self._entry = entry
        self._obis = obis
        self._attr_unique_id = f"{entry.entry_id}_{obis}"
        self._attr_name = meta.get("name", obis)

    @property
    def device_info(self) -> DeviceInfo:
        return _device_info(self._entry)

    @property
    def is_on(self) -> bool | None:
        obj = (self.coordinator.data or {}).get(self._obis)
        if obj is None:
            return None
        value = obj.get("value")
        if isinstance(value, bool):
            return value
        try:
            return int(value) != 0
        except (TypeError, ValueError):
            return None

    @property
    def available(self) -> bool:
        return self.coordinator.data is not None
