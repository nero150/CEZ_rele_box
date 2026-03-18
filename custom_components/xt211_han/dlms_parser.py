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
    objects: list[DLMSObject] = None
    raw_hex: str = ""
    error: str = ""

    def __post_init__(self):
        if self.objects is None:
            self.objects = []


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
        Try to extract and parse one complete frame from the buffer.

        Supports two formats:
          1. HDLC-wrapped:  7E A0 xx ... 7E
          2. Raw DLMS APDU: 0F [4B invoke-id] [optional datetime] [body]
             (USR-DR134 strips the HDLC wrapper and sends raw APDU)
        """
        buf = self._buffer

        if not buf:
            return None

        # ----------------------------------------------------------------
        # Format 2: Raw DLMS APDU starting with 0x0F (Data-Notification)
        # USR-DR134 sends this directly without HDLC framing
        # ----------------------------------------------------------------
        if buf[0] == 0x0F:
            # We need at least 5 bytes (tag + 4B invoke-id)
            if len(buf) < 5:
                return None

            # Heuristic: find the end of this APDU
            # The USR-DR134 sends one complete APDU per TCP segment
            # We consume everything in the buffer as one frame
            raw = bytes(buf)
            self._buffer.clear()

            raw_hex = raw.hex()
            _LOGGER.debug("Raw DLMS APDU (%d bytes): %s", len(raw), raw_hex[:80])

            try:
                result = self._parse_apdu(raw)
                result.raw_hex = raw_hex
                return result
            except Exception as exc:
                _LOGGER.exception("Error parsing raw DLMS APDU")
                return ParseResult(success=False, raw_hex=raw_hex, error=str(exc))

        # ----------------------------------------------------------------
        # Format 1: HDLC-wrapped frame starting with 0x7E
        # ----------------------------------------------------------------
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

        frame_len = ((buf[1] & 0x07) << 8) | buf[2]
        total = frame_len + 2
        if len(buf) < total:
            return None

        raw = bytes(buf[:total])
        del self._buffer[:total]

        raw_hex = raw.hex()
        _LOGGER.debug("HDLC frame (%d bytes): %s", len(raw), raw_hex[:80])

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
            return ParseResult(success=False, objects=[], error="Empty APDU")

        tag = apdu[0]
        if tag != 0x0F:
            return ParseResult(
                success=False, objects=[],
                error=f"Unexpected APDU tag 0x{tag:02X} (expected 0x0F)"
            )

        pos = 1
        if len(apdu) < 5:
            return ParseResult(success=False, objects=[], error="APDU too short")

        # Invoke-id (4 bytes) - bit 31 set = data frame, clear = push-setup (ignore)
        invoke_id = struct.unpack_from(">I", apdu, pos)[0]
        pos += 4
        _LOGGER.debug("Invoke ID: 0x%08X", invoke_id)

        # Push-setup frames (invoke_id MSB = 0) contain no measurement data
        if not (invoke_id & 0x80000000):
            _LOGGER.debug("Push-setup frame (invoke_id MSB=0), skipping")
            return ParseResult(success=True, objects=[])

        # Optional date-time
        if pos < len(apdu) and apdu[pos] == 0x09:
            pos += 1
            dt_len = apdu[pos]; pos += 1
            pos += dt_len
        elif pos < len(apdu) and apdu[pos] == 0x00:
            pos += 1  # absent

        # Notification body:
        # structure(2):
        #   [0] enum  = push type (ignore)
        #   [1] array(N) of structure(2): [obis_octet_string, value_structure(2): [scaler_unit, value]]
        if pos >= len(apdu):
            return ParseResult(success=True, objects=[])

        # Outer structure tag
        if apdu[pos] != 0x02:
            _LOGGER.debug("Expected structure (0x02) at pos %d, got 0x%02X", pos, apdu[pos])
            return ParseResult(success=True, objects=[])
        pos += 1
        outer_count, pos = self._decode_length(apdu, pos)
        _LOGGER.debug("Outer structure count: %d", outer_count)

        # Skip push type (first element, usually enum)
        if pos < len(apdu) and apdu[pos] == 0x16:
            pos += 2  # enum tag + 1 byte value

        # Next should be array of COSEM objects
        if pos >= len(apdu) or apdu[pos] != 0x01:
            _LOGGER.debug("Expected array (0x01) at pos %d, got 0x%02X", pos, apdu[pos] if pos < len(apdu) else -1)
            return ParseResult(success=True, objects=[])
        pos += 1
        array_count, pos = self._decode_length(apdu, pos)
        _LOGGER.debug("Array count: %d objects", array_count)

        objects = []
        for i in range(array_count):
            if pos >= len(apdu):
                break
            try:
                obj, pos = self._parse_cosem_object(apdu, pos)
                if obj:
                    objects.append(obj)
            except Exception as exc:
                _LOGGER.debug("Error parsing COSEM object %d at pos %d: %s", i, pos, exc)
                break

        return ParseResult(success=True, objects=objects)

    def _parse_cosem_object(self, data: bytes, pos: int) -> tuple[DLMSObject | None, int]:
        """
        Parse one COSEM object entry from the push array.

        Expected structure(2):
          [0] octet-string(6) = OBIS code
          [1] structure(2):
                [0] structure(2): [int8 scaler, enum unit]
                [1] value (any type)
        Or simplified structure(2):
          [0] octet-string(6) = OBIS
          [1] value directly
        """
        if data[pos] != 0x02:
            # Not a structure - skip unknown type
            val, pos = self._decode_value(data, pos)
            return None, pos
        pos += 1  # skip structure tag
        count, pos = self._decode_length(data, pos)

        if count < 2:
            return None, pos

        # Element 0: OBIS code as octet-string
        if data[pos] != 0x09:
            val, pos = self._decode_value(data, pos)
            return None, pos
        pos += 1  # skip octet-string tag
        obis_len, pos = self._decode_length(data, pos)
        obis_raw = data[pos:pos+obis_len]
        pos += obis_len

        if len(obis_raw) != 6:
            # Skip remaining elements
            for _ in range(count - 1):
                _, pos = self._decode_value(data, pos)
            return None, pos

        obis_str = self._format_obis(obis_raw)

        # Element 1: value wrapper
        # Can be: structure(2)[scaler_unit, value] OR direct value
        scaler = 0
        unit_code = 255
        value = None

        if pos < len(data) and data[pos] == 0x02:
            # structure(2): [scaler_unit_struct, value]
            pos += 1  # skip structure tag
            inner_count, pos = self._decode_length(data, pos)

            if inner_count >= 2:
                # First inner: scaler+unit structure(2)[int8, enum]
                if pos < len(data) and data[pos] == 0x02:
                    pos += 1
                    su_count, pos = self._decode_length(data, pos)
                    if su_count >= 2:
                        raw_scaler, pos = self._decode_value(data, pos)
                        raw_unit, pos = self._decode_value(data, pos)
                        if isinstance(raw_scaler, int):
                            scaler = raw_scaler if raw_scaler < 128 else raw_scaler - 256
                        if isinstance(raw_unit, int):
                            unit_code = raw_unit
                    # skip extra
                    for _ in range(su_count - 2):
                        _, pos = self._decode_value(data, pos)
                else:
                    _, pos = self._decode_value(data, pos)

                # Second inner: actual value
                value, pos = self._decode_value(data, pos)

                # skip extra inner elements
                for _ in range(inner_count - 2):
                    _, pos = self._decode_value(data, pos)
            else:
                for _ in range(inner_count):
                    _, pos = self._decode_value(data, pos)
        else:
            # Direct value
            value, pos = self._decode_value(data, pos)

        # Skip any extra elements in the outer structure
        for _ in range(count - 2):
            _, pos = self._decode_value(data, pos)

        # Apply scaler
        if isinstance(value, int) and scaler != 0:
            final_value: Any = apply_scaler(value, scaler)
        elif isinstance(value, (bytes, bytearray)):
            try:
                final_value = value.decode("ascii", errors="replace").strip("\x00")
            except Exception:
                final_value = value.hex()
        else:
            final_value = value

        unit_str = self.UNIT_MAP.get(unit_code, "")
        meta = OBIS_DESCRIPTIONS.get(obis_str, {})

        return DLMSObject(
            obis=obis_str,
            value=final_value,
            unit=unit_str or meta.get("unit", ""),
            scaler=scaler,
        ), pos

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
        elif dtype == 0x16:  # enum = uint8
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
            _LOGGER.debug("Unknown DLMS type 0x%02X at pos %d, skipping", dtype, pos)
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
