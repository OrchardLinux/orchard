"""ARMv7 producer boundaries use synthetic ELF and DTB inputs, with no device access."""
import gzip
import hashlib
import io
import json
from pathlib import Path
import struct
import tarfile
import tempfile
import unittest
from unittest import mock

from orchard import build, inputs, distro
from orchard.hardware import ROOT
from orchard.image import elf, platform, prepare_runtime


def executable(architecture='arm', dynamic=False):
    width = 1 if architecture == 'arm' else 2
    data = bytearray(384)
    data[:7] = b'\x7fELF' + bytes((width, 1, 1))
    struct.pack_into('<HHI', data, 16, 2, 40 if width == 1 else 183, 1)
    if width == 1:
        struct.pack_into('<I', data, 28, 52)
        struct.pack_into('<I', data, 36, 0x05000400)
        struct.pack_into('<HHH', data, 40, 52, 32, 2 if dynamic else 0)
        if dynamic:
            struct.pack_into('<8I', data, 52, 1, 0, 0x1000, 0, len(data), len(data), 5, 4)
            struct.pack_into('<8I', data, 84, 2, 128, 0x1080, 0, 32, 32, 4, 4)
            for index, pair in enumerate(((1, 0), (5, 0x10c0), (10, 10), (0, 0))):
                struct.pack_into('<II', data, 128 + 8 * index, *pair)
            data[192:202] = b'libm.so.6\0'
    else:
        struct.pack_into('<Q', data, 32, 64)
        struct.pack_into('<HHH', data, 52, 64, 56, 2 if dynamic else 0)
        if dynamic:
            struct.pack_into('<II6Q', data, 64, 1, 5, 0, 0x1000, 0, len(data), len(data), 8)
            struct.pack_into('<II6Q', data, 120, 2, 4, 192, 0x10c0, 0, 64, 64, 8)
            for index, pair in enumerate(((1, 0), (5, 0x1100), (10, 10), (0, 0))):
                struct.pack_into('<QQ', data, 192 + 16 * index, *pair)
            data[256:266] = b'libm.so.6\0'
    return bytes(data)


class Armv7ProducerTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.work = Path(temporary.name)

    def components(self):
        tools = self.work / 'tools'
        tools.mkdir()
        for name in inputs.TOOLS:
            (tools / name).write_bytes(executable())
        (tools / 'orchard-lwvm').write_bytes(executable())
        loader = self.work / 'loader.bin'
        loader.write_bytes(b'caller-supplied loader')
        return loader, tools

    def test_armv7_offline_inputs_do_not_build_arm64_components(self):
        loader, tools = self.components()
        with mock.patch('orchard.build.prepare_shared') as shared, \
                mock.patch('orchard.build.prepare_payload_loader') as prepare_loader, \
                mock.patch('orchard.build.prepare_tools') as prepare_tools, \
                mock.patch('orchard.build.prepare_lwvm') as prepare_lwvm, \
                mock.patch('orchard.image.platform.kernel') as prepare_kernel:
            result = inputs.resolve(['apple-armv7'], {'apple-armv7': self.work}, device='p105',
                                    platform_loaders={'apple-armv7': loader},
                                    platform_tools={'apple-armv7': tools},
                                    platform_lwvm={'apple-armv7': tools / 'orchard-lwvm'})
        for producer in (shared, prepare_loader, prepare_tools, prepare_lwvm, prepare_kernel):
            producer.assert_not_called()
        target = result['platforms']['apple-armv7']
        self.assertEqual(result['architecture'], 'arm')
        self.assertEqual(target['architecture'], 'arm')
        self.assertEqual(target['image'], 'zImage')
        self.assertEqual(target['loader'], {'kind': 'cascadia', 'file': str(loader.resolve())})
        self.assertEqual(target['boards'], {'p105': 's5l8942x-p105'})
        self.assertEqual(target['tools']['lwvm'], str((tools / 'orchard-lwvm').resolve()))
        self.assertEqual(target['root_format'], 'lwvm')
        self.assertEqual(result['shared'], {})

    def test_lwvm_inputs_prepare_the_selected_board_helper_and_reject_wrong_isa(self):
        loader, tools = self.components()
        helper = tools / 'orchard-lwvm'
        with mock.patch('orchard.build.prepare_lwvm', return_value=helper) as prepare:
            result = inputs.resolve(['apple-armv7'], {'apple-armv7': self.work}, device='p105',
                                    platform_loaders={'apple-armv7': loader}, platform_tools={'apple-armv7': tools})
        prepare.assert_called_once_with(ROOT, None, device='p105')
        self.assertEqual(result['platforms']['apple-armv7']['tools']['lwvm'], str(helper.resolve()))
        helper.write_bytes(executable('arm64'))
        with self.assertRaisesRegex(ValueError, 'helper differs'):
            inputs.resolve(['apple-armv7'], {'apple-armv7': self.work}, device='p105',
                           platform_loaders={'apple-armv7': loader}, platform_tools={'apple-armv7': tools},
                           platform_lwvm={'apple-armv7': helper})
        helper.write_bytes(executable(dynamic=True))
        with self.assertRaisesRegex(ValueError, 'static ELF'):
            inputs.resolve(['apple-armv7'], {'apple-armv7': self.work}, device='p105',
                           platform_loaders={'apple-armv7': loader}, platform_tools={'apple-armv7': tools},
                           platform_lwvm={'apple-armv7': helper})

    def test_j1_does_not_inherit_the_p105_installed_root_backend(self):
        loader, tools = self.components()
        with mock.patch('orchard.build.prepare_lwvm') as prepare:
            result = inputs.resolve(['apple-armv7'], {'apple-armv7': self.work}, device='j1',
                                    platform_loaders={'apple-armv7': loader}, platform_tools={'apple-armv7': tools})
        prepare.assert_not_called()
        self.assertEqual(result['platforms']['apple-armv7']['root_format'], 'gpt')
        self.assertNotIn('lwvm', result['platforms']['apple-armv7']['tools'])
        with mock.patch.object(build, '_build_context', return_value=(ROOT, self.work)), \
                mock.patch.object(build, 'command') as command:
            with self.assertRaisesRegex(ValueError, 'no LwVM installed-root'):
                build.prepare_lwvm(device='j1')
        command.assert_not_called()
        with self.assertRaisesRegex(ValueError, 'require --device'):
            platform.build('apple-armv7', self.work, root_architecture='arm')

    def test_armv7_device_is_required_for_platform_with_multiple_profiles(self):
        with self.assertRaisesRegex(ValueError, 'requires --device'):
            inputs.resolve(['apple-armv7'])

    def test_mixed_architectures_are_refused_before_building(self):
        with mock.patch('orchard.build.prepare_shared') as shared:
            with self.assertRaisesRegex(ValueError, 'one architecture'):
                inputs.resolve(['apple-16k', 'apple-armv7'])
        shared.assert_not_called()

    def test_device_must_match_requested_platform(self):
        with self.assertRaisesRegex(ValueError, 'does not match'):
            inputs.resolve(['apple-16k'], device='p105')

    def test_explicit_arm64_shared_assets_cannot_be_relabelled_armv7(self):
        with self.assertRaisesRegex(ValueError, 'platform-loader'):
            inputs.resolve(['apple-armv7'], device='p105', shared={'tools': self.work})

    def test_arm_tools_do_not_reuse_the_arm64_published_bundle(self):
        with mock.patch.object(build, '_build_context', return_value=(ROOT, self.work)), \
                mock.patch('orchard.published.tools') as published, \
                mock.patch.object(build, 'component_lock'), \
                mock.patch.object(build, '_prepare_tools', return_value=self.work) as prepare:
            self.assertEqual(build.prepare_tools(device='p105'), self.work)
        published.assert_not_called()
        prepare.assert_called_once_with(ROOT, self.work / 'components-v2', 'arm')

    def test_cascadia_components_refuse_pongo_before_any_producer(self):
        with mock.patch.object(build, 'prepare_payload_loader') as loader:
            with self.assertRaisesRegex(ValueError, 'recovery-autogo firmware stages'):
                build.prepare_components(ROOT, self.work, device='p105', kernel=self.work,
                                         busybox=self.work / 'busybox', loader=self.work / 'loader',
                                         pongo=self.work / 'pongo.bin')
        loader.assert_not_called()

    def test_platform_selection_can_filter_by_architecture(self):
        self.assertEqual(platform.platforms(architecture='arm'), ['apple-armv7'])
        self.assertNotIn('apple-armv7', platform.platforms())
        self.assertIn('apple-armv7', platform.platforms(architecture=None))

    def test_platform_layer_refuses_an_incompatible_root(self):
        with self.assertRaisesRegex(ValueError, 'differs from root'):
            platform.build('apple-armv7', self.work, root_architecture='arm64')

    def test_armv7_platform_layer_uses_zimage_and_one_board(self):
        from test_cascadia_loader import kernel
        bundle = self.work / 'kernel'
        modules = bundle / 'lib/modules/test-arm'
        modules.mkdir(parents=True)
        (modules / 'modules.dep').write_text('')
        (modules / 'modules.order').write_text('')
        (bundle / 'kernel.release').write_text('test-arm\n')
        (bundle / 'zImage').write_bytes(kernel())
        (bundle / 'dtbs').mkdir()
        (bundle / 'dtbs/s5l8942x-p105.dtb').write_bytes(b'caller-supplied-dtb')
        busybox = self.work / 'busybox'
        busybox.write_bytes(executable())
        mapper = self.work / 'orchard-lwvm'
        mapper.write_bytes(executable())
        rescue = self.work / 'rescue'
        rescue.mkdir()
        for name in ('dropbear', 'dropbearkey'):
            (rescue / name).write_bytes(executable())
        result = platform.build('apple-armv7', self.work, bundle=bundle, busybox=busybox,
                                root_architecture='arm', device='p105', lwvm=mapper, rescue_tools=rescue)
        self.assertEqual(result['architecture'], 'arm')
        self.assertEqual(result['boards'], ['p105'])
        with tarfile.open(self.work / result['file']) as archive:
            description = json.load(archive.extractfile('usr/lib/orchard/platform.json'))
            self.assertEqual(description['architecture'], 'arm')
            self.assertEqual(description['image'], 'zImage')
            self.assertEqual(description['boards'], {'p105': 's5l8942x-p105'})
            self.assertEqual(archive.extractfile('usr/lib/modules/test-arm/vmlinuz').read(), kernel())
            initrd = archive.extractfile('usr/lib/modules/test-arm/initrd').read()
            stream = io.BytesIO(gzip.decompress(initrd))
            entries = {}
            while True:
                h = stream.read(110)
                fields = [int(h[i:i + 8], 16) for i in range(6, 110, 8)]
                name = stream.read(fields[11])[:-1].decode()
                stream.read(-(110 + fields[11]) % 4)
                if name == 'TRAILER!!!': break
                entries[name] = stream.read(fields[6])
                stream.read(-fields[6] % 4)
            self.assertEqual(entries['bin/orchard-lwvm'], executable())
            self.assertEqual(entries['bin/dropbear'], executable())
            self.assertIn(b'/run/orchard/rescue/dropbear', entries['bin/recovery-start-ssh'])
            self.assertEqual(entries['etc/orchard/modules'], b'\n')
            self.assertNotIn('bin/python3', entries)
        busybox.write_bytes(executable('arm64'))
        other = self.work / 'wrong'
        other.mkdir()
        with self.assertRaisesRegex(ValueError, 'BusyBox differs'):
            platform.build('apple-armv7', other, bundle=bundle, busybox=busybox,
                           root_architecture='arm', device='p105')
        self.assertEqual(list(other.iterdir()), [])
        busybox.write_bytes(executable())
        mapper.write_bytes(executable('arm64'))
        with self.assertRaisesRegex(ValueError, 'helper differs'):
            platform.build('apple-armv7', other, bundle=bundle, busybox=busybox,
                           root_architecture='arm', device='p105', lwvm=mapper, rescue_tools=rescue)
        mapper.write_bytes(executable(dynamic=True))
        with self.assertRaisesRegex(ValueError, 'static ELF'):
            platform.build('apple-armv7', other, bundle=bundle, busybox=busybox,
                           root_architecture='arm', device='p105', lwvm=mapper, rescue_tools=rescue)

    def test_elf_dependency_reader_handles_both_instruction_sets(self):
        for architecture in ('arm', 'arm64'):
            with self.subTest(architecture=architecture):
                data = executable(architecture, dynamic=True)
                self.assertEqual(elf.architecture(data), architecture)
                self.assertEqual(elf.needed(data), {'libm.so.6'})
                self.assertEqual(elf.needed(executable(architecture)), set())

    def test_elf_refuses_malformed_dependency_bounds_and_soft_float(self):
        raw = bytearray(executable(dynamic=True))
        struct.pack_into('<I', raw, 128 + 8 + 4, 0x9000)
        with self.assertRaisesRegex(ValueError, 'outside load segments'):
            elf.needed(raw)
        for flags in (0x05000200, 0x04000400):
            raw = bytearray(executable())
            struct.pack_into('<I', raw, 36, flags)
            with self.assertRaisesRegex(ValueError, 'hard-float'):
                elf.architecture(raw)
        raw = bytearray(executable(dynamic=True))
        struct.pack_into('<I', raw, 128 + 4, 1000)
        with self.assertRaisesRegex(ValueError, 'offset exceeds'):
            elf.needed(raw)

    def runtime(self, foreign=False):
        base, receipt = self.work / 'base.tar.gz', self.work / 'base.json'
        files = {'usr/bin/python3.12': executable(), 'usr/lib/ld-linux-armhf.so.3': executable(),
                 'usr/lib/libm.so.6': executable('arm64' if foreign else 'arm'),
                 'usr/sbin/blkid': executable(), 'usr/sbin/sfdisk': executable(),
                 'usr/sbin/mke2fs': executable(), 'etc/mke2fs.conf': b'[defaults]\n'}
        with tarfile.open(base, 'w:gz') as archive:
            for name, data in files.items():
                member = tarfile.TarInfo(name)
                member.size, member.mode = len(data), 0o755
                archive.addfile(member, io.BytesIO(data))
            member = tarfile.TarInfo('usr/bin/python3')
            member.type, member.linkname = tarfile.SYMTYPE, 'python3.12'
            archive.addfile(member)
        receipt.write_text(json.dumps({'bytes': base.stat().st_size,
                                      'sha256': hashlib.sha256(base.read_bytes()).hexdigest(),
                                      'architecture': 'armv7h'}))
        output = self.work / 'runtime'
        output.mkdir()
        return base, receipt, output

    def test_runtime_extracts_armhf_loader_and_verifies_elf_closure(self):
        base, receipt, output = self.runtime()
        prepare_runtime.stage_runtime(base, receipt, output, 'arm')
        self.assertTrue((output / 'usr/lib/ld-linux-armhf.so.3').is_file())
        self.assertTrue((output / 'usr/bin/python3').exists())

    def test_runtime_refuses_foreign_architecture_library(self):
        base, receipt, output = self.runtime(foreign=True)
        with self.assertRaisesRegex(ValueError, 'another architecture'):
            prepare_runtime.stage_runtime(base, receipt, output, 'arm')

    def test_armv7_ram_payload_uses_the_selected_profile_without_pongo(self):
        from orchard import ram
        from orchard.image import cascadia
        from test_cascadia_loader import loader, kernel, tree
        bundle = self.work / 'kernel'
        (bundle / 'dtbs').mkdir(parents=True)
        (bundle / 'kernel.release').write_text('test-armv7\n')
        (bundle / 'zImage').write_bytes(kernel())
        dtb = tree()
        dtb['properties']['compatible'] = b'apple,p105ap\0apple,s5l8942x\0'
        (bundle / 'dtbs/s5l8942x-p105.dtb').write_bytes(cascadia.write_fdt(dtb, [], 0))
        busybox, trampoline, init = self.work / 'busybox', self.work / 'loader.bin', self.work / 'init'
        busybox.write_bytes(executable())
        trampoline.write_bytes(loader())
        init.write_text('#!/bin/busybox sh\nexec /bin/busybox sh\n')
        output = self.work / 'out'
        ram.assemble_ram(ROOT, output, kernel=bundle, busybox=busybox, loader=trampoline,
                         pongo=None, init=init, platform='apple-armv7', boards={'p105': 's5l8942x-p105'})
        record = ram.verify_ram(output)
        self.assertEqual(record['loader_format'], 'cascadia')
        self.assertEqual(record['boot_method'], 'recovery-autogo')
        self.assertEqual(set(record['files']), {'linux.bin'})
        self.assertFalse((output / 'pongo.bin').exists())
        assembled = json.loads((output / 'linux.bin.json').read_text())
        self.assertEqual(assembled['initramfs_encoding'], 'cascadia-dtb')
        self.assertEqual(assembled['architecture'], 'arm')
        trampoline.write_bytes(b'changed')
        with self.assertRaisesRegex(ValueError, 'changed; rebuild'):
            ram.verify_ram(output)

    def test_cascadia_profile_requires_matching_dtb_compatible(self):
        from orchard.image import cascadia
        from test_cascadia_loader import tree
        data = cascadia.write_fdt(tree(), [], 0)
        with self.assertRaisesRegex(ValueError, 'differs from the selected board'):
            cascadia.check_compatible(data, ['apple,p105ap'])
        cascadia.check_compatible(data, ['example,test'])

    def test_release_device_selection_is_forwarded_only_when_declared(self):
        (self.work / 'tools').mkdir()
        (self.work / 'tools/build.py').write_text('')
        project = self.work / 'orchard.toml'
        project.write_text('name="fixture-linux"\n[release]\nbuild="tools/build.py"\n'
                           'selects-platform=true\nselects-device=true\n')
        with mock.patch('orchard.distro.subprocess.run', side_effect=RuntimeError('stop')) as run:
            with self.assertRaisesRegex(RuntimeError, 'stop'):
                distro.build(self.work, version='dev-1', platforms=['apple-armv7'], device='p105')
        self.assertEqual(run.call_args.args[0][-2:], ['--device', 'p105'])
        project.write_text('name="fixture-linux"\n[release]\nbuild="tools/build.py"\nselects-device=true\n')
        with self.assertRaisesRegex(ValueError, 'requires selects-platform'):
            distro.settings(self.work)


if __name__ == '__main__':
    unittest.main()
