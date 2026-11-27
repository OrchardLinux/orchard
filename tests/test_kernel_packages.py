"""The kernel as the root's own package: installed by the root's package manager."""
import hashlib
import io
from pathlib import Path
import tarfile
import tempfile
import unittest
from unittest import mock

from orchard.image import platform
from install_files import install_device as D


def tar(files, end=True):
    data = io.BytesIO()
    archive = tarfile.open(fileobj=data, mode='w')
    for name, content in files.items():
        member = tarfile.TarInfo(name)
        member.size = len(content)
        archive.addfile(member, io.BytesIO(content))
    if end:
        archive.close()
    return data.getvalue()


class Install(unittest.TestCase):
    def setUp(self):
        work = tempfile.TemporaryDirectory()
        self.addCleanup(work.cleanup)
        self.work = Path(work.name)
        self.root = self.work / 'root'
        (self.root / 'usr/lib').mkdir(parents=True)
        self.layer = self.work / 'platform.tar.gz'
        platform.write({'usr/lib/modules/7.3/vmlinuz': (b'kernel', 0o644)}, self.layer)
        data = self.layer.read_bytes()
        self.receipt = {'size': len(data), 'sha256': hashlib.sha256(data).hexdigest(), 'expanded_limit': 1 << 20}
        self.package = self.work / 'linux-orchard-apple-16k-0.4.0-1-aarch64.pkg.tar.zst'
        self.package.write_bytes(b'package')
        self.plan = {'path': str(self.package), 'base': 'pacman', 'bytes': 7,
                     'sha256': hashlib.sha256(b'package').hexdigest()}

    def test_the_roots_pacman_installs_the_uploaded_package(self):
        (self.root / 'usr/bin').mkdir(parents=True)
        (self.root / 'usr/bin/pacman').write_text('')
        with mock.patch.object(D, 'run') as run:
            result = D.install_kernel(self.layer, self.root, **self.receipt, package=self.plan)
        self.assertEqual(result, {'package': self.package.name, 'base': 'pacman'})
        run.assert_called_once_with('chroot', str(self.root), 'pacman', '-U', '--noconfirm',
                                    '/var/tmp/' + self.package.name, timeout=900)
        self.assertFalse((self.root / 'var/tmp' / self.package.name).exists())
        self.assertFalse((self.root / 'usr/lib/modules').exists())

    def test_a_package_for_another_base_or_other_bytes_is_refused(self):
        with self.assertRaisesRegex(D.InstallError, 'no pacman'):
            D.install_kernel(self.layer, self.root, **self.receipt, package=self.plan)
        (self.root / 'usr/bin').mkdir(parents=True)
        (self.root / 'usr/bin/pacman').write_text('')
        with self.assertRaisesRegex(D.InstallError, 'differs from the plan'):
            D.install_kernel(self.layer, self.root, **self.receipt, package={**self.plan, 'sha256': '0' * 64})

    def test_without_a_package_the_layers_files_go_in(self):
        with mock.patch.object(D.os, 'fchown'):
            self.assertEqual(D.install_kernel(self.layer, self.root, **self.receipt), {'files': 1})
        self.assertEqual((self.root / 'usr/lib/modules/7.3/vmlinuz').read_bytes(), b'kernel')


if __name__ == '__main__':
    unittest.main()
