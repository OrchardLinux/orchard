"""Both host backends preserve and validate the per-attempt USB identity."""
from pathlib import Path
import plistlib
import subprocess
import tempfile
import unittest
from unittest.mock import patch

from orchard.apple import linux_usb, usb


BOOT = '11111111-1111-4111-8111-111111111111'
HANDOFF = '0123456789abcdef0123456789abcdef'
LEGACY = f'ECID:0000000000000002 CPID:8942 BDID:0a BOOT:{BOOT} BOARD:p105'


class USBHandoff(unittest.TestCase):
    def read_macos(self, serial):
        raw = plistlib.dumps([{'IOObjectClass': 'IOUSBHostDevice', 'idVendor': 0x05ac,
                              'idProduct': 0x4142, 'locationID': 123,
                              'USB Serial Number': serial,
                              'USB Product Name': 'Orchard Recovery'}])
        with patch.object(usb.sys, 'platform', 'darwin'), patch.object(
                usb.subprocess, 'run', return_value=subprocess.CompletedProcess([], 0, raw, b'')):
            return usb.devices()[0]

    def read_linux(self, serial):
        with tempfile.TemporaryDirectory() as root:
            node = Path(root) / '1-2'
            node.mkdir()
            for name, value in {'idVendor': '05ac', 'idProduct': '4142',
                                'product': 'Orchard Recovery', 'serial': serial}.items():
                (node / name).write_text(value)
            return linux_usb.devices(Path(root))[0]

    def test_handoff_matches_on_both_host_backends(self):
        for read in (self.read_macos, self.read_linux):
            with self.subTest(backend=read.__name__):
                row = read(f'{LEGACY} HND:{HANDOFF}')
                self.assertEqual(row['handoff_id'], HANDOFF)
                self.assertEqual((row['ecid'], row['chip'], row['board']), (2, 0x8942, 0x0a))
                self.assertEqual((row['boot_id'], row['profile']), (BOOT, 'p105'))

    def test_legacy_descriptor_keeps_no_handoff(self):
        for read in (self.read_macos, self.read_linux):
            with self.subTest(backend=read.__name__):
                self.assertIsNone(read(LEGACY)['handoff_id'])

    def test_malformed_or_duplicate_handoff_is_refused(self):
        fields = ('HND', 'HND:', f'HND:{HANDOFF[:-1]}', f'HND:{HANDOFF}0',
                  f'HND:{HANDOFF.upper()}', f'HND:{HANDOFF}!',
                  f'HND:{HANDOFF} HND:{HANDOFF}', f'HND:{HANDOFF} HND:',
                  f'HND:{HANDOFF},HND:{HANDOFF}', f'HND={HANDOFF}')
        for read in (self.read_macos, self.read_linux):
            for field in fields:
                with self.subTest(backend=read.__name__, field=field), self.assertRaisesRegex(
                        ValueError, 'USB handoff identity'):
                    read(f'{LEGACY} {field}')


if __name__ == '__main__':
    unittest.main()
