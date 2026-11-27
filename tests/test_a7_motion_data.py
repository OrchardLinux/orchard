"""The A7 iPads' motion sensors: the M7 firmware recipe and the catalog agree.

Every A7 iPad (s5l8960x-j*) has its accelerometer, gyroscope and compass behind
the M7 motion coprocessor on UART4, which the kernel's apple-oscar driver boots
with lib/firmware/apple/s5l8960x-oscar.bin from the board's restore image.
"""
import json
from pathlib import Path
import tomllib
import unittest

ROOT = Path(__file__).resolve().parents[1]
FIRMWARE = 'lib/firmware/apple/s5l8960x-oscar.bin'
SHA256 = '515adeca57c3021722b2e0553c21779a8035133955525a3db9cd25bf23d879a2'
SOURCE = 'usr/standalone/firmware/oscar/CoreMotionOscarFirmware.image'


def a7_ipads():
    for path in sorted(ROOT.glob('devices/*/device.toml')):
        described = tomllib.loads(path.read_text())
        if described.get('kernel', {}).get('dtb', '').startswith('s5l8960x-j'):
            yield path.parent.name, described


class A7MotionData(unittest.TestCase):
    def test_there_are_a7_ipads(self):
        self.assertGreaterEqual(len(list(a7_ipads())), 9)

    def test_every_a7_ipad_installs_the_m7_firmware(self):
        for board, _ in a7_ipads():
            with self.subTest(board=board):
                recipe = json.loads((ROOT / 'devices' / board / 'firmware.json').read_text())
                outputs = [o for o in recipe['outputs'] if o['destination'] == FIRMWARE]
                self.assertEqual(len(outputs), 1)
                self.assertEqual(outputs[0]['sha256'], SHA256)
                self.assertEqual(outputs[0]['steps'], [{'step': 'copy@1', 'input': 'CoreMotionOscarFirmware.image'}])
                source = recipe['inputs']['CoreMotionOscarFirmware.image']
                self.assertEqual(source, {'ipsw': {'rootfs': SOURCE}, 'sha256': SHA256})

    def test_the_catalog_knows_the_driver(self):
        wanted = tomllib.loads((ROOT / 'tools/catalog/feature-drivers.toml').read_text())
        self.assertIn('apple_oscar', wanted['accelerometer'])
        self.assertIn('apple_oscar', wanted['gyroscope'])

    def test_every_a7_ipad_states_its_motion_sensors(self):
        bound = json.loads((ROOT / 'tools/catalog/drivers.json').read_text())['boards']
        for board, described in a7_ipads():
            with self.subTest(board=board):
                for feature in ('accelerometer', 'gyroscope'):
                    self.assertIn(feature, bound[board])
                    self.assertIn(described['features'][feature], ('working', 'untested'))


if __name__ == '__main__':
    unittest.main()
