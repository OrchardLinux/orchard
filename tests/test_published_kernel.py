"""A distribution build takes the nightly's kernel when its source key matches this checkout."""
import contextlib
import hashlib
import io
import json
import os
from pathlib import Path
import sys
import tarfile
import tempfile
import unittest
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'src'))
from orchard import http, published  # noqa: E402
from orchard.hardware import ROOT  # noqa: E402


def bundle():
    data = io.BytesIO()
    with tarfile.open(fileobj=data, mode='w:gz') as tar:
        for name, content in (('components/kernel/Image.gz', b'kernel'),
                              ('components/kernel/kernel.release', b'7.3.0-test\n'),
                              ('components/boot/busybox', b'not the kernel')):
            info = tarfile.TarInfo(name)
            info.size = len(content)
            tar.addfile(info, io.BytesIO(content))
    return data.getvalue()


class PublishedKernel(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.cache = Path(temporary.name)
        self.bundle = bundle()
        self.keys = json.dumps({'schema': 1, 'platforms': {'apple-16k': {
            'key': published.source_key(ROOT, 'apple-16k'), 'asset': 'apple-16k-components.tar.gz'}}}).encode()
        self.fetched = []
        environment = mock.patch.dict(os.environ)
        environment.start()
        self.addCleanup(environment.stop)
        os.environ.pop('ORCHARD_KERNEL', None)

    def release(self, keys=None, bundle=None):
        keys, bundle = keys or self.keys, bundle or self.bundle
        sums = (f'{hashlib.sha256(self.bundle).hexdigest()}  ./apple-16k-components.tar.gz\n'
                f'{hashlib.sha256(self.keys).hexdigest()}  ./component-keys.json\n').encode()
        name = 'nightly-20261103-0123456789ab'
        files = {'nightly/latest.json': json.dumps({'name': name}).encode(), f'{name}/SHA256SUMS': sums,
                 f'{name}/component-keys.json': keys, f'{name}/apple-16k-components.tar.gz': bundle}
        def open_url(url, timeout=60):
            self.fetched.append(url.rsplit('/', 1)[1])
            return io.BytesIO(files['/'.join(url.rsplit('/', 2)[1:])])
        return mock.patch.object(http, 'open_url', side_effect=open_url)

    def kernel(self):
        with contextlib.redirect_stdout(io.StringIO()):
            return published.kernel(ROOT, self.cache, 'apple-16k')

    def test_a_matching_key_uses_the_published_kernel_and_reuses_it(self):
        with self.release():
            directory = self.kernel()
        self.assertEqual((directory / 'Image.gz').read_bytes(), b'kernel')
        self.assertFalse((directory.parent / 'boot').exists())
        with self.release():
            self.assertEqual(self.kernel(), directory)
        self.assertEqual(self.fetched.count('apple-16k-components.tar.gz'), 1)

    def test_another_kernel_state_compiles(self):
        other = json.dumps({'schema': 1, 'platforms': {'apple-16k': {
            'key': '0' * 64, 'asset': 'apple-16k-components.tar.gz'}}}).encode()
        self.keys = other
        with self.release():
            self.assertIsNone(self.kernel())
        self.assertNotIn('apple-16k-components.tar.gz', self.fetched)

    def test_keys_not_in_the_checksums_are_not_trusted(self):
        with self.release(keys=self.keys + b' '):
            self.assertIsNone(self.kernel())

    def test_a_tampered_bundle_is_refused(self):
        with self.release(bundle=self.bundle + b'x'), self.assertRaisesRegex(ValueError, 'does not match'):
            self.kernel()

    def test_local_or_offline_compiles(self):
        os.environ['ORCHARD_KERNEL'] = 'local'
        with self.release():
            self.assertIsNone(self.kernel())
        self.assertEqual(self.fetched, [])
        del os.environ['ORCHARD_KERNEL']
        with mock.patch.object(http, 'open_url', side_effect=OSError('offline')):
            self.assertIsNone(self.kernel())

    def test_the_key_follows_the_kernel_files_not_the_rest(self):
        inputs = published.source_inputs(ROOT, 'apple-16k')
        self.assertIn('kernel/configs/apple-16k/config', inputs['recipes'])
        self.assertTrue(any(name.startswith('kernel/patches/') for name in inputs['recipes']))
        self.assertFalse(any(name.startswith(('devices/', 'docs/', 'src/')) for name in inputs['recipes']))


if __name__ == '__main__':
    unittest.main()


class PublishedShared(unittest.TestCase):
    """m1n1 and the static tools come from the nightly the same way."""
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.cache = Path(temporary.name)
        environment = mock.patch.dict(os.environ)
        environment.start()
        self.addCleanup(environment.stop)
        os.environ.pop('ORCHARD_PUBLISHED', None)
        data = io.BytesIO()
        with tarfile.open(fileobj=data, mode='w:gz') as tar:
            for name in published.TOOLS + ('../escape',):
                if name.startswith('..'):
                    continue
                info = tarfile.TarInfo(f'components/boot/{name}')
                info.size, info.mode = 4, 0o755
                tar.addfile(info, io.BytesIO(b'tool'))
        self.tools, self.m1n1 = data.getvalue(), b'm1n1 binary'
        shared = published.shared_keys(ROOT)
        self.keys = json.dumps({'schema': 1, 'platforms': {}, 'shared': {
            'm1n1': {'key': shared['m1n1'], 'asset': 'm1n1.bin'},
            'tools': {'key': shared['tools'], 'asset': 'tool-components.tar.gz'}}}).encode()

    def release(self):
        name = 'nightly-20261103-0123456789ab'
        sums = ''.join(f'{hashlib.sha256(data).hexdigest()}  ./{file}\n' for file, data in
                       (('m1n1.bin', self.m1n1), ('tool-components.tar.gz', self.tools),
                        ('component-keys.json', self.keys))).encode()
        files = {'nightly/latest.json': json.dumps({'name': name}).encode(), f'{name}/SHA256SUMS': sums,
                 f'{name}/component-keys.json': self.keys, f'{name}/m1n1.bin': self.m1n1,
                 f'{name}/tool-components.tar.gz': self.tools}
        return mock.patch.object(http, 'open_url',
                                 side_effect=lambda url, timeout=60: io.BytesIO(files['/'.join(url.rsplit('/', 2)[1:])]))

    def test_m1n1_and_tools_from_the_nightly(self):
        with self.release(), contextlib.redirect_stdout(io.StringIO()):
            loader = published.m1n1(ROOT, self.cache)
            tools = published.tools(ROOT, self.cache)
        self.assertEqual(loader.read_bytes(), b'm1n1 binary')
        for name in published.TOOLS:
            self.assertTrue(os.access(tools / name, os.X_OK), name)

    def test_off_builds_them(self):
        os.environ['ORCHARD_PUBLISHED'] = 'off'
        with self.release():
            self.assertIsNone(published.m1n1(ROOT, self.cache))
            self.assertIsNone(published.tools(ROOT, self.cache))

    def test_ram_tools_reuse_the_verified_published_producer_without_local_build(self):
        from orchard import build
        with self.release(), mock.patch.object(build, '_prepare_tools', side_effect=AssertionError('local build')), \
                contextlib.redirect_stdout(io.StringIO()):
            tools = build.prepare_tools(ROOT, self.cache, device='j71b')
        for name in published.TOOLS:
            self.assertEqual((tools / name).read_bytes(), b'tool')

    def test_ram_tools_preserve_explicit_local_build_policy(self):
        from orchard import build
        os.environ['ORCHARD_PUBLISHED'] = 'off'
        with self.release(), mock.patch.object(build, '_prepare_tools', return_value=self.cache/'local') as local:
            self.assertEqual(build.prepare_tools(ROOT, self.cache, device='j71b'), self.cache/'local')
        local.assert_called_once_with(ROOT, self.cache/'components-v2')
