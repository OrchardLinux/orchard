# SPDX-License-Identifier: GPL-2.0-only
import hashlib
import io
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from tools.p105 import compare_erase as evidence


TARGET = (1, 2, 600)
PROGRAMMED = (0x40, b'witness')
BLANK = (0x49, None)


def sample():
    cells = {(b, c, 600) for b in range(2) for c in range(8)}
    cells.update({(1, 2, 599), (1, 2, 601)})
    return {(*cell, view, page): PROGRAMMED for cell in cells
            for view, count in evidence.PAGES.items() for page in range(count)}


def record(*, row=(1 << 22) | (2 << 19) | (600 << 7), status=0x40, raw=bytes(evidence.RAW_SIZE)):
    return evidence.HEADER.pack(b'PPNS', 1, 1, 2, status, row, len(raw)) + raw


class EraseComparison(unittest.TestCase):
    def setUp(self):
        self.before = sample()
        self.after = self.before.copy()
        for page in range(128):
            self.after[(*TARGET, 'normal', page)] = BLANK

    def compare(self):
        return evidence.compare(self.before, self.after, TARGET, 'normal')

    def test_reports_the_sampled_aliases_and_controls(self):
        self.after[(*TARGET, 'slc', 15)] = BLANK
        result = self.compare()
        self.assertEqual(result['observed_erased_pages'], {'normal': list(range(128)), 'slc': [15]})
        self.assertEqual(result['unchanged_control_pages'], 17 * 192)
        self.assertEqual(result['sampled_blocks'], 18)

    def test_slc_issued_erase_compares_the_normal_alias(self):
        self.after = self.before.copy()
        for page in range(64):
            self.after[(*TARGET, 'slc', page)] = BLANK
        result = evidence.compare(self.before, self.after, TARGET, 'slc')
        self.assertEqual(result['observed_erased_pages'], {'normal': [], 'slc': list(range(64))})

    def test_reports_blank_ranges_and_separates_measured_transitions(self):
        blanks = [(1, 2, 599, 'slc', p) for p in (0, 1, 4)]
        blanks += [(*TARGET, 'normal', 6), (*TARGET, 'slc', 63)]
        for key in blanks:
            self.before[key] = self.after[key] = BLANK
        self.before[(*TARGET, 'normal', 6)] = (0x40, None)
        result = self.compare()
        self.assertEqual(result['comparison'], 'inconclusive')
        self.assertEqual(result['previously_blank_pages'], 5)
        self.assertEqual(result['previously_blank_ranges'],
                         {'normal': [[1, 2, 600, 6, 6]],
                          'slc': [[1, 2, 599, 0, 1], [1, 2, 599, 4, 4], [1, 2, 600, 63, 63]]})
        self.assertEqual(result['observed_erased_pages']['normal'], [p for p in range(128) if p != 6])
        self.assertEqual(result['unchanged_target_pages']['slc'], list(range(63)))
        self.assertEqual(result['unchanged_control_pages'], 17 * 192 - 3)
        self.after[(1, 2, 599, 'slc', 4)] = PROGRAMMED
        with self.assertRaisesRegex(ValueError, 'unexpected change to blank page'):
            self.compare()

    def test_empty_capture_with_valid_coverage_prints_observations_and_exits_one(self):
        blank = dict.fromkeys(self.before, BLANK)
        manifest = dict(device='unit-A', trial='empty', source='synthetic', target=list(TARGET), view='normal')
        with patch('sys.argv', ['compare_erase.py', 'before.json', 'after.json']), \
                patch.object(evidence, 'load_capture', return_value=(manifest, blank)), \
                patch('sys.stdout', new_callable=io.StringIO) as output, self.assertRaises(SystemExit) as err:
            evidence.main()
        self.assertEqual(err.exception.code, 1)
        result = json.loads(output.getvalue())
        self.assertEqual(result['previously_blank_pages'], 18 * 192)
        self.assertEqual(result['unchanged_control_pages'], 0)
        self.assertEqual(result['observed_erased_pages'], {'normal': [], 'slc': []})

    def test_mismatched_or_incomplete_comparisons_and_unexpected_changes_fail(self):
        for fault, message in [('missing phase page', 'different pages'), ('both missing', 'missing page'),
                               ('lost neighbor', 'missing target or collateral'),
                               ('collateral', 'unexpected change'), ('missed erase', 'retained its contents')]:
            before, after = self.before.copy(), self.after.copy()
            guard = (1, 2, 599, 'slc', 63)
            with self.subTest(fault=fault):
                if fault == 'missing phase page':
                    del after[guard]
                elif fault == 'both missing':
                    del before[guard]
                    del after[guard]
                elif fault == 'lost neighbor':
                    before = {k: v for k, v in before.items() if k[:3] != guard[:3]}
                    after = {k: v for k, v in after.items() if k[:3] != guard[:3]}
                elif fault == 'collateral':
                    after[guard] = BLANK
                else:
                    after[(*TARGET, 'normal', 127)] = PROGRAMMED
                with self.assertRaisesRegex(ValueError, message):
                    evidence.compare(before, after, TARGET, 'normal')

    def test_checks_raw_header_coordinates_status_data_and_oob(self):
        key, original = evidence.decode_record(record())
        self.assertEqual(key, (*TARGET, 'slc', 0))
        raw = bytearray(evidence.RAW_SIZE)
        raw[1024] = 1  # First chunk's out-of-band field.
        self.assertNotEqual(evidence.decode_record(record(raw=raw))[1], original)
        for raw_record in (record()[:-1], record(row=0), record(row=(1 << 22) | (2 << 19) | 64),
                           record(status=0x42), record(status=0x49)):
            with self.assertRaises(ValueError):
                evidence.decode_record(raw_record)
        self.assertEqual(evidence.decode_record(record(status=0x49, raw=evidence.ERASED))[1], BLANK)

    def test_checks_stream_digest_and_rejects_duplicate_coordinates(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / 'before.json'
            data = record()
            manifest = dict(phase='before', device='unit-A', trial='trial-1', source='synthetic',
                            target=list(TARGET), view='normal', sha256=hashlib.sha256(data).hexdigest())
            path.write_text(json.dumps(manifest))
            stream = path.with_suffix('.ppns')
            stream.write_bytes(data)
            self.assertEqual(len(evidence.load_capture(path, 'before')[1]), 1)
            with self.assertRaisesRegex(ValueError, 'expected the after'):
                evidence.load_capture(path, 'after')
            stream.write_bytes(record(raw=b'\x01' * evidence.RAW_SIZE))
            with self.assertRaisesRegex(ValueError, 'differs from its manifest'):
                evidence.load_capture(path, 'before')
            stream.write_bytes(data + data)
            with self.assertRaisesRegex(ValueError, 'duplicate'):
                evidence.load_capture(path, 'before')
