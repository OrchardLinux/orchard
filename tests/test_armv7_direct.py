"""32-bit boards boot like 64-bit ones, recovery-less: the Cascadia loader takes m1n1's place.

The same platform-layer kernel, initrd and install add-on; what orchard publishes for 32-bit
roots sits next to the arm64 index, which stays as it was. No device is touched.
"""
import copy
import gzip
import hashlib
import io
import json
from pathlib import Path
import struct
import sys
import tarfile
import tempfile
import tomllib
import unittest
from unittest import mock

from orchard import inputs, install_direct as direct, releases
from orchard.hardware import ROOT
from orchard.image import cascadia, platform, platforms_index

sys.path.insert(0, str(ROOT / 'tools'))
sys.path.insert(0, str(Path(__file__).resolve().parent))
from validate_manifest import ManifestError, validate  # noqa: E402
from test_cascadia_loader import kernel, loader, tree  # noqa: E402
from test_install_direct import layer as arm64_layer, manifest as arm64_manifest, receipt  # noqa: E402

P105 = cascadia.profile(ROOT / 'devices/p105/cascadia.toml')


def armv7_layer(path, dtb):
    with tarfile.open(path, 'w:gz') as archive:
        for name, data in (('usr/lib/modules/6.12/vmlinuz', kernel()), ('usr/lib/modules/6.12/initrd', b'INITRD'),
                           ('usr/lib/modules/6.12/dtb/s5l8942x-p105.dtb', dtb),
                           ('usr/lib/modules/6.12/dtb/s5l8945x-j1.dtb', b'OTHER')):
            member = tarfile.TarInfo(name)
            member.size = len(data)
            archive.addfile(member, io.BytesIO(data))


def armv7_manifest():
    document = arm64_manifest()
    for name in ('pongo', 'm1n1', 'logo'):
        del document[name]
    document['boot_method'] = 'recovery-autogo'
    document['install_tools'] = receipt('install-tools-armv7.tar.gz')
    document['cascadia'] = {'p105': receipt('cascadia-p105.bin')}
    document['platforms'] = {'apple-armv7': {'boards': ['p105', 'j1'], 'layer': {
        **receipt('platform-apple-armv7.tar.gz'), 'compression': 'gzip', 'expanded_bytes': 1, 'members': 1,
        'kernel_release': '6.12'}}}
    return document


class Payload(unittest.TestCase):
    def setUp(self):
        work = tempfile.TemporaryDirectory()
        self.addCleanup(work.cleanup)
        self.work = Path(work.name)
        self.dtb = cascadia.write_fdt(tree())
        armv7_layer(self.work / 'layer', self.dtb)
        (self.work / 'loader').write_bytes(loader(P105))
        self.adapter = mock.Mock(PROFILE='p105', DEVICETREE='s5l8942x-p105.dtb', NAME='iPad mini 1 (Wi-Fi)')
        self.assets = {'cascadia': self.work / 'loader', 'layer': self.work / 'layer', 'install_tools': self.work / 'tools'}

    def test_the_bundle_is_loader_kernel_tree_then_initrd_and_add_on(self):
        image = direct.boot_image(self.adapter, self.assets, b'ADD-ON')
        config = cascadia.configuration(image)
        self.assertEqual(image[0x8000:0x8000 + len(kernel())], kernel())
        # One archive, as on 64-bit: the initrd, padded to four bytes, then the add-on.
        self.assertEqual(image[config['initrd_offset']:], b'INITRD\0\0ADD-ON')
        self.assertEqual(config['initrd_address'], 0x88000000)
        tree_bytes = image[config['dtb_offset']:config['dtb_offset'] + config['dtb_size']]
        chosen = cascadia.read_fdt(tree_bytes)[0]['children'][0]['properties']
        self.assertEqual(chosen['bootargs'], b'old\0')    # the board's own
        self.assertEqual(struct.unpack('>I', chosen['linux,initrd-start'])[0], 0x88000000)
        self.assertEqual(struct.unpack('>I', chosen['linux,initrd-end'])[0], 0x88000000 + 14)

    def test_the_loader_must_be_built_for_the_boards_addresses(self):
        (self.work / 'loader').write_bytes(loader(dict(P105, initrd_address=0x98000000, initrd_limit=0x9e000000)))
        with self.assertRaisesRegex(ValueError, 'differs from the handoff profile'):
            direct.boot_image(self.adapter, self.assets, b'ADD-ON')

    def test_64_bit_boards_get_exactly_m1n1s_payload(self):
        (self.work / 'm1n1').write_bytes(b'M1N1')
        arm64_layer(self.work / 'arm64-layer')
        adapter = mock.Mock(PROFILE='j71b', DEVICETREE='t8010-j71b.dtb')
        assets = {'m1n1': self.work / 'm1n1', 'layer': self.work / 'arm64-layer', 'pongo': self.work / 'pongo'}
        self.assertEqual(direct.boot_image(adapter, assets, b'ADD-ON'),
                         direct.payload(self.work / 'm1n1', None, self.work / 'arm64-layer', 't8010-j71b.dtb', b'ADD-ON'))
        direct.start(adapter, {}, self.work / 'image', assets, None)
        adapter.boot_payload.assert_called_once_with({}, self.work / 'image', self.work / 'pongo', None)

    def test_delivering_a_cascadia_bundle_is_not_orchards_yet(self):
        with self.assertRaisesRegex(ValueError, 'patched iBEC'):
            direct.start(self.adapter, {}, self.work / 'image', self.assets, None)
        self.adapter.boot_payload.assert_not_called()


class Release(unittest.TestCase):
    def test_a_32_bit_release_names_its_boards_loaders_instead_of_m1n1(self):
        validate(armv7_manifest())
        both = armv7_manifest()
        both['m1n1'] = receipt('m1n1.bin')
        with self.assertRaises(ManifestError):
            validate(both)
        neither = armv7_manifest()
        del neither['cascadia']
        with self.assertRaises(ManifestError):
            validate(neither)

    def test_a_32_bit_boot_needs_the_layer_the_tools_and_its_boards_loader(self):
        _, boot = releases.receipts_for(armv7_manifest(), 'p105', 'boot')
        self.assertEqual(boot, {'layer': armv7_manifest()['platforms']['apple-armv7']['layer'],
                                'cascadia': receipt('cascadia-p105.bin'),
                                'install_tools': receipt('install-tools-armv7.tar.gz')})
        _, install = releases.receipts_for(armv7_manifest(), 'p105', 'install')
        self.assertEqual(sorted(install), ['cascadia', 'install_tools', 'layer', 'map', 'root'])
        with self.assertRaisesRegex(ValueError, 'no Cascadia loader for j1'):
            releases.receipts_for(armv7_manifest(), 'j1', 'boot')

    def test_64_bit_releases_resolve_as_before(self):
        _, boot = releases.receipts_for(arm64_manifest(), 'j71b', 'boot')
        self.assertEqual(sorted(boot), ['install_tools', 'layer', 'logo', 'm1n1', 'pongo'])


class Index(unittest.TestCase):
    def setUp(self):
        work = tempfile.TemporaryDirectory()
        self.addCleanup(work.cleanup)
        self.work = Path(work.name)
        self.tools = self.work / 'tools'
        self.tools.mkdir()
        for tool in (*platforms_index.INSTALL_TOOLS, 'busybox'):
            (self.tools / tool).write_bytes(b'arm ' + tool.encode())
        (self.work / 'loader.bin').write_bytes(b'LOADER')

    def test_the_32_bit_index_sits_next_to_platforms_toml(self):
        def layer(name, directory, *, bundle, busybox, root_architecture):
            self.assertEqual((name, root_architecture, busybox), ('apple-armv7', 'arm', self.tools / 'busybox'))
            path = directory / f'platform-{name}.tar.gz'
            path.write_bytes(b'LAYER')
            return {'file': path.name, 'bytes': 5, 'sha256': hashlib.sha256(b'LAYER').hexdigest(),
                    'kernel_release': '6.12', 'boards': ['j1', 'p105'], 'architecture': 'arm'}
        with mock.patch.object(platform, 'build', layer):
            platforms_index.build_armv7(self.work, kernels={'apple-armv7': self.work / 'kernel'}, tools=self.tools,
                                        commit='abc', loaders={'p105': self.work / 'loader.bin'})
        self.assertFalse((self.work / 'platforms.toml').exists())
        index = tomllib.loads((self.work / 'platforms-armv7.toml').read_text())
        self.assertEqual((index['kind'], index['architecture'], index['commit']), ('orchard.platforms', 'arm', 'abc'))
        self.assertEqual(sorted(index['loaders']), ['cascadia'])
        self.assertEqual(index['loaders']['cascadia']['p105']['file'], 'cascadia-p105.bin')
        self.assertEqual(index['install_tools']['file'], 'install-tools-armv7.tar.gz')
        for entry in platforms_index.receipts(index):
            data = (self.work / entry['file']).read_bytes()
            self.assertEqual((entry['bytes'], entry['sha256']), (len(data), hashlib.sha256(data).hexdigest()))
        with tarfile.open(self.work / 'install-tools-armv7.tar.gz') as archive:
            self.assertEqual(archive.extractfile('dropbear').read(), b'arm dropbear')

    def test_an_inputs_document_of_the_32_bit_root_builds_its_index(self):
        document = {'schema': 1, 'orchard': {'commit': 'abc'}, 'architecture': 'arm',
                    'platforms': {'apple-armv7': {'kernel': str(self.work / 'kernel'),
                                                  'boards': platform.boards('apple-armv7'),
                                                  'loaders': {'p105': str(self.work / 'loader.bin')}}},
                    'shared': {'tools': {name: str(self.tools / name) for name in (*platforms_index.INSTALL_TOOLS, 'busybox')}}}
        with mock.patch.object(platforms_index, 'build_armv7') as build, \
                mock.patch.object(platforms_index, 'build') as arm64:
            platforms_index.from_inputs(self.work, document)
        arm64.assert_not_called()
        self.assertEqual(build.call_args.kwargs['loaders'], {'p105': self.work / 'loader.bin'})
        self.assertEqual(build.call_args.kwargs['commit'], 'abc')


class Inputs(unittest.TestCase):
    def setUp(self):
        work = tempfile.TemporaryDirectory()
        self.addCleanup(work.cleanup)
        self.work = Path(work.name)
        self.tools = self.work / 'tools'
        self.tools.mkdir()
        for name in inputs.TOOLS:
            (self.tools / name).write_bytes(name.encode())
        (self.work / 'loader.bin').write_bytes(b'LOADER')

    def test_the_32_bit_root_gets_its_tools_kernel_and_loaders_and_no_m1n1(self):
        with mock.patch('orchard.build.prepare_shared') as shared, \
                mock.patch('orchard.build.prepare_payload_loader') as built_loader, \
                mock.patch('orchard.build.prepare_tools') as built_tools, \
                mock.patch('orchard.image.platform.kernel') as built_kernel:
            document = inputs.resolve(['apple-armv7'], {'apple-armv7': self.work}, shared={'tools': self.tools},
                                      loaders={'p105': self.work / 'loader.bin'})
        for producer in (shared, built_loader, built_tools, built_kernel):
            producer.assert_not_called()
        self.assertEqual(document['architecture'], 'arm')
        row = document['platforms']['apple-armv7']
        self.assertEqual(row['boards'], platform.boards('apple-armv7'))
        # j1 has no Cascadia profile yet, so no loader; its DTB is still in the layer.
        self.assertEqual(row['loaders'], {'p105': str((self.work / 'loader.bin').resolve())})
        self.assertEqual(set(document['shared']), {'tools'})

    def test_one_document_is_one_root_architecture(self):
        with self.assertRaisesRegex(ValueError, 'one root architecture'):
            inputs.resolve(['apple-16k', 'apple-armv7'])
        with self.assertRaisesRegex(ValueError, 'no m1n1 or Pongo'):
            inputs.resolve(['apple-armv7'], shared={'m1n1': self.work / 'm1n1'})
        with self.assertRaisesRegex(ValueError, 'for ARMv7'):
            inputs.resolve(['apple-16k'], loaders={'p105': self.work / 'loader.bin'})


class Initrd(unittest.TestCase):
    """A kernel with the USB gadget as modules (orchard's ARMv7 one) gets them in the initrd."""
    def names(self, data):
        names, stream = {}, io.BytesIO(gzip.decompress(data))
        while True:
            header = stream.read(110)
            fields = [int(header[i:i + 8], 16) for i in range(6, 110, 8)]
            name = stream.read(fields[11])[:-1].decode()
            stream.read(-(110 + fields[11]) % 4)
            if name == 'TRAILER!!!':
                return names
            names[name] = stream.read(fields[6])
            stream.read(-fields[6] % 4)

    def modules(self, lines):
        base = 'usr/lib/modules/6.12/'
        result = {base + 'modules.dep': ('\n'.join(lines) + '\n').encode()}
        for line in lines:
            result[base + line.split(':')[0]] = (line.split(':')[0].encode(), 0o644)
        result[base + 'modules.dep'] = (result[base + 'modules.dep'], 0o644)
        return result

    def test_gadget_modules_load_before_storage_and_cannot_stop_the_boot(self):
        g = 'kernel/drivers/usb/gadget/'
        lines = [g + 'libcomposite.ko:', g + 'function/u_ether.ko: ' + g + 'libcomposite.ko',
                 g + 'function/usb_f_ncm.ko: ' + g + 'function/u_ether.ko ' + g + 'libcomposite.ko',
                 'kernel/drivers/other.ko:']
        with tempfile.TemporaryDirectory() as temporary:
            busybox = Path(temporary) / 'busybox'
            busybox.write_bytes(b'busybox')
            names = self.names(platform.initrd(self.modules(lines), '6.12', busybox, storage_modules=()))
        self.assertEqual(names['etc/orchard/usb-modules'], b'libcomposite\nusb_f_ncm\n')
        self.assertEqual(names['etc/orchard/modules'], b'\n')
        self.assertEqual(sorted(name.rsplit('/', 1)[1] for name in names if name.endswith('.ko')),
                         ['libcomposite.ko', 'u_ether.ko', 'usb_f_ncm.ko'])
        init = (ROOT / 'boot/initramfs/root/init').read_text()
        self.assertLess(init.index('/etc/orchard/usb-modules'), init.index('$(cat /etc/orchard/modules)'))
        self.assertIn('modprobe "$module" || echo', init)

    def test_kernels_with_the_gadget_built_in_get_no_list(self):
        lines = ['kernel/drivers/nvme/host/nvme.ko:']
        with tempfile.TemporaryDirectory() as temporary:
            busybox = Path(temporary) / 'busybox'
            busybox.write_bytes(b'busybox')
            names = self.names(platform.initrd(self.modules(lines), '6.12', busybox))
        self.assertNotIn('etc/orchard/usb-modules', names)
        self.assertEqual(names['etc/orchard/modules'], b'nvme\n')


if __name__ == '__main__':
    unittest.main()
