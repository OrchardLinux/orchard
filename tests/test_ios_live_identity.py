# SPDX-License-Identifier: GPL-2.0-only
"""The iOS helper accepts the exact live identity representation it checked."""
import json
from pathlib import Path
import shutil
import subprocess
import sys
from tempfile import TemporaryDirectory
import unittest

ROOT = Path(__file__).resolve().parents[1]


@unittest.skipUnless(sys.platform == 'darwin' and shutil.which('xcrun'),
                     'Native live identity checks need CoreFoundation')
class NativeLiveIdentity(unittest.TestCase):
    def test_named_read_only_properties_have_exact_type_width_endian_and_alias_guards(self):
        with TemporaryDirectory() as temporary:
            executable = Path(temporary) / 'ios-live-identity'
            command = ['xcrun', '--sdk', 'macosx', 'clang', '-std=c11', '-Wall', '-Wextra', '-Werror',
                       '-fsanitize=address,undefined', '-fno-omit-frame-pointer',
                       '-I', str(ROOT / 'boot/ios-launcher'),
                       str(ROOT / 'tests/fixtures/ios-live-identity.c'),
                       '-framework', 'CoreFoundation', '-o', str(executable)]
            subprocess.run(command, check=True, capture_output=True, text=True, timeout=60)
            result = subprocess.run([str(executable)], check=True, capture_output=True, text=True, timeout=10)
        records = result.stdout.splitlines()
        self.assertEqual(records.pop(), 'native-live-identity-checks-passed')
        values = [json.loads(line) for line in records]
        self.assertEqual(len(values), 40)
        successful = [row for row in values if row['accepted']]
        self.assertEqual([row['case'] for row in successful], ['valid', 'fresh-ecid', 'ecid-reversed'])
        self.assertEqual([row['ecid'] for row in successful],
                         ['0102030405060708', '0102030405060709', '0807060504030201'])
        for row in values:
            with self.subTest(case=row['case']):
                self.assertEqual(row['opened'], row['closed'])
                self.assertLessEqual(row['properties'], 5)
                if not row['accepted']:
                    self.assertEqual(row['ecid'], 'a5a5a5a5a5a5a5a5')
        for row in values[:10]:
            if row['case'].startswith('sysctl-') or row['case'].startswith('wrong-'):
                self.assertEqual(row['opened'], 0)


if __name__ == '__main__':
    unittest.main()
