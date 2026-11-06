# SPDX-License-Identifier: GPL-2.0-only
"""Host-only checks for the pinned iOS launcher producer."""
import copy
import hashlib
import json
from pathlib import Path
import shutil
import struct
import subprocess
import sys
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import patch

from orchard import hardware
from orchard.apple import ios_launcher, ios_kloader


class RecipeTests(unittest.TestCase):
    def setUp(self):
        self.profile = ios_launcher.boot_profile('p105', root=hardware.ROOT)

    def test_profile_has_exact_prepared_firmware_and_thumb_entries(self):
        self.assertEqual(self.profile['machine'], 'iPad2,5')
        self.assertEqual(self.profile['prepared_ibec_bytes'], 282624)
        self.assertEqual(self.profile['prepared_ibec_sha256'],
                         'e987b101800bedf03c086aa55854ff594fcce1b0f55ea1424f3ccf180a40abce')
        self.assertEqual(self.profile['ibec_boot_path'], '/boot' + self.profile['boot_path'])
        self.assertTrue(all(self.profile[k] & 1 for k in ('finish_init', 'mount_bootfs', 'load_file')))

    def test_other_board_is_refused(self):
        with self.assertRaisesRegex(ValueError, 'iOS local-file'):
            ios_launcher.boot_profile('j71b', root=hardware.ROOT)

    def test_address_and_string_bounds(self):
        for key, value in (('shim_end', self.profile['prepared_ibec_bytes'] + 1),
                           ('finish_init', self.profile['finish_init'] & ~1),
                           ('load_address', self.profile['base']), ('wake_delay_seconds', 0),
                           ('boot_path', '/boot/../private/file'), ('ibec_path', 'relative'),
                           ('machine', 'iPad2,5\n'), ('prepared_ibec_bytes', True)):
            with self.subTest(key=key):
                p = dict(self.profile, **{key: value})
                with self.assertRaises(ValueError):
                    ios_launcher.validate_profile(p)

    def test_native_configuration_uses_selected_data(self):
        p = dict(self.profile, wake_delay_seconds=90, machine='Synthetic1,1')
        header = ios_launcher.launcher_config(p, 'A "test"')
        self.assertIn('#define TARGET_MACHINE "Synthetic1,1"', header)
        self.assertIn('#define WAKE_DELAY_SECONDS 90', header)
        self.assertIn('#define LAUNCHER_TITLE @"A \\"test\\""', header)
        self.assertIn('90 seconds.', header)

    def test_wrong_firmware_is_refused_before_output_or_compiler(self):
        with TemporaryDirectory() as d:
            d = Path(d)
            (d / 'ibec').write_bytes(b'wrong')
            (d / 'bundle').write_bytes(bytes.fromhex('c0000cf1') + bytes(40000))
            with patch.object(ios_launcher.subprocess, 'run') as run:
                with self.assertRaisesRegex(ValueError, 'exact supported image'):
                    ios_launcher.build_shim(d / 'ibec', d / 'bundle', d / 'out', self.profile)
                run.assert_not_called()
                self.assertFalse((d / 'out').exists())

    def test_patch_changes_only_hook_and_stub(self):
        p = copy.deepcopy(self.profile)
        original = bytearray(p['prepared_ibec_bytes'])
        hook = p['hook']
        original[hook:hook + 4] = ios_launcher.thumb_bl(p['base'] + hook, p['finish_init'] & ~1)
        p['prepared_ibec_sha256'] = hashlib.sha256(original).hexdigest()
        stub = bytes.fromhex('00bf7047')
        result = ios_launcher.patch_ibec(original, stub, p)
        expected = bytearray(original)
        expected[p['shim_start']:p['shim_start'] + len(stub)] = stub
        expected[hook:hook + 4] = ios_launcher.thumb_bl(p['base'] + hook, p['base'] + p['shim_start'])
        self.assertEqual(result, bytes(expected))
        for changed in (dict(p, prepared_ibec_sha256='0' * 64), dict(p, hook=hook + 2)):
            with self.assertRaises(ValueError):
                ios_launcher.patch_ibec(original, stub, changed)
        original[p['shim_start']] = 1
        p['prepared_ibec_sha256'] = hashlib.sha256(original).hexdigest()
        with self.assertRaisesRegex(ValueError, 'padding'):
            ios_launcher.patch_ibec(original, stub, p)

    def test_thumb_branch_range(self):
        self.assertEqual(len(ios_launcher.thumb_bl(0x1000, 0x2000)), 4)
        for target in (0x2001, 0x1000 + (1 << 25)):
            with self.assertRaises(ValueError):
                ios_launcher.thumb_bl(0x1000, target)


class KloaderReceiptTests(unittest.TestCase):
    def test_signed_bounded_source_receipt_is_bound_to_artifact(self):
        with TemporaryDirectory() as directory:
            d = Path(directory)
            executable, receipt = d / 'kloader', d / 'result.json'
            executable.write_bytes(b'synthetic signed artifact')
            record = {'schema': 1, 'kind': 'ios-kloader-build',
                      'source_commit': ios_kloader.SOURCE_COMMIT,
                      'source_sha256': ios_kloader.SOURCE_SHA256,
                      'hardening_patch_sha256': ios_kloader.HARDENING_PATCH_SHA256,
                      'derived_source_sha256': ios_kloader.DERIVED_SOURCE_SHA256,
                      'sleep_wait_seconds': ios_kloader.SLEEP_WAIT_SECONDS,
                      'artifact_sha256': hashlib.sha256(executable.read_bytes()).hexdigest(),
                      'artifact_bytes': executable.stat().st_size, 'signed': True}
            receipt.write_text(json.dumps(record))
            with patch.object(ios_launcher, 'check_entry') as entry:
                self.assertEqual(ios_launcher.checked_kloader(executable, receipt), record)
                entry.assert_called_once_with(executable)
            for key, value in (('hardening_patch_sha256', '0' * 64),
                               ('derived_source_sha256', '0' * 64), ('sleep_wait_seconds', 1),
                               ('artifact_sha256', '0' * 64), ('signed', False)):
                with self.subTest(key=key):
                    receipt.write_text(json.dumps(dict(record, **{key: value})))
                    with self.assertRaisesRegex(ValueError, 'source-build receipt'):
                        ios_launcher.checked_kloader(executable, receipt)
            receipt.write_text(json.dumps(record))
            executable.write_bytes(b'changed artifact')
            with self.assertRaises(ValueError):
                ios_launcher.checked_kloader(executable, receipt)


@unittest.skipUnless(sys.platform == 'darwin' and shutil.which('xcrun'),
                     'Native launcher checks need Apple clang and codesign')
class NativeBuildTests(unittest.TestCase):
    def test_arm_entry_and_framework_stubs_build(self):
        p = ios_launcher.boot_profile('p105', root=hardware.ROOT)
        with TemporaryDirectory() as directory:
            d = Path(directory)
            for name in ('bundle', 'ibec', 'kloader'):
                (d / name).write_bytes(b'synthetic build-only input')
            result = ios_launcher.build_launcher(d / 'bundle', d / 'ibec', d / 'kloader', d / 'out', p)
            self.assertEqual(result['device_operations'], 0)
            self.assertFalse(result['on_device_startup_qualified'])
            repeated = ios_launcher.build_launcher(d / 'bundle', d / 'ibec', d / 'kloader', d / 'repeat', p)
            self.assertEqual(result['outputs'], repeated['outputs'])
            for entry in result['outputs'].values():
                self.assertEqual(entry['entry']['mode'], 'arm')
            executable = d / 'out/Orchard.app/Orchard'
            bad = d / 'bad'
            # Reproduce the historical LC_MAIN instruction-mode mismatch.
            wrong_entry = bytearray(executable.read_bytes())
            cursor = 28
            for _ in range(struct.unpack_from('<I', wrong_entry, 16)[0]):
                command, length = struct.unpack_from('<II', wrong_entry, cursor)
                if command == 0x80000028:
                    entry = struct.unpack_from('<Q', wrong_entry, cursor + 8)[0]
                    struct.pack_into('<Q', wrong_entry, cursor + 8, entry | 1)
                    break
                cursor += length
            bad.write_bytes(wrong_entry)
            with self.assertRaisesRegex(ValueError, 'instruction mode'):
                ios_launcher.check_entry(bad)
            raw = bytearray(executable.read_bytes())
            raw[0] = 0
            bad.write_bytes(raw)
            with self.assertRaises(ValueError):
                ios_launcher.check_entry(bad)
            with self.assertRaises(FileExistsError):
                ios_launcher.build_launcher(d / 'bundle', d / 'ibec', d / 'kloader', d / 'out', p)


if __name__ == '__main__':
    unittest.main()
