"""Recovery tools must match their target ISA before entering an image."""
import importlib.util
import json
import os
from pathlib import Path
import struct
import unittest
from unittest import mock

from orchard import buildenv

ROOT = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location('check_recovery_tools',
                                             ROOT / 'boot/initramfs/recovery/check-tools.py')
checker = importlib.util.module_from_spec(spec)
spec.loader.exec_module(checker)


def elf(arch, *, kind=1, dynamic_tag=None, flags=0x05000400):
    arm = arch == 'arm'
    header_size, ph_size = (52, 32) if arm else (64, 56)
    ident = b'\x7fELF' + bytes((1 if arm else 2, 1, 1)) + bytes(9)
    dynamic = struct.pack('<iI' if arm else '<qQ', dynamic_tag or 0, 0)
    length = header_size + ph_size + len(dynamic)
    if arm:
        header = struct.pack('<16sHHIIIIIHHHHHH', ident, 2, 40, 1, 0x10000,
                             header_size, 0, flags, header_size, ph_size, 1, 0, 0, 0)
        segment = struct.pack('<8I', kind, header_size + ph_size, 0, 0, len(dynamic), len(dynamic), 5, 4)
    else:
        header = struct.pack('<16sHHIQQQIHHHHHH', ident, 2, 183, 1, 0x10000,
                             header_size, 0, 0, header_size, ph_size, 1, 0, 0, 0)
        segment = struct.pack('<II6Q', kind, 5, header_size + ph_size, 0, 0,
                              len(dynamic), len(dynamic), 8)
    return header + segment + dynamic


class RecoveryArchitecture(unittest.TestCase):
    def test_both_static_target_formats_and_wrong_isa_refusal(self):
        for arch, other in (('arm', 'arm64'), ('arm64', 'arm')):
            with self.subTest(arch=arch):
                checker.validate(elf(arch), arch)
                with self.assertRaisesRegex(ValueError, 'class or byte order'):
                    checker.validate(elf(other), arch)

    def test_arm_soft_float_and_old_eabi_are_refused(self):
        for flags in (0x05000200, 0x05000000, 0x04000400):
            with self.subTest(flags=flags), self.assertRaisesRegex(ValueError, 'hard-float'):
                checker.validate(elf('arm', flags=flags), 'arm')

    def test_dynamic_linkage_is_refused_for_both_classes(self):
        for arch in ('arm', 'arm64'):
            with self.subTest(arch=arch), self.assertRaisesRegex(ValueError, 'PT_INTERP'):
                checker.validate(elf(arch, kind=3), arch)
            with self.subTest(arch=arch), self.assertRaisesRegex(ValueError, 'DT_NEEDED'):
                checker.validate(elf(arch, kind=2, dynamic_tag=1), arch)

    def test_truncated_program_headers_and_segments_are_refused(self):
        for arch in ('arm', 'arm64'):
            for end in (20, 60, len(elf(arch)) - 1):
                with self.subTest(arch=arch, end=end), self.assertRaises(ValueError):
                    checker.validate(elf(arch)[:end], arch)

    def test_armhf_and_arm64_busybox_have_distinct_pins(self):
        lock = json.loads((ROOT / 'boot/initramfs/recovery/upstream.lock').read_text())
        self.assertTrue(lock['busybox_arm']['url'].endswith('_armhf.deb'))
        self.assertTrue(lock['busybox']['url'].endswith('_arm64.deb'))
        self.assertNotEqual(lock['busybox_arm']['sha256'], lock['busybox']['sha256'])

    def test_arm_podman_build_and_run_select_the_v7_variant(self):
        with mock.patch.dict(os.environ, {'ORCHARD_BUILD_NATIVE': '0'}), \
                mock.patch.object(buildenv.subprocess, 'run') as run:
            buildenv.build('image', 'Containerfile', '.', arch='arm')
            self.assertEqual(run.call_args.args[0][:4], ['podman', 'build', '--platform', 'linux/arm/v7'])
            buildenv.run('image', ['true'], arch='arm')
            self.assertIn('linux/arm/v7', run.call_args.args[0])

    def test_native_build_requires_an_explicit_target_to_match_the_host(self):
        for machine, arch, other in (('armv7l', 'arm', 'arm64'), ('aarch64', 'arm64', 'arm')):
            with self.subTest(machine=machine), \
                    mock.patch.dict(os.environ, {'ORCHARD_BUILD_NATIVE': '1', 'ORCHARD_BUILD_ENV_ID': 'test'}), \
                    mock.patch.object(buildenv.platform, 'system', return_value='Linux'), \
                    mock.patch.object(buildenv.platform, 'machine', return_value=machine), \
                    mock.patch.object(buildenv.subprocess, 'run') as run:
                buildenv.build('image', 'Containerfile', '.', arch=arch)
                run.assert_not_called()
                with self.assertRaisesRegex(ValueError, 'differs from host'):
                    buildenv.build('image', 'Containerfile', '.', arch=other)
                with self.assertRaisesRegex(ValueError, 'differs from host'):
                    buildenv.run('image', ['true'], arch=other)


if __name__ == '__main__':
    unittest.main()
