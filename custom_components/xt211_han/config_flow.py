"""Config flow for XT211 HAN integration."""

from __future__ import annotations

import asyncio
import logging
from typing import Any

import voluptuous as vol

from homeassistant import config_entries
from homeassistant.const import CONF_HOST, CONF_PORT, CONF_NAME
from homeassistant.data_entry_flow import FlowResult

from .const import (
    DOMAIN,
    DEFAULT_PORT,
    DEFAULT_NAME,
    CONF_PHASES,
    CONF_HAS_FVE,
    CONF_TARIFFS,
    CONF_RELAY_COUNT,
    PHASES_1,
    PHASES_3,
    TARIFFS_1,
    TARIFFS_2,
    TARIFFS_4,
    RELAYS_0,
    RELAYS_4,
    RELAYS_6,
)

_LOGGER = logging.getLogger(__name__)

STEP_CONNECTION_SCHEMA = vol.Schema(
    {
        vol.Required(CONF_HOST): str,
        vol.Required(CONF_PORT, default=DEFAULT_PORT): int,
        vol.Optional(CONF_NAME, default=DEFAULT_NAME): str,
    }
)

STEP_METER_SCHEMA = vol.Schema(
    {
        vol.Required(CONF_PHASES, default=PHASES_3): vol.In(
            {PHASES_1: "Jednofázový", PHASES_3: "Třífázový"}
        ),
        vol.Required(CONF_HAS_FVE, default=False): bool,
        vol.Required(CONF_TARIFFS, default=TARIFFS_2): vol.In(
            {
                TARIFFS_1: "Jeden tarif (pouze T1)",
                TARIFFS_2: "Dva tarify (T1 + T2)",
                TARIFFS_4: "Čtyři tarify (T1 – T4)",
            }
        ),
        vol.Required(CONF_RELAY_COUNT, default=RELAYS_4): vol.In(
            {
                RELAYS_0: "Žádné relé",
                RELAYS_4: "WM-RelayBox (R1 – R4)",
                RELAYS_6: "WM-RelayBox rozšířený (R1 – R6)",
            }
        ),
    }
)


async def _test_connection(host: str, port: int) -> str | None:
    """Try to open a TCP connection. Returns error string or None on success."""
    try:
        reader, writer = await asyncio.wait_for(
            asyncio.open_connection(host, port),
            timeout=5,
        )
        writer.close()
        await writer.wait_closed()
        return None
    except asyncio.TimeoutError:
        return "cannot_connect"
    except OSError:
        return "cannot_connect"
    except Exception:
        return "unknown"


class XT211HANConfigFlow(config_entries.ConfigFlow, domain=DOMAIN):
    """Handle the config flow for XT211 HAN – two steps."""

    VERSION = 1

    def __init__(self) -> None:
        self._connection_data: dict[str, Any] = {}

    # ------------------------------------------------------------------
    # Step 1 – connection (host / port / name)
    # ------------------------------------------------------------------

    async def async_step_user(
        self, user_input: dict[str, Any] | None = None
    ) -> FlowResult:
        errors: dict[str, str] = {}

        if user_input is not None:
            host = user_input[CONF_HOST]
            port = user_input[CONF_PORT]

            await self.async_set_unique_id(f"{host}:{port}")
            self._abort_if_unique_id_configured()

            error = await _test_connection(host, port)
            if error:
                errors["base"] = error
            else:
                self._connection_data = user_input
                return await self.async_step_meter()

        return self.async_show_form(
            step_id="user",
            data_schema=STEP_CONNECTION_SCHEMA,
            errors=errors,
        )

    # ------------------------------------------------------------------
    # Step 2 – meter configuration
    # ------------------------------------------------------------------

    async def async_step_meter(
        self, user_input: dict[str, Any] | None = None
    ) -> FlowResult:
        if user_input is not None:
            data = {**self._connection_data, **user_input}
            name = data.get(CONF_NAME, DEFAULT_NAME)
            host = data[CONF_HOST]
            port = data[CONF_PORT]
            return self.async_create_entry(
                title=f"{name} ({host}:{port})",
                data=data,
            )

        return self.async_show_form(
            step_id="meter",
            data_schema=STEP_METER_SCHEMA,
        )
