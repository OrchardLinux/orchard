"""The kernel as the root's own package: found and checked on the host, installed by the root's package manager."""
import base64
import gzip
import hashlib
import io
import json
from pathlib import Path
import tarfile
import tempfile
import unittest
import urllib.error
from unittest import mock

from orchard import install_device as D
from orchard import kernel_packages as K
from orchard.image import platform


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


def apk(name, version):
    """An apk package as abuild writes it: signature, control and data, gzipped apart."""
    signature = gzip.compress(tar({'.SIGN.RSA.key.rsa.pub': b'sig'}, end=False))
    control = gzip.compress(tar({'.PKGINFO': f'pkgname = {name}\npkgver = {version}\n'.encode()}, end=False))
    return signature + control + gzip.compress(tar({'usr/lib/x': b'x'})), control


class Repository(unittest.TestCase):
    def test_pacman_index(self):
        index = tar({'linux-orchard-apple-16k-0.4.0-1/desc':
                     b'%FILENAME%\nlinux-orchard-apple-16k-0.4.0-1-aarch64.pkg.tar.zst\n\n%NAME%\nlinux-orchard-apple-16k\n\n'
                     b'%VERSION%\n0.4.0-1\n\n%SHA256SUM%\n' + b'a' * 64 + b'\n'})
        self.assertEqual(K.pacman_entry(gzip.compress(index), 'linux-orchard-apple-16k'),
                         {'file': 'linux-orchard-apple-16k-0.4.0-1-aarch64.pkg.tar.zst', 'version': '0.4.0-1',
                          'sha256': 'a' * 64})
        self.assertIsNone(K.pacman_entry(gzip.compress(index), 'linux-orchard-apple-4k'))

    def test_apk_identity_is_the_control_streams_checksum(self):
        data, control = apk('linux-orchard-apple-armv7', '0.4.0-r0')
        identity = 'Q1' + base64.b64encode(hashlib.sha1(control).digest()).decode()
        self.assertEqual(K.apk_identity(data), identity)
        index = gzip.compress(tar({'.SIGN.RSA.key.rsa.pub': b'sig'}, end=False)) + gzip.compress(
            tar({'APKINDEX': f'C:{identity}\nP:linux-orchard-apple-armv7\nV:0.4.0-r0\nA:armv7\n\n'.encode()}))
        self.assertEqual(K.apk_entry(index, 'linux-orchard-apple-armv7'),
                         {'file': 'linux-orchard-apple-armv7-0.4.0-r0.apk', 'version': '0.4.0-r0', 'identity': identity})

    def test_resolve_and_download_check_the_file_against_the_index(self):
        data, control = apk('linux-orchard-apple-armv7', '0.4.0-r0')
        identity = 'Q1' + base64.b64encode(hashlib.sha1(control).digest()).decode()
        index = gzip.compress(tar({'APKINDEX': f'C:{identity}\nP:linux-orchard-apple-armv7\nV:0.4.0-r0\n\n'.encode()}))
        answers = {K.REPOSITORY + '/armv7/APKINDEX.tar.gz': index,
                   K.REPOSITORY + '/armv7/linux-orchard-apple-armv7-0.4.0-r0.apk': data,
                   K.PIN.format(platform='apple-armv7'): b'{"orchard": "0.4.0", "sha256": "aa"}'}
        with mock.patch.object(K, 'open_url', lambda url, timeout: io.BytesIO(answers[url])):
            # Another layer (a nightly, a local build) keeps its own kernel: no package.
            self.assertIsNone(K.resolve('apk', 'apple-armv7', 'arm', 'bb'))
            entry = K.resolve('apk', 'apple-armv7', 'arm', 'aa')
            self.assertEqual(entry['url'], K.REPOSITORY + '/armv7/linux-orchard-apple-armv7-0.4.0-r0.apk')
            with tempfile.TemporaryDirectory() as directory:
                self.assertEqual(K.download(entry, directory).read_bytes(), data)
                answers[entry['url']] = apk('linux-orchard-apple-armv7', '0.4.1-r0')[0]
                with self.assertRaisesRegex(ValueError, 'differs from the package index'):
                    K.download(entry, directory)
            self.assertIsNone(K.apk_entry(index, 'none'))

    def test_an_unreachable_repository_is_an_error_and_a_missing_package_is_none(self):
        with mock.patch.object(K, 'open_url', side_effect=OSError('offline')):
            with self.assertRaises(K.Unavailable):
                K.resolve('pacman', 'apple-16k', 'arm64', 'aa')
        missing = urllib.error.HTTPError('url', 404, 'Not Found', {}, None)
        with mock.patch.object(K, 'open_url', side_effect=missing):
            self.assertIsNone(K.resolve('pacman', 'apple-16k', 'arm64', 'aa'))
        failing = urllib.error.HTTPError('url', 503, 'Unavailable', {}, None)
        with mock.patch.object(K, 'open_url', side_effect=failing):
            with self.assertRaises(K.Unavailable):
                K.resolve('pacman', 'apple-16k', 'arm64', 'aa')

    def test_the_layer_names_its_platform(self):
        with tempfile.TemporaryDirectory() as directory:
            layer = Path(directory) / 'layer.tar.gz'
            platform.write({'usr/lib/orchard/platform.json': (json.dumps(
                {'platform': 'apple-armv7', 'architecture': 'arm', 'kernel_release': '6.12'}).encode(), 0o644)}, layer)
            self.assertEqual(K.layer_platform(layer), ('apple-armv7', 'arm'))


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
