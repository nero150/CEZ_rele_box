#!/usr/bin/env python3
"""
Standalone test / debug script for the DLMS parser and TCP listener.
"""

import argparse
import asyncio
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "custom_components"))

from xt211_han.dlms_parser import DLMSParser, OBIS_DESCRIPTIONS


def print_result(result) -> None:
    if not result.success:
        print(f"  Parse error: {result.error}")
        return
    if not result.objects:
        print("  Frame OK but no DLMS objects extracted")
        return
    print(f"  {len(result.objects)} OBIS objects decoded:")
    for obj in result.objects:
        meta = OBIS_DESCRIPTIONS.get(obj.obis, {})
        name = meta.get("name", obj.obis)
        unit = obj.unit or meta.get("unit", "")
        print(f"     {obj.obis:25s}  {name:35s}  {obj.value} {unit}")


def test_hex(hex_str: str) -> None:
    raw = bytes.fromhex(hex_str.replace(" ", "").replace("\n", ""))
    print(f"\nFrame: {len(raw)} bytes")
    parser = DLMSParser()
    parser.feed(raw)
    result = parser.get_frame()
    if result:
        print_result(result)
    else:
        print("  No complete frame found in data")


async def listen_tcp(host: str, port: int) -> None:
    print(f"\nConnecting to {host}:{port} ...")
    reader, writer = await asyncio.open_connection(host, port)
    print("Connected. Waiting for DLMS PUSH frames...\n")
    parser = DLMSParser()
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
                print_result(result)
    except asyncio.TimeoutError:
        print("No data for 120 s, giving up.")
    finally:
        writer.close()


def main() -> None:
    parser = argparse.ArgumentParser(description="XT211 DLMS parser test tool")
    group = parser.add_mutually_exclusive_group(required=True)
    group.add_argument("--hex", help="Hex-encoded raw frame to parse")
    group.add_argument("--host", help="Adapter IP address for live TCP test")
    parser.add_argument("--port", type=int, default=8899, help="TCP port (default 8899)")
    args = parser.parse_args()

    if args.hex:
        test_hex(args.hex)
    elif args.host:
        asyncio.run(listen_tcp(args.host, args.port))


if __name__ == "__main__":
    main()
