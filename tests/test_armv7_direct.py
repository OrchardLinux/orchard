"""32-bit boards boot like 64-bit ones, recovery-less: the Cascadia loader takes m1n1's place.

The same platform-layer kernel, initrd and install add-on; what orchard publishes for 32-bit
roots sits next to the arm64 index, which stays as it was. No device is touched.
"""
import gzip
import hashlib
import io
from pathlib import Path
import sys
import tarfile
import tempfile
import tomllib
import unittest
from unittest import mock

from orchard import inputs
from orchard.hardware import ROOT
from orchard.image import cascadia, platform, platforms_index

sys.path.insert(0, str(ROOT / 'tools'))
sys.path.insert(0, str(Path(__file__).resolve().parent))
from validate_manifest import ManifestError, validate  # noqa: E402
from test_cascadia_loader import kernel  # noqa: E402
from test_install_direct import manifest as arm64_manifest, receipt  # noqa: E402

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
        document = inputs.resolve(['apple-armv7'], {'apple-armv7': self.work}, shared={'tools': self.tools},
                                  loaders={'p105': self.work / 'loader.bin'})
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

    def test_a_board_with_a_profile_needs_its_loader(self):
        with self.assertRaisesRegex(ValueError, '--loader p105=FILE is not given'):
            inputs.resolve(['apple-armv7'], {'apple-armv7': self.work}, shared={'tools': self.tools})


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
