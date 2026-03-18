"""Coordinator for XT211 HAN integration."""

from __future__ import annotations

import asyncio
import logging
from typing import Any

from homeassistant.core import HomeAssistant
from homeassistant.helpers.update_coordinator import DataUpdateCoordinator

from .dlms_parser import DLMSObject, DLMSParser, OBIS_DESCRIPTIONS

_LOGGER = logging.getLogger(__name__)

PUSH_TIMEOUT = 90
RECONNECT_DELAY = 10


class XT211Coordinator(DataUpdateCoordinator[dict[str, Any]]):
    """Persistent TCP listener for XT211 DLMS push frames."""

    def __init__(self, hass: HomeAssistant, host: str, port: int, name: str) -> None:
        super().__init__(
            hass,
            _LOGGER,
            name=f"XT211 HAN ({host}:{port})",
            update_interval=None,
        )
        self.host = host
        self.port = port
        self.device_name = name
        self._parser = DLMSParser()
        self._reader: asyncio.StreamReader | None = None
        self._writer: asyncio.StreamWriter | None = None
        self._listen_task: asyncio.Task | None = None
        self._connected = False
        self._frames_received = 0

    @property
    def connected(self) -> bool:
        return self._connected

    async def async_setup(self) -> None:
        if self._listen_task is None or self._listen_task.done():
            self._listen_task = self.hass.async_create_background_task(
                self._listen_loop(),
                name=f"xt211_han_{self.host}_{self.port}",
            )

    async def async_shutdown(self) -> None:
        if self._listen_task:
            self._listen_task.cancel()
            try:
                await self._listen_task
            except asyncio.CancelledError:
                pass
        await self._disconnect()

    async def _async_update_data(self) -> dict[str, Any]:
        return self.data or {}

    async def _listen_loop(self) -> None:
        while True:
            try:
                await self._connect()
                await self._receive_loop()
            except asyncio.CancelledError:
                _LOGGER.info("XT211 listener task cancelled")
                raise
            except Exception as exc:
                self._connected = False
                _LOGGER.warning(
                    "XT211 connection error (%s:%d): %s – retrying in %ds",
                    self.host,
                    self.port,
                    exc,
                    RECONNECT_DELAY,
                )
            finally:
                await self._disconnect()

            await asyncio.sleep(RECONNECT_DELAY)

    async def _connect(self) -> None:
        _LOGGER.info("Connecting to XT211 adapter at %s:%d", self.host, self.port)
        self._reader, self._writer = await asyncio.wait_for(
            asyncio.open_connection(self.host, self.port),
            timeout=10,
        )
        self._parser = DLMSParser()
        self._connected = True
        _LOGGER.info("Connected to XT211 adapter at %s:%d", self.host, self.port)

    async def _disconnect(self) -> None:
        self._connected = False
        if self._writer:
            try:
                self._writer.close()
                await self._writer.wait_closed()
            except Exception:
                pass
            self._writer = None
        self._reader = None

    async def _receive_loop(self) -> None:
        assert self._reader is not None

        while True:
            try:
                chunk = await asyncio.wait_for(self._reader.read(4096), timeout=PUSH_TIMEOUT)
            except asyncio.TimeoutError as exc:
                _LOGGER.warning("No data from XT211 for %d s – reconnecting", PUSH_TIMEOUT)
                raise ConnectionError("Push timeout") from exc

            if not chunk:
                _LOGGER.warning("XT211 adapter closed connection")
                raise ConnectionError("Remote closed")

            _LOGGER.debug("XT211 RX %d bytes: %s", len(chunk), chunk.hex())
            self._parser.feed(chunk)

            while True:
                result = self._parser.get_frame()
                if result is None:
                    break

                self._frames_received += 1
                if result.success:
                    _LOGGER.debug(
                        "XT211 frame #%d parsed OK: %d object(s)",
                        self._frames_received,
                        len(result.objects),
                    )
                    await self._process_frame(result.objects)
                else:
                    _LOGGER.debug(
                        "XT211 frame #%d parse error: %s (raw: %s)",
                        self._frames_received,
                        result.error,
                        result.raw_hex[:120],
                    )

    async def _process_frame(self, objects: list[DLMSObject]) -> None:
        if not objects:
            _LOGGER.debug("Received empty DLMS frame")
            return

        current = dict(self.data or {})
        changed: list[str] = []

        for obj in objects:
            meta = OBIS_DESCRIPTIONS.get(obj.obis, {})
            new_value = {
                "value": obj.value,
                "unit": obj.unit or meta.get("unit", ""),
                "name": meta.get("name", obj.obis),
                "class": meta.get("class", "sensor"),
            }
            if current.get(obj.obis) != new_value:
                changed.append(obj.obis)
            current[obj.obis] = new_value
            _LOGGER.debug("XT211 OBIS %s = %r %s", obj.obis, obj.value, new_value["unit"])

        self.async_set_updated_data(current)
        _LOGGER.debug(
            "Coordinator updated with %d object(s), %d changed: %s",
            len(objects),
            len(changed),
            ", ".join(changed[:10]),
        )
