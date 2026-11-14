import hashlib
import json
from pathlib import Path
import unittest
from unittest.mock import Mock, patch

from orchard.apple import agx

ROOT = Path(__file__).resolve().parents[1]


class G5PFirmware(unittest.TestCase):
    def test_original_bytes_and_bundle(self):
        data = b'synthetic firmware, not Apple data'
        kernel = Mock()
        kernel.kext_sections.return_value = [dict(segment='__TEXT', name='__const', addr=4096, size=len(data))]
        kernel.read.return_value = data
        with patch.object(agx, 'Kernelcache', return_value=kernel), patch.object(agx, 'G5P_SIZE', len(data)), \
                patch.object(agx, 'G5P_SHA256', hashlib.sha256(data).hexdigest()):
            self.assertIs(agx.extract_g5p(b'input'), data)
        kernel.kext_sections.assert_called_once_with(agx.G5P, 'G5P firmware')
        kernel.read.assert_called_once_with(4096, len(data))

    def test_wrong_size_is_rejected_before_read(self):
        kernel = Mock()
        kernel.kext_sections.return_value = [dict(segment='__TEXT', name='__const', addr=4096, size=1)]
        with patch.object(agx, 'Kernelcache', return_value=kernel), self.assertRaisesRegex(ValueError, 'size'):
            agx.extract_g5p(b'input')
        kernel.read.assert_not_called()

    def test_wrong_digest_is_rejected(self):
        kernel = Mock()
        kernel.kext_sections.return_value = [dict(segment='__TEXT', name='__const', addr=4096, size=agx.G5P_SIZE)]
        kernel.read.return_value = bytes(agx.G5P_SIZE)
        with patch.object(agx, 'Kernelcache', return_value=kernel), self.assertRaisesRegex(ValueError, 'digest'):
            agx.extract_g5p(b'input')

    def test_j71t_recipe_writes_the_pinned_image(self):
        recipe = json.loads((ROOT / 'devices/j71t/firmware.json').read_text())
        output, = [o for o in recipe['outputs'] if o['destination'] == 'lib/firmware/apple/gfx-s8003.bin']
        self.assertEqual(output['sha256'], agx.G5P_SHA256)
        self.assertEqual(output['steps'], [{'step': 'agx-g5p-ldr-from-kernelcache@1', 'input': 'kernelcache'}])


if __name__ == '__main__':
    unittest.main()
