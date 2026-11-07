# SPDX-License-Identifier: GPL-2.0-only
"""The production iOS boot lease permits one prepared start per iOS boot."""
from pathlib import Path
import shutil
import subprocess
import sys
from tempfile import TemporaryDirectory
import unittest


ROOT = Path(__file__).resolve().parents[1]
SOURCE = ROOT / 'boot/ios-launcher/ios-start.c'
FIXTURE = ROOT / 'tests/fixtures/ios-boot-lease.c'


@unittest.skipUnless(sys.platform == 'darwin' and shutil.which('xcrun'),
                     'Native iOS lease checks need the Apple SDK')
class IOSBootLease(unittest.TestCase):
    def test_actual_lease_is_secure_durable_and_limited_to_one_start_per_boot(self):
        source = SOURCE.read_text()
        start = source.index('static int lease_number(')
        stop = source.index('\nstruct startup_wake {', start)
        actual = source[start:stop]
        with TemporaryDirectory(prefix='orchard-ios-boot-lease-') as temporary:
            work = Path(temporary)
            (work / 'ios-boot-lease-source.inc').write_text(actual)
            executable = work / 'fixture'
            compiled = subprocess.run([
                'xcrun', '--sdk', 'macosx', 'clang', '-std=c11', '-Wall', '-Wextra', '-Werror',
                '-fsanitize=address,undefined', '-fno-omit-frame-pointer', '-I', str(work),
                str(FIXTURE), '-o', str(executable),
            ], capture_output=True, text=True, timeout=60)
            self.assertEqual(compiled.returncode, 0, compiled.stdout + compiled.stderr)
            result = subprocess.run(
                [str(executable), str(work / 'lease')], capture_output=True, text=True, timeout=15)
            self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
            self.assertIn('native-ios-boot-lease-passed', result.stdout.splitlines())
            cases = {line.split('"')[3] for line in result.stdout.splitlines()
                     if line.startswith('{"case":')}
            self.assertEqual(cases, {
                'first-acquisition', 'same-boot-refusal', 'changed-boot', 'concurrent-lock',
                'canonical-minimum',
                'malformed-prefix', 'malformed-v1', 'noncanonical-v1', 'embedded-nul',
                'trailing-bytes', 'seconds-overflow', 'microseconds-range', 'zero-seconds',
                'wrong-owner', 'wrong-mode', 'oversized-state', 'short-read', 'symlink',
                'short-write-residue', 'zero-write-residue', 'failed-fsync-residue',
                'prepare-failure-consumes',
            })


if __name__ == '__main__':
    unittest.main()
