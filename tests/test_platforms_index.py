"""platforms.toml and what it names: everything a distribution takes from an orchard build."""
import hashlib
from pathlib import Path
import tarfile
import tempfile
import tomllib
import unittest
from unittest import mock

from orchard import hardware
from orchard.hardware import ROOT
from orchard.image import platform, platforms_index


class PlatformsIndex(unittest.TestCase):
    def setUp(self):
        work = tempfile.TemporaryDirectory()
        self.addCleanup(work.cleanup)
        self.work = Path(work.name)
        self.published = self.work / 'publication'
        self.published.mkdir()
        for loader in platforms_index.LOADERS:
            (self.published / loader).write_bytes(loader.encode())
        self.tools = self.work / 'tools'
        self.tools.mkdir()
        for tool in (*platforms_index.INSTALL_TOOLS, 'busybox', 'iw'):
            (self.tools / tool).write_bytes(tool.encode())

    def build(self):
        def layer(name, directory, *, bundle, busybox):
            self.assertEqual(bundle, self.work / 'kernels' / name / 'components/kernel')
            self.assertEqual(busybox, self.tools / 'busybox')
            path = directory / f'platform-{name}.tar.gz'
            path.write_bytes(name.encode())
            return {'file': path.name, 'bytes': path.stat().st_size, 'sha256': hashlib.sha256(name.encode()).hexdigest(),
                    'kernel_release': '6.18.0-' + name, 'boards': ['j71b']}
        with mock.patch.object(platform, 'build', layer):
            return platforms_index.build(self.published, tools=self.tools, commit='abc',
                                         kernels={name: self.work / 'kernels' / name / 'components/kernel'
                                                  for name in platform.platforms()})

    def test_the_index_names_every_file_by_relative_name_with_its_hash(self):
        self.build()
        index = tomllib.loads((self.published / 'platforms.toml').read_text())
        self.assertEqual((index['schema'], index['kind'], index['commit']), (1, 'orchard.platforms', 'abc'))
        self.assertEqual(sorted(index['platforms']), platform.platforms())
        self.assertNotIn('apple-armv7', index['platforms'])
        entries = [*index['loaders'].values(), index['install_tools'], index['board_data'], *index['platforms'].values()]
        self.assertEqual(sorted(index['loaders']), ['m1n1', 'pongo'])
        for entry in entries:
            data = (self.published / entry['file']).read_bytes()
            self.assertEqual((entry['bytes'], entry['sha256']), (len(data), hashlib.sha256(data).hexdigest()))
            self.assertNotIn('/', entry['file'])

    def test_board_data_is_the_board_files_and_reproducible(self):
        self.build()
        archive = self.published / 'board-data.tar.gz'
        first = archive.read_bytes()
        with tarfile.open(archive) as opened:
            names = opened.getnames()
            self.assertEqual(opened.extractfile('devices/j71b/device.toml').read(),
                             hardware.board_data_files()['devices/j71b/device.toml'].read_bytes())
        self.assertIn('features.toml', names)
        self.assertIn('chips/t8010.toml', names)
        self.assertIn('platforms/apple-16k.toml', names)
        self.assertTrue(all(name.split('/')[0] in hardware.BOARD_DATA for name in names))
        archive.unlink()
        platforms_index.write_board_data(archive)
        self.assertEqual(archive.read_bytes(), first)

    def test_install_tools_are_only_the_installer_programs_and_reproducible(self):
        self.build()
        archive = self.published / 'install-tools.tar.gz'
        first = archive.read_bytes()
        with tarfile.open(archive) as opened:
            members = opened.getmembers()
        self.assertEqual([member.name for member in members],
                         [*platforms_index.INSTALL_TOOLS, *platforms_index.INSTALL_DATA])
        self.assertEqual([member.mode for member in members],
                         [0o755] * len(platforms_index.INSTALL_TOOLS) + [0o644] * len(platforms_index.INSTALL_DATA))
        self.assertTrue(all(member.mtime == 0 for member in members))
        with tarfile.open(archive) as opened:
            self.assertEqual(opened.extractfile('run').read(), (platforms_index.INSTALL_FILES / 'run').read_bytes())
            self.assertEqual(opened.extractfile('install_device.py').read(), (ROOT / 'boot/initramfs/install/install_device.py').read_bytes())
            self.assertIn('orchard-install-usb', opened.getnames())
        archive.unlink()
        platforms_index.write_install_tools(self.tools, archive)
        self.assertEqual(archive.read_bytes(), first)


if __name__ == '__main__':
    unittest.main()
