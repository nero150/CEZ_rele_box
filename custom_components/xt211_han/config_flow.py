"""Config flow for XT211 HAN integration.

Discovery order:
  1. DHCP discovery  – automatic, triggered by HA when USR-DR134 appears on network
  2. Network scan    – user clicks "Search network" in the UI
  3. Manual entry    – user types IP + port manually (always available as fallback)
"""

from __future__ import annotations

import asyncio
import logging
import socket
import struct
from ipaddress import IPv4Network, IPv4Address
from typing import Any

import voluptuous as vol

from homeassistant import config_entries
from homeassistant.components import dhcp
from homeassistant.const import CONF_HOST, CONF_PORT, CONF_NAME
from homeassistant.data_entry_flow import FlowResult
import homeassistant.helpers.config_validation as cv

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

# Known MAC prefixes for USR IOT devices (USR-DR134)
USR_IOT_MAC_PREFIXES = ("d8b04c", "b4e62d")

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


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

async def _test_connection(host: str, port: int, timeout: float = 5.0) -> str | None:
    """Try TCP connection. Returns error key or None on success."""
    try:
        reader, writer = await asyncio.wait_for(
            asyncio.open_connection(host, port),
            timeout=timeout,
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


async def _scan_network(port: int, timeout: float = 0.5) -> list[str]:
    """
    Scan the local network for open TCP port (default 8899).
    Returns list of IP addresses that responded.
    """
    # Determine local subnet from hostname
    try:
        local_ip = socket.gethostbyname(socket.gethostname())
    except OSError:
        local_ip = "192.168.1.1"

    try:
        network = IPv4Network(f"{local_ip}/24", strict=False)
    except ValueError:
        network = IPv4Network("192.168.1.0/24", strict=False)

    found: list[str] = []

    async def _probe(ip: str) -> None:
        try:
            _, writer = await asyncio.wait_for(
                asyncio.open_connection(ip, port),
                timeout=timeout,
            )
            writer.close()
            await writer.wait_closed()
            found.append(ip)
        except Exception:
            pass

    # Probe all hosts in /24 concurrently (skip network and broadcast)
    hosts = [str(h) for h in network.hosts()]
    await asyncio.gather(*[_probe(ip) for ip in hosts])
    return sorted(found)


# ---------------------------------------------------------------------------
# Config Flow
# ---------------------------------------------------------------------------

class XT211HANConfigFlow(config_entries.ConfigFlow, domain=DOMAIN):
    """
    Three-path config flow:
      - DHCP discovery  (automatic)
      - Network scan    (semi-automatic)
      - Manual entry    (always available)
    """

    VERSION = 1

    def __init__(self) -> None:
        self._connection_data: dict[str, Any] = {}
        self._discovered_host: str | None = None
        self._discovered_port: int = DEFAULT_PORT
        self._scan_results: list[str] = []

    # ------------------------------------------------------------------
    # Path 1 – DHCP discovery (triggered automatically by HA)
    # ------------------------------------------------------------------

    async def async_step_dhcp(self, discovery_info: dhcp.DhcpServiceInfo) -> FlowResult:
        """Handle DHCP discovery of a USR IOT device."""
        mac = discovery_info.macaddress.replace(":", "").lower()
        if not any(mac.startswith(prefix) for prefix in USR_IOT_MAC_PREFIXES):
            return self.async_abort(reason="not_supported")

        ip = discovery_info.ip
        _LOGGER.info("XT211 HAN: DHCP discovered USR IOT device at %s (MAC %s)", ip, mac)

        # Check not already configured
        await self.async_set_unique_id(f"{ip}:{DEFAULT_PORT}")
        self._abort_if_unique_id_configured(updates={CONF_HOST: ip})

        self._discovered_host = ip
        self._discovered_port = DEFAULT_PORT
        return await self.async_step_dhcp_confirm()

    async def async_step_dhcp_confirm(
        self, user_input: dict[str, Any] | None = None
    ) -> FlowResult:
        """Ask user to confirm the DHCP-discovered device."""
        if user_input is not None:
            error = await _test_connection(self._discovered_host, self._discovered_port)
            if error:
                return self.async_show_form(
                    step_id="dhcp_confirm",
                    errors={"base": error},
                    description_placeholders={
                        "host": self._discovered_host,
                        "port": str(self._discovered_port),
                    },
                )
            self._connection_data = {
                CONF_HOST: self._discovered_host,
                CONF_PORT: self._discovered_port,
                CONF_NAME: DEFAULT_NAME,
            }
            return await self.async_step_meter()

        return self.async_show_form(
            step_id="dhcp_confirm",
            description_placeholders={
                "host": self._discovered_host,
                "port": str(self._discovered_port),
            },
        )

    # ------------------------------------------------------------------
    # Path 2 + 3 – User-initiated: scan or manual
    # ------------------------------------------------------------------

    async def async_step_user(
        self, user_input: dict[str, Any] | None = None
    ) -> FlowResult:
        """First screen: choose between scan or manual entry."""
        if user_input is not None:
            if user_input.get("method") == "scan":
                return await self.async_step_scan()
            else:
                return await self.async_step_manual()

        return self.async_show_form(
            step_id="user",
            data_schema=vol.Schema(
                {
                    vol.Required("method", default="scan"): vol.In(
                        {
                            "scan": "🔍 Automaticky vyhledat v síti",
                            "manual": "✏️ Zadat IP adresu ručně",
                        }
                    )
                }
            ),
        )

    # ------------------------------------------------------------------
    # Path 2 – Network scan
    # ------------------------------------------------------------------

    async def async_step_scan(
        self, user_input: dict[str, Any] | None = None
    ) -> FlowResult:
        """Scan the local network for devices with the configured port open."""
        if user_input is not None:
            host = user_input[CONF_HOST]
            port = user_input.get(CONF_PORT, DEFAULT_PORT)
            name = user_input.get(CONF_NAME, DEFAULT_NAME)

            await self.async_set_unique_id(f"{host}:{port}")
            self._abort_if_unique_id_configured()

            error = await _test_connection(host, port)
            if error:
                return self.async_show_form(
                    step_id="scan",
                    data_schema=self._scan_schema(port),
                    errors={"base": error},
                )

            self._connection_data = {
                CONF_HOST: host,
                CONF_PORT: port,
                CONF_NAME: name,
            }
            return await self.async_step_meter()

        # Run the scan
        _LOGGER.debug("XT211 HAN: scanning network for port %d", DEFAULT_PORT)
        self._scan_results = await _scan_network(DEFAULT_PORT)
        _LOGGER.debug("XT211 HAN: scan found %d device(s): %s", len(self._scan_results), self._scan_results)

        if not self._scan_results:
            # Nothing found – fall through to manual with a warning
            return self.async_show_form(
                step_id="scan",
                data_schema=self._scan_schema(DEFAULT_PORT),
                errors={"base": "no_devices_found"},
            )

        # Build selector: found IPs + option to type manually
        choices = {ip: f"{ip}:{DEFAULT_PORT}" for ip in self._scan_results}
        choices["manual"] = "✏️ Zadat jinak ručně"

        return self.async_show_form(
            step_id="scan",
            data_schema=self._scan_schema(DEFAULT_PORT, choices),
        )

    def _scan_schema(
        self, port: int, choices: dict | None = None
    ) -> vol.Schema:
        if choices:
            return vol.Schema(
                {
                    vol.Required(CONF_HOST): vol.In(choices),
                    vol.Optional(CONF_NAME, default=DEFAULT_NAME): str,
                }
            )
        return vol.Schema(
            {
                vol.Required(CONF_HOST): str,
                vol.Required(CONF_PORT, default=port): int,
                vol.Optional(CONF_NAME, default=DEFAULT_NAME): str,
            }
        )

    # ------------------------------------------------------------------
    # Path 3 – Manual entry
    # ------------------------------------------------------------------

    async def async_step_manual(
        self, user_input: dict[str, Any] | None = None
    ) -> FlowResult:
        """Manual IP + port entry."""
        errors: dict[str, str] = {}

        if user_input is not None:
            host = user_input[CONF_HOST]
            port = user_input[CONF_PORT]
            name = user_input.get(CONF_NAME, DEFAULT_NAME)

            await self.async_set_unique_id(f"{host}:{port}")
            self._abort_if_unique_id_configured()

            error = await _test_connection(host, port)
            if error:
                errors["base"] = error
            else:
                self._connection_data = {
                    CONF_HOST: host,
                    CONF_PORT: port,
                    CONF_NAME: name,
                }
                return await self.async_step_meter()

        return self.async_show_form(
            step_id="manual",
            data_schema=STEP_CONNECTION_SCHEMA,
            errors=errors,
        )

    # ------------------------------------------------------------------
    # Step: meter configuration (shared by all paths)
    # ------------------------------------------------------------------

    async def async_step_meter(
        self, user_input: dict[str, Any] | None = None
    ) -> FlowResult:
        """Meter type, FVE, tariffs, relays."""
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
