"""
DLMS/COSEM PUSH mode parser for Sagemcom XT211 smart meter.

The XT211 sends unsolicited HDLC-framed DLMS/COSEM data every 60 seconds
over RS485 (9600 baud, 8N1). This module decodes those frames.

Frame structure (HDLC):
  7E                      - HDLC flag
  A0 xx                   - Frame type + length
  00 02 00 01 ...         - Destination / source addresses
  13                      - Control byte (UI frame)
  xx xx                   - HCS (header checksum)
  [LLC header]            - E6 E7 00
  [APDU]                  - DLMS application data (tag 0F = Data-notification)
  xx xx                   - FCS (frame checksum)
  7E                      - HDLC flag

OBIS codes supported (from ČEZ Distribuce spec):
  0-0:96.1.1.255   - Serial number (Device ID)
  0-0:96.3.10.255  - Disconnector status
  0-0:96.14.0.255  - Current tariff
  1-0:1.7.0.255    - Instant active power consumption (W)
  1-0:2.7.0.255    - Instant active power delivery (W)
  1-0:21.7.0.255   - Instant power L1 (W)
  1-0:41.7.0.255   - Instant power L2 (W)
  1-0:61.7.0.255   - Instant power L3 (W)
  1-0:1.8.0.255    - Active energy consumed (Wh)
  1-0:1.8.1.255    - Active energy T1 (Wh)
  1-0:1.8.2.255    - Active energy T2 (Wh)
  1-0:2.8.0.255    - Active energy delivered (Wh)
  0-1:96.3.10.255  - Relay R1 status
  0-2:96.3.10.255  - Relay R2 status
  0-3:96.3.10.255  - Relay R3 status
  0-4:96.3.10.255  - Relay R4 status
"""

from __future__ import annotations

import logging
import struct
from dataclasses import dataclass
from typing import Any

_LOGGER = logging.getLogger(__name__)

HDLC_FLAG = 0x7E

# DLMS data types
DLMS_TYPE_NULL = 0x00
DLMS_TYPE_BOOL = 0x03
DLMS_TYPE_INT8 = 0x0F
DLMS_TYPE_INT16 = 0x10
DLMS_TYPE_UINT8 = 0x11
DLMS_TYPE_UINT16 = 0x12
DLMS_TYPE_INT32 = 0x05
DLMS_TYPE_UINT32 = 0x06
DLMS_TYPE_INT64 = 0x14
DLMS_TYPE_UINT64 = 0x15
DLMS_TYPE_FLOAT32 = 0x16
DLMS_TYPE_FLOAT64 = 0x17
DLMS_TYPE_OCTET_STRING = 0x09
DLMS_TYPE_VISIBLE_STRING = 0x0A
DLMS_TYPE_ARRAY = 0x01
DLMS_TYPE_STRUCTURE = 0x02
DLMS_TYPE_COMPACT_ARRAY = 0x13

# SI unit multipliers (DLMS scaler)
# Scaler is a signed int8 representing 10^scaler
def apply_scaler(value: int | float, scaler: int) -> float:
    """Apply DLMS scaler (10^scaler) to a raw value."""
    return float(value) * (10 ** scaler)


@dataclass
class DLMSObject:
    """A single decoded DLMS COSEM object."""
    obis: str           # e.g. "1-0:1.8.0.255"
    value: Any          # decoded Python value
    unit: str = ""      # e.g. "W", "Wh", ""
    scaler: int = 0     # raw scaler from frame


@dataclass
class ParseResult:
    """Result of parsing one HDLC frame."""
    success: bool
    objects: list[DLMSObject]
    raw_hex: str = ""
    error: str = ""


class DLMSParser:
    """
    Stateful DLMS/COSEM PUSH mode parser for XT211.

    Usage:
        parser = DLMSParser()
        parser.feed(bytes_from_tcp)
        while (result := parser.get_frame()):
            process(result)
    """

    # DLMS unit codes → human readable strings
    UNIT_MAP = {
        1: "a",     2: "mo",    3: "wk",    4: "d",     5: "h",
        6: "min",   7: "s",     8: "°",     9: "°C",    10: "currency",
        11: "m",    12: "m/s",  13: "m³",   14: "m³",   15: "m³/h",
        16: "m³/h", 17: "m³/d", 18: "m³/d", 19: "l",    20: "kg",
        21: "N",    22: "Nm",   23: "Pa",   24: "bar",   25: "J",
        26: "J/h",  27: "W",    28: "VA",   29: "var",   30: "Wh",
        31: "VAh",  32: "varh", 33: "A",    34: "C",     35: "V",
        36: "V/m",  37: "F",    38: "Ω",    39: "Ωm²/m",40: "Wb",
        41: "T",    42: "A/m",  43: "H",    44: "Hz",    45: "1/Wh",
        46: "1/varh",47: "1/VAh",48: "V²h", 49: "A²h",  50: "kg/s",
        51: "S",    52: "K",    53: "1/(V²h)",54: "1/(A²h)",
        255: "",    0: "",
    }

    def __init__(self) -> None:
        self._buffer = bytearray()

    def feed(self, data: bytes) -> None:
        """Add raw bytes from TCP socket to the internal buffer."""
        self._buffer.extend(data)

    def get_frame(self) -> ParseResult | None:
        """
        Try to extract and parse one complete HDLC frame from the buffer.
        Returns ParseResult if a frame was found, None if more data is needed.
        """
        buf = self._buffer

        # Find opening flag
        start = buf.find(HDLC_FLAG)
        if start == -1:
            self._buffer.clear()
            return None
        if start > 0:
            _LOGGER.debug("Discarding %d bytes before HDLC flag", start)
            del self._buffer[:start]
            buf = self._buffer

        if len(buf) < 3:
            return None

        # Parse frame length from bytes 1-2 (A0 XX or A8 XX)
        # Bits 11-0 of bytes 1-2 give frame length
        frame_len = ((buf[1] & 0x07) << 8) | buf[2]

        # Total on-wire length = frame_len + 2 flags (opening already at 0, closing at frame_len+1)
        total = frame_len + 2
        if len(buf) < total:
            return None  # incomplete frame, wait for more data

        raw = bytes(buf[:total])
        del self._buffer[:total]

        raw_hex = raw.hex()
        _LOGGER.debug("HDLC frame: %s", raw_hex)

        # Basic sanity: starts and ends with 0x7E
        if raw[0] != HDLC_FLAG or raw[-1] != HDLC_FLAG:
            return ParseResult(success=False, raw_hex=raw_hex, error="Missing HDLC flags")

        try:
            result = self._parse_hdlc(raw)
            result.raw_hex = raw_hex
            return result
        except Exception as exc:
            _LOGGER.exception("Error parsing HDLC frame")
            return ParseResult(success=False, raw_hex=raw_hex, error=str(exc))

    # ------------------------------------------------------------------
    # Internal parsing methods
    # ------------------------------------------------------------------

    def _parse_hdlc(self, raw: bytes) -> ParseResult:
        """Parse full HDLC frame and extract DLMS objects."""
        pos = 1  # skip opening flag

        # Frame format byte (should be A0 or A8)
        # bits 11-0 = length
        _frame_type = raw[pos] & 0xF8
        frame_len = ((raw[pos] & 0x07) << 8) | raw[pos + 1]
        pos += 2

        # Destination address (variable length, LSB=1 means last byte)
        dest_addr, pos = self._read_hdlc_address(raw, pos)
        # Source address
        src_addr, pos = self._read_hdlc_address(raw, pos)

        # Control byte
        control = raw[pos]; pos += 1

        # HCS (2 bytes header checksum) - skip
        pos += 2

        # From here: LLC + APDU
        # LLC header: E6 E7 00 (or E6 E6 00 for request)
        if pos + 3 > len(raw) - 3:
            return ParseResult(success=False, error="Frame too short for LLC")

        llc = raw[pos:pos+3]; pos += 3
        _LOGGER.debug("LLC: %s  dest=%s  src=%s", llc.hex(), dest_addr, src_addr)

        # APDU starts here, ends 3 bytes before end (FCS + closing flag)
        apdu = raw[pos:-3]
        _LOGGER.debug("APDU (%d bytes): %s", len(apdu), apdu.hex())

        return self._parse_apdu(apdu)

    def _read_hdlc_address(self, data: bytes, pos: int) -> tuple[int, int]:
        """Read HDLC variable-length address. Returns (address_value, new_pos)."""
        addr = 0
        shift = 0
        while pos < len(data):
            byte = data[pos]; pos += 1
            addr |= (byte >> 1) << shift
            shift += 7
            if byte & 0x01:  # last byte of address
                break
        return addr, pos

    def _parse_apdu(self, apdu: bytes) -> ParseResult:
        """Parse DLMS APDU (Data-Notification = tag 0x0F)."""
        if not apdu:
            return ParseResult(success=False, error="Empty APDU")

        tag = apdu[0]

        if tag != 0x0F:
            return ParseResult(
                success=False,
                error=f"Unexpected APDU tag 0x{tag:02X} (expected 0x0F Data-Notification)"
            )

        # Data-Notification structure:
        # 0F [long-invoke-id-and-priority 4B] [date-time opt] [notification-body]
        pos = 1
        if len(apdu) < 5:
            return ParseResult(success=False, error="APDU too short")

        # Long invoke-id-and-priority (4 bytes)
        invoke_id = struct.unpack_from(">I", apdu, pos)[0]; pos += 4
        _LOGGER.debug("Invoke ID: 0x%08X", invoke_id)

        # Optional date-time: if next byte == 0x09 then it's an octet string with time
        if pos < len(apdu) and apdu[pos] == 0x09:
            pos += 1  # skip type tag
            dt_len = apdu[pos]; pos += 1
            _dt_bytes = apdu[pos:pos+dt_len]; pos += dt_len
            _LOGGER.debug("Timestamp bytes: %s", _dt_bytes.hex())
        elif pos < len(apdu) and apdu[pos] == 0x00:
            pos += 1  # optional field absent

        # Notification body is a structure containing the push data
        objects, _ = self._decode_value(apdu, pos)
        dlms_objects = self._extract_objects(objects)

        return ParseResult(success=True, objects=dlms_objects)

    def _decode_value(self, data: bytes, pos: int) -> tuple[Any, int]:
        """Recursively decode a DLMS typed value. Returns (value, new_pos)."""
        if pos >= len(data):
            return None, pos

        dtype = data[pos]; pos += 1

        if dtype == DLMS_TYPE_NULL:
            return None, pos

        elif dtype == DLMS_TYPE_BOOL:
            return bool(data[pos]), pos + 1

        elif dtype == DLMS_TYPE_INT8:
            return struct.unpack_from(">b", data, pos)[0], pos + 1

        elif dtype == DLMS_TYPE_UINT8:
            return data[pos], pos + 1

        elif dtype == DLMS_TYPE_INT16:
            return struct.unpack_from(">h", data, pos)[0], pos + 2

        elif dtype == DLMS_TYPE_UINT16:
            return struct.unpack_from(">H", data, pos)[0], pos + 2

        elif dtype == DLMS_TYPE_INT32:
            return struct.unpack_from(">i", data, pos)[0], pos + 4

        elif dtype == DLMS_TYPE_UINT32:
            return struct.unpack_from(">I", data, pos)[0], pos + 4

        elif dtype == DLMS_TYPE_INT64:
            return struct.unpack_from(">q", data, pos)[0], pos + 8

        elif dtype == DLMS_TYPE_UINT64:
            return struct.unpack_from(">Q", data, pos)[0], pos + 8

        elif dtype == DLMS_TYPE_FLOAT32:
            return struct.unpack_from(">f", data, pos)[0], pos + 4

        elif dtype == DLMS_TYPE_FLOAT64:
            return struct.unpack_from(">d", data, pos)[0], pos + 8

        elif dtype in (DLMS_TYPE_OCTET_STRING, DLMS_TYPE_VISIBLE_STRING):
            length, pos = self._decode_length(data, pos)
            raw_bytes = data[pos:pos+length]
            pos += length
            if dtype == DLMS_TYPE_VISIBLE_STRING:
                try:
                    return raw_bytes.decode("ascii", errors="replace"), pos
                except Exception:
                    return raw_bytes.hex(), pos
            return raw_bytes, pos

        elif dtype in (DLMS_TYPE_ARRAY, DLMS_TYPE_STRUCTURE, DLMS_TYPE_COMPACT_ARRAY):
            count, pos = self._decode_length(data, pos)
            items = []
            for _ in range(count):
                val, pos = self._decode_value(data, pos)
                items.append(val)
            return items, pos

        else:
            _LOGGER.warning("Unknown DLMS type 0x%02X at pos %d", dtype, pos)
            return None, pos

    def _decode_length(self, data: bytes, pos: int) -> tuple[int, int]:
        """Decode BER-style length field."""
        first = data[pos]; pos += 1
        if first < 0x80:
            return first, pos
        num_bytes = first & 0x7F
        length = 0
        for _ in range(num_bytes):
            length = (length << 8) | data[pos]; pos += 1
        return length, pos

    def _extract_objects(self, notification_body: Any) -> list[DLMSObject]:
        """
        Walk the decoded notification body and extract OBIS-keyed objects.

        The XT211 push notification body is a structure containing an array
        of structures, each typically:
          [OBIS bytes (6B octet-string), value (structure with scaler+unit), data]

        We try to handle both flat and nested layouts.
        """
        objects = []
        if not isinstance(notification_body, list):
            return objects

        # The outer structure may wrap an inner array
        # Try to unwrap one level of nesting
        payload = notification_body
        if len(payload) == 1 and isinstance(payload[0], list):
            payload = payload[0]

        for item in payload:
            if not isinstance(item, list) or len(item) < 2:
                continue
            try:
                obj = self._parse_cosem_entry(item)
                if obj:
                    objects.append(obj)
            except Exception as exc:
                _LOGGER.debug("Could not parse COSEM entry %s: %s", item, exc)

        return objects

    def _parse_cosem_entry(self, entry: list) -> DLMSObject | None:
        """
        Parse one COSEM entry from the push notification.
        Expected layout: [obis_bytes, [scaler, unit], value]
        or simplified:   [obis_bytes, value]
        """
        if len(entry) < 2:
            return None

        # First element should be the OBIS code as 6-byte octet string
        obis_raw = entry[0]
        if not isinstance(obis_raw, (bytes, bytearray)) or len(obis_raw) != 6:
            return None

        obis_str = self._format_obis(obis_raw)

        scaler = 0
        unit_code = 255
        value = None

        if len(entry) == 3:
            # entry[1] = [scaler, unit], entry[2] = value
            scaler_unit = entry[1]
            if isinstance(scaler_unit, list) and len(scaler_unit) == 2:
                raw_scaler = scaler_unit[0]
                # scaler is signed int8
                if isinstance(raw_scaler, int):
                    scaler = raw_scaler if raw_scaler < 128 else raw_scaler - 256
                unit_code = scaler_unit[1] if isinstance(scaler_unit[1], int) else 255
            value = entry[2]
        else:
            value = entry[1]

        # Apply scaler to numeric values
        if isinstance(value, int) and scaler != 0:
            final_value: Any = apply_scaler(value, scaler)
        elif isinstance(value, bytes):
            # Try to decode as ASCII string (e.g. serial number)
            try:
                final_value = value.decode("ascii", errors="replace").strip("\x00")
            except Exception:
                final_value = value.hex()
        else:
            final_value = value

        unit_str = self.UNIT_MAP.get(unit_code, "")

        return DLMSObject(
            obis=obis_str,
            value=final_value,
            unit=unit_str,
            scaler=scaler,
        )

    @staticmethod
    def _format_obis(raw: bytes) -> str:
        """Convert 6 raw bytes to OBIS string notation A-B:C.D.E.F"""
        if len(raw) != 6:
            return raw.hex()
        a, b, c, d, e, f = raw
        return f"{a}-{b}:{c}.{d}.{e}.{f}"


# ---------------------------------------------------------------------------
# Convenience: known OBIS codes for the XT211
# ---------------------------------------------------------------------------

OBIS_DESCRIPTIONS: dict[str, dict] = {
    # --- Idx 1: COSEM logical device name ---
    "0-0:42.0.0.255":  {"name": "Název zařízení",                    "unit": "",    "class": "text"},

    # --- Idx 3: Serial number ---
    "0-0:96.1.0.255":  {"name": "Výrobní číslo",                     "unit": "",    "class": "text"},

    # --- Idx 4: Disconnector ---
    "0-0:96.3.10.255": {"name": "Stav odpojovače",                   "unit": "",    "class": "binary"},

    # --- Idx 5: Power limiter ---
    "0-0:17.0.0.255":  {"name": "Limitér",                           "unit": "W",   "class": "power"},

    # --- Idx 6–11: Relays R1–R6 ---
    "0-1:96.3.10.255": {"name": "Stav relé R1",                      "unit": "",    "class": "binary"},
    "0-2:96.3.10.255": {"name": "Stav relé R2",                      "unit": "",    "class": "binary"},
    "0-3:96.3.10.255": {"name": "Stav relé R3",                      "unit": "",    "class": "binary"},
    "0-4:96.3.10.255": {"name": "Stav relé R4",                      "unit": "",    "class": "binary"},
    "0-5:96.3.10.255": {"name": "Stav relé R5",                      "unit": "",    "class": "binary"},
    "0-6:96.3.10.255": {"name": "Stav relé R6",                      "unit": "",    "class": "binary"},

    # --- Idx 12: Active tariff ---
    "0-0:96.14.0.255": {"name": "Aktuální tarif",                    "unit": "",    "class": "text"},

    # --- Idx 13–16: Instant power import (odběr) ---
    "1-0:1.7.0.255":   {"name": "Okamžitý příkon odběru celkem",     "unit": "W",   "class": "power"},
    "1-0:21.7.0.255":  {"name": "Okamžitý příkon odběru L1",         "unit": "W",   "class": "power"},
    "1-0:41.7.0.255":  {"name": "Okamžitý příkon odběru L2",         "unit": "W",   "class": "power"},
    "1-0:61.7.0.255":  {"name": "Okamžitý příkon odběru L3",         "unit": "W",   "class": "power"},

    # --- Idx 17–20: Instant power export (dodávka / FVE) ---
    "1-0:2.7.0.255":   {"name": "Okamžitý výkon dodávky celkem",     "unit": "W",   "class": "power"},
    "1-0:22.7.0.255":  {"name": "Okamžitý výkon dodávky L1",         "unit": "W",   "class": "power"},
    "1-0:42.7.0.255":  {"name": "Okamžitý výkon dodávky L2",         "unit": "W",   "class": "power"},
    "1-0:62.7.0.255":  {"name": "Okamžitý výkon dodávky L3",         "unit": "W",   "class": "power"},

    # --- Idx 21–25: Cumulative energy import (odběr kWh) ---
    "1-0:1.8.0.255":   {"name": "Spotřeba energie celkem",           "unit": "Wh",  "class": "energy"},
    "1-0:1.8.1.255":   {"name": "Spotřeba energie T1",               "unit": "Wh",  "class": "energy"},
    "1-0:1.8.2.255":   {"name": "Spotřeba energie T2",               "unit": "Wh",  "class": "energy"},
    "1-0:1.8.3.255":   {"name": "Spotřeba energie T3",               "unit": "Wh",  "class": "energy"},
    "1-0:1.8.4.255":   {"name": "Spotřeba energie T4",               "unit": "Wh",  "class": "energy"},

    # --- Idx 26: Cumulative energy export (dodávka kWh) ---
    "1-0:2.8.0.255":   {"name": "Dodávka energie celkem",            "unit": "Wh",  "class": "energy"},

    # --- Idx 27: Consumer message ---
    "0-0:96.13.0.255": {"name": "Zpráva pro zákazníka",              "unit": "",    "class": "text"},
}
