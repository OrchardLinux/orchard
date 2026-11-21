#!/usr/bin/env python3
# SPDX-License-Identifier: GPL-2.0-only
"""Compare the full paired iOS flash replies from two controlled read intervals."""
import argparse
from pathlib import Path
import struct
import sys

from capture_ios_counters import MAX_PLIST, inspect_nandinfo


def same_changes(before_a, after_a, before_b, after_b):
    samples = (before_a, after_a, before_b, after_b)
    layouts = [(len(sample), inspect_nandinfo(sample)[1]) for sample in samples]
    if any(layout != layouts[0] for layout in layouts[1:]):
        raise ValueError("samples have different diagnostic layouts")

    # Replay each interval's changes against the other starting sample.
    # Headers and opaque properties keep every byte from that starting sample.
    predicted_a = bytearray(before_a)
    predicted_b = bytearray(before_b)
    word = struct.Struct("<Q")
    for start, end in layouts[0][1]:
        for offset in range(start, end, word.size):
            a0, a1, b0, b1 = (word.unpack_from(sample, offset)[0] for sample in samples)
            word.pack_into(predicted_a, offset, (a0 + (b1 - b0)) % (1 << 64))
            word.pack_into(predicted_b, offset, (b0 + (a1 - a0)) % (1 << 64))
    return predicted_a == after_a and predicted_b == after_b


def read_sample(path):
    with path.open("rb") as stream:
        data = stream.read(MAX_PLIST + 1)
    if len(data) > MAX_PLIST:
        raise ValueError("oversized NANDInfo sample")
    return data


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    for name in ("before_a", "after_a", "before_b", "after_b"):
        parser.add_argument(name, type=Path)
    args = parser.parse_args()
    try:
        same = same_changes(*(read_sample(path) for path in
                              (args.before_a, args.after_a, args.before_b, args.after_b)))
    except (OSError, ValueError) as exc:
        parser.exit(2, f"Could not compare iOS samples: {exc}\n")
    if same:
        print("The same counter increments reproduce both complete ending replies.")
        return 0
    print("The full replies contain different increments or changes outside the counters.")
    return 1


if __name__ == "__main__":
    sys.exit(main())
