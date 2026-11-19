#!/usr/bin/env python3
# SPDX-License-Identifier: GPL-2.0-only
"""Capture the live SwissPPNFTL statistics from a paired 64 GB P105 on 13G36."""
import argparse
from datetime import datetime, timezone
from hashlib import sha256
import json
import os
from pathlib import Path
import plistlib
import struct
import subprocess
import sys
import time


# Record numbers, flags and sizes as returned by 13G36, little endian.
OPEN, CLOSE = 0x30000100, 0x3000ff00
TABLE = 0x01000200
GEOMETRY = 0x03000F00
P105_GEOMETRY = (2, 2088, 8, 64, 128)  # buses, blocks/CAU, CAUs/bus, SLC and normal pages
DIRECT = {255: (0x02000200, 88), 254: (0x03000200, 464)}
TABLE_LAYOUT = [(i, 2) for i in range(1, 44)] + [(255, 22), (254, 116)]
MAX_PLIST = 1024 * 1024


def decode_nandinfo(blob):
    """Require one complete envelope, the measured table, and matching direct records."""
    wanted = {TABLE, GEOMETRY, *(selector for selector, _ in DIRECT.values())}
    chunks = {}
    offset = 0
    closed = False
    while offset < len(blob):
        if closed or len(blob) - offset < 16:
            raise ValueError("trailing or incomplete envelope header")
        selector, flags, size, reserved = struct.unpack_from("<4I", blob, offset)
        if offset == 0 and selector != OPEN:
            raise ValueError("unrecognized NANDInfo envelope")
        start = offset + 16
        end = start + size
        if end > len(blob) or reserved:
            raise ValueError("invalid envelope length or reserved field")
        if selector in (OPEN, CLOSE):
            if size or flags != 0x20001 or (selector == OPEN and offset != 0):
                raise ValueError("invalid envelope boundary")
            closed = selector == CLOSE
        if selector in wanted:
            if flags or selector in chunks:
                raise ValueError("flagged or repeated counter record")
            chunks[selector] = blob[start:end]
        offset = end
    if not closed or set(chunks) != wanted:
        raise ValueError("incomplete NANDInfo counter envelope")
    geometry = chunks[GEOMETRY]
    if len(geometry) != 116 or struct.unpack_from("<5I", geometry, 48) != P105_GEOMETRY:
        raise ValueError("unexpected flash geometry for the 64 GB P105")
    table = chunks[TABLE]
    if len(table) < 4 or struct.unpack_from("<I", table)[0] != len(TABLE_LAYOUT):
        raise ValueError("unexpected SwissPPNFTL table count")
    offset = 4
    result = {}
    for tag, words in TABLE_LAYOUT:
        start = offset + 8
        end = start + words * 4
        if end > len(table) or struct.unpack_from("<II", table, offset) != (tag, words):
            raise ValueError("unexpected SwissPPNFTL table layout")
        if tag in DIRECT:
            selector, size = DIRECT[tag]
            raw = chunks[selector]
            if len(raw) != size or raw != table[start:end]:
                raise ValueError(f"direct and table copies of record {tag} disagree")
            result[tag] = raw
        offset = end
    if offset != len(table):
        raise ValueError("trailing SwissPPNFTL table bytes")
    return result


def usb_query(tool, udid, *args):
    request = subprocess.run([tool, "-u", udid, *args], capture_output=True, timeout=10)
    if request.returncode or not request.stdout:
        raise ValueError("paired iOS query unavailable")
    if len(request.stdout) > MAX_PLIST:
        raise ValueError("oversized iOS response")
    return request.stdout


def timestamp():
    return datetime.now(timezone.utc).isoformat(timespec="microseconds")


def save_private(path, data):
    with os.fdopen(os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600), "wb") as stream:
        stream.write(data)


def save_json(path, obj):
    save_private(path, (json.dumps(obj, sort_keys=True, indent=2) + "\n").encode())


def capture(args):
    if args.samples < 1 or args.samples > 10000 or not 0 <= args.interval <= 60:
        raise ValueError("choose 1 to 10000 samples and a 0 to 60 second interval")
    for key, expected in (("ProductType", "iPad2,5"), ("HardwareModel", "p105ap"), ("BuildVersion", "13G36")):
        value = usb_query(args.ideviceinfo, args.udid, "-k", key).decode().strip()
        if value.lower() != expected.lower():
            raise ValueError(f"unexpected {key} for this profile")
    args.output.mkdir(mode=0o700)
    count = 0
    error = None
    try:
        for n in range(args.samples):
            if n:
                time.sleep(args.interval)
            wall = timestamp()
            start = time.monotonic_ns()
            data = usb_query(args.ideviceinfo, args.udid, "-q", "com.apple.disk_usage", "-k", "NANDInfo", "-x")
            end = time.monotonic_ns()
            received = timestamp()
            raw = plistlib.loads(data)
            if not isinstance(raw, bytes):
                raise ValueError("NANDInfo is not a data property")
            pair = decode_nandinfo(raw)
            stem = f"sample-{n:05d}"
            save_private(args.output / (stem + ".bin"), raw)
            save_json(args.output / (stem + ".json"), {
                "requested_utc": wall, "received_utc": received, "start_monotonic_ns": start,
                "end_monotonic_ns": end, "product": "iPad2,5", "build": "13G36",
                "nandinfo_sha256": sha256(raw).hexdigest(),
                "provider_sha256": {tag: sha256(buf).hexdigest() for tag, buf in pair.items()},
            })
            count += 1
    except (ValueError, OSError, KeyboardInterrupt, subprocess.TimeoutExpired, plistlib.InvalidFileException) as exc:
        # A connection loss is an observation gap even if the next OS comes up.
        error = str(exc) if isinstance(exc, (ValueError, OSError)) else type(exc).__name__
        raise
    finally:
        save_json(args.output / "run.json", {
            "samples": count, "requested": args.samples, "error": error, "finished_utc": timestamp(),
            "coverage": "iOS USB snapshots", "requested_series_complete": count == args.samples,
            "continuous_handoff": False,
        })
    print(f"Saved {count} iOS snapshots with matching provider records.")


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--ideviceinfo", default="ideviceinfo")
    parser.add_argument("--udid", required=True, help="paired P105 identifier from idevice_id -l")
    parser.add_argument("--samples", type=int, default=1)
    parser.add_argument("--interval", type=float, default=0, help="seconds between completed queries")
    parser.add_argument("output", type=Path, help="new private directory on the host")
    args = parser.parse_args()
    try:
        capture(args)
    except (ValueError, OSError, subprocess.TimeoutExpired, plistlib.InvalidFileException) as exc:
        print(f"Capture stopped: {type(exc).__name__ if isinstance(exc, subprocess.TimeoutExpired) else exc}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
