# SPDX-License-Identifier: GPL-2.0-only
import importlib.util
from hashlib import sha256
import io
import json
from pathlib import Path
import tempfile
import unittest
import uuid

path = Path(__file__).resolve().parents[1] / "tools/p105/check_flash_trace.py"
spec = importlib.util.spec_from_file_location("check_flash_trace", path)
linux = importlib.util.module_from_spec(spec)
spec.loader.exec_module(linux)


def append_event(rows, req, bus, address, kind, op, value):
    seq = len(rows) + 1
    rows.append(f"task [000] 1.00: apple_h2fmi_transaction: dev=fmi seq={seq} req={req} "
                f"bus={bus} kind={kind} op={op:04x} row={address:08x} value={value}")


def request(rows, bus, op, actions, address=0, result=0):
    req = len(rows) + 1
    for kind, code, val in [(0, op, 0), *actions, (3, op, result)]:
        append_event(rows, req, bus, address, kind, code, val)


def read_actions(op, status, data=True):
    part = [(1, op, 0), (1, linux.OP_STATUS, 0), (2, linux.OP_STATUS, status)]
    if data:
        part += [(1, linux.TRANSFER, 0)]
    return [*part, (1, linux.FINISH, 0)]


def observed_launches(actions):
    rows = []
    for kind, code, value in actions:
        rows.append((kind, code, value))
        if kind == 1:
            rows.extend((4, code, control) for control in linux.LAUNCH_CONTROLS[code])
    return rows


def trace_fixture(launches=False):
    rows = []
    discover = read_actions(linux.GEOMETRY, 0x40)
    for bus in (0, 1):
        request(rows, bus, linux.GEOMETRY, observed_launches(discover) if launches else discover)
    return rows


class ControllerTrace(unittest.TestCase):
    def test_older_capture_lacks_cross_bus_launch_order(self):
        rows = trace_fixture()
        report = linux.verify(rows, "fmi", f"1 {len(rows)}")
        self.assertFalse(report["linux_launch_order_observed"])
        with self.assertRaisesRegex(ValueError, "no controller launch"):
            linux.verify(rows, "fmi", f"1 {len(rows)}", require_launches=True)

    def test_observed_launches_for_discovery_read_program_and_erase(self):
        rows = trace_fixture(launches=True)
        request(rows, 1, linux.READ, observed_launches(read_actions(linux.READ, 0x61, False)), 117)
        program = [(1, linux.PROGRAM, 0), (1, 0x17, 0), (1, linux.MUT_STATUS, 0),
                   (2, linux.MUT_STATUS, 0x40)]
        request(rows, 0, linux.PROGRAM, observed_launches(program), 118)
        erase = [(1, linux.ERASE, 0), (1, linux.MUT_STATUS, 0), (2, linux.MUT_STATUS, 0x40),
                 (1, linux.OP_STATUS, 0), (2, linux.OP_STATUS, 0x40), (1, linux.FINISH, 0)]
        request(rows, 1, linux.ERASE, observed_launches(erase), 119)
        timeline = io.StringIO()
        report = linux.verify(reversed(rows), "fmi", f"1 {len(rows)}", timeline=timeline)
        self.assertEqual(report["controller_launches"], 24)
        self.assertTrue(report["linux_launch_order_observed"])
        self.assertFalse(report["continuous_ios_to_linux_handoff"])
        events = [json.loads(line) for line in timeline.getvalue().splitlines()]
        self.assertEqual([event["sequence"] for event in events], sorted(event["sequence"] for event in events))
        self.assertEqual([e["value"] for e in events if e["event"] == "reply"],
                         [0x40, 0x40, 0x61, 0x40, 0x40, 0x40])

    def test_rejects_missing_repeated_and_wrong_launch_controls_even_with_contiguous_numbers(self):
        rows = []
        good = observed_launches(read_actions(linux.GEOMETRY, 0x40))
        request(rows, 0, linux.GEOMETRY, good)
        poll = (4, linux.OP_STATUS, 0x50)
        issue = good.index((4, linux.GEOMETRY, 0xb))
        poll_index = good.index(poll)
        cases = {
            "missing issue": good[:issue] + good[issue + 1:],
            "missing poll": good[:poll_index] + good[poll_index + 1:],
            "duplicate poll": good[:poll_index] + [poll] + good[poll_index:],
            "wrong control": good[:issue] + [(4, linux.GEOMETRY, 9)] + good[issue + 1:],
        }
        for name, bad in cases.items():
            invalid = rows.copy()
            request(invalid, 1, linux.GEOMETRY, bad)
            timeline = io.StringIO()
            with self.subTest(name=name), self.assertRaises(ValueError):
                linux.verify(invalid, "fmi", f"1 {len(invalid)}", timeline=timeline)
            self.assertEqual(timeline.getvalue(), "")

    def test_overlapping_buses_can_launch_in_reverse_preparation_order(self):
        rows = trace_fixture(launches=True)
        req0 = len(rows) + 1
        read = observed_launches(read_actions(linux.READ, 0x40))
        append_event(rows, req0, 0, 32, linux.BEGIN, linux.READ, 0)
        append_event(rows, req0, 0, 32, *read[0])
        request(rows, 1, linux.READ, read, address=33)
        for event in read[1:]:
            append_event(rows, req0, 0, 32, *event)
        append_event(rows, req0, 0, 32, linux.END_REQUEST, linux.READ, 0)
        output = io.StringIO()
        linux.verify(reversed(rows), "fmi", f"1 {len(rows)}", timeline=output)
        events = [json.loads(line) for line in output.getvalue().splitlines()]

        def read_buses(event):
            return [item["bus"] for item in events if item["event"] == event and item["opcode"] == linux.READ]

        self.assertEqual(read_buses("request_start"), [0, 1])
        self.assertEqual(read_buses("launch"), [1, 0])
        self.assertEqual(read_buses("request_end"), [1, 0])

    def test_geometry_status_only_program_and_erase_with_two_replies(self):
        rows = trace_fixture()
        request(rows, 1, linux.READ, read_actions(linux.READ, 0x61, False), 110)
        request(rows, 0, linux.PROGRAM, [(1, linux.PROGRAM, 0), (1, 0x17, 0),
                                         (1, linux.MUT_STATUS, 0), (2, linux.MUT_STATUS, 0x40)], 111)
        request(rows, 0, linux.ERASE, [(1, linux.ERASE, 0), (1, linux.MUT_STATUS, 0), (2, linux.MUT_STATUS, 0x40),
                                       (1, linux.OP_STATUS, 0), (2, linux.OP_STATUS, 0x40), (1, linux.FINISH, 0)], 112)
        receipt = linux.verify(reversed(rows), "fmi", f"1 {len(rows)}")
        self.assertEqual(receipt["completed_requests"], {"geometry": 2, "read": 1, "program": 1, "erase": 1})
        self.assertFalse(receipt["continuous_ios_to_linux_handoff"])

    def test_timeout_is_distinct_from_a_status_byte(self):
        rows = trace_fixture(launches=True)
        actions = [(1, linux.READ, 0), (1, linux.OP_STATUS, 0), (2, linux.OP_STATUS, -110),
                   (1, linux.FINISH, 0)]
        request(rows, 1, linux.READ, observed_launches(actions), result=-110)
        timeline = io.StringIO()
        report = linux.verify(rows, "fmi", f"1 {len(rows)}", timeline=timeline)
        self.assertEqual(report["error_returns"], {"-110": 1})
        self.assertEqual(report["poll_errors"], {"-110": 1})
        self.assertNotIn("-110", report["status_replies"])
        events = [json.loads(line) for line in timeline.getvalue().splitlines()]
        self.assertEqual([e["event"] for e in events if e["value"] == -110], ["poll_error", "request_end"])

    def test_failure_does_not_explain_wrong_commands_or_a_missing_poll_result(self):
        rows = trace_fixture(launches=True)
        for actions in ([(1, linux.ERASE, 0), (1, linux.FINISH, 0)],
                        [(1, linux.READ, 0), (1, linux.OP_STATUS, 0), (1, linux.FINISH, 0)]):
            bad = rows.copy()
            request(bad, 1, linux.READ, observed_launches(actions), result=-110)
            with self.subTest(actions=actions), self.assertRaises(ValueError):
                linux.verify(bad, "fmi", f"1 {len(bad)}", require_launches=True)

    def test_rejects_drops_at_the_beginning_middle_and_end(self):
        rows = trace_fixture()
        for i in (0, 7, len(rows) - 1):
            with self.subTest(i=i), self.assertRaises(ValueError):
                linux.verify(rows[:i] + rows[i + 1:], "fmi", f"1 {len(rows)}")

    def test_rejects_rebind_open_request_wrong_status_or_duplicate(self):
        rows = trace_fixture()
        wrong_status = [line.replace("kind=2 op=7d77", "kind=2 op=0079") for line in rows]
        for events, cut in ((rows, f"2 {len(rows)}"), (rows, f"1 {len(rows) - 1}"),
                            (rows + [rows[0]], f"1 {len(rows)}"), (wrong_status, f"1 {len(rows)}")):
            with self.subTest(cut=cut), self.assertRaises(ValueError):
                linux.verify(events, "fmi", cut)

    def test_an_operation_after_a_complete_cut_is_excluded(self):
        rows = trace_fixture()
        end = len(rows)
        request(rows, 1, linux.READ, read_actions(linux.READ, 0x49), 17)
        self.assertEqual(linux.verify(rows, "fmi", f"1 {end}")["completed_requests"], {"geometry": 2})

    def test_one_boot_transfer_checks_identity_and_detects_an_incomplete_stream(self):
        values = trace_fixture(launches=True)
        boot = str(uuid.uuid4())
        notes = b"test kernel"
        expected = sha256(notes).hexdigest()
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            paths = {"sys/kernel/notes": notes, "sys/firmware/devicetree/base/compatible": b"apple,p105ap\0",
                     "proc/sys/kernel/random/boot_id": boot.encode(),
                     "sys/kernel/tracing/events/apple_h2fmi/apple_h2fmi_transaction/enable": b"1\n",
                     "sys/bus/platform/drivers/apple-h2fmi/fmi/flash_trace_sequence": f"1 {len(values)}".encode(),
                     "sys/kernel/tracing/per_cpu/cpu0/stats": b"overrun: 0\n",
                     "sys/kernel/tracing/trace": ("\n".join(values) + "\n").encode()}
            for name, data in paths.items():
                path = root / name
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_bytes(data)
            output = io.StringIO()
            linux.stream_capture(expected, output, root / "sys", root / "proc")
            saved = output.getvalue()
            report = linux.verify_capture(io.StringIO(saved), expected)
            self.assertEqual(report["boot_id"], boot)
            for invalid in (saved.split(linux.END)[0], saved + "extra", saved.replace(boot, str(uuid.uuid4()), 1)):
                timeline = io.StringIO()
                with self.assertRaises(ValueError):
                    linux.verify_capture(io.StringIO(invalid), expected, timeline=timeline)
                self.assertEqual(timeline.getvalue(), "")
            with self.assertRaises(ValueError):
                linux.stream_capture("a" * 64, io.StringIO(), root / "sys", root / "proc")
