"""A cached kernel is keyed by what determines it, not by the text of the scripts that build it."""
from pathlib import Path
import shutil
import tempfile
import unittest
from unittest.mock import patch

from orchard import build, kernel

DESCRIPTOR = {'platform': 'apple-16k', 'arch': 'arm64', 'image': 'Image.gz', 'dtbs': 'arch/arm64/boot/dts/apple',
              'upstream': 'kernel/upstream.lock',
              'config': 'kernel/configs/apple-16k/config', 'defconfig': 'orchard_apple16k_defconfig',
              'branch': 'main'}


class KernelCacheKey(unittest.TestCase):
    def setUp(self):
        self.hardware = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.hardware)
        for name, text in {'kernel/configs/apple-16k/config': 'CONFIG_A=y\n', 'kernel/Containerfile': 'FROM x\n',
                           'kernel/install-toolchain': 'apt-get install clang-19\n', 'kernel/build': 'make\n',
                           'kernel/series.py': 'series\n', 'kernel/stage-modules.py': 'stage\n',
                           'producers/buildenv.py': 'env\n', 'kernel/build-modules': 'modules\n',
                           'kernel/modules/j71b/Makefile': 'obj-m += x.o\n'}.items():
            path = self.hardware / name
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(text)
        self.described = dict(DESCRIPTOR)
        self.tree = 'a' * 40
        for patcher in (patch.object(kernel, 'layout', return_value=('apple-16k', 't8010-j71b')),
                        patch.object(kernel, 'descriptor', side_effect=lambda *_: dict(self.described)),
                        patch.object(kernel, 'source_tree', side_effect=lambda *_: self.tree)):
            patcher.start()
            self.addCleanup(patcher.stop)

    def base(self):
        return build.kernel_base_key(self.hardware, 'j71b', None)

    def device(self):
        return build.keyed(lambda: build.kernel_inputs(self.hardware, 'j71b', 'base'))()

    def test_editing_the_build_scripts_keeps_cached_kernels(self):
        base, device = self.base(), self.device()
        for name in ('kernel/build', 'kernel/series.py', 'kernel/stage-modules.py', 'producers/buildenv.py',
                     'kernel/build-modules'):
            (self.hardware / name).write_text('refactored\n')
        self.assertEqual(self.base(), base)
        self.assertEqual(self.device(), device)

    def test_descriptor_fields_that_build_nothing_keep_cached_kernels(self):
        base = self.base()
        self.described.update(branch='a5', defconfig='other_defconfig')
        self.assertEqual(self.base(), base)

    def test_what_determines_the_kernel_changes_the_key(self):
        changes = {
            'config': lambda: (self.hardware / DESCRIPTOR['config']).write_text('CONFIG_A=n\n'),
            'toolchain': lambda: (self.hardware / 'kernel/install-toolchain').write_text('clang-20\n'),
            'source tree': lambda: setattr(self, 'tree', 'b' * 40),
            'arch': lambda: self.described.update(arch='arm'),
            'image': lambda: self.described.update(image='zImage'),
        }
        for name, change in changes.items():
            with self.subTest(name=name):
                before = self.base()
                change()
                self.assertNotEqual(self.base(), before)

    def test_raising_the_producer_version_rebuilds_base_and_device_layer(self):
        base, device = self.base(), self.device()
        with patch.object(build, 'KERNEL_PRODUCER', build.KERNEL_PRODUCER + 1):
            self.assertNotEqual(self.base(), base)
            self.assertNotEqual(self.device(), device)

    def test_a_boards_own_module_sources_change_its_device_layer(self):
        device = self.device()
        (self.hardware / 'kernel/modules/j71b/Makefile').write_text('obj-m += y.o\n')
        self.assertNotEqual(self.device(), device)


if __name__ == '__main__':
    unittest.main()
