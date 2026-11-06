# SPDX-License-Identifier: GPL-2.0-only
"""Run the startup helper's wake lifecycle against local, side-effect-free APIs."""
from pathlib import Path
import shutil
import subprocess
import sys
from tempfile import TemporaryDirectory
import unittest


ROOT = Path(__file__).resolve().parents[1]
SOURCE = ROOT / 'boot/ios-launcher/ios-start.c'
FIXTURE = ROOT / 'tests/fixtures/ios-launcher-wake.c'
WAKE_START = 'struct startup_wake {'


@unittest.skipUnless(sys.platform == 'darwin' and shutil.which('xcrun'),
                     'The wake fixture requires Darwin CoreFoundation and xcrun')
class IOSWakeTests(unittest.TestCase):
    def test_owned_wake_lifecycle(self):
        source = SOURCE.read_text()
        self.assertEqual(source.count(WAKE_START), 1,
                         'Expected one production wake implementation')
        actual = source[source.index(WAKE_START):]
        with TemporaryDirectory(prefix='orchard-p105-ios-wake-') as directory:
            temporary = Path(directory)
            (temporary / 'p105-ios-wake-source.inc').write_text(actual)
            executable = temporary / 'fixture'
            compiled = subprocess.run([
                'xcrun', 'clang', '-std=c11', '-O1', '-Wall', '-Wextra', '-Werror',
                '-framework', 'CoreFoundation', '-I', str(temporary), str(FIXTURE),
                '-o', str(executable),
            ], capture_output=True, text=True, timeout=60)
            self.assertEqual(compiled.returncode, 0, compiled.stdout + compiled.stderr)
            checked = subprocess.run([str(executable)], capture_output=True,
                                     text=True, timeout=15)
            self.assertEqual(checked.returncode, 0, checked.stdout + checked.stderr)
            self.assertIn('owned_wake_cases=31', checked.stdout.splitlines())


if __name__ == '__main__':
    unittest.main()
