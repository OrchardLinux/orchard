"""The platform layer's boot step that replaces the Wi-Fi firmware placeholder address."""
import hashlib
import os
from pathlib import Path
import shutil
import subprocess
import tempfile
import unittest

from orchard.apple import trx

ROOT = Path(__file__).resolve().parents[1]
DEFAULTS = ROOT / 'system/defaults'
HELPER = DEFAULTS / 'usr/lib/orchard/wifi-address'
RULE = DEFAULTS / 'usr/lib/udev/rules.d/70-orchard-wifi-address.rules'
SERIAL = 'TESTSERIAL01'  # generated, not a unit's


def expected(serial):
    digest = hashlib.sha256(b'orchard-wifi-address:' + serial.encode()).digest()[:6]
    return ':'.join(f'{byte:02x}' for byte in bytes([(digest[0] & 0xfc) | 2]) + digest[1:])


@unittest.skipUnless(shutil.which('sha256sum') and shutil.which('od'), 'needs sha256sum and od')
class Helper(unittest.TestCase):
    def setUp(self):
        self.work = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.work)
        self.tree, self.net, self.bin = self.work / 'dt', self.work / 'net', self.work / 'bin'
        for directory in (self.tree, self.net, self.bin):
            directory.mkdir()
        self.calls = self.work / 'calls'
        for tool in ('ip', 'logger'):
            stub = self.bin / tool
            stub.write_text(f'#!/bin/sh\necho "{tool} $*" >> "{self.calls}"\n')
            stub.chmod(0o755)

    def interface(self, name, index, address):
        (self.net / name).mkdir()
        (self.net / name / 'ifindex').write_text(f'{index}\n')
        (self.net / name / 'address').write_text(address + '\n')

    def run_helper(self, index):
        env = {**os.environ, 'ORCHARD_DEVICE_TREE': str(self.tree), 'ORCHARD_SYSFS_NET': str(self.net),
               'PATH': f'{self.bin}:{os.environ["PATH"]}'}
        subprocess.run(['sh', str(HELPER), str(index)], env=env, check=True, timeout=30)
        return self.calls.read_text().splitlines() if self.calls.exists() else []

    def address_set(self, calls):
        found = [line.split() for line in calls if line.startswith('ip ')]
        self.assertEqual(len(found), 1)
        self.assertEqual(found[0][:5], ['ip', 'link', 'set', 'dev', 'wlan0'])
        return found[0][-1]

    def test_serial_gives_a_stable_locally_administered_address(self):
        self.interface('usb0', 2, '12:34:56:78:9a:bc')
        self.interface('wlan0', 3, trx.PLACEHOLDER_MAC)
        (self.tree / 'serial-number').write_bytes(SERIAL.encode() + b'\0')
        calls = self.run_helper(3)
        address = self.address_set(calls)
        self.assertEqual(address, expected(SERIAL))
        first = int(address[:2], 16)
        self.assertEqual(first & 3, 2)
        self.assertNotEqual(address, trx.PLACEHOLDER_MAC)
        # Neither the address nor the serial reaches the journal.
        log = [line for line in calls if line.startswith('logger ')]
        self.assertEqual(len(log), 1)
        self.assertNotIn(address, log[0])
        self.assertNotIn(SERIAL, log[0])
        self.calls.unlink()
        self.assertEqual(self.address_set(self.run_helper(3)), address)

    def test_different_units_get_different_addresses(self):
        self.assertNotEqual(expected('TESTSERIAL01'), expected('TESTSERIAL02'))

    def test_without_a_serial_a_random_address_for_this_boot(self):
        self.interface('wlan0', 3, trx.PLACEHOLDER_MAC)
        calls = self.run_helper(3)
        address = self.address_set(calls)
        self.assertRegex(address, r'^[0-9a-f]{2}(:[0-9a-f]{2}){5}$')
        self.assertEqual(int(address[:2], 16) & 3, 2)
        self.assertIn('no serial-number', calls[-1])

    def test_an_interface_with_its_own_address_is_left_alone(self):
        self.interface('wlan0', 3, '12:34:56:78:9a:bc')
        (self.tree / 'serial-number').write_text(SERIAL)
        self.assertEqual(self.run_helper(3), [])
        self.assertEqual(self.run_helper(9), [])


class Wiring(unittest.TestCase):
    def test_the_rule_matches_the_firmware_placeholder_and_runs_the_helper(self):
        rule = '\n'.join(line for line in RULE.read_text().splitlines() if not line.startswith('#'))
        self.assertIn(f'ATTR{{address}}=="{trx.PLACEHOLDER_MAC}"', rule)
        self.assertIn('ACTION=="add"', rule)
        self.assertIn('SUBSYSTEM=="net"', rule)
        self.assertIn('RUN+="/usr/lib/orchard/wifi-address $attr{ifindex}"', rule)
        self.assertIn(f'PLACEHOLDER={trx.PLACEHOLDER_MAC}\n', HELPER.read_text())
        self.assertTrue(HELPER.stat().st_mode & 0o111)

    def test_every_recipe_placeholder_is_the_one_the_boot_step_replaces(self):
        import json
        for path in sorted((ROOT / 'devices').glob('*/firmware.json')):
            for output in json.loads(path.read_text())['outputs']:
                for step in output['steps']:
                    if 'placeholder_mac' in step:
                        self.assertEqual(step['placeholder_mac'], trx.PLACEHOLDER_MAC, path)

    def test_no_real_or_legacy_address_in_the_recipes(self):
        text = (ROOT / 'devices/p105/firmware.json').read_text()
        self.assertNotIn('02:10:5a:05:00:03', text)


if __name__ == '__main__':
    unittest.main()
