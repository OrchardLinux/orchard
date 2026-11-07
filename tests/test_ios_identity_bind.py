# SPDX-License-Identifier: GPL-2.0-only
"""The native iOS encoder shares the public handoff wire format and bounds."""
import base64
import json
from pathlib import Path
import shutil
import subprocess
from tempfile import TemporaryDirectory
import unittest

from orchard import recovery_identity

ROOT = Path(__file__).resolve().parents[1]


@unittest.skipUnless(shutil.which('clang'), 'Native identity checks need clang')
class NativeIdentityBinding(unittest.TestCase):
    def test_native_encoder_matches_codec_and_keeps_mutations_in_checked_slots(self):
        with TemporaryDirectory() as temporary:
            executable = Path(temporary) / 'ios-identity-bind'
            command = ['clang', '-std=c11', '-Wall', '-Wextra', '-Werror',
                       '-fsanitize=address,undefined', '-fno-omit-frame-pointer',
                       '-I', str(ROOT / 'boot/ios-launcher'),
                       str(ROOT / 'tests/fixtures/ios-identity-bind.c'), '-o', str(executable)]
            subprocess.run(command, check=True, capture_output=True, text=True, timeout=60)
            result = subprocess.run([str(executable)], check=True, capture_output=True, text=True, timeout=10)
        records = result.stdout.splitlines()
        self.assertEqual(records.pop(), 'native-boundary-checks-passed')
        expected = [
            {'profile': 'p105', 'ecid': 123, 'chip': 0x8942, 'board': 10,
             'handoff_id': '0123456789abcdef0123456789abcdef', 'template_sha256': 'a' * 64},
            {'profile': 'a', 'ecid': 1, 'chip': 1, 'board': 0,
             'handoff_id': '0' * 31 + '1', 'template_sha256': '0' * 64},
            {'profile': 'a' * 80, 'ecid': (1 << 64) - 1, 'chip': (1 << 32) - 1, 'board': (1 << 32) - 1,
             'handoff_id': 'f' * 32, 'template_sha256': 'f' * 64},
        ]
        self.assertEqual(len(records), len(expected))
        for token, fields in zip(records, expected):
            value = {'schema': 1, 'source': 'ios-device', **fields}
            with self.subTest(profile=fields['profile']):
                self.assertEqual(token, recovery_identity.encode_parameter(recovery_identity.HANDOFF_PARAMETER, value))
                encoded = token.partition('=')[2]
                self.assertNotIn('=', encoded)
                raw = base64.urlsafe_b64decode(encoded + '=' * (-len(encoded) % 4))
                self.assertEqual(json.loads(raw), value)
                self.assertLessEqual(len(token), recovery_identity.MAX_CMDLINE_BYTES)


if __name__ == '__main__':
    unittest.main()
