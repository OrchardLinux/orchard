"""platforms.toml and what it names: everything a distribution takes from an orchard build."""
import hashlib
import copy
import json
from pathlib import Path
import struct
import sys
import tarfile
import tempfile
import tomllib
import unittest
from unittest import mock

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
        entries = [*index['loaders'].values(), index['install_tools'], *index['platforms'].values()]
        self.assertEqual(sorted(index['loaders']), ['m1n1', 'pongo'])
        for entry in entries:
            data = (self.published / entry['file']).read_bytes()
            self.assertEqual((entry['bytes'], entry['sha256']), (len(data), hashlib.sha256(data).hexdigest()))
            self.assertNotIn('/', entry['file'])

    def test_install_tools_are_only_the_installer_programs_and_reproducible(self):
        self.build()
        archive = self.published / 'install-tools.tar.gz'
        first = archive.read_bytes()
        with tarfile.open(archive) as opened:
            members = opened.getmembers()
        self.assertEqual([member.name for member in members], list(platforms_index.INSTALL_TOOLS))
        self.assertTrue(all(member.mode == 0o755 and member.mtime == 0 for member in members))
        archive.unlink()
        platforms_index.write_install_tools(self.tools, archive)
        self.assertEqual(archive.read_bytes(), first)


def static_executable(architecture='arm64'):
    data = bytearray(128)
    width = 2 if architecture == 'arm64' else 1
    data[:7] = b'\x7fELF' + bytes((width, 1, 1))
    struct.pack_into('<HHI', data, 16, 2, 183 if width == 2 else 40, 1)
    if width == 2:
        struct.pack_into('<Q', data, 32, 64)
        struct.pack_into('<HHH', data, 52, 64, 56, 0)
    else:
        struct.pack_into('<I', data, 28, 52)
        struct.pack_into('<I', data, 36, 0x05000400)
        struct.pack_into('<HHH', data, 40, 52, 32, 0)
    return bytes(data)


class SelectedPlatformInputs(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        from orchard.hardware import ROOT
        from orchard.ram import boards_by_platform
        cls.boards = boards_by_platform(ROOT)

    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.work = Path(temporary.name)
        self.output = self.work / 'output'
        self.tools = self.work / 'selected-tools'
        self.tools.mkdir()
        for name in (*platforms_index.INSTALL_TOOLS, 'busybox', 'dropbearkey', 'lwvm'):
            (self.tools / name).write_bytes(static_executable() + name.encode())
        self.loader = self.work / 'selected-m1n1.bin'
        self.loader.write_bytes(b'selected m1n1')
        self.pongo = self.work / 'pongo.bin'
        self.pongo.write_bytes(b'Pongo')
        self.document = {'schema': 1, 'architecture': 'arm64', 'orchard': {'commit': 'c' * 40},
                         'shared': {'pongo': str(self.pongo), 'm1n1': '/unused/shared-m1n1',
                                    'tools': {'busybox': '/unused/shared-busybox'}},
                         'platforms': {}}
        for name, board in (('apple-16k', 'j71b'), ('apple-4k', 'n61')):
            kernel = self.work / ('kernel-' + name)
            kernel.mkdir()
            self.document['platforms'][name] = {
                'architecture': 'arm64', 'image': 'Image.gz', 'root_format': 'gpt',
                'kernel': str(kernel), 'boards': {board: self.boards[name][board]},
                'loader': {'kind': 'm1n1', 'file': str(self.loader)},
                'tools': {tool: str(self.tools / tool)
                          for tool in (*platforms_index.INSTALL_TOOLS, 'busybox', 'dropbearkey')},
            }

    def layer(self, name, directory, **options):
        output = directory / ('platform-' + name + '.tar.gz')
        output.write_bytes(name.encode())
        boards = [options['device']] if options['device'] else sorted(self.boards[name])
        return {**platforms_index.receipt(output), 'kernel_release': 'test', 'boards': boards}

    def test_exact_row_tools_loader_and_board_reach_the_layer(self):
        with mock.patch.object(platform, 'build', side_effect=self.layer) as build:
            result = platforms_index.from_inputs(self.output, self.document)
        self.assertEqual((self.output / 'm1n1.bin').read_bytes(), self.loader.read_bytes())
        self.assertEqual(build.call_count, 2)
        for call, board in zip(build.call_args_list, ('j71b', 'n61')):
            self.assertEqual(call.kwargs['device'], board)
            self.assertEqual(call.kwargs['root_architecture'], 'arm64')
            self.assertEqual(call.kwargs['busybox'], self.tools / 'busybox')
        self.assertEqual(result['platforms']['apple-16k']['boards'], ['j71b'])
        with tarfile.open(self.output / 'install-tools.tar.gz') as archive:
            self.assertEqual(archive.extractfile('orchard-write').read(),
                             (self.tools / 'orchard-write').read_bytes())

    def test_legacy_shared_records_keep_the_full_board_selection(self):
        row = self.document['platforms']['apple-16k']
        self.document['platforms'] = {'apple-16k': row}
        self.document.pop('architecture')
        row['boards'] = self.boards['apple-16k']
        self.document['shared'].update(m1n1=row.pop('loader')['file'], tools=row.pop('tools'))
        for key in ('architecture', 'image', 'root_format'):
            row.pop(key)
        with mock.patch.object(platform, 'build', side_effect=self.layer) as build:
            result = platforms_index.from_inputs(self.output, self.document)
        self.assertIsNone(build.call_args.kwargs['device'])
        self.assertEqual(result['platforms']['apple-16k']['boards'], sorted(self.boards['apple-16k']))

    def test_lwvm_passes_the_selected_mapper_and_rescue_directory(self):
        self.document['platforms'] = {'apple-16k': self.document['platforms']['apple-16k']}
        row = self.document['platforms']['apple-16k']
        row['root_format'], row['tools']['lwvm'] = 'lwvm', str(self.tools / 'lwvm')
        with mock.patch.object(platform, 'root_format', return_value='lwvm'), \
                mock.patch.object(platform, 'build', side_effect=self.layer) as build:
            platforms_index.from_inputs(self.output, self.document)
        self.assertEqual(build.call_args.kwargs['lwvm'], self.tools / 'lwvm')
        self.assertEqual(build.call_args.kwargs['rescue_tools'], self.tools)

    def test_invalid_second_record_stops_before_any_output(self):
        def change(field, value):
            data = copy.deepcopy(self.document)
            data['platforms']['apple-4k'][field] = value
            return data
        cases = [
            change('architecture', 'arm'), change('image', 'zImage'),
            change('root_format', 'lwvm'), change('boards', {'j71b': 't8010-j71b'}),
            change('boards', {'n61': 'wrong-dtb'}), change('tools', {}),
            change('loader', {}), change('loader', {'kind': 'cascadia', 'file': str(self.loader)}),
        ]
        unknown = copy.deepcopy(self.document)
        unknown['platforms']['unknown'] = unknown['platforms'].pop('apple-4k')
        cases.append(unknown)
        armv7 = copy.deepcopy(self.document)
        armv7['architecture'] = 'arm'
        cases.append(armv7)
        self.output.mkdir()
        sentinel = self.output / 'preserved'
        sentinel.write_bytes(b'owner file')
        for data in cases:
            with self.subTest(data=data), mock.patch.object(platform, 'build') as build:
                with self.assertRaises(ValueError):
                    platforms_index.from_inputs(self.output, data)
                build.assert_not_called()
                self.assertEqual(list(self.output.iterdir()), [sentinel])
                self.assertEqual(sentinel.read_bytes(), b'owner file')

    def test_different_shared_output_inputs_are_rejected_before_copy(self):
        other = self.work / 'other'
        other.mkdir()
        for role in ('loader', 'install-tool'):
            data = copy.deepcopy(self.document)
            row = data['platforms']['apple-4k']
            changed = other / role
            if role == 'loader':
                changed.write_bytes(b'other m1n1')
                row['loader']['file'] = str(changed)
            else:
                changed.write_bytes(static_executable() + b'other orchard-write')
                row['tools']['orchard-write'] = str(changed)
            with self.subTest(role=role), self.assertRaisesRegex(ValueError, 'same'):
                platforms_index.from_inputs(self.output, data)
            self.assertFalse(self.output.exists())

    def test_foreign_or_dynamic_lwvm_helpers_stop_before_output(self):
        row = self.document['platforms']['apple-4k']
        row['root_format'], row['tools']['lwvm'] = 'lwvm', str(self.tools / 'lwvm')
        dynamic = bytearray(static_executable())
        struct.pack_into('<H', dynamic, 56, 1)
        struct.pack_into('<II6Q', dynamic, 64, 3, 0, 0, 0, 0, 0, 0, 8)
        with mock.patch.object(platform, 'root_format', return_value='lwvm'):
            self.document['platforms']['apple-16k']['root_format'] = 'lwvm'
            self.document['platforms']['apple-16k']['tools']['lwvm'] = str(self.tools / 'lwvm')
            for contents in (static_executable('arm'), bytes(dynamic)):
                (self.tools / 'lwvm').write_bytes(contents)
                with self.subTest(contents=contents), self.assertRaises(ValueError):
                    platforms_index.from_inputs(self.output, self.document)
        self.assertFalse(self.output.exists())

    def test_main_rejects_armv7_without_creating_an_output_directory(self):
        self.document['architecture'] = 'arm'
        path = self.work / 'inputs.json'
        path.write_text(json.dumps(self.document))
        with mock.patch.object(sys, 'argv', ['platforms_index', str(self.output), '--inputs', str(path)]), \
                self.assertRaisesRegex(ValueError, 'ARMv7 platform producer'):
            platforms_index.main()
        self.assertFalse(self.output.exists())

    def test_real_layer_contains_only_the_selected_board_and_busybox(self):
        from test_armv7_producers import initrd_entries
        row = self.document['platforms']['apple-16k']
        self.document['platforms'] = {'apple-16k': row}
        bundle = Path(row['kernel'])
        modules = bundle / 'lib/modules/test-index/kernel/drivers/nvme/host'
        modules.mkdir(parents=True)
        (modules / 'nvme.ko').write_bytes(b'synthetic module')
        (modules.parents[3] / 'modules.dep').write_text('kernel/drivers/nvme/host/nvme.ko:\n')
        (bundle / 'kernel.release').write_text('test-index\n')
        (bundle / 'Image.gz').write_bytes(b'synthetic kernel')
        (bundle / 'dtbs').mkdir()
        dtb = next(iter(row['boards'].values()))
        (bundle / 'dtbs' / (dtb + '.dtb')).write_bytes(b'selected dtb')
        result = platforms_index.from_inputs(self.output, self.document)
        with tarfile.open(self.output / result['platforms']['apple-16k']['file']) as archive:
            description = json.load(archive.extractfile('usr/lib/orchard/platform.json'))
            entries = initrd_entries(archive.extractfile('usr/lib/modules/test-index/initrd').read())
        self.assertEqual(description['boards'], row['boards'])
        self.assertEqual(entries['bin/busybox'], (self.tools / 'busybox').read_bytes())


if __name__ == '__main__':
    unittest.main()
