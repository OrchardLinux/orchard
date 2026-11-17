"""The module staging recipe supports native CI without changing local Podman."""
import importlib.util
from pathlib import Path
import sys
import tempfile
import unittest
from unittest import mock

SCRIPT = Path(__file__).resolve().parents[1] / 'kernel/stage-modules.py'


class ModuleStaging(unittest.TestCase):
    def test_native_depmod_never_requires_podman(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            build = root / 'build'
            (build / 'include/config').mkdir(parents=True)
            (build / 'include/config/kernel.release').write_text('test-release\n')
            for name in ('modules.builtin', 'modules.builtin.modinfo'):
                (build / name).write_text('')
            (build / 'test.ko').write_bytes(b'test-module')
            (build / 'modules.order').write_text('test.o\n')
            spec = importlib.util.spec_from_file_location('stage_modules', SCRIPT)
            module = importlib.util.module_from_spec(spec)
            spec.loader.exec_module(module)
            output = root / 'stage'
            with mock.patch.object(sys, 'argv', [str(SCRIPT), '--build', str(build),
                                                '--output', str(output)]), \
                    mock.patch.object(module.buildenv, 'native', return_value=True), \
                    mock.patch.object(module.shutil, 'which') as which, \
                    mock.patch.object(module.subprocess, 'run') as run:
                module.main()
            which.assert_not_called()
            self.assertEqual(run.call_args_list, [
                mock.call(['llvm-strip-19', '--strip-debug',
                           str(output / 'lib/modules/test-release/kernel/test.ko')], check=True),
                mock.call(['depmod', '-b', str(output.resolve()), 'test-release'], check=True),
            ])
            self.assertEqual((output / 'lib/modules/test-release/kernel/test.ko').read_bytes(),
                             b'test-module')

    def stage(self, build, output):
        spec = importlib.util.spec_from_file_location('stage_modules', SCRIPT)
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        with mock.patch.object(sys, 'argv', [str(SCRIPT), '--build', str(build), '--output', str(output)]), \
                mock.patch.object(module.buildenv, 'native', return_value=True), \
                mock.patch.object(module.subprocess, 'run'):
            module.main()

    def build_tree(self, root, order):
        build = root / 'build'
        (build / 'include/config').mkdir(parents=True)
        (build / 'include/config/kernel.release').write_text('test-release\n')
        for name in ('modules.builtin', 'modules.builtin.modinfo'):
            (build / name).write_text('')
        (build / 'modules.order').write_text(order)
        return build

    def test_a_module_left_from_an_earlier_config_is_not_staged(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            build = self.build_tree(root, 'drivers/a/now.o\n')
            for name in ('drivers/a/now.ko', 'drivers/b/stale.ko'):
                (build / name).parent.mkdir(parents=True, exist_ok=True)
                (build / name).write_bytes(b'x')
            self.stage(build, root / 'stage')
            modules = root / 'stage/lib/modules/test-release'
            self.assertTrue((modules / 'kernel/drivers/a/now.ko').is_file())
            self.assertFalse((modules / 'kernel/drivers/b/stale.ko').exists())
            self.assertEqual((modules / 'modules.order').read_text(), 'kernel/drivers/a/now.ko\n')

    def test_a_listed_module_that_was_not_built_is_refused(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            build = self.build_tree(root, 'a.o\nb.o\n')
            (build / 'a.ko').write_bytes(b'x')
            with self.assertRaises(SystemExit):
                self.stage(build, root / 'stage')

    def test_an_sdk_without_modules_stages_none(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            build = self.build_tree(root, 'a.o\n')
            self.stage(build, root / 'stage')
            self.assertEqual((root / 'stage/lib/modules/test-release/modules.order').read_text(), '')
