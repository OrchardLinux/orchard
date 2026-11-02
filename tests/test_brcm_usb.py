"""Private BCM43241 USB conversion: synthetic firmware and MAC only."""
import hashlib
import io
import json
import os
from pathlib import Path
import stat
import struct
import tarfile
import tempfile
import unittest
from unittest.mock import patch

from orchard.apple import brcm_usb as usb


MAC = bytes([2, 0, 0, 0, 0, 1])  # Synthetic; never used as a default.
BODY = bytes(range(128))


def firmware():
    raw = bytearray(struct.pack('<7I', 0x30524448, 172, 0, 0x10020, 128, 4, 0)
                    + BODY + bytes(16))
    struct.pack_into('<I', raw, 8, usb.crc(raw))
    return bytes(raw)


def changed(offset, value):
    raw = bytearray(firmware())
    struct.pack_into('<I', raw, offset, value)
    if offset != 8:
        struct.pack_into('<I', raw, 8, usb.crc(raw))
    return bytes(raw)


class UsbFirmwareTests(unittest.TestCase):
    def test_layout_firmware_bytes_token_and_crc(self):
        image = usb.pack(firmware(), '# board comment\nboardrev=1\nccode=XY\n', MAC)
        board = b'boardrev=1\0ccode=XY\0macaddr=02:00:00:00:00:01\0\0'
        board = board.ljust((len(board) + 3) & ~3, b'\0')
        words = len(board) // 4
        nvram = board + struct.pack('<I', words | ((~words & 0xffff) << 16))
        magic, length, crc, flags, fwlen, jump, nvlen = struct.unpack_from('<7I', image)
        self.assertEqual((magic, length, crc, flags, fwlen, jump, nvlen),
                         (0x30524448, len(image), usb.crc(image), 0x10020, len(BODY), 4, len(nvram)))
        self.assertEqual(image[28:156], BODY)
        self.assertEqual(image[156:], nvram)

    def test_malformed_headers_and_already_packed_refused(self):
        for offset, value in ((0, 0), (4, 1), (8, 0), (12, 0), (16, 0),
                              (16, 0xffffffff), (20, 128), (24, 4)):
            with self.subTest(offset=offset, value=value), self.assertRaises(ValueError):
                usb.pack(changed(offset, value), 'boardrev=1', MAC)
        with self.assertRaises(ValueError):
            usb.pack(usb.pack(firmware(), 'boardrev=1', MAC), 'boardrev=1', MAC)

    def test_only_zero_original_padding_can_be_replaced(self):
        raw = bytearray(firmware())
        raw[-1] = 1
        struct.pack_into('<I', raw, 8, usb.crc(raw))
        with self.assertRaisesRegex(ValueError, 'Non-padding'):
            usb.pack(bytes(raw), 'boardrev=1', MAC)

    def test_lengths_and_combined_ram_bound(self):
        for raw in (b'', b'HDR0', bytes(usb.RAM + 1)):
            with self.subTest(length=len(raw)), self.assertRaises(ValueError):
                usb.pack(raw, 'a=1', MAC)
        # A legal but large text input exceeds the 16-bit NVRAM word token.
        with self.assertRaisesRegex(ValueError, 'token range'):
            usb.pack(firmware(), 'a=' + 'x' * (0x40000), MAC)
        # A valid nearly-RAM-sized firmware plus ordinary board settings.
        raw = bytearray(struct.pack('<7I', 0x30524448, usb.RAM, 0, 0x10020,
                                    usb.RAM - 28, 0, 0) + bytes(usb.RAM - 28))
        struct.pack_into('<I', raw, 8, usb.crc(raw))
        with self.assertRaisesRegex(ValueError, 'Combined image'):
            usb.pack(bytes(raw), 'a=1', MAC)

    def test_board_settings_are_preserved_and_mac_must_agree(self):
        text = 'boardrev = 1 # comment\nccode=XY\nmacaddr=02:00:00:00:00:01\n'
        self.assertEqual(usb.pack(firmware(), text, MAC),
                         usb.pack(firmware(), 'boardrev=1\nccode=XY', MAC))
        with self.assertRaisesRegex(ValueError, 'differs'):
            usb.pack(firmware(), 'boardrev=1\nmacaddr=02:00:00:00:00:02', MAC)

    def test_malformed_nvram_refused(self):
        for text in ('', '# comment', 'a=1\na=2', 'a', 'a=\0', 'a=é',
                     'invalid key=1', 'a=\x01'):
            with self.subTest(text=text), self.assertRaises((ValueError, UnicodeError)):
                usb.pack(firmware(), text, MAC)

    def test_owner_mac_required_no_synthetic_default(self):
        for mac in (b'', bytes(5), bytes(7), bytes(6), b'\xff' * 6,
                    bytes([1, 0, 0, 0, 0, 1])):
            with self.subTest(mac=mac), self.assertRaises(ValueError):
                usb.pack(firmware(), 'boardrev=1', mac)

    def inputs(self, directory):
        paths = [directory / name for name in ('original.trx', 'board.txt', 'owner-mac.bin')]
        for path, data in zip(paths, (firmware(), b'boardrev=1\n', MAC)):
            path.write_bytes(data)
        return paths

    def test_mixin_uses_existing_data_only_format_and_private_modes(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            inputs = self.inputs(root)
            archive, receipt = usb.prepare(*inputs, root / 'private-output')
            self.assertEqual(stat.S_IMODE(archive.parent.stat().st_mode), 0o700)
            self.assertEqual(stat.S_IMODE(archive.stat().st_mode), 0o600)
            self.assertEqual(stat.S_IMODE((archive.parent / 'mixin.json').stat().st_mode), 0o600)
            self.assertEqual(receipt['sha256'], hashlib.sha256(archive.read_bytes()).hexdigest())
            with tarfile.open(archive) as bundle:
                self.assertEqual(set(bundle.getnames()), {'mixin.json', usb.DESTINATION})
                image = bundle.extractfile(usb.DESTINATION).read()
                self.assertEqual(image, usb.pack(firmware(), 'boardrev=1\n', MAC))
                metadata = json.load(bundle.extractfile('mixin.json'))
                self.assertEqual(metadata['profile'], 'j72')
                self.assertEqual(metadata['files'][usb.DESTINATION]['sha256'], hashlib.sha256(image).hexdigest())
                self.assertTrue(all(member.mode == 0o600 for member in bundle.getmembers()))

    def test_existing_output_and_dangling_link_not_replaced(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            inputs = self.inputs(root)
            output = root / 'output'
            output.mkdir()
            marker = output / 'keep'
            marker.write_bytes(b'keep')
            with self.assertRaises(FileExistsError):
                usb.prepare(*inputs, output)
            self.assertEqual(marker.read_bytes(), b'keep')
            dangling = root / 'dangling'
            dangling.symlink_to(root / 'absent')
            with self.assertRaises(FileExistsError):
                usb.prepare(*inputs, dangling)
            self.assertTrue(dangling.is_symlink())

    def test_link_nonregular_and_oversized_inputs_refused_before_output(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            inputs = self.inputs(root)
            linked = root / 'linked'
            linked.symlink_to(inputs[0])
            with self.assertRaises(OSError):
                usb.prepare(linked, *inputs[1:], root / 'output')
            fifo = root / 'fifo'
            os.mkfifo(fifo)
            with self.assertRaisesRegex(ValueError, 'regular'):
                usb.prepare(fifo, *inputs[1:], root / 'output')
            inputs[0].write_bytes(bytes(usb.RAM + 1))
            with self.assertRaisesRegex(ValueError, 'bound'):
                usb.prepare(*inputs, root / 'output')
            self.assertFalse((root / 'output').exists())

    def test_command_has_no_device_discovery_and_prints_no_owner_mac(self):
        from orchard import command
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            inputs = self.inputs(root)
            argv = ['orchard', 'wifi-firmware', '--firmware', str(inputs[0]),
                    '--nvram', str(inputs[1]), '--mac-file', str(inputs[2]),
                    '--output', str(root / 'output')]
            with patch('sys.argv', argv), patch('sys.stdout', new_callable=io.StringIO) as stdout, \
                    patch.object(command, 'version', return_value='test'), \
                    patch('orchard.cache.check_state'), \
                    patch.object(command, 'choose', side_effect=AssertionError('device access')), \
                    patch.object(command, 'load', side_effect=AssertionError('device access')):
                command.main()
                receipt = json.loads(stdout.getvalue())
            self.assertEqual(receipt['kind'], 'private-mixin')
            self.assertNotIn('02:00:00:00:00:01', stdout.getvalue())


if __name__ == '__main__':
    unittest.main()
