"""Config flow for XT211 HAN integration."""

from __future__ import annotations

import asyncio
import logging
from typing import Any

import voluptuous as vol

from homeassistant import config_entries
from homeassistant.const import CONF_HOST, CONF_PORT, CONF_NAME
from homeassistant.data_entry_flow import FlowResult

from .const import DOMAIN, DEFAULT_PORT, DEFAULT_NAME

_LOGGER = logging.getLogger(__name__)

STEP_USER_DATA_SCHEMA = vol.Schema(
    {
        vol.Required(CONF_HOST): str,
        vol.Required(CONF_PORT, default=DEFAULT_PORT): int,
        vol.Optional(CONF_NAME, default=DEFAULT_NAME): str,
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
    """Handle the config flow for XT211 HAN."""

    VERSION = 1

    async def async_step_user(
        self, user_input: dict[str, Any] | None = None
    ) -> FlowResult:
        errors: dict[str, str] = {}

        if user_input is not None:
            host = user_input[CONF_HOST]
            port = user_input[CONF_PORT]
            name = user_input.get(CONF_NAME, DEFAULT_NAME)

            # Prevent duplicate entries
            await self.async_set_unique_id(f"{host}:{port}")
            self._abort_if_unique_id_configured()

            error = await _test_connection(host, port)
            if error:
                errors["base"] = error
            else:
                return self.async_create_entry(
                    title=f"{name} ({host}:{port})",
                    data={
                        CONF_HOST: host,
                        CONF_PORT: port,
                        CONF_NAME: name,
                    },
                )

        return self.async_show_form(
            step_id="user",
            data_schema=STEP_USER_DATA_SCHEMA,
            errors=errors,
            description_placeholders={
                "default_port": str(DEFAULT_PORT),
            },
        )
