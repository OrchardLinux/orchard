# SPDX-License-Identifier: GPL-2.0-only
import json
from pathlib import Path
import struct
import shutil
import subprocess
import sys
import tempfile
import unittest
import zlib

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'src'))
from orchard.image import cascadia


PROFILE = dict(schema=1, load_address=0x80000000, kernel_offset=0x8000,
               watchdog_address=0x0f103020, l2_address=0x3e000000,
               ram_start=0x80000000, ram_end=0xa0000000,
               initrd_address=0x98000000, initrd_limit=0x9e000000)


def loader(described=PROFILE):
    record = dict(magic=b'CB32', version=1, header_size=cascadia.CONFIG.size,
                  dtb_offset=0, dtb_size=0, initrd_offset=0, initrd_size=0,
                  image_size=0, crc32=0)
    record.update({key: value for key, value in described.items() if key != 'schema'})
    raw = bytearray(cascadia.CONFIG_OFFSET + cascadia.CONFIG.size)
    raw[:4] = bytes.fromhex('c0000cf1')
    raw[cascadia.CONFIG_OFFSET:] = cascadia.CONFIG.pack(*(record[key] for key in cascadia.CONFIG_FIELDS))
    return bytes(raw)


def executable(raw, described=PROFILE):
    data = bytearray(0x300 + 3 * 40)
    data[:7] = b'\x7fELF\x01\x01\x01'
    struct.pack_into('<HHII', data, 16, 2, 40, 1, described['load_address'])
    struct.pack_into('<I', data, 32, 0x300)
    struct.pack_into('<HH', data, 46, 40, 3)
    data[0x100:0x104] = raw[:4]
    data[0x200:0x200 + cascadia.CONFIG.size] = raw[cascadia.CONFIG_OFFSET:]
    struct.pack_into('<10I', data, 0x300 + 40, 0, 1, 6, described['load_address'], 0x100, 4, 0, 0, 4, 0)
    struct.pack_into('<10I', data, 0x300 + 80, 0, 1, 2,
                     described['load_address'] + cascadia.CONFIG_OFFSET, 0x200, cascadia.CONFIG.size, 0, 0, 4, 0)
    return bytes(data)


def kernel():
    result = bytearray(256)
    struct.pack_into('<III', result, 0x24, 0x016f2818, 0, len(result))
    struct.pack_into('<III', result, 0x30, 0x04030201, 0x45454545, 0x60)
    struct.pack_into('<7I', result, 0x60, 6, 0x5a534c4b, 0xc0, 0x20000, 0x8000, 0x10000, 0)
    struct.pack_into('<I', result, 0xc0, 0x400000)
    return bytes(result)


def tree(cells=1):
    return {'name': '', 'properties': {'#address-cells': struct.pack('>I', cells),
                                       'compatible': b'example,test\0'},
            'children': [{'name': 'chosen', 'properties': {'bootargs': b'old\0', 'stdout-path': b'serial0\0'},
                          'children': []},
                         {'name': 'memory@80000000', 'properties': {'device_type': b'memory\0',
                                                                  'reg': b'\x80\0\0\0\x20\0\0\0'},
                          'children': []}]}


class CascadiaLoaderTests(unittest.TestCase):
    @unittest.skipUnless(sys.platform == 'linux' and shutil.which('cc'), 'The C runtime fixture needs Linux mmap and a C compiler')
    def test_actual_runtime_c_copy_and_rejection_paths(self):
        with tempfile.TemporaryDirectory() as temporary:
            folder = Path(temporary)
            (folder / 'cascadia-generated.h').write_text(''.join(
                f'#define {name.upper()} {hex(value)}\n' for name, value in PROFILE.items() if name != 'schema'))
            executable = folder / 'runtime-check'
            command = ['cc', '-std=c11', '-O2', '-Wall', '-Wextra', '-Werror', '-Wno-int-to-pointer-cast',
                       '-I', str(folder), '-I', str(ROOT / 'boot/cascadia'),
                       str(ROOT / 'tests/fixtures/cascadia-runtime.c'), '-o', str(executable)]
            compiled = subprocess.run(command, capture_output=True, text=True)
            self.assertEqual(compiled.returncode, 0, compiled.stderr)
            result = subprocess.run([str(executable)], capture_output=True, text=True, timeout=10)
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertIn('runtime checks passed', result.stdout)

    def test_elf_entry_and_raw_code_must_match(self):
        binary = loader()
        elf = executable(binary)
        cascadia.verify_loader(binary, PROFILE, elf)
        shifted = bytearray(elf)
        struct.pack_into('<I', shifted, 24, PROFILE['load_address'] + 16)
        with self.assertRaisesRegex(ValueError, 'ELF entry'):
            cascadia.verify_loader(binary, PROFILE, shifted)
        mismatch = bytearray(elf)
        mismatch[0x100] ^= 1
        with self.assertRaisesRegex(ValueError, 'allocated section'):
            cascadia.verify_loader(binary, PROFILE, mismatch)

    def test_unwind_metadata_before_entry_is_rejected(self):
        binary = loader()
        elf = bytearray(executable(binary))
        struct.pack_into('<I', elf, 0x300 + 40 + 8, 2)
        with self.assertRaisesRegex(ValueError, 'non-executable metadata'):
            cascadia.verify_loader(binary, PROFILE, elf)
        with self.assertRaisesRegex(ValueError, 'CPSID'):
            cascadia.verify_loader(b'\0' * 16 + binary[:-16], PROFILE)

    def test_rejects_wrong_profile_and_previously_assembled_loader(self):
        different = dict(PROFILE, watchdog_address=0)
        with self.assertRaisesRegex(ValueError, 'profile'):
            cascadia.verify_loader(loader(), different)
        image, _ = self.assemble()
        with self.assertRaisesRegex(ValueError, 'pristine'):
            cascadia.verify_loader(image[:len(loader())], PROFILE)

    def assemble(self, **changes):
        arguments = dict(loader=loader(), kernel=kernel(), dtb=cascadia.write_fdt(tree()),
                         initramfs=b'compressed-cpio', described=PROFILE, bootargs='console=tty0 rdinit=/init',
                         elf=executable(loader()))
        arguments.update(changes)
        return cascadia.assemble(**arguments)

    def test_complete_payload_preserves_kernel_and_describes_every_byte(self):
        image, receipt = self.assemble()
        config = cascadia.configuration(image)
        self.assertEqual(config['crc32'], zlib.crc32(image[cascadia.CONFIG_OFFSET:cascadia.CONFIG_OFFSET + cascadia.CONFIG.size - 4]))
        self.assertEqual(config['image_size'], len(image))
        self.assertEqual(image[0x8000:0x8000 + len(kernel())], kernel())
        self.assertEqual(image[config['initrd_offset']:], b'compressed-cpio')
        dtb = image[config['dtb_offset']:config['dtb_offset'] + config['dtb_size']]
        root, reservations, _ = cascadia.read_fdt(dtb)
        chosen = root['children'][0]['properties']
        self.assertEqual(chosen['stdout-path'], b'serial0\0')
        self.assertEqual(chosen['bootargs'], b'console=tty0 rdinit=/init\0')
        self.assertEqual(int.from_bytes(chosen['linux,initrd-start'], 'big'), PROFILE['initrd_address'])
        self.assertEqual(int.from_bytes(chosen['linux,initrd-end'], 'big'), PROFILE['initrd_address'] + len(b'compressed-cpio'))
        self.assertEqual(reservations, [(PROFILE['initrd_address'], len(b'compressed-cpio'))])
        position = 0
        for component in receipt['components']:
            self.assertEqual(component['offset'], position)
            data = image[position:position + component['bytes']]
            self.assertEqual(cascadia.digest(data), component['sha256'])
            position += component['bytes']
        self.assertEqual(position, len(image))
        self.assertEqual(receipt['image_sha256'], cascadia.digest(image))
        self.assertEqual(receipt['architecture'], 'arm')
        self.assertEqual(receipt['initramfs_encoding'], 'cascadia-dtb')

    def test_chosen_creation_and_two_address_cells(self):
        value = tree(2)
        value['children'].pop(0)
        dtb = cascadia.chosen(cascadia.write_fdt(value), 'test=1', PROFILE['initrd_address'], 12)
        root, _, _ = cascadia.read_fdt(dtb)
        self.assertEqual(root['children'][0], value['children'][0])
        chosen = root['children'][-1]['properties']
        self.assertEqual(len(chosen['linux,initrd-start']), 8)
        self.assertEqual(int.from_bytes(chosen['linux,initrd-start'], 'big'), PROFILE['initrd_address'])

    def test_previous_initrd_reservation_replaced_and_other_reservations_retained(self):
        value = tree()
        value['children'][0]['properties'].update({'linux,initrd-start': struct.pack('>I', 0x98000000),
                                                  'linux,initrd-end': struct.pack('>I', 0x98000010)})
        old = cascadia.write_fdt(value, [(0x98000000, 16), (0x9f000000, 4096)])
        new = cascadia.chosen(old, 'new', 0x98000000, 32)
        self.assertEqual(cascadia.read_fdt(new)[1], [(0x9f000000, 4096), (0x98000000, 32)])

    def test_reserved_destination_refused(self):
        old = cascadia.write_fdt(tree(), [(PROFILE['initrd_address'] + 4, 4096)])
        with self.assertRaisesRegex(ValueError, 'reserved memory'):
            self.assemble(dtb=old)

    def test_fdt_corruption_is_rejected(self):
        original = cascadia.write_fdt(tree())
        for offset, value in ((4, len(original) + 1), (8, 0xffffffff), (12, 0), (16, 41), (20, 16)):
            with self.subTest(offset=offset):
                bad = bytearray(original)
                struct.pack_into('>I', bad, offset, value)
                with self.assertRaises(ValueError):
                    cascadia.read_fdt(bad)
        with self.assertRaisesRegex(ValueError, 'overlap'):
            bad = bytearray(original)
            struct.pack_into('>I', bad, 12, struct.unpack_from('>I', bad, 8)[0])
            cascadia.read_fdt(bad)

    def test_wrong_kernel_and_out_of_bounds_initramfs_refused(self):
        with self.assertRaisesRegex(ValueError, 'ARM zImage'):
            self.assemble(kernel=b'arm64 Image')
        with self.assertRaisesRegex(ValueError, 'length'):
            self.assemble(kernel=kernel() + b'appended old dtb')
        smaller = dict(PROFILE, initrd_limit=PROFILE['initrd_address'] + 8)
        with self.assertRaisesRegex(ValueError, 'destination'):
            self.assemble(loader=loader(smaller), described=smaller, elf=None)
        with self.assertRaisesRegex(ValueError, 'one line'):
            self.assemble(bootargs='a\nb')

    def test_decompressor_workspace_cannot_reach_high_initramfs(self):
        large = bytearray(kernel())
        struct.pack_into('<I', large, 0xc0, 0x18000000)
        with self.assertRaisesRegex(ValueError, 'decompression workspace'):
            self.assemble(kernel=large)
        unknown = bytearray(kernel())
        struct.pack_into('<I', unknown, 0x64, 0)
        with self.assertRaisesRegex(ValueError, 'size table'):
            self.assemble(kernel=unknown)
        unaligned = bytearray(kernel() + b'\0' * 4)
        struct.pack_into('<I', unaligned, 0x2c, len(unaligned))
        with self.assertRaisesRegex(ValueError, 'eight-byte'):
            self.assemble(kernel=unaligned)

    def test_profile_rejects_overlap_boolean_and_unknown_fields(self):
        for changed in (dict(PROFILE, initrd_address=0x80001000),
                        dict(PROFILE, schema=True), dict(PROFILE, unknown=1),
                        dict(PROFILE, ram_start=0x70000000),
                        dict(PROFILE, kernel_offset=0x4000)):
            with self.subTest(profile=changed), self.assertRaises(ValueError):
                cascadia.validate_profile(changed)

    def test_cli_writes_receipt_and_refuses_overwrite(self):
        with tempfile.TemporaryDirectory() as temporary:
            directory = Path(temporary)
            files = {'loader': loader(), 'loader-elf': executable(loader()), 'kernel': kernel(),
                     'dtb': cascadia.write_fdt(tree()), 'initramfs': b'cpio'}
            command = [sys.executable, str(ROOT / 'src/orchard/image/cascadia.py')]
            for name, value in files.items():
                path = directory / name
                path.write_bytes(value)
                command.extend(['--' + name, str(path)])
            described = directory / 'profile.toml'
            described.write_text(''.join(f'{name} = {value}\n' for name, value in PROFILE.items()))
            out = directory / 'linux.bin'
            command.extend(['--profile', str(described), '--bootargs', 'rdinit=/init', '--output', str(out)])
            first = subprocess.run(command, capture_output=True, text=True)
            self.assertEqual(first.returncode, 0, first.stderr)
            self.assertEqual(json.loads((directory / 'linux.bin.json').read_text())['image_sha256'], cascadia.digest(out.read_bytes()))
            second = subprocess.run(command, capture_output=True, text=True)
            self.assertNotEqual(second.returncode, 0)
            self.assertIn('new image and receipt', second.stderr)


if __name__ == '__main__':
    unittest.main()
