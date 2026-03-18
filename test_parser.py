#!/usr/bin/env python3
"""
Standalone test / debug script for the DLMS parser and TCP listener.

Usage:
    # Parse a raw hex frame from the meter (paste from HA debug log):
    python3 test_parser.py --hex "7ea0...7e"

    # Live listen on TCP socket (forward output to terminal):
    python3 test_parser.py --host 192.168.1.100 --port 8899

    # Replay a saved binary capture file:
    python3 test_parser.py --file capture.bin
"""

import argparse
import asyncio
import sys
import os

# Allow running from repo root without installing
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "custom_components"))

from xt211_han.dlms_parser import DLMSParser, OBIS_DESCRIPTIONS


def print_result(result) -> None:
    if not result.success:
        print(f"  ❌ Parse error: {result.error}")
        return
    if not result.objects:
        print("  ⚠️  Frame OK but no DLMS objects extracted")
        return
    print(f"  ✅ {len(result.objects)} OBIS objects decoded:")
    for obj in result.objects:
        meta = OBIS_DESCRIPTIONS.get(obj.obis, {})
        name = meta.get("name", obj.obis)
        unit = obj.unit or meta.get("unit", "")
        print(f"     {obj.obis:25s}  {name:35s}  {obj.value} {unit}")


def test_hex(hex_str: str) -> None:
    """Parse a single hex-encoded frame."""
    raw = bytes.fromhex(hex_str.replace(" ", "").replace("\n", ""))
    print(f"\n📦 Frame: {len(raw)} bytes")
    parser = DLMSParser()
    parser.feed(raw)
    result = parser.get_frame()
    if result:
        print_result(result)
    else:
        print("  ⚠️  No complete frame found in data")


def test_file(path: str) -> None:
    """Parse all frames from a binary capture file."""
    with open(path, "rb") as f:
        data = f.read()
    print(f"\n📂 File: {path} ({len(data)} bytes)")
    parser = DLMSParser()
    parser.feed(data)
    count = 0
    while True:
        result = parser.get_frame()
        if result is None:
            break
        count += 1
        print(f"\n--- Frame #{count} ---")
        print_result(result)
    print(f"\nTotal frames parsed: {count}")


async def listen_tcp(host: str, port: int) -> None:
    """Connect to the TCP adapter and print decoded frames as they arrive."""
    print(f"\n🔌 Connecting to {host}:{port} ...")
    reader, writer = await asyncio.open_connection(host, port)
    print("   Connected. Waiting for DLMS PUSH frames (every ~60 s)...\n")
    parser = DLMSParser()
    frame_count = 0
    try:
        while True:
            chunk = await asyncio.wait_for(reader.read(4096), timeout=120)
            if not chunk:
                print("Connection closed by remote.")
                break
            parser.feed(chunk)
            while True:
                result = parser.get_frame()
                if result is None:
                    break
                frame_count += 1
                print(f"\n--- Frame #{frame_count}  raw: {result.raw_hex[:40]}... ---")
                print_result(result)
    except asyncio.TimeoutError:
        print("No data for 120 s, giving up.")
    finally:
        writer.close()


def main() -> None:
    parser = argparse.ArgumentParser(description="XT211 DLMS parser test tool")
    group = parser.add_mutually_exclusive_group(required=True)
    group.add_argument("--hex", help="Hex-encoded raw frame to parse")
    group.add_argument("--file", help="Binary capture file to parse")
    group.add_argument("--host", help="Adapter IP address for live TCP test")
    parser.add_argument("--port", type=int, default=8899, help="TCP port (default 8899)")
    args = parser.parse_args()

    if args.hex:
        test_hex(args.hex)
    elif args.file:
        test_file(args.file)
    elif args.host:
        asyncio.run(listen_tcp(args.host, args.port))


if __name__ == "__main__":
    main()
