"""The install without a recovery: what is sent to the device, and what a release must say for it."""
import copy
import hashlib
import io
from pathlib import Path
import struct
import subprocess
import sys
import tarfile
import tempfile
import tomllib
import unittest
import unittest.mock
import uuid
import zlib

from orchard import install_device as D
from orchard import install_direct as direct
from orchard import releases
from orchard.image import platforms_index, publish
from orchard.hardware import ROOT

sys.path.insert(0, str(ROOT / 'tools'))
from validate_manifest import ManifestError, validate  # noqa: E402

BLOCK = 4096
ENTRIES = 32   # entries in a block-sized table
OTHER = '6d6ef1a7-44c7-4a4a-8a6b-3a2f4a0f0a01'


def gpt(blocks=1 << 20):
    """(header, entries) of a disk with one partition at blocks 100..199."""
    table = bytearray(ENTRIES * 128)
    table[:128] = direct.entry_bytes(D.ROOT_ARM64, OTHER, 100, 100, 'other')
    header = bytearray(92)
    header[:8] = b'EFI PART'
    struct.pack_into('<II', header, 8, 0x10000, 92)
    struct.pack_into('<QQQQ', header, 24, 1, blocks - 1, 6, blocks - 3)
    header[56:72] = uuid.uuid4().bytes_le
    struct.pack_into('<QIII', header, 72, 2, ENTRIES, 128, zlib.crc32(table))
    struct.pack_into('<I', header, 16, zlib.crc32(header))
    return bytes(header).ljust(BLOCK, b'\0'), bytes(table)


def layer(path):
    with tarfile.open(path, 'w:gz') as archive:
        for name, data in (('usr/lib/modules/7.0/vmlinuz', b'KERNEL'), ('usr/lib/modules/7.0/initrd', b'INITRD'),
                           ('usr/lib/modules/7.0/dtb/t8010-j71b.dtb', b'TREE'),
                           ('usr/lib/modules/7.0/dtb/t8010-j72b.dtb', b'OTHER')):
            member = tarfile.TarInfo(name)
            member.size = len(data)
            archive.addfile(member, io.BytesIO(data))


class Payload(unittest.TestCase):
    def setUp(self):
        work = tempfile.TemporaryDirectory()
        self.addCleanup(work.cleanup)
        self.work = Path(work.name)
        self.tools = self.work / 'install-tools.tar.gz'
        with tarfile.open(self.tools, 'w:gz') as archive:
            for name in platforms_index.INSTALL_TOOLS:
                member = tarfile.TarInfo(name)
                data = name.encode()
                member.size = len(data)
                archive.addfile(member, io.BytesIO(data))

    def test_the_payload_is_m1n1s_own_format_with_the_add_on_after_the_initrd(self):
        (self.work / 'm1n1').write_bytes(b'M1N1')
        (self.work / 'logo').write_bytes(b'LOGO')
        layer(self.work / 'layer')
        data = direct.payload(self.work / 'm1n1', self.work / 'logo', self.work / 'layer', 't8010-j71b.dtb', b'ADD-ON')
        head = b'M1N1LOGOchosen.bootargs=' + direct.BOOT_ARGS.encode() + b'\nTREEKERNEL' + direct.INITRAMFS_MAGIC
        initrd = b'INITRD\0\0ADD-ON'    # archives start on a 4-byte boundary
        self.assertEqual(data, head + struct.pack('<I', len(initrd)) + initrd)

    def test_the_add_on_carries_the_releases_tools_and_this_boots_identity(self):
        archive = direct.add_on(self.tools, 0x1234, 'ssh-ed25519 KEY\n', b'HOST')
        for name in (*platforms_index.INSTALL_TOOLS, 'run', 'udhcpd.conf', 'ecid', 'authorized_keys', 'host_key'):
            self.assertIn(b'install/' + name.encode() + b'\0', archive)
        self.assertIn(b'1234\n', archive)
        self.assertIn(b'root:x:0:0', archive)
        self.assertTrue(archive.startswith(b'070701'))

    def test_a_key_made_for_ssh_converts_to_dropbears_form(self):
        path = self.work / 'key'
        subprocess.run(['ssh-keygen', '-q', '-t', 'ed25519', '-N', '', '-f', path], check=True)
        converted = direct.dropbear_key(path.read_text())
        self.assertTrue(converted.startswith(struct.pack('>I', 11) + b'ssh-ed25519' + struct.pack('>I', 64)))
        self.assertEqual(len(converted), 4 + 11 + 4 + 64)
        with self.assertRaises(ValueError):
            direct.dropbear_key('-----BEGIN-----\nAAAA\n-----END-----\n')


class Inspection(unittest.TestCase):
    def test_the_installers_system_starts_with_this_computers_keys_and_writes_nothing(self):
        with tempfile.TemporaryDirectory() as temporary:
            work = Path(temporary)
            for name in ('m1n1', 'pongo'):
                (work / name).write_bytes(name.encode())
            layer(work / 'layer')
            with tarfile.open(work / 'tools', 'w:gz') as archive:
                for name in platforms_index.INSTALL_TOOLS:
                    member = tarfile.TarInfo(name)
                    archive.addfile(member, io.BytesIO(b''))
            adapter = unittest.mock.Mock(DEVICETREE='t8010-j71b.dtb')
            sent = []
            adapter.boot_payload.side_effect = lambda device, image, pongo, progress: sent.append(image.read_bytes())
            assets = {'m1n1': work / 'm1n1', 'pongo': work / 'pongo', 'layer': work / 'layer', 'install_tools': work / 'tools'}
            with unittest.mock.patch('orchard.computer_key.public_keys', return_value=['ssh-ed25519 MINE']):
                direct.start_for_inspection(adapter, {}, assets, 0x12, None)
            self.assertIn(b'ssh-ed25519 MINE\n', sent[0])
            adapter.install.assert_not_called()


class Tables(unittest.TestCase):
    def plan(self, boot=True):
        return {'boot': {'create': boot, 'partuuid': str(uuid.uuid4()), 'start': 1000, 'blocks': 100},
                'root': {'partuuid': str(uuid.uuid4()), 'start': 2000, 'blocks': 500, 'label': 'omarchy'}}

    def apply(self, plan):
        header, entries = gpt()
        writes, numbers = direct.new_tables(plan, BLOCK, header, entries)
        return writes, numbers, header

    def test_both_copies_are_valid_and_keep_the_other_partition(self):
        plan = self.plan()
        writes, numbers, _ = self.apply(plan)
        self.assertEqual([lba for lba, _ in writes][:2], [(1 << 20) - 2, (1 << 20) - 1], 'the backup goes first')
        for (_, table), (_, raw) in ((writes[0], writes[1]), (writes[2], writes[3])):
            size = struct.unpack_from('<I', raw, 12)[0]
            blank = bytearray(raw[:size])
            struct.pack_into('<I', blank, 16, 0)
            self.assertEqual(struct.unpack_from('<I', raw, 16)[0], zlib.crc32(blank))
            self.assertEqual(struct.unpack_from('<I', raw, 88)[0], zlib.crc32(table))
            _, partitions, _ = D.parse_gpt('disk', BLOCK, raw, table)
            found = {p['partuuid']: (p['start'], p['blocks']) for p in partitions}
            self.assertEqual(found, {OTHER: (100, 100), plan['boot']['partuuid']: (1000, 100),
                                     plan['root']['partuuid']: (2000, 500)})
        self.assertEqual(sorted(numbers.values()), [2, 3])

    def test_a_replaced_partition_leaves_the_table(self):
        plan = self.plan(boot=False)
        plan['remove'] = {'number': 1, 'partuuid': OTHER}
        writes, numbers, _ = self.apply(plan)
        _, partitions, _ = D.parse_gpt('disk', BLOCK, writes[3][1], writes[2][1])
        self.assertEqual([p['partuuid'] for p in partitions], [plan['root']['partuuid']])
        self.assertEqual(list(numbers.values()), [1])

    def test_the_probe_reads_filesystems_without_blkid(self):
        ext4 = bytearray(4096)
        ext4[1080:1082] = b'\x53\xef'
        uid = uuid.uuid4()
        ext4[1128:1144] = uid.bytes
        ext4[1144:1148] = b'root'
        self.assertEqual(direct.probe(bytes(ext4)), {'fs': 'ext4', 'fs_uuid': str(uid), 'label': 'root'})
        self.assertIsNone(direct.probe(bytes(4096))['fs'])


class LinkLocal(unittest.TestCase):
    def test_the_new_interface_is_tried_before_dhcp(self):
        with unittest.mock.patch.object(direct, 'link_local_interfaces', return_value={'en1', 'en9'}):
            self.assertEqual(direct.device_hosts({'en1'}), ['fe80::1%en9', direct.HOST])


def receipt(name, data=b'x'):
    return {'file': name, 'bytes': len(data), 'sha256': hashlib.sha256(data).hexdigest()}


def manifest():
    root = {**receipt('root.blocks.zst'), 'compression': 'zstd', 'format': 'blocks', 'expanded_bytes': 4096,
            'members': 1, 'map': receipt('root.map.json')}
    layer_receipt = {**receipt('platform-apple-16k.tar.gz'), 'compression': 'gzip', 'expanded_bytes': 1, 'members': 1,
                     'kernel_release': '7.0'}
    return {'schema': 2, 'kind': 'orchard.install', 'distro': 'omarchy-a', 'name': 'Omarchy A', 'version': '1',
            'boot_method': 'checkm8-pongo', 'min_orchard': '0.2.0', 'protocol': 1,
            'orchard': {'commit': 'a' * 40, 'modified': False}, 'install': {'default_root': '/omarchy'}, 'root': root, 'pongo': receipt('pongo.bin'),
            'm1n1': receipt('m1n1.bin'), 'install_tools': receipt('install-tools.tar.gz'), 'logo': receipt('logo.bin'),
            'platforms': {'apple-16k': {'boards': ['j71b'], 'layer': layer_receipt}}}


class Release(unittest.TestCase):
    def test_a_block_image_release_needs_no_recovery_but_its_install_pieces(self):
        validate(manifest())
        for name in ('m1n1', 'install_tools'):
            broken = copy.deepcopy(manifest())
            del broken[name]
            with self.subTest(name=name), self.assertRaises(ManifestError):
                validate(broken)

    def test_starting_the_device_needs_only_the_small_files(self):
        _, boot = releases.receipts_for(manifest(), 'j71b', 'boot')
        self.assertEqual(sorted(boot), ['install_tools', 'layer', 'logo', 'm1n1', 'pongo'])
        _, install = releases.receipts_for(manifest(), 'j71b', 'install')
        self.assertEqual(sorted(install), ['install_tools', 'layer', 'logo', 'm1n1', 'map', 'pongo', 'root'])


class Publishing(unittest.TestCase):
    """A release links what it took finished from orchard, and hosts only what is its own."""
    def stage(self, base_url):
        work = tempfile.TemporaryDirectory()
        self.addCleanup(work.cleanup)
        directory = Path(work.name) / 'release'
        directory.mkdir()
        document = manifest()
        names = {}
        def fill(row):
            data = row['file'].encode()
            (directory / row['file']).write_bytes(data)
            row.update(bytes=len(data), sha256=hashlib.sha256(data).hexdigest())
            names[row['file']] = row
        for row in (document['root'], document['root']['map'], document['pongo'], document['m1n1'],
                    document['install_tools'], document['logo'], document['platforms']['apple-16k']['layer']):
            fill(row)
        index = {'schema': 1, 'kind': 'orchard.platforms', 'commit': 'abc',
                 'loaders': {'pongo': document['pongo'], 'm1n1': document['m1n1']},
                 'install_tools': document['install_tools'],
                 'platforms': {'apple-16k': document['platforms']['apple-16k']['layer']}}
        if base_url:
            index['base_url'] = base_url
        (directory / 'platforms.toml').write_text(publish.toml(index))
        (directory / 'release.toml').write_text(publish.toml(document))
        data = (directory / 'release.toml').read_bytes()
        (directory / 'index.toml').write_text(publish.toml({
            'schema': 2, 'kind': 'orchard.index', 'distro': 'omarchy-a',
            'release': {'file': 'release.toml', 'bytes': len(data), 'sha256': hashlib.sha256(data).hexdigest()}}))
        output = Path(work.name) / 'hosted'
        publish.publish(directory, 'v1', output, distro='omarchy-a')
        return output, tomllib.loads((output / 'release.toml').read_text())

    def test_orchards_files_are_linked_and_not_copied(self):
        base = 'https://github.com/OrchardLinux/orchard/releases/download/nightly-1'
        output, hosted = self.stage(base)
        self.assertEqual(hosted['m1n1']['url'], base + '/m1n1.bin')
        self.assertEqual(hosted['platforms']['apple-16k']['layer']['url'], base + '/platform-apple-16k.tar.gz')
        distro = 'https://github.com/OrchardLinux/omarchy-a/releases/download/v1/'
        self.assertEqual(hosted['root']['url'], distro + 'root.blocks.zst')
        self.assertEqual(hosted['root']['map']['url'], distro + 'root.map.json')
        self.assertEqual(hosted['logo']['url'], distro + 'logo.bin')
        self.assertEqual(sorted(path.name for path in output.iterdir()),
                         ['index.toml', 'logo.bin', 'release.toml', 'root.blocks.zst', 'root.map.json'])
        validate(hosted)

    def test_a_local_build_hosts_everything(self):
        output, hosted = self.stage(None)
        self.assertIn('/omarchy-a/', hosted['m1n1']['url'])
        self.assertTrue((output / 'm1n1.bin').is_file())


if __name__ == '__main__':
    unittest.main()
