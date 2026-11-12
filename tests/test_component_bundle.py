"""Public component archives retain integrity and reject changed inputs."""
import importlib.util
from pathlib import Path
import tarfile
import tempfile
import unittest

ROOT = Path(__file__).resolve().parents[1]


def load(name):
    spec = importlib.util.spec_from_file_location(name, ROOT / 'ci' / (name + '.py'))
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


package_components = load('package_components')


class ComponentBundle(unittest.TestCase):
    def fixtures(self, root):
        for name in ('kernel/Image.gz', 'kernel/j71b.dtb', 'kernel/kernel.release',
                     'kernel/lib/modules/test/extra/test.ko', 'boot/busybox',
                     'boot/dropbear', 'boot/dropbearkey', 'boot/wpa_supplicant', 'boot/wpa_cli',
                     'boot/iw', 'quiet/m1n1.bin', 'quiet/pongo.bin'):
            path = root / name
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(name.encode())
            path.chmod(0o755)

    def test_package_keeps_executable_modes(self):
        with tempfile.TemporaryDirectory() as directory:
            parent = Path(directory)
            root = parent / 'components'
            self.fixtures(root)
            package_components.package(root, parent / 'bundle.tar.gz')
            with tarfile.open(parent / 'bundle.tar.gz') as archive:
                member = archive.getmember('components/boot/dropbear')
                self.assertEqual(member.mode & 0o777, 0o755)

    def test_a_platform_on_another_loader_is_its_kernel_alone(self):
        with tempfile.TemporaryDirectory() as directory:
            parent = Path(directory)
            root = parent / 'components'
            for name in ('kernel/zImage', 'kernel/dtbs/s5l8942x-p105.dtb', 'kernel/kernel.release',
                         'kernel/lib/modules/test/extra/test.ko'):
                path = root / name
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_bytes(name.encode())
            package_components.package(root, parent / 'bundle.tar.gz', platform='apple-armv7')
            with tarfile.open(parent / 'bundle.tar.gz') as archive:
                names = archive.getnames()
            self.assertIn('components/kernel/zImage', names)
            self.assertNotIn('components/quiet/quiet-boot.json', names)

    def test_a_platform_on_m1n1_needs_its_tools(self):
        with tempfile.TemporaryDirectory() as directory:
            parent = Path(directory)
            root = parent / 'components'
            self.fixtures(root)
            (root / 'kernel/dtbs').mkdir()
            (root / 'kernel/dtbs/t8010-j71b.dtb').write_bytes(b'dtb')
            (root / 'boot/iw').unlink()
            with self.assertRaises(ValueError):
                package_components.package(root, parent / 'bundle.tar.gz', platform='apple-16k')

    def test_missing_modules_or_links_are_not_packaged(self):
        with tempfile.TemporaryDirectory() as directory:
            parent = Path(directory)
            root = parent / 'components'
            self.fixtures(root)
            module = root / 'kernel/lib/modules/test/extra/test.ko'
            module.unlink()
            with self.assertRaisesRegex(ValueError, 'modules are missing'):
                package_components.package(root, parent / 'bundle.tar.gz')
            module.write_bytes(b'module')
            (root / 'linked').symlink_to(root / 'kernel/Image.gz')
            with self.assertRaisesRegex(ValueError, 'Linked component'):
                package_components.package(root, parent / 'bundle.tar.gz')


if __name__ == '__main__':
    unittest.main()
