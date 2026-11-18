"""One distro release, two roots: the index pins a manifest per root architecture, the board picks one.

[release] is the 64-bit root, [releases.armv7] the 32-bit one; an index of one release (every
release published so far) resolves as before. No device is touched.
"""
import copy
import hashlib
import json
from pathlib import Path
import sys
import tempfile
import tomllib
import unittest
from unittest import mock
import zipfile

from orchard import hardware, installer, releases
from orchard.hardware import ROOT
from orchard.image import publish
from orchard.manifests import sources

sys.path.insert(0, str(ROOT / 'tools'))
sys.path.insert(0, str(Path(__file__).resolve().parent))
from validate_manifest import ManifestError, validate  # noqa: E402
from test_armv7_direct import armv7_manifest  # noqa: E402
from test_install_direct import manifest as arm64_manifest  # noqa: E402


def write(directory, name, document):
    (directory / name).write_text(publish.toml(document))
    data = (directory / name).read_bytes()
    return {'file': name, 'bytes': len(data), 'sha256': hashlib.sha256(data).hexdigest()}


def built(directory, document):
    """A built release DIRECTORY of DOCUMENT, with every file it names, as a distribution's build leaves it."""
    directory.mkdir()
    for _, row in sources(document):
        data = (directory.name + '/' + row['file']).encode()
        (directory / row['file']).write_bytes(data)
        row.update(bytes=len(data), sha256=hashlib.sha256(data).hexdigest())
    release = write(directory, 'release.toml', document)
    write(directory, 'index.toml', {'schema': 2, 'kind': 'orchard.index', 'distro': document['distro'],
                                    'release': release})
    return directory


class Architecture(unittest.TestCase):
    def test_the_boards_chip_names_its_root(self):
        self.assertEqual(hardware.root_architecture(hardware.device('p105')), 'armv7')
        self.assertEqual(hardware.root_architecture(hardware.device('j71b')), 'aarch64')
        self.assertTrue(hardware.has_recovery(hardware.device('j71b')))
        self.assertFalse(hardware.has_recovery(hardware.device('p105')))


class Index(unittest.TestCase):
    def setUp(self):
        work = tempfile.TemporaryDirectory()
        self.addCleanup(work.cleanup)
        self.work = Path(work.name).resolve()
        self.arm64 = arm64_manifest()
        self.armv7 = armv7_manifest()
        self.index = {'schema': 2, 'kind': 'orchard.index', 'distro': 'omarchy-a',
                      'release': write(self.work, 'release.toml', self.arm64),
                      'releases': {'armv7': write(self.work, 'release-armv7.toml', self.armv7)}}
        self.path = self.work / 'index.toml'
        self.path.write_text(publish.toml(self.index))

    def select(self, board, index=None):
        if index is not None:
            self.path.write_text(publish.toml(index))
        with mock.patch.object(releases, 'method_implemented', return_value=True):
            return releases.select_manifest(self.path, board)[0]

    def test_the_index_names_a_release_per_root_architecture(self):
        validate(self.index, schema=True)
        self.assertEqual(dict(sources(self.index))['releases.armv7'], self.index['releases']['armv7'])
        for releases_table in ({'x86_64': self.index['release']}, {}):
            broken = copy.deepcopy(self.index)
            broken['releases'] = releases_table
            with self.subTest(releases=releases_table), self.assertRaises(ManifestError):
                validate(broken, schema=True)

    def test_each_board_installs_the_root_of_its_architecture(self):
        self.assertEqual(self.select('p105')['platforms'], self.armv7['platforms'])
        self.assertEqual(self.select('j71b')['platforms'], self.arm64['platforms'])

    def test_an_index_of_one_release_resolves_as_before(self):
        one = {key: value for key, value in self.index.items() if key != 'releases'}
        self.assertEqual(self.select('j71b', one)['platforms'], self.arm64['platforms'])
        # A 32-bit release alone: [release] is the one root, whatever its architecture.
        one['release'] = self.index['releases']['armv7']
        self.assertEqual(self.select('p105', one)['platforms'], self.armv7['platforms'])

    def test_a_release_without_the_boards_root_says_so(self):
        one = {key: value for key, value in self.index.items() if key != 'releases'}
        with self.assertRaisesRegex(ValueError, 'no armv7 root, which p105 needs'):
            self.select('p105', one)
        one['release'] = self.index['releases']['armv7']
        with self.assertRaisesRegex(ValueError, 'no aarch64 root, which j71b needs'):
            self.select('j71b', one)

    def test_a_root_that_does_not_cover_the_board_yet_says_that(self):
        with self.assertRaisesRegex(ValueError, 'does not cover n61 yet'):
            self.select('n61')


class Publishing(unittest.TestCase):
    def setUp(self):
        work = tempfile.TemporaryDirectory()
        self.addCleanup(work.cleanup)
        self.work = Path(work.name).resolve()
        self.arm64 = built(self.work / 'arm64', arm64_manifest())
        armv7 = armv7_manifest()
        armv7['distro'] = 'omarchy-a'
        self.armv7 = built(self.work / 'armv7', armv7)

    def test_two_built_releases_become_one_with_distinct_names(self):
        output = self.work / 'hosted'
        index = tomllib.loads(publish.publish([self.armv7, self.arm64], 'v1', output, distro='omarchy-a').read_text())
        base = 'https://github.com/OrchardLinux/omarchy-a/releases/download/v1/'
        validate(index, schema=True)
        self.assertEqual(index['release']['url'], base + 'release.toml')
        self.assertEqual(index['releases']['armv7']['url'], base + 'release-armv7.toml')
        arm64 = tomllib.loads((output / 'release.toml').read_text())
        armv7 = tomllib.loads((output / 'release-armv7.toml').read_text())
        validate(arm64, schema=True), validate(armv7, schema=True)
        self.assertEqual(arm64['root']['url'], base + 'root.blocks.zst')
        self.assertEqual(armv7['root']['url'], base + 'root-armv7.blocks.zst')
        self.assertEqual(armv7['root']['map']['url'], base + 'root-armv7.map.json')
        self.assertEqual(armv7['cascadia']['p105']['url'], base + 'cascadia-p105.bin')
        self.assertEqual((output / 'root-armv7.blocks.zst').read_bytes(), (self.armv7 / 'root.blocks.zst').read_bytes())
        self.assertEqual(sorted(path.name for path in output.iterdir()), [
            'cascadia-p105.bin', 'index.toml', 'install-tools-armv7.tar.gz', 'install-tools.tar.gz', 'logo.bin',
            'm1n1.bin', 'platform-apple-16k.tar.gz', 'platform-apple-armv7.tar.gz', 'pongo.bin', 'release-armv7.toml',
            'release.toml', 'root-armv7.blocks.zst', 'root-armv7.map.json', 'root.blocks.zst', 'root.map.json'])
        for document in (arm64, armv7):
            self.assertFalse([where for where, row in sources(document) if 'file' in row])

    def test_one_built_release_publishes_as_before(self):
        index = tomllib.loads(publish.publish(self.armv7, 'v1', self.work / 'hosted', distro='omarchy-a').read_text())
        self.assertNotIn('releases', index)
        self.assertTrue(index['release']['url'].endswith('/release.toml'))
        self.assertTrue((self.work / 'hosted/root.blocks.zst').is_file())

    def test_one_root_per_architecture(self):
        with self.assertRaisesRegex(ValueError, 'Two aarch64 releases'):
            publish.publish([self.arm64, self.arm64], 'v1', self.work / 'hosted', distro='omarchy-a')

    def test_the_installer_covers_the_boards_of_both_roots(self):
        loaders = {name: self.work / f'{name}.bin' for name in ('pongo', 'm1n1')}
        for path in loaders.values():
            path.write_bytes(path.stem.encode())
        installer.release(self.arm64, loaders=loaders)
        output = self.work / 'hosted'
        publish.publish([self.arm64, self.armv7], 'v1', output, distro='omarchy-a')
        with zipfile.ZipFile(output / 'orchard-install.pyz') as archive:
            self.assertEqual(json.loads(archive.read('core.json'))['boards'], ['j1', 'j71b', 'p105'])
            self.assertEqual(archive.read('release/index.toml'), (output / 'index.toml').read_bytes())


if __name__ == '__main__':
    unittest.main()
