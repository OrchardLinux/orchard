"""ci/reuse_kernel.py: a kernel job takes the nightly's kernel only for the same sources and build scripts."""
import hashlib
import importlib.util
import io
import json
from pathlib import Path
import tarfile
import tempfile
import unittest
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location('reuse_kernel', ROOT / 'ci/reuse_kernel.py')
reuse_kernel = importlib.util.module_from_spec(spec)
spec.loader.exec_module(reuse_kernel)

from orchard import published  # noqa: E402

TAG = 'nightly-20260101-000000000000'
BASE = 'https://example.invalid/download'


def archive(files):
    data = io.BytesIO()
    with tarfile.open(fileobj=data, mode='w:gz') as tar:
        for name, content in files.items():
            member = tarfile.TarInfo(name)
            member.size = len(content)
            tar.addfile(member, io.BytesIO(content))
    return data.getvalue()


class ReuseKernel(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.output = Path(temporary.name) / 'out'
        self.key = published.source_key(ROOT, 'apple-armv7')
        scripts = {name: (ROOT / name).read_bytes() for name in reuse_kernel.checkout_scripts(ROOT)}
        self.assets = {
            'orchard-source.tar.gz': archive({**scripts, 'README.md': b'x'}),
            'apple-armv7-components.tar.gz': archive({
                'components/kernel/kernel.release': b'6.12.0-orchard-apple-armv7\n',
                'components/kernel/zImage': b'kernel',
                'components/kernel-toolchain-packages.lock': b'clang-19\n',
                'components/components.json': b'{}'}),
            'kernel-source-apple-armv7.tar.gz': archive({'./Makefile': b'all:\n'}),
        }
        self.publish(self.key)

    def publish(self, key):
        keys = json.dumps({'schema': 1, 'platforms': {
            'apple-armv7': {'key': key, 'asset': 'apple-armv7-components.tar.gz'}}}).encode()
        self.assets[published.KEYS] = keys
        self.sums()

    def sums(self):
        self.assets[published.SUMS] = ''.join(
            f'{hashlib.sha256(data).hexdigest()}  ./{name}\n'
            for name, data in self.assets.items() if name != published.SUMS).encode()

    def run_reuse(self):
        def fetch(url, limit=None):
            if url == f'{BASE}/nightly/latest.json':
                return json.dumps({'name': TAG}).encode()
            name = url.removeprefix(f'{BASE}/{TAG}/')
            if name not in self.assets:
                raise OSError('404 ' + url)
            return self.assets[name]

        def download(url, path, checksum):
            data = fetch(url)
            if hashlib.sha256(data).hexdigest() != checksum:
                raise ValueError(f'{path.name} does not match SHA256SUMS')
            path.write_bytes(data)
        with mock.patch.object(published, '_fetch', fetch), mock.patch.object(reuse_kernel, 'download', download):
            return reuse_kernel.reuse('apple-armv7', self.output, releases=BASE)

    def test_same_key_and_scripts_give_the_kernel_artifact_and_the_unchanged_source(self):
        self.assertEqual(self.run_reuse(), TAG)
        self.assertEqual(sorted(path.name for path in self.output.iterdir()),
                         ['kernel-components-apple-armv7.tar.gz', 'kernel-source-apple-armv7.tar.gz'])
        self.assertEqual((self.output / 'kernel-source-apple-armv7.tar.gz').read_bytes(),
                         self.assets['kernel-source-apple-armv7.tar.gz'])
        with tarfile.open(self.output / 'kernel-components-apple-armv7.tar.gz') as tar:
            # The kernel and its toolchain lock, as the compile job archives them; nothing else of the bundle.
            self.assertEqual(sorted(tar.getnames()), ['components/kernel-toolchain-packages.lock',
                                                      'components/kernel/kernel.release', 'components/kernel/zImage'])
            self.assertEqual(tar.extractfile('components/kernel/zImage').read(), b'kernel')

    def refused(self, reason):
        with self.assertRaisesRegex(ValueError, reason):
            self.run_reuse()
        self.assertFalse(self.output.exists() and any(self.output.iterdir()))

    def test_another_kernel_state_compiles(self):
        self.publish('0' * 64)
        self.refused('another kernel state')

    def test_a_changed_build_script_compiles_although_the_key_matches(self):
        self.assets['orchard-source.tar.gz'] = archive({'kernel/build': b'#!/bin/sh\n'})
        self.sums()
        self.refused('build scripts changed since .*kernel/build')

    def test_a_download_that_differs_from_the_checksums_compiles(self):
        self.assets['apple-armv7-components.tar.gz'] += b'x'
        self.refused('does not match SHA256SUMS')

    def test_keys_that_differ_from_the_checksums_compile(self):
        self.assets[published.KEYS] += b' '
        self.refused('does not match its SHA256SUMS')

    def test_no_nightly_compiles(self):
        del self.assets[published.SUMS]
        self.refused('no nightly to ask')

    def test_every_guarded_script_exists_or_is_a_directory_that_may_appear(self):
        for name in reuse_kernel.GUARDED:
            if name != 'kernel/modules':
                self.assertTrue((ROOT / name).is_file(), name)


if __name__ == '__main__':
    unittest.main()
