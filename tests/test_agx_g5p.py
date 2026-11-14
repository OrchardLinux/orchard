import hashlib
import unittest
from unittest.mock import Mock, patch
from orchard.apple import agx_g5p

class G5PFirmware(unittest.TestCase):
    def test_original_bytes_and_bundle(self):
        data = b'synthetic firmware, not Apple data'
        kernel = Mock()
        kernel.kext_sections.return_value = [dict(segment='__TEXT', name='__const', addr=4096, size=len(data))]
        kernel.read.return_value = data
        with patch.object(agx_g5p, 'Kernelcache', return_value=kernel), patch.object(agx_g5p, 'SIZE', len(data)), patch.object(agx_g5p, 'SHA256', hashlib.sha256(data).hexdigest()):
            self.assertIs(agx_g5p.extract(b'input'), data)
        kernel.kext_sections.assert_called_once_with(agx_g5p.BUNDLE, 'G5P firmware')
        kernel.read.assert_called_once_with(4096, len(data))

    def test_wrong_size_is_rejected_before_read(self):
        kernel = Mock()
        kernel.kext_sections.return_value = [dict(segment='__TEXT', name='__const', addr=4096, size=1)]
        with patch.object(agx_g5p, 'Kernelcache', return_value=kernel), self.assertRaisesRegex(ValueError, 'size'):
            agx_g5p.extract(b'input')
        kernel.read.assert_not_called()

    def test_wrong_digest_is_rejected(self):
        kernel = Mock()
        kernel.kext_sections.return_value = [dict(segment='__TEXT', name='__const', addr=4096, size=agx_g5p.SIZE)]
        kernel.read.return_value = bytes(agx_g5p.SIZE)
        with patch.object(agx_g5p, 'Kernelcache', return_value=kernel), self.assertRaisesRegex(ValueError, 'digest'):
            agx_g5p.extract(b'input')
