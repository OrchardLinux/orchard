"""ci/changed_kernels.py: a pull request compiles the kernel of exactly the platforms it changes."""
import importlib.util
from pathlib import Path
import shutil
import tempfile
import unittest

ROOT = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location('changed_kernels', ROOT / 'ci/changed_kernels.py')
changed_kernels = importlib.util.module_from_spec(spec)
spec.loader.exec_module(changed_kernels)

PLATFORMS = ['apple-16k', 'apple-4k', 'apple-armv7']


class ChangedKernels(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        # A copy of what decides a kernel; the checkout is the pull request's side.
        self.base = Path(temporary.name) / 'base'
        for name in ('kernel', 'src', 'platforms', 'devices', 'chips', 'features.toml', 'methods.toml'):
            source = ROOT / name
            if source.is_dir():
                shutil.copytree(source, self.base / name, ignore=shutil.ignore_patterns('__pycache__', '.work'))
            elif source.exists():
                shutil.copy2(source, self.base / name)

    def changed(self):
        return changed_kernels.changed(self.base, PLATFORMS)

    def test_the_same_sources_change_no_kernel(self):
        self.assertEqual(self.changed(), [])

    def test_a_config_changes_only_its_platform(self):
        with (self.base / 'kernel/configs/apple-4k/config').open('a') as config:
            config.write('# CONFIG_EXAMPLE is not set\n')
        self.assertEqual(self.changed(), ['apple-4k'])

    def test_a_shared_patch_layer_changes_both_arm64_platforms(self):
        patch = sorted((self.base / 'kernel/patches/drivers').rglob('*.patch'))[0]
        patch.write_text(patch.read_text() + '\n')
        self.assertEqual(self.changed(), ['apple-16k', 'apple-4k'])

    def test_an_armv7_patch_layer_changes_only_armv7(self):
        patch = sorted((self.base / 'kernel/patches/drivers/mtd').glob('*.patch'))[0]
        patch.write_text(patch.read_text() + '\n')
        self.assertEqual(self.changed(), ['apple-armv7'])

    def test_documentation_changes_no_kernel(self):
        (self.base / 'kernel/README.md').write_text('older notes\n')
        self.assertEqual(self.changed(), [])

    def test_a_build_script_changes_every_kernel(self):
        (self.base / 'kernel/build').write_text('#!/bin/bash\n')
        self.assertEqual(self.changed(), PLATFORMS)

    def test_a_platform_the_base_lacks_is_changed(self):
        (self.base / 'kernel/series/apple-armv7').unlink()
        self.assertEqual(self.changed(), ['apple-armv7'])


if __name__ == '__main__':
    unittest.main()
