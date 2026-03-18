"""Config flow for XT211 HAN integration."""

from __future__ import annotations

import asyncio
import logging
import socket
from ipaddress import IPv4Network
from typing import Any

import voluptuous as vol

from homeassistant import config_entries
from homeassistant.components import dhcp
from homeassistant.const import CONF_HOST, CONF_NAME, CONF_PORT
from homeassistant.data_entry_flow import FlowResult

from .const import (
    CONF_HAS_FVE,
    CONF_PHASES,
    CONF_RELAY_COUNT,
    CONF_TARIFFS,
    DEFAULT_NAME,
    DEFAULT_PORT,
    DOMAIN,
    PHASES_1,
    PHASES_3,
    RELAYS_0,
    RELAYS_4,
    RELAYS_6,
    TARIFFS_1,
    TARIFFS_2,
    TARIFFS_4,
)

_LOGGER = logging.getLogger(__name__)

USR_IOT_MAC_PREFIXES = ("d8b04c", "b4e62d")
MANUAL_CHOICE = "__manual__"

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


async def _test_connection(host: str, port: int, timeout: float = 5.0) -> str | None:
    try:
        reader, writer = await asyncio.wait_for(asyncio.open_connection(host, port), timeout=timeout)
        writer.close()
        await writer.wait_closed()
        return None
    except asyncio.TimeoutError:
        return "cannot_connect"
    except OSError:
        return "cannot_connect"
    except Exception:  # pragma: no cover - defensive
        return "unknown"


async def _scan_network(port: int, timeout: float = 1.0) -> list[str]:
    local_ip = "192.168.1.1"
    try:
        with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as sock:
            sock.settimeout(0)
            sock.connect(("8.8.8.8", 80))
            local_ip = sock.getsockname()[0]
    except Exception:
        try:
            local_ip = socket.gethostbyname(socket.gethostname())
        except Exception:
            pass

    if local_ip.startswith("127.") or local_ip == "0.0.0.0":
        local_ip = "192.168.1.1"

    try:
        network = IPv4Network(f"{local_ip}/24", strict=False)
    except ValueError:
        network = IPv4Network("192.168.1.0/24", strict=False)

    found: list[str] = []

    async def _probe(ip: str) -> None:
        try:
            _, writer = await asyncio.wait_for(asyncio.open_connection(ip, port), timeout=timeout)
            writer.close()
            try:
                await writer.wait_closed()
            except Exception:
                pass
            found.append(ip)
        except Exception:
            pass

    hosts = [str(host) for host in network.hosts()]
    for index in range(0, len(hosts), 50):
        await asyncio.gather(*(_probe(ip) for ip in hosts[index:index + 50]))

    found.sort()
    _LOGGER.debug("XT211 scan found %d host(s) on port %d: %s", len(found), port, found)
    return found


class XT211HANConfigFlow(config_entries.ConfigFlow, domain=DOMAIN):
    VERSION = 1

    def __init__(self) -> None:
        self._connection_data: dict[str, Any] = {}
        self._discovered_host: str | None = None
        self._discovered_port: int = DEFAULT_PORT
        self._scan_results: list[str] = []

    async def async_step_dhcp(self, discovery_info: dhcp.DhcpServiceInfo) -> FlowResult:
        mac = discovery_info.macaddress.replace(":", "").lower()
        if not any(mac.startswith(prefix) for prefix in USR_IOT_MAC_PREFIXES):
            return self.async_abort(reason="not_supported")

        ip = discovery_info.ip
        await self.async_set_unique_id(f"{ip}:{DEFAULT_PORT}")
        self._abort_if_unique_id_configured(updates={CONF_HOST: ip})

        self._discovered_host = ip
        self._discovered_port = DEFAULT_PORT
        _LOGGER.info("XT211 HAN: DHCP discovered USR IOT device at %s (MAC %s)", ip, mac)
        return await self.async_step_dhcp_confirm()

    async def async_step_dhcp_confirm(self, user_input: dict[str, Any] | None = None) -> FlowResult:
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

    async def async_step_user(self, user_input: dict[str, Any] | None = None) -> FlowResult:
        if user_input is not None:
            return await (self.async_step_scan() if user_input.get("method") == "scan" else self.async_step_manual())

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

    async def async_step_scan(self, user_input: dict[str, Any] | None = None) -> FlowResult:
        if user_input is not None:
            host = user_input[CONF_HOST]
            if host == MANUAL_CHOICE:
                return await self.async_step_manual()

            port = user_input.get(CONF_PORT, DEFAULT_PORT)
            name = user_input.get(CONF_NAME, DEFAULT_NAME)

            await self.async_set_unique_id(f"{host}:{port}")
            self._abort_if_unique_id_configured()

            error = await _test_connection(host, port)
            if error:
                return self.async_show_form(
                    step_id="scan",
                    data_schema=self._scan_schema(port, include_choices=not self._scan_results == []),
                    errors={"base": error},
                )

            self._connection_data = {CONF_HOST: host, CONF_PORT: port, CONF_NAME: name}
            return await self.async_step_meter()

        self._scan_results = await _scan_network(DEFAULT_PORT)
        if not self._scan_results:
            return self.async_show_form(
                step_id="scan",
                data_schema=self._scan_schema(DEFAULT_PORT, include_choices=False),
                errors={"base": "no_devices_found"},
            )

        return self.async_show_form(
            step_id="scan",
            data_schema=self._scan_schema(DEFAULT_PORT, include_choices=True),
        )

    def _scan_schema(self, port: int, include_choices: bool) -> vol.Schema:
        if include_choices:
            choices = {ip: f"{ip}:{port}" for ip in self._scan_results}
            choices[MANUAL_CHOICE] = "✏️ Zadat IP adresu ručně"
            return vol.Schema(
                {
                    vol.Required(CONF_HOST): vol.In(choices),
                    vol.Optional(CONF_PORT, default=port): int,
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

    async def async_step_manual(self, user_input: dict[str, Any] | None = None) -> FlowResult:
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
                self._connection_data = {CONF_HOST: host, CONF_PORT: port, CONF_NAME: name}
                return await self.async_step_meter()

        return self.async_show_form(step_id="manual", data_schema=STEP_CONNECTION_SCHEMA, errors=errors)

    async def async_step_meter(self, user_input: dict[str, Any] | None = None) -> FlowResult:
        if user_input is not None:
            data = {**self._connection_data, **user_input}
            name = data.get(CONF_NAME, DEFAULT_NAME)
            host = data[CONF_HOST]
            port = data[CONF_PORT]
            return self.async_create_entry(title=f"{name} ({host}:{port})", data=data)

        return self.async_show_form(step_id="meter", data_schema=STEP_METER_SCHEMA)
