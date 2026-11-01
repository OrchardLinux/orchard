"""Platform descriptors: platforms/<name>.toml is the one place a platform is described."""
import json
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import unittest

SOURCE = Path(__file__).resolve().parents[1]
VALID = '''schema = 1
kind = "hardware.platform"
name = "test-arm"
description = "test"

[kernel]
arch = "arm"
image = "zImage"
dtbs = "arch/arm/boot/dts/apple"
upstream = "kernel/upstream.lock"
series = "kernel/series/test-arm"
config = "kernel/configs/test-arm/config"
'''


class Platforms(unittest.TestCase):
    def series(self, *args, root=SOURCE):
        return subprocess.run([sys.executable, str(root / 'kernel/series.py'), *args],
                              capture_output=True, text=True)

    def test_every_platform_and_device_resolves_to_a_valid_descriptor(self):
        platforms = sorted(p.stem for p in (SOURCE / 'platforms').glob('*.toml'))
        self.assertIn('apple-16k', platforms)
        boards = [d.name for d in (SOURCE / 'devices').iterdir() if (d / 'device.toml').is_file()]
        for name in platforms + boards:
            with self.subTest(name=name):
                result = self.series('--platform', name)
                self.assertEqual(result.returncode, 0, result.stderr)
                described = json.loads(result.stdout)
                for key in ('upstream', 'series', 'config'):
                    self.assertTrue((SOURCE / described[key]).is_file(), described[key])

    def layout(self, descriptor):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        root = Path(temporary.name)
        (root / 'kernel').mkdir()
        shutil.copy2(SOURCE / 'kernel/series.py', root / 'kernel/series.py')
        for name, text in {'kernel/upstream.lock': 'url=x\ncommit=y\n',
                           'kernel/series/test-arm': '', 'kernel/configs/test-arm/config': '',
                           'platforms/test-arm.toml': descriptor}.items():
            (root / name).parent.mkdir(parents=True, exist_ok=True)
            (root / name).write_text(text)
        return root

    def test_a_32_bit_platform_is_described_like_any_other(self):
        result = self.series('--platform', 'test-arm', root=self.layout(VALID))
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(json.loads(result.stdout)['arch'], 'arm')
        self.assertEqual(json.loads(result.stdout)['image'], 'zImage')

    def test_broken_descriptors_are_refused(self):
        cases = {
            'unknown arch': VALID.replace('arch = "arm"', 'arch = "x86"'),
            'config outside the repository': VALID.replace('kernel/configs/test-arm/config', '../config'),
            'missing series': VALID.replace('kernel/series/test-arm', 'kernel/series/none'),
            'image with a path': VALID.replace('"zImage"', '"../zImage"'),
            'other name': VALID.replace('name = "test-arm"', 'name = "other"'),
            'other schema': VALID.replace('schema = 1', 'schema = 2'),
        }
        for case, text in cases.items():
            with self.subTest(case=case):
                result = self.series('--platform', 'test-arm', root=self.layout(text))
                self.assertNotEqual(result.returncode, 0)
                self.assertIn('platforms/test-arm.toml', result.stderr)


if __name__ == '__main__':
    unittest.main()
