"""XT211 HAN integration for Home Assistant.

Reads DLMS/COSEM PUSH data from a Sagemcom XT211 smart meter via a
RS485-to-Ethernet adapter (e.g. PUSR USR-DR134) over TCP.
No ESP32 or dedicated hardware needed beyond the adapter.
"""

from __future__ import annotations

import logging

from homeassistant.config_entries import ConfigEntry
from homeassistant.const import CONF_HOST, CONF_PORT, CONF_NAME, Platform
from homeassistant.core import HomeAssistant

from .const import DOMAIN, DEFAULT_NAME
from .coordinator import XT211Coordinator

_LOGGER = logging.getLogger(__name__)

PLATFORMS = [Platform.SENSOR]


async def async_setup_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    """Set up XT211 HAN from a config entry."""
    hass.data.setdefault(DOMAIN, {})

    coordinator = XT211Coordinator(
        hass,
        host=entry.data[CONF_HOST],
        port=entry.data[CONF_PORT],
        name=entry.data.get(CONF_NAME, DEFAULT_NAME),
    )

    hass.data[DOMAIN][entry.entry_id] = coordinator

    # Start the background TCP listener
    await coordinator.async_setup()

    # Set up sensor platform
    await hass.config_entries.async_forward_entry_setups(entry, PLATFORMS)

    _LOGGER.info(
        "XT211 HAN integration started for %s:%d",
        entry.data[CONF_HOST],
        entry.data[CONF_PORT],
    )
    return True


async def async_unload_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    """Unload a config entry."""
    coordinator: XT211Coordinator = hass.data[DOMAIN].get(entry.entry_id)
    if coordinator:
        await coordinator.async_shutdown()

    unload_ok = await hass.config_entries.async_unload_platforms(entry, PLATFORMS)

    if unload_ok:
        hass.data[DOMAIN].pop(entry.entry_id, None)

    return unload_ok
