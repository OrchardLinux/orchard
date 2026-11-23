#!/usr/bin/env python3
# SPDX-License-Identifier: GPL-2.0-only
"""Verify a P105 H2FMI trace from the first geometry request to a measured cut."""
import argparse
from collections import Counter
from dataclasses import dataclass, field
from hashlib import sha256
import json
from pathlib import Path
import re
import shutil
import sys
import uuid


EVENT = "apple_h2fmi_transaction:"
FIELDS = re.compile(r"dev=(\S+) seq=(\d+) req=(\d+) bus=(\d+) kind=(\d+) "
                    r"op=([0-9a-f]{4}) row=([0-9a-f]{8}) value=(-?\d+)")
GEOMETRY, READ, PROGRAM, ERASE = 0x9792, 0x370a, 0x008a, 0xd76a
OP_STATUS, MUT_STATUS, TRANSFER, FINISH = 0x7d77, 0x79, 0x7a, 0x77
OPERATIONS = {GEOMETRY: "geometry", READ: "read", PROGRAM: "program", ERASE: "erase"}
BEGIN, STAGE, REPLY, END_REQUEST, LAUNCH = range(5)
TIMELINE_EVENTS = {BEGIN: "request_start", END_REQUEST: "request_end", LAUNCH: "launch", REPLY: "reply"}
# Nonzero FMC_RW_CTRL writes for each staged opcode. A status command
# first runs the command, then starts the FMC's automatic status poll.
LAUNCH_CONTROLS = {
    GEOMETRY: (0xb,), READ: (0xb,), ERASE: (0xb,), PROGRAM: (9,),
    OP_STATUS: (3, 0x50), MUT_STATUS: (1, 0x50),
    TRANSFER: (1,), FINISH: (1,), 0x17: (1,), 0x70: (1,),
}
START = "# orchard-p105-start "
END = "# orchard-p105-end "


@dataclass
class Request:
    start: int
    operation: int
    row: int
    commands: list = field(default_factory=list)
    replies: dict = field(default_factory=dict)
    launches: list = field(default_factory=list)


def kernel_identity(sysfs, procfs, expected):
    digest = sha256((sysfs / "kernel/notes").read_bytes()).hexdigest()
    if digest != expected:
        raise ValueError("running kernel notes differ from the selected diagnostic build")
    compatible = (sysfs / "firmware/devicetree/base/compatible").read_bytes().split(b"\0")
    if b"apple,p105ap" not in compatible:
        raise ValueError("unexpected board")
    boot = uuid.UUID((procfs / "sys/kernel/random/boot_id").read_text().strip())
    return {"kernel_notes_sha256": digest, "boot_id": str(boot)}


def stream_capture(expected, output, sysfs=Path("/sys"), procfs=Path("/proc")):
    identity = kernel_identity(sysfs, procfs, expected)
    tracing = sysfs / "kernel/tracing"
    enabled = tracing / "events/apple_h2fmi/apple_h2fmi_transaction/enable"
    if enabled.read_text().strip() != "1":
        raise ValueError("controller tracepoint is disabled")
    boundaries = list((sysfs / "bus/platform/drivers/apple-h2fmi").glob("*/flash_trace_sequence"))
    if len(boundaries) != 1:
        raise ValueError("expected one bound H2FMI device")
    boundary = boundaries[0]
    device = boundary.parent.name
    cut = boundary.read_text().strip()
    if not re.fullmatch(r"1 [1-9][0-9]*", cut):
        raise ValueError("controller capture did not start at its first probe")
    output.write(START + json.dumps({**identity, "device": device, "boundary": cut}) + "\n")
    with (tracing / "trace").open() as stream:
        shutil.copyfileobj(stream, output)
    later = boundary.read_text().split()
    if (len(later) != 2 or later[0] != "1" or int(later[1]) < int(cut.split()[1])
            or kernel_identity(sysfs, procfs, expected) != identity):
        raise ValueError("boot or controller changed during transfer")
    stats = {p.parent.name: p.read_text() for p in sorted((tracing / "per_cpu").glob("cpu*/stats"))}
    output.write(END + json.dumps({**identity, "device": device, "later_sequence": int(later[1]),
                                   "tracefs_cpu_stats": stats}) + "\n")


def verify_capture(stream, expected, *, require_launches=False, timeline=None):
    first = stream.readline()
    if not first.startswith(START):
        raise ValueError("missing session header")
    meta = json.loads(first[len(START):])
    if meta["kernel_notes_sha256"] != expected or str(uuid.UUID(meta["boot_id"])) != meta["boot_id"]:
        raise ValueError("unexpected kernel or boot identity")

    def body():
        for line in stream:
            if line.startswith(END):
                tail = json.loads(line[len(END):])
                same = all(meta[k] == tail[k] for k in ("kernel_notes_sha256", "boot_id", "device"))
                if not same or tail["later_sequence"] < int(meta["boundary"].split()[1]) or stream.read(1):
                    raise ValueError("session changed or extra data follows the receipt")
                return
            if line.startswith(START):
                raise ValueError("second session header")
            yield line
        raise ValueError("missing session footer")

    result = verify(body(), meta["device"], meta["boundary"],
                    require_launches=require_launches, timeline=timeline)
    result.update({k: meta[k] for k in ("kernel_notes_sha256", "boot_id", "device")})
    return result


def expected_sequence(op, replies):
    if op in (GEOMETRY, READ):
        if len(replies) != 1 or replies[0] < 0:
            raise ValueError("successful read missing its status byte")
        transfers = (0x40,) if op == GEOMETRY else (0x40, 0x42, 0x49)
        data = [TRANSFER] if replies[0] in transfers else []
        return [op, OP_STATUS, *data, FINISH]
    if op == PROGRAM:
        if len(replies) != 1 or replies[0] < 0 or not replies[0] & 0x40 or replies[0] & 0x11:
            raise ValueError("successful program missing its ready status")
        return [PROGRAM, 0x17, MUT_STATUS]
    if replies != [0x40, 0x40]:
        raise ValueError("successful erase missing its queue or completion reply")
    return [ERASE, MUT_STATUS, OP_STATUS, FINISH]


def check_completion(request, result, has_launches):
    commands = request.commands
    values = list(request.replies.values())
    op = request.operation
    if has_launches:
        for index, (code, count) in enumerate(zip(commands, request.launches)):
            if count < 1:
                raise ValueError("staged opcode missing its primary launch")
            if code in (MUT_STATUS, OP_STATUS) and (count == 2) != (index in request.replies):
                raise ValueError("status poll and result disagree")
    if result == 0:
        if commands != expected_sequence(op, values):
            raise ValueError("successful request command sequence differs from the driver")
        if op == GEOMETRY and values != [0x40]:
            raise ValueError("invalid discovery response")
        return
    if op == GEOMETRY:
        raise ValueError("failed package discovery")
    if not commands:
        if result != -5:
            raise ValueError("unexpected result before the first command")
        return
    if op == READ:
        # Reads always issue FINISH after asserting chip enable, including on timeout.
        if commands[-1] != FINISH:
            raise ValueError("failed read missing its cleanup command")
        prefix = commands[:-1]
        path = [READ, OP_STATUS, TRANSFER]
        if TRANSFER in prefix and values not in ([0x40], [0x42], [0x49]):
            raise ValueError("data transfer without a matching read status")
    else:
        prefix = commands
        path = [PROGRAM, 0x17, MUT_STATUS] if op == PROGRAM else [ERASE, MUT_STATUS, OP_STATUS, FINISH]
    if not prefix or prefix != path[:len(prefix)]:
        raise ValueError("failed request command sequence differs from the driver")
    for index, code in enumerate(prefix[:-1]):
        if code in (OP_STATUS, MUT_STATUS) and request.replies.get(index, -1) < 0:
            raise ValueError("operation continued past an unfinished status poll")


def verify(lines, device, boundary, *, require_launches=False, timeline=None):
    fields = boundary.split()
    if len(fields) != 2 or fields[0] != "1" or not fields[1].isascii() or not fields[1].isdigit():
        raise ValueError("need the sequence from the first driver probe")
    limit = int(fields[1])
    if limit <= 0:
        raise ValueError("empty controller trace")
    rows = {}
    for line in lines:
        if EVENT not in line:
            continue
        match = FIELDS.fullmatch(line.split(EVENT, 1)[1].strip())
        if not match:
            raise ValueError("malformed controller event")
        dev, seq, req, bus, kind, op, row, val = match.groups()
        if dev != device:
            continue
        seq = int(seq)
        if seq == 0 or seq in rows:
            raise ValueError("invalid or repeated controller sequence")
        if seq <= limit:
            rows[seq] = (int(req), int(bus), int(kind), int(op, 16), int(row, 16), int(val))
    if len(rows) != limit:
        raise ValueError(f"trace contains {len(rows)} of {limit} required events")
    has_launches = any(kind == LAUNCH for _, _, kind, _, _, _ in rows.values())
    if (require_launches or timeline is not None) and not has_launches:
        raise ValueError("this capture has no controller launch events")
    active = {}
    counts, errors, status, poll_errors = Counter(), Counter(), Counter(), Counter()
    opened = launch_count = 0
    for seq in range(1, limit + 1):
        req, bus, kind, op, row, val = rows[seq]
        if bus not in (0, 1):
            raise ValueError("unexpected bus")
        if kind == BEGIN:
            if bus in active or req != seq or op not in OPERATIONS or val:
                raise ValueError("invalid request start")
            if opened < 2 and (bus != opened or op != GEOMETRY or row or active):
                raise ValueError("trace must start with both package geometry requests")
            opened += 1
            active[bus] = Request(req, op, row)
            continue
        request = active.get(bus)
        if request is None or request.start != req or request.row != row:
            raise ValueError("orphaned event or address change within a request")
        commands, launches = request.commands, request.launches
        if kind == STAGE:
            if op not in LAUNCH_CONTROLS or val:
                raise ValueError("unknown command-register event")
            commands.append(op)
            launches.append(0)
        elif kind == LAUNCH:
            if not commands or commands[-1] != op:
                raise ValueError("launch without its staged opcode")
            controls = LAUNCH_CONTROLS[op]
            index = launches[-1]
            if index >= len(controls) or controls[index] != val:
                raise ValueError("unexpected or repeated controller launch")
            launches[-1] += 1
            launch_count += 1
        elif kind == REPLY:
            index = len(commands) - 1
            if (op not in (MUT_STATUS, OP_STATUS) or not commands or commands[-1] != op or
                    index in request.replies or not -4095 <= val <= 255):
                raise ValueError("status without a matching command")
            if has_launches and launches[-1] != len(LAUNCH_CONTROLS[op]):
                raise ValueError("status arrived before the poll launch")
            request.replies[index] = val
            if val < 0:
                poll_errors[str(val)] += 1
            else:
                status[str(val)] += 1
        elif kind == END_REQUEST:
            if op != request.operation or not -4095 <= val <= 0:
                raise ValueError("invalid request completion")
            check_completion(request, val, has_launches)
            if val:
                errors[str(val)] += 1
            counts[OPERATIONS[op]] += 1
            del active[bus]
        else:
            raise ValueError("unknown event kind")
    if active or counts["geometry"] != 2:
        raise ValueError("in-flight requests at the cut or incomplete discovery")
    if timeline is not None:
        for seq in range(1, limit + 1):
            req, bus, kind, op, row, val = rows[seq]
            if kind == STAGE:
                continue
            name = "poll_error" if kind == REPLY and val < 0 else TIMELINE_EVENTS[kind]
            event = {"sequence": seq, "request": req, "bus": bus, "opcode": op, "row": row,
                     "event": name, "value": val}
            timeline.write(json.dumps(event) + "\n")
    return {"controller_events": limit, "completed_requests": dict(counts), "error_returns": dict(errors),
            "status_replies": dict(status), "poll_errors": dict(poll_errors),
            "linux_controller_prefix_complete": True,
            "linux_launch_order_observed": has_launches, "controller_launches": launch_count,
            "native_provider_grouping_verified": False, "continuous_ios_to_linux_handoff": False}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    subs = parser.add_subparsers(dest="command", required=True)
    take = subs.add_parser("capture", help="stream live P105 evidence to standard output")
    check = subs.add_parser("verify", help="verify the saved session on the host")
    check.add_argument("trace", type=Path)
    check.add_argument("--require-launches", action="store_true",
                       help="require the recorder that observes both controllers' I/O launches")
    check.add_argument("--timeline", action="store_true",
                       help="export ordered requests, launches and poll results as JSON Lines with a final summary")
    for sub in (take, check):
        sub.add_argument("--kernel-notes", required=True, help="expected SHA-256 of the diagnostic kernel notes")
    args = parser.parse_args()
    try:
        if not re.fullmatch("[0-9a-f]{64}", args.kernel_notes):
            raise ValueError("expected a lowercase SHA-256 for --kernel-notes")
        if args.command == "capture":
            stream_capture(args.kernel_notes, sys.stdout)
            return
        with args.trace.open() as stream:
            report = verify_capture(stream, args.kernel_notes, require_launches=args.require_launches,
                                    timeline=sys.stdout if args.timeline else None)
    except (ValueError, OSError, TypeError, KeyError, IndexError) as exc:
        parser.exit(1, f"Capture incomplete: {exc}\n")
    if args.timeline:
        print(json.dumps({"summary": report}))
    else:
        print(json.dumps(report, sort_keys=True, indent=2))


if __name__ == "__main__":
    sys.exit(main())
