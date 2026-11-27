"""ARMv7 producer boundaries use synthetic ELF and DTB inputs, with no device access."""
import json
from pathlib import Path
import struct
import tarfile
import tempfile
import unittest

from orchard.image import elf, platform


def executable(architecture='arm', dynamic=False):
    width = 1 if architecture == 'arm' else 2
    data = bytearray(384)
    data[:7] = b'\x7fELF' + bytes((width, 1, 1))
    struct.pack_into('<HHI', data, 16, 2, 40 if width == 1 else 183, 1)
    if width == 1:
        struct.pack_into('<I', data, 28, 52)
        struct.pack_into('<I', data, 36, 0x05000400)
        struct.pack_into('<HHH', data, 40, 52, 32, 2 if dynamic else 0)
        if dynamic:
            struct.pack_into('<8I', data, 52, 1, 0, 0x1000, 0, len(data), len(data), 5, 4)
            struct.pack_into('<8I', data, 84, 2, 128, 0x1080, 0, 32, 32, 4, 4)
            for index, pair in enumerate(((1, 0), (5, 0x10c0), (10, 10), (0, 0))):
                struct.pack_into('<II', data, 128 + 8 * index, *pair)
            data[192:202] = b'libm.so.6\0'
    else:
        struct.pack_into('<Q', data, 32, 64)
        struct.pack_into('<HHH', data, 52, 64, 56, 2 if dynamic else 0)
        if dynamic:
            struct.pack_into('<II6Q', data, 64, 1, 5, 0, 0x1000, 0, len(data), len(data), 8)
            struct.pack_into('<II6Q', data, 120, 2, 4, 192, 0x10c0, 0, 64, 64, 8)
            for index, pair in enumerate(((1, 0), (5, 0x1100), (10, 10), (0, 0))):
                struct.pack_into('<QQ', data, 192 + 16 * index, *pair)
            data[256:266] = b'libm.so.6\0'
    return bytes(data)


class Armv7ProducerTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.work = Path(temporary.name)


    def test_platform_selection_can_filter_by_architecture(self):
        self.assertEqual(platform.platforms(architecture='arm'), ['apple-armv7'])
        self.assertNotIn('apple-armv7', platform.platforms())
        self.assertIn('apple-armv7', platform.platforms(architecture=None))

    def test_platform_layer_refuses_an_incompatible_root(self):
        with self.assertRaisesRegex(ValueError, 'differs from root'):
            platform.build('apple-armv7', self.work, root_architecture='arm64')

    def test_armv7_platform_layer_uses_zimage_and_every_board(self):
        from test_cascadia_loader import kernel
        bundle = self.work / 'kernel'
        modules = bundle / 'lib/modules/test-arm'
        modules.mkdir(parents=True)
        (modules / 'modules.dep').write_text('')
        (modules / 'modules.order').write_text('')
        (bundle / 'kernel.release').write_text('test-arm\n')
        (bundle / 'zImage').write_bytes(kernel())
        (bundle / 'dtbs').mkdir()
        for dtb in platform.boards('apple-armv7').values():
            (bundle / 'dtbs' / (dtb + '.dtb')).write_bytes(b'caller-supplied-dtb')
        busybox = self.work / 'busybox'
        busybox.write_bytes(executable())
        result = platform.build('apple-armv7', self.work, bundle=bundle, busybox=busybox,
                                root_architecture='arm')
        self.assertEqual(result['architecture'], 'arm')
        self.assertEqual(result['boards'], sorted(platform.boards('apple-armv7')))
        with tarfile.open(self.work / result['file']) as archive:
            description = json.load(archive.extractfile('usr/lib/orchard/platform.json'))
            self.assertEqual(description['architecture'], 'arm')
            self.assertEqual(description['image'], 'zImage')
            self.assertEqual(description['boards'], platform.boards('apple-armv7'))
            self.assertEqual(archive.extractfile('usr/lib/modules/test-arm/vmlinuz').read(), kernel())
        busybox.write_bytes(executable('arm64'))
        other = self.work / 'wrong'
        other.mkdir()
        with self.assertRaisesRegex(ValueError, 'BusyBox differs'):
            platform.build('apple-armv7', other, bundle=bundle, busybox=busybox,
                           root_architecture='arm')
        self.assertEqual(list(other.iterdir()), [])

    def test_elf_dependency_reader_handles_both_instruction_sets(self):
        for architecture in ('arm', 'arm64'):
            with self.subTest(architecture=architecture):
                data = executable(architecture, dynamic=True)
                self.assertEqual(elf.architecture(data), architecture)
                self.assertEqual(elf.needed(data), {'libm.so.6'})
                self.assertEqual(elf.needed(executable(architecture)), set())

    def test_elf_refuses_malformed_dependency_bounds_and_soft_float(self):
        raw = bytearray(executable(dynamic=True))
        struct.pack_into('<I', raw, 128 + 8 + 4, 0x9000)
        with self.assertRaisesRegex(ValueError, 'outside load segments'):
            elf.needed(raw)
        for flags in (0x05000200, 0x04000400):
            raw = bytearray(executable())
            struct.pack_into('<I', raw, 36, flags)
            with self.assertRaisesRegex(ValueError, 'hard-float'):
                elf.architecture(raw)
        raw = bytearray(executable(dynamic=True))
        struct.pack_into('<I', raw, 128 + 4, 1000)
        with self.assertRaisesRegex(ValueError, 'offset exceeds'):
            elf.needed(raw)


    def test_cascadia_profile_requires_matching_dtb_compatible(self):
        from orchard.image import cascadia
        from test_cascadia_loader import tree
        data = cascadia.write_fdt(tree(), [], 0)
        with self.assertRaisesRegex(ValueError, 'differs from the selected board'):
            cascadia.check_compatible(data, ['apple,p105ap'])
        cascadia.check_compatible(data, ['example,test'])


if __name__ == '__main__':
    unittest.main()
