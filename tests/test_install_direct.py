"""The install without a recovery: what is sent to the device, and what a release must say for it."""
import copy
import hashlib
import io
from pathlib import Path
import sys
import tarfile
import tempfile
import tomllib
import unittest
import unittest.mock

from orchard.image import publish
from orchard.hardware import ROOT

sys.path.insert(0, str(ROOT / 'tools'))
from validate_manifest import ManifestError, validate  # noqa: E402

BLOCK = 4096
ENTRIES = 32   # entries in a block-sized table
OTHER = '6d6ef1a7-44c7-4a4a-8a6b-3a2f4a0f0a01'


def layer(path):
    with tarfile.open(path, 'w:gz') as archive:
        for name, data in (('usr/lib/modules/7.0/vmlinuz', b'KERNEL'), ('usr/lib/modules/7.0/initrd', b'INITRD'),
                           ('usr/lib/modules/7.0/dtb/t8010-j71b.dtb', b'TREE'),
                           ('usr/lib/modules/7.0/dtb/t8010-j72b.dtb', b'OTHER')):
            member = tarfile.TarInfo(name)
            member.size = len(data)
            archive.addfile(member, io.BytesIO(data))


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
