"""J71t firmware recipe: the AOP images."""
import json
from pathlib import Path
import tomllib
import unittest

HARDWARE = Path(__file__).resolve().parents[1]
DEVICE = HARDWARE / 'devices/j71t/device.toml'


class J71tData(unittest.TestCase):
    def setUp(self):
        device = tomllib.loads(DEVICE.read_text())
        self.recipe = json.loads((DEVICE.parent / device['firmware']['recipe']).read_text())

    def test_aop_images_come_from_the_board_firmware(self):
        aop = self.recipe['inputs']['aop.im4p']
        self.assertEqual(aop['ipsw'], {'zip': 'Firmware/AOP/aopfw-ipad6faop.im4p'})
        self.assertEqual(aop['sha256'], '5a4d8378bb83e175fc589cc1cf978430639d7ffcef07b2d94e9d6a4801124efa')
        for name, role, digest in (
                ('aop-ipad6f.bin', 'AOP', 'af5d51a368ceb89dc5c359e7c84e54cb6082e308956be742c17622eb4a6047c8'),
                ('aop-ipad6f-ext.bin', 'AOP_ext', '2528d2686ed1262de9910c2d60e45b68137e50aa601a0f8d54ea7d1347b9de22')):
            output, = [o for o in self.recipe['outputs'] if o['destination'] == 'lib/firmware/apple/' + name]
            self.assertEqual(output['sha256'], digest)
            self.assertEqual(output['steps'], [{'step': 'im4p-payload@1', 'input': 'aop.im4p'},
                                               {'step': 'aop-image@1', 'coprocessor': role}])
            self.assertFalse((HARDWARE / output['destination']).exists())


if __name__ == '__main__':
    unittest.main()
