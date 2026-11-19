# SPDX-License-Identifier: GPL-2.0-only
import importlib.util
import json
from pathlib import Path
import plistlib
import struct
import sys
import tempfile
import unittest
from types import SimpleNamespace
from unittest.mock import patch

tools = Path(__file__).resolve().parents[1] / "tools/p105"


def load(name):
    spec = importlib.util.spec_from_file_location(name, tools / (name + ".py"))
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


ios = load("capture_ios_counters")
with patch.dict(sys.modules, {"capture_ios_counters": ios}):
    compare = load("compare_ios_intervals")


def envelope(selector, raw=b"", flags=0):
    return struct.pack("<4I", selector, flags, len(raw), 0) + raw


def nandinfo(vfl_value=None, table_value=0, extra=b"odd"):
    raw = {tag: bytes([tag]) * width for tag, (_, width) in ios.DIRECT.items()}
    if vfl_value is not None:
        raw[255] = struct.pack("<Q", vfl_value) + raw[255][8:]
    table = struct.pack("<I", 45)
    for tag, words in ios.TABLE_LAYOUT:
        value = struct.pack("<Q", table_value) if tag == 1 else raw.get(tag, bytes(words * 4))
        table += struct.pack("<II", tag, words) + value
    geometry = bytearray(116)
    struct.pack_into("<5I", geometry, 48, 2, 2088, 8, 64, 128)
    return (envelope(ios.OPEN, flags=0x20001) + envelope(0x123, extra) +
            envelope(ios.GEOMETRY, geometry) + envelope(ios.TABLE, table) +
            b"".join(envelope(selector, raw[tag]) for tag, (selector, _) in ios.DIRECT.items()) +
            envelope(ios.CLOSE, flags=0x20001))


class IOSCounters(unittest.TestCase):
    def test_same_increments_across_wrap_and_different_starting_values(self):
        self.assertTrue(compare.same_changes(
            nandinfo(2**64 - 2, 10), nandinfo(1, 11),
            nandinfo(253, 100), nandinfo(256, 101),
        ))

    def test_full_reply_accounts_for_table_and_opaque_payload(self):
        first = nandinfo(0, 5), nandinfo(1, 6)
        for change, changed in (("table counter", nandinfo(21, 8)),
                                ("provider counter", nandinfo(22, 7)),
                                ("opaque payload", nandinfo(21, 7, extra=b"new"))):
            with self.subTest(change=change):
                self.assertFalse(compare.same_changes(*first, nandinfo(20, 6), changed))

    def test_interval_requires_consistent_valid_layout(self):
        good = nandinfo()
        longer = nandinfo(extra=b"longer")
        with self.assertRaisesRegex(ValueError, "different diagnostic layouts"):
            compare.same_changes(good, longer, good, good)
        with self.assertRaisesRegex(ValueError, "different diagnostic layouts"):
            compare.same_changes(good, good, longer, longer)
        with self.assertRaises(ValueError):
            compare.same_changes(good, good, good, good[:-1])

    def test_complete_odd_aligned_envelope(self):
        pair = ios.decode_nandinfo(nandinfo())
        self.assertEqual(pair, {255: b"\xff" * 88, 254: b"\xfe" * 464})

    def test_rejects_duplicate_trailing_or_truncated_envelopes(self):
        good = nandinfo()
        duplicate = envelope(ios.DIRECT[255][0], bytes(88))
        for bad in (good[16:], good[:-1], good + b"x", good[:-16] + duplicate + good[-16:],
                    good[:-16] + envelope(ios.OPEN, flags=0x20001) + good[-16:]):
            with self.subTest(size=len(bad)), self.assertRaises(ValueError):
                ios.decode_nandinfo(bad)

    def test_checks_every_layout_field_and_both_independent_payloads(self):
        good = nandinfo()
        for key, needle in (("table count", struct.pack("<III", 45, 1, 2)),
                            ("nested width", struct.pack("<II", 255, 22)),
                            ("direct VFL bytes", envelope(ios.DIRECT[255][0], b"\xff"*88)),
                            ("direct lower bytes", envelope(ios.DIRECT[254][0], b"\xfe"*464))):
            idx = good.index(needle) + (16 if "direct" in key else 0)
            changed = bytearray(good)
            changed[idx] ^= 0x01
            with self.subTest(key=key), self.assertRaises(ValueError):
                ios.decode_nandinfo(changed)

    def test_rejects_other_geometries_and_missing_geometry(self):
        good = nandinfo()
        start = good.index(struct.pack("<4I", ios.GEOMETRY, 0, 116, 0))
        with self.assertRaisesRegex(ValueError, "incomplete"):
            ios.decode_nandinfo(good[:start] + good[start + 16 + 116:])
        for field in range(5):
            data = bytearray(good)
            data[start + 16 + 48 + 4 * field] ^= 1
            with self.subTest(field=field), self.assertRaisesRegex(ValueError, "geometry"):
                ios.decode_nandinfo(data)

    def test_disconnect_keeps_successful_samples_and_marks_the_gap(self):
        responses = [b"iPad2,5", b"P105AP", b"13G36", plistlib.dumps(nandinfo()), ValueError("disconnected")]
        with tempfile.TemporaryDirectory() as tmp:
            dest = Path(tmp) / "private"
            args = SimpleNamespace(samples=2, interval=0, output=dest, udid="test", ideviceinfo="test")
            with patch.object(ios, "usb_query", side_effect=responses), self.assertRaisesRegex(ValueError,"disconnected"):
                ios.capture(args)
            self.assertEqual((dest/"sample-00000.bin").read_bytes(), nandinfo())
            self.assertEqual((dest/"sample-00000.bin").stat().st_mode & 0o777, 0o600)
            result = json.loads((dest/"run.json").read_text())
            self.assertEqual(result["samples"], 1)
            self.assertFalse(result["continuous_handoff"])
