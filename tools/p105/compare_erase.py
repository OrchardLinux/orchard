#!/usr/bin/env python3
# SPDX-License-Identifier: GPL-2.0-only
"""Compare saved P105 erase observations. This tool only reads local files.

Pass before.json and after.json. Each manifest describes its sibling .ppns file,
which concatenates 16-byte PPNS v1 headers and 16,448-byte raw page payloads.
The header contains magic, version, bus, CAU, status, row and payload length
(the last two are little-endian u32). Row bit 22 selects the SLC view.
Example manifest fields (use the corresponding phase and actual SHA-256):

  {"phase":"before", "device":"unit-A", "trial":"run-01", "source":"synthetic",
   "target":[0,2,600], "view":"normal", "sha256":"..."}

Use the same capture-local device and trial identifiers in both phases.
Source is "hardware" or "synthetic". Target is [bus, CAU, physical block].
Capture all pages of both views at the target, its immediate block neighbors
on that CAU, and the same block index on every CAU of both buses. Additional
complete blocks are also compared. A page already blank before the operation
cannot reveal an erase. The tool reports these pages as inclusive ranges
[bus, CAU, block, first page, last page] and exits 1 after printing the
available observations. Output describes only observed bytes and statuses.
Acquisition, command provenance and physical ownership require independent evidence.
"""
import argparse
import hashlib
import json
from pathlib import Path
import struct


HEADER = struct.Struct('<4sBBBBII')
RAW_SIZE = 16448
ERASED = b'\xff' * RAW_SIZE
PAGES = {'normal': 128, 'slc': 64}
ERASED_STATUS = 0x49
GOOD_STATUS = 0x40


def decode_record(record):
    if len(record) != HEADER.size + RAW_SIZE:
        raise ValueError('truncated PPNS record')
    magic, version, bus, cau, status, row, length = HEADER.unpack_from(record)
    if magic != b'PPNS' or version != 1 or length != RAW_SIZE:
        raise ValueError('invalid PPNS page header or payload length')
    block = (row >> 7) & 0xfff
    page = row & 0x7f
    view = 'slc' if row & (1 << 22) else 'normal'
    if (bus >= 2 or cau >= 8 or ((row >> 19) & 7) != cau or row >> 23
            or block >= 2088 or page >= PAGES[view]):
        raise ValueError('PPNS row outside the measured P105 geometry')
    location = (bus, cau, block, view, page)
    raw = record[HEADER.size:]
    if status not in (GOOD_STATUS, ERASED_STATUS) or (status == ERASED_STATUS and raw != ERASED):
        raise ValueError(f'{location}: uncertain status or invalid erased contents')
    digest = None if raw == ERASED else hashlib.sha256(raw).digest()
    return location, (status, digest)


def load_capture(path, phase):
    stream_path = path.with_suffix('.ppns')
    if not path.is_file() or not stream_path.is_file():
        raise ValueError(f'{phase} requires a regular manifest and PPNS file')
    manifest = json.loads(path.read_text())
    if not isinstance(manifest, dict) or manifest.get('phase') != phase:
        raise ValueError(f'expected the {phase} manifest')
    for key in ('device', 'trial'):
        if not isinstance(manifest.get(key), str) or not manifest[key].strip():
            raise ValueError(f'missing {key} identifier')
    if manifest.get('source') not in ('hardware', 'synthetic') or manifest.get('view') not in ('normal', 'slc'):
        raise ValueError('invalid source or erase view')
    target = manifest.get('target')
    if (not isinstance(target, list) or len(target) != 3 or
            not all(type(v) is int and 0 <= v < limit for v, limit in zip(target, (2, 8, 2088)))):
        raise ValueError('invalid erase target')
    stream_hash = hashlib.sha256()
    pages = {}
    with stream_path.open('rb') as stream:
        while record := stream.read(HEADER.size + RAW_SIZE):
            stream_hash.update(record)
            location, value = decode_record(record)
            if location in pages:
                raise ValueError(f'duplicate PPNS page {location}')
            pages[location] = value
    if not pages or stream_hash.hexdigest() != manifest.get('sha256'):
        raise ValueError(f'{phase} stream is empty or differs from its manifest')
    return manifest, pages


def compare(before, after, target, issued_view):
    if before.keys() != after.keys():
        raise ValueError('before and after cover different pages')
    bus, cau, block = target
    required = {(b, c, block) for b in range(2) for c in range(8)}
    required.update((bus, cau, n) for n in (block - 1, block + 1) if 0 <= n < 2088)
    captured = {key[:3] for key in before}
    missing = required - captured
    if missing:
        raise ValueError(f'missing target or collateral control {min(missing)}')
    changed = {view: [] for view in PAGES}
    stable_target = {view: [] for view in PAGES}
    blank_runs = {view: [] for view in PAGES}
    blank_pages = 0
    stable_controls = 0
    for cell in sorted(captured):
        for view, count in PAGES.items():
            runs = blank_runs[view]
            for page in range(count):
                key = (*cell, view, page)
                if key not in before:
                    raise ValueError(f'missing page {key}')
                old, new = before[key], after[key]
                if old[1] is None:
                    if old != new and (cell != target or new != (ERASED_STATUS, None)):
                        raise ValueError(f'{key}: unexpected change to blank page bytes or status')
                    blank_pages += 1
                    if runs and tuple(runs[-1][:3]) == cell and runs[-1][4] + 1 == page:
                        runs[-1][4] = page
                    else:
                        runs.append([*cell, page, page])
                elif cell == target and new == (ERASED_STATUS, None):
                    changed[view].append(page)
                elif old != new:
                    raise ValueError(f'{key}: unexpected change to bytes or status')
                elif cell == target and view == issued_view:
                    raise ValueError(f'{key}: addressed page retained its contents')
                elif cell == target:
                    stable_target[view].append(page)
                else:
                    stable_controls += 1
    return {'target': list(target), 'issued_view': issued_view,
            'observed_erased_pages': changed, 'unchanged_target_pages': stable_target,
            'unchanged_control_pages': stable_controls,
            'previously_blank_pages': blank_pages, 'previously_blank_ranges': blank_runs,
            'comparison': 'inconclusive' if blank_pages else 'all sampled pages accounted for',
            'sampled_blocks': len(captured)}


def main():
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument('before', type=Path)
    parser.add_argument('after', type=Path)
    args = parser.parse_args()
    try:
        first, before = load_capture(args.before, 'before')
        second, after = load_capture(args.after, 'after')
        if any(first[k] != second[k] for k in ('device', 'trial', 'source', 'target', 'view')):
            raise ValueError('manifests describe different devices, trials or operations')
        result = compare(before, after, tuple(first['target']), first['view'])
        result['declared_source'] = first['source']
    except (OSError, ValueError) as error:
        parser.exit(1, f'Inconclusive: {error}\n')
    print(json.dumps(result, indent=2))
    if result['previously_blank_pages']:
        parser.exit(1)


if __name__ == '__main__':
    main()
