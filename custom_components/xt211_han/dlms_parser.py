"""DLMS/COSEM PUSH parser for Sagemcom XT211 smart meter."""

from __future__ import annotations

import logging
import struct
from dataclasses import dataclass, field
from typing import Any

_LOGGER = logging.getLogger(__name__)
HDLC_FLAG = 0x7E
DLMS_TYPE_NULL = 0x00
DLMS_TYPE_ARRAY = 0x01
DLMS_TYPE_STRUCTURE = 0x02
DLMS_TYPE_BOOL = 0x03
DLMS_TYPE_INT32 = 0x05
DLMS_TYPE_UINT32 = 0x06
DLMS_TYPE_OCTET_STRING = 0x09
DLMS_TYPE_VISIBLE_STRING = 0x0A
DLMS_TYPE_INT8 = 0x0F
DLMS_TYPE_INT16 = 0x10
DLMS_TYPE_UINT8 = 0x11
DLMS_TYPE_UINT16 = 0x12
DLMS_TYPE_COMPACT_ARRAY = 0x13
DLMS_TYPE_INT64 = 0x14
DLMS_TYPE_UINT64 = 0x15
DLMS_TYPE_ENUM = 0x16
DLMS_TYPE_FLOAT32 = 0x17
DLMS_TYPE_FLOAT64 = 0x18


class NeedMoreData(Exception):
    pass


@dataclass
class DLMSObject:
    obis: str
    value: Any
    unit: str = ""
    scaler: int = 0


@dataclass
class ParseResult:
    success: bool
    objects: list[DLMSObject] = field(default_factory=list)
    raw_hex: str = ""
    error: str = ""


class DLMSParser:
    def __init__(self) -> None:
        self._buffer = bytearray()

    def feed(self, data: bytes) -> None:
        self._buffer.extend(data)

    def get_frame(self) -> ParseResult | None:
        if not self._buffer:
            return None
        if self._buffer[0] == HDLC_FLAG:
            return self._get_hdlc_frame()
        start = self._find_apdu_start(self._buffer)
        if start == -1:
            _LOGGER.debug("Discarding %d bytes without known frame start", len(self._buffer))
            self._buffer.clear()
            return None
        if start > 0:
            _LOGGER.debug("Discarding %d leading byte(s) before APDU", start)
            del self._buffer[:start]
        if self._buffer and self._buffer[0] == 0x0F:
            return self._get_raw_apdu_frame()
        return None

    def _get_hdlc_frame(self) -> ParseResult | None:
        buf = self._buffer
        if len(buf) < 3:
            return None
        frame_len = ((buf[1] & 0x07) << 8) | buf[2]
        total = frame_len + 2
        if len(buf) < total:
            return None
        raw = bytes(buf[:total])
        del buf[:total]
        raw_hex = raw.hex()
        if raw[0] != HDLC_FLAG or raw[-1] != HDLC_FLAG:
            return ParseResult(success=False, raw_hex=raw_hex, error="Missing HDLC flags")
        try:
            result = self._parse_hdlc(raw)
            result.raw_hex = raw_hex
            return result
        except NeedMoreData:
            return None
        except Exception as exc:
            _LOGGER.exception("Error parsing HDLC frame")
            return ParseResult(success=False, raw_hex=raw_hex, error=str(exc))

    def _get_raw_apdu_frame(self) -> ParseResult | None:
        buf = self._buffer
        try:
            result, consumed = self._parse_apdu_with_length(bytes(buf))
        except NeedMoreData:
            return None
        except Exception as exc:
            raw_hex = bytes(buf).hex()
            _LOGGER.exception("Error parsing raw DLMS APDU")
            del buf[:]
            return ParseResult(success=False, raw_hex=raw_hex, error=str(exc))
        raw = bytes(buf[:consumed])
        del buf[:consumed]
        result.raw_hex = raw.hex()
        return result

    def _parse_hdlc(self, raw: bytes) -> ParseResult:
        pos = 1
        pos += 2
        _, pos = self._read_hdlc_address(raw, pos)
        _, pos = self._read_hdlc_address(raw, pos)
        pos += 1
        pos += 2
        if pos + 3 > len(raw) - 3:
            raise ValueError("Frame too short for LLC")
        pos += 3
        apdu = raw[pos:-3]
        result, _ = self._parse_apdu_with_length(apdu)
        return result

    def _read_hdlc_address(self, data: bytes, pos: int) -> tuple[int, int]:
        addr = 0
        shift = 0
        while True:
            if pos >= len(data):
                raise NeedMoreData
            byte = data[pos]
            pos += 1
            addr |= (byte >> 1) << shift
            shift += 7
            if byte & 0x01:
                return addr, pos

    def _parse_apdu_with_length(self, apdu: bytes) -> tuple[ParseResult, int]:
        if not apdu:
            raise NeedMoreData
        if apdu[0] != 0x0F:
            raise ValueError(f"Unexpected APDU tag 0x{apdu[0]:02X}")
        if len(apdu) < 6:
            raise NeedMoreData
        pos = 1
        invoke_id = struct.unpack_from(">I", apdu, pos)[0]
        pos += 4
        _LOGGER.debug("XT211 invoke_id=0x%08X", invoke_id)
        if pos >= len(apdu):
            raise NeedMoreData
        if apdu[pos] == DLMS_TYPE_OCTET_STRING:
            pos += 1
            dt_len, pos = self._decode_length(apdu, pos)
            self._require(apdu, pos, dt_len)
            pos += dt_len
        elif apdu[pos] == DLMS_TYPE_NULL:
            pos += 1
        self._require(apdu, pos, 2)
        if apdu[pos] != DLMS_TYPE_STRUCTURE:
            return ParseResult(success=True, objects=[]), pos
        structure_count = apdu[pos + 1]
        pos += 2
        if structure_count < 2:
            return ParseResult(success=True, objects=[]), pos
        if pos >= len(apdu):
            raise NeedMoreData
        if apdu[pos] == DLMS_TYPE_ENUM:
            self._require(apdu, pos, 2)
            pos += 2
        else:
            _, pos = self._decode_value(apdu, pos)
        if pos >= len(apdu):
            raise NeedMoreData
        if apdu[pos] != DLMS_TYPE_ARRAY:
            return ParseResult(success=True, objects=[]), pos
        pos += 1
        array_count, pos = self._decode_length(apdu, pos)
        objects: list[DLMSObject] = []
        for _ in range(array_count):
            obj, pos = self._parse_xt211_object(apdu, pos)
            if obj is not None:
                objects.append(obj)
        return ParseResult(success=True, objects=objects), pos

    def _parse_xt211_object(self, data: bytes, pos: int) -> tuple[DLMSObject | None, int]:
        self._require(data, pos, 1)
        if data[pos] != DLMS_TYPE_STRUCTURE:
            raise ValueError(f"Expected object structure at {pos}, got 0x{data[pos]:02X}")
        pos += 1
        count, pos = self._decode_length(data, pos)
        if count < 1:
            raise ValueError(f"Unexpected object element count {count}")
        if pos < len(data) and data[pos] == 0x00:
            if pos + 10 > len(data):
                raise NeedMoreData
            class_id = int.from_bytes(data[pos:pos + 2], "big")
            pos += 2
            obis_raw = bytes(data[pos:pos + 6])
            pos += 6
            pos += 1
            value, pos = self._decode_value(data, pos)
            if isinstance(value, (bytes, bytearray)):
                try:
                    value = bytes(value).decode("ascii", errors="replace").strip("\x00")
                except Exception:
                    value = bytes(value).hex()
            obis = self._format_obis(obis_raw)
            meta = OBIS_DESCRIPTIONS.get(obis, {})
            _LOGGER.debug("Parsed XT211 object class_id=%s obis=%s value=%r unit=%s", class_id, obis, value, meta.get("unit", ""))
            return DLMSObject(obis=obis, value=value, unit=meta.get("unit", ""), scaler=0), pos
        last_value: Any = None
        for _ in range(count):
            last_value, pos = self._decode_value(data, pos)
        _LOGGER.debug("Ignoring non-measurement structure value=%r", last_value)
        return None, pos

    def _decode_value(self, data: bytes, pos: int) -> tuple[Any, int]:
        self._require(data, pos, 1)
        dtype = data[pos]
        pos += 1
        if dtype == DLMS_TYPE_NULL:
            return None, pos
        if dtype == DLMS_TYPE_BOOL:
            self._require(data, pos, 1)
            return bool(data[pos]), pos + 1
        if dtype == DLMS_TYPE_INT8:
            self._require(data, pos, 1)
            return struct.unpack_from(">b", data, pos)[0], pos + 1
        if dtype == DLMS_TYPE_UINT8 or dtype == DLMS_TYPE_ENUM:
            self._require(data, pos, 1)
            return data[pos], pos + 1
        if dtype == DLMS_TYPE_INT16:
            self._require(data, pos, 2)
            return struct.unpack_from(">h", data, pos)[0], pos + 2
        if dtype == DLMS_TYPE_UINT16:
            self._require(data, pos, 2)
            return struct.unpack_from(">H", data, pos)[0], pos + 2
        if dtype == DLMS_TYPE_INT32:
            self._require(data, pos, 4)
            return struct.unpack_from(">i", data, pos)[0], pos + 4
        if dtype == DLMS_TYPE_UINT32:
            self._require(data, pos, 4)
            return struct.unpack_from(">I", data, pos)[0], pos + 4
        if dtype == DLMS_TYPE_INT64:
            self._require(data, pos, 8)
            return struct.unpack_from(">q", data, pos)[0], pos + 8
        if dtype == DLMS_TYPE_UINT64:
            self._require(data, pos, 8)
            return struct.unpack_from(">Q", data, pos)[0], pos + 8
        if dtype == DLMS_TYPE_FLOAT32:
            self._require(data, pos, 4)
            return struct.unpack_from(">f", data, pos)[0], pos + 4
        if dtype == DLMS_TYPE_FLOAT64:
            self._require(data, pos, 8)
            return struct.unpack_from(">d", data, pos)[0], pos + 8
        if dtype in (DLMS_TYPE_OCTET_STRING, DLMS_TYPE_VISIBLE_STRING):
            length, pos = self._decode_length(data, pos)
            self._require(data, pos, length)
            raw = data[pos:pos + length]
            pos += length
            if dtype == DLMS_TYPE_VISIBLE_STRING:
                return raw.decode("ascii", errors="replace"), pos
            return bytes(raw), pos
        if dtype in (DLMS_TYPE_ARRAY, DLMS_TYPE_STRUCTURE, DLMS_TYPE_COMPACT_ARRAY):
            count, pos = self._decode_length(data, pos)
            items: list[Any] = []
            for _ in range(count):
                item, pos = self._decode_value(data, pos)
                items.append(item)
            return items, pos
        raise ValueError(f"Unknown DLMS type 0x{dtype:02X} at pos {pos - 1}")

    def _decode_length(self, data: bytes, pos: int) -> tuple[int, int]:
        self._require(data, pos, 1)
        first = data[pos]
        pos += 1
        if first < 0x80:
            return first, pos
        num_bytes = first & 0x7F
        self._require(data, pos, num_bytes)
        length = 0
        for _ in range(num_bytes):
            length = (length << 8) | data[pos]
            pos += 1
        return length, pos

    def _require(self, data: bytes, pos: int, count: int) -> None:
        if pos + count > len(data):
            raise NeedMoreData

    def _find_apdu_start(self, data: bytes) -> int:
        try:
            return data.index(0x0F)
        except ValueError:
            return -1

    def _format_obis(self, raw: bytes) -> str:
        if len(raw) != 6:
            return raw.hex()
        a, b, c, d, e, f = raw
        return f"{a}-{b}:{c}.{d}.{e}.{f}"


OBIS_DESCRIPTIONS = {
    "0-0:42.0.0.255": {"name": "Název zařízení", "unit": "", "class": "text"},
    "0-0:96.1.0.255": {"name": "Výrobní číslo", "unit": "", "class": "text"},
    "0-0:96.1.1.255": {"name": "Výrobní číslo", "unit": "", "class": "text"},
    "0-0:96.3.10.255": {"name": "Stav odpojovače", "unit": "", "class": "binary"},
    "0-0:17.0.0.255": {"name": "Limitér", "unit": "W", "class": "power"},
    "0-1:96.3.10.255": {"name": "Stav relé R1", "unit": "", "class": "binary"},
    "0-2:96.3.10.255": {"name": "Stav relé R2", "unit": "", "class": "binary"},
    "0-3:96.3.10.255": {"name": "Stav relé R3", "unit": "", "class": "binary"},
    "0-4:96.3.10.255": {"name": "Stav relé R4", "unit": "", "class": "binary"},
    "0-5:96.3.10.255": {"name": "Stav relé R5", "unit": "", "class": "binary"},
    "0-6:96.3.10.255": {"name": "Stav relé R6", "unit": "", "class": "binary"},
    "0-0:96.14.0.255": {"name": "Aktuální tarif", "unit": "", "class": "text"},
    "1-0:1.7.0.255": {"name": "Okamžitý příkon odběru celkem", "unit": "W", "class": "power"},
    "1-0:21.7.0.255": {"name": "Okamžitý příkon odběru L1", "unit": "W", "class": "power"},
    "1-0:41.7.0.255": {"name": "Okamžitý příkon odběru L2", "unit": "W", "class": "power"},
    "1-0:61.7.0.255": {"name": "Okamžitý příkon odběru L3", "unit": "W", "class": "power"},
    "1-0:2.7.0.255": {"name": "Okamžitý výkon dodávky celkem", "unit": "W", "class": "power"},
    "1-0:22.7.0.255": {"name": "Okamžitý výkon dodávky L1", "unit": "W", "class": "power"},
    "1-0:42.7.0.255": {"name": "Okamžitý výkon dodávky L2", "unit": "W", "class": "power"},
    "1-0:62.7.0.255": {"name": "Okamžitý výkon dodávky L3", "unit": "W", "class": "power"},
    "1-0:1.8.0.255": {"name": "Spotřeba energie celkem", "unit": "Wh", "class": "energy"},
    "1-0:1.8.1.255": {"name": "Spotřeba energie T1", "unit": "Wh", "class": "energy"},
    "1-0:1.8.2.255": {"name": "Spotřeba energie T2", "unit": "Wh", "class": "energy"},
    "1-0:1.8.3.255": {"name": "Spotřeba energie T3", "unit": "Wh", "class": "energy"},
    "1-0:1.8.4.255": {"name": "Spotřeba energie T4", "unit": "Wh", "class": "energy"},
    "1-0:2.8.0.255": {"name": "Dodávka energie celkem", "unit": "Wh", "class": "energy"},
    "0-0:96.13.0.255": {"name": "Zpráva pro zákazníka", "unit": "", "class": "text"}
}
