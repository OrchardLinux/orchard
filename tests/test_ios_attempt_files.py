# SPDX-License-Identifier: GPL-2.0-only
"""The actual iOS attempt preparation stays within private, checked file copies."""
import hashlib
import json
from pathlib import Path
import shutil
import struct
import subprocess
import sys
from tempfile import TemporaryDirectory
import unittest
import zlib

from orchard import cascadia_payload as image, recovery_identity
from orchard.apple import ios_identity, ios_launcher
from test_ios_launch_identity import template

ROOT = Path(__file__).resolve().parents[1]
SOURCE = ROOT / 'boot/ios-launcher/ios-start.c'
FIXTURE = ROOT / 'tests/fixtures/ios-attempt-files.c'


@unittest.skipUnless(sys.platform == 'darwin' and shutil.which('xcrun'),
                     'Native iOS preparation checks need Apple CommonCrypto')
class IOSAttemptFiles(unittest.TestCase):
    def test_actual_preparation_binds_and_checks_private_files_before_boot_apis(self):
        source = SOURCE.read_text()
        checked_at = source.index('static int checked(')
        checked_end = source.index('\n#ifdef IOS_IDENTITY_SLOT_OFFSET', checked_at)
        start = source.index('static void sha_text(')
        stop = source.index('\n#endif\n', start)
        actual = source[checked_at:checked_end] + '\n' + source[start:stop]
        profile = ios_launcher.boot_profile('p105')
        original, identity = ios_identity.prepare_template(template(), profile)
        crc_offset = 24
        ibec = bytearray(b'\xa5' * 128)
        struct.pack_into('<I', ibec, crc_offset, zlib.crc32(original))
        ibec = bytes(ibec)
        with TemporaryDirectory(prefix='orchard-ios-attempt-') as temporary:
            work = Path(temporary)
            paths = {name: work / file for name, file in (
                ('ROOT_BOOT_PATH', 'canonical-boot.bin'), ('IBEC_PATH', 'canonical-ibec.bin'),
                ('ATTEMPT_BOOT_PATH', 'attempt-boot.bin'), ('ATTEMPT_IBEC_PATH', 'attempt-ibec.bin'),
                ('FIXTURE_BOOT_SOURCE', 'pristine-boot.bin'), ('FIXTURE_IBEC_SOURCE', 'pristine-ibec.bin'),
                ('FIXTURE_VICTIM_PATH', 'linked-victim.bin'))}
            paths['FIXTURE_BOOT_SOURCE'].write_bytes(original)
            paths['FIXTURE_IBEC_SOURCE'].write_bytes(ibec)
            config = ''.join('#define ' + name + ' ' + json.dumps(str(path)) + '\n'
                             for name, path in paths.items())
            config += '#define ROOT_BOOT_SHA256 root_hash\n#define IBEC_SHA256 ibec_hash\n'
            config += '#define IOS_TEMPLATE_CRC32 template_crc\n#define TARGET_PROFILE "p105"\n'
            config += '#define TARGET_CHIP 0x8942u\n#define TARGET_BOARD 10u\n'
            for name, value in (('ROOT_BOOT_BYTES', len(original)), ('IBEC_BYTES', len(ibec)),
                                ('IOS_IDENTITY_SLOT_OFFSET', identity['slot_offset']),
                                ('IOS_IDENTITY_SLOT_BYTES', identity['slot_bytes']),
                                ('IOS_IBEC_CRC_OFFSET', crc_offset)):
                config += f'#define {name} {value}u\n'
            (work / 'ios-attempt-config.h').write_text(config)
            (work / 'ios-attempt-source.inc').write_text(actual)
            executable = work / 'fixture'
            compiled = subprocess.run([
                'xcrun', '--sdk', 'macosx', 'clang', '-std=c11', '-Wall', '-Wextra', '-Werror',
                '-fsanitize=address,undefined', '-fno-omit-frame-pointer',
                '-I', str(work), '-I', str(ROOT / 'boot/ios-launcher'), str(FIXTURE),
                '-o', str(executable),
            ], capture_output=True, text=True, timeout=60)
            self.assertEqual(compiled.returncode, 0, compiled.stdout + compiled.stderr)
            nonces = []
            for case in ('valid', 'fresh-nonce', 'partial-read', 'partial-write'):
                with self.subTest(case=case):
                    result = subprocess.run([str(executable), case], capture_output=True, text=True, timeout=15)
                    self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
                    record = json.loads(result.stdout)
                    self.assertEqual(record['result'], 0)
                    self.assertEqual(record['file_syncs'], 2)
                    self.assertEqual(record['directory_opens'], 2)
                    self.assertEqual(record['directory_syncs'], 2)
                    self.assertEqual(record['directory_closes'], 2)
                    self.assertEqual(record['renames'], 2)
                    bound = paths['ATTEMPT_BOOT_PATH'].read_bytes()
                    bound_ibec = paths['ATTEMPT_IBEC_PATH'].read_bytes()
                    self.assertEqual(len(bound), len(original))
                    at, count = identity['slot_offset'], identity['slot_bytes']
                    self.assertEqual(bound[:at], original[:at])
                    self.assertEqual(bound[at + count:], original[at + count:])
                    self.assertEqual(image.configuration(bound), image.configuration(original))
                    values = image.configuration(bound)
                    dtb = bound[values['dtb_offset']:values['dtb_offset'] + values['dtb_size']]
                    tree, _, _ = image.read_fdt(dtb)
                    chosen = next(child for child in tree['children'] if child['name'] == 'chosen')
                    fields = recovery_identity.parameters(chosen['properties']['bootargs'][:-1])[
                        recovery_identity.HANDOFF_PARAMETER]
                    expected_nonce = bytes(range(17, 33) if case == 'fresh-nonce' else range(1, 17)).hex()
                    self.assertEqual(fields, {'schema': 1, 'source': 'ios-device', 'profile': 'p105',
                        'ecid': 0x0102030405060708, 'chip': 0x8942, 'board': 10,
                        'handoff_id': expected_nonce, 'template_sha256': hashlib.sha256(original).hexdigest()})
                    nonces.append(fields['handoff_id'])
                    expected_ibec = bytearray(ibec)
                    struct.pack_into('<I', expected_ibec, crc_offset, zlib.crc32(bound))
                    self.assertEqual(bound_ibec, bytes(expected_ibec))
                    self.assertEqual(paths['ROOT_BOOT_PATH'].read_bytes(), original)
                    self.assertEqual(paths['IBEC_PATH'].read_bytes(), ibec)
                    for name in ('ATTEMPT_BOOT_PATH', 'ATTEMPT_IBEC_PATH'):
                        self.assertFalse(Path(str(paths[name]) + '.pending').exists())
            self.assertNotEqual(nonces[0], nonces[1])
            early = {'bad-sha', 'bad-template-crc', 'bad-ibec-crc', 'nonspace-slot', 'zero-nonce',
                     'linked-pending', 'failed-read', 'short-read-zero', 'wrong-owner', 'wrong-mode', 'input-symlink'}
            failures = sorted(early) + ['failed-write', 'zero-write', 'short-write-zero', 'short-write-error',
                                       'failed-sync', 'failed-write-close', 'failed-rename',
                                       'second-write-failed', 'second-rename-failed',
                                       'failed-directory-open', 'second-directory-open-failed',
                                       'failed-directory-sync', 'second-directory-sync-failed']
            failures += ['failed-directory-close', 'second-directory-close-failed']
            for case in failures:
                with self.subTest(case=case):
                    result = subprocess.run([str(executable), case], capture_output=True, text=True, timeout=15)
                    self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
                    record = json.loads(result.stdout)
                    self.assertEqual(record['result'], -1)
                    if case in early:
                        self.assertEqual(record['writes'], 0)
                    if case == 'linked-pending':
                        self.assertTrue(Path(str(paths['ATTEMPT_BOOT_PATH']) + '.pending').is_symlink())
                        self.assertEqual(paths['FIXTURE_VICTIM_PATH'].read_bytes(), b'untouched-owner-local-victim\0')
                    else:
                        for name in ('ATTEMPT_BOOT_PATH', 'ATTEMPT_IBEC_PATH'):
                            self.assertFalse(Path(str(paths[name]) + '.pending').exists())
                    second_attempt_failures = {'second-write-failed', 'second-rename-failed',
                                               'second-directory-open-failed', 'second-directory-sync-failed',
                                               'second-directory-close-failed'}
                    self.assertEqual(record['ibec_exists'], case in {
                        'second-directory-open-failed', 'second-directory-sync-failed',
                        'second-directory-close-failed'})
                    self.assertEqual(record['boot_exists'], case in second_attempt_failures or case in {
                        'failed-directory-open', 'failed-directory-sync', 'failed-directory-close'})
                    if record['boot_exists']:
                        surviving = paths['ATTEMPT_BOOT_PATH'].read_bytes()
                        self.assertEqual(len(surviving), len(original))
                        at, count = identity['slot_offset'], identity['slot_bytes']
                        self.assertEqual(surviving[:at], original[:at])
                        self.assertEqual(surviving[at + count:], original[at + count:])
                        values = image.configuration(surviving)
                        dtb = surviving[values['dtb_offset']:values['dtb_offset'] + values['dtb_size']]
                        tree, _, _ = image.read_fdt(dtb)
                        chosen = next(child for child in tree['children'] if child['name'] == 'chosen')
                        fields = recovery_identity.parameters(chosen['properties']['bootargs'][:-1])[
                            recovery_identity.HANDOFF_PARAMETER]
                        self.assertEqual(fields, {'schema': 1, 'source': 'ios-device', 'profile': 'p105',
                            'ecid': 0x0102030405060708, 'chip': 0x8942, 'board': 10,
                            'handoff_id': bytes(range(1, 17)).hex(),
                            'template_sha256': hashlib.sha256(original).hexdigest()})
                    if record['ibec_exists']:
                        expected_ibec = bytearray(ibec)
                        struct.pack_into('<I', expected_ibec, crc_offset, zlib.crc32(surviving))
                        self.assertEqual(paths['ATTEMPT_IBEC_PATH'].read_bytes(), bytes(expected_ibec))
            self.assertEqual(paths['FIXTURE_BOOT_SOURCE'].read_bytes(), original)
            self.assertEqual(paths['FIXTURE_IBEC_SOURCE'].read_bytes(), ibec)


@unittest.skipUnless(sys.platform == 'darwin' and shutil.which('xcrun'),
                     'Native startup ordering checks need CoreFoundation')
class IOSAttemptWakeOrder(unittest.TestCase):
    def test_actual_main_prepares_live_identity_and_files_before_scheduling_or_spawn(self):
        source = SOURCE.read_text()
        actual = source[source.index('struct startup_wake {'):]
        with TemporaryDirectory(prefix='orchard-ios-attempt-order-') as temporary:
            work = Path(temporary)
            (work / 'ios-attempt-wake-source.inc').write_text(actual)
            executable = work / 'fixture'
            compiled = subprocess.run([
                'xcrun', '--sdk', 'macosx', 'clang', '-std=c11', '-Wall', '-Wextra', '-Werror',
                '-fsanitize=address,undefined', '-fno-omit-frame-pointer',
                '-I', str(work), str(ROOT / 'tests/fixtures/ios-attempt-wake-order.c'),
                '-framework', 'CoreFoundation', '-o', str(executable),
            ], capture_output=True, text=True, timeout=60)
            self.assertEqual(compiled.returncode, 0, compiled.stdout + compiled.stderr)
            result = subprocess.run([str(executable)], capture_output=True, text=True, timeout=15)
            self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        records = [line for line in result.stdout.splitlines() if line.startswith('{')]
        self.assertIn('native-attempt-wake-order-passed', result.stdout.splitlines())
        cases = {row['case']: row for row in map(json.loads, records)}
        self.assertEqual(set(cases), {'failed-live', 'failed-prepare', 'failed-lease',
                                      'failed-hash', 'inspect', 'prepared-start'})
        for case in ('failed-live', 'failed-prepare', 'failed-lease', 'failed-hash', 'inspect'):
            with self.subTest(case=case):
                self.assertEqual(cases[case]['schedule'], 0)
                self.assertEqual(cases[case]['spawn'], 0)
        self.assertEqual(cases['prepared-start'], {'case': 'prepared-start', 'live': 1,
                         'lease': 1, 'prepare': 1, 'schedule': 1, 'spawn': 1})


if __name__ == '__main__':
    unittest.main()
