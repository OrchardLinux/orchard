"""Run the source-built C parser against synthetic LwVM media, never a device."""
import json
from pathlib import Path
import shutil
import struct
import subprocess
import tempfile
import unittest
import uuid
import zlib

from orchard.hardware import ROOT

LINUX = uuid.UUID('0fc63daf-8483-4772-8e79-3d69d8477de4').bytes
ROOT_UUID = '12345678-1234-4321-8765-123456789abc'
MEDIA = 16 * 1024 * 1024
CHUNK = 16384


def crc(header):
    struct.pack_into('<I', header, 0x2c, 0)
    struct.pack_into('<I', header, 0x2c, zlib.crc32(header))
    return header


def header(media=MEDIA):
    """Invented identities; root's physical order is 10,6,7 with a partial tail."""
    h = bytearray(4096)
    h[:16] = bytes.fromhex('6a9088cf8afd630ae351e24887e0b98b')
    h[16:32] = uuid.UUID('aaaaaaaa-bbbb-4ccc-8ddd-eeeeeeeeeeee').bytes
    struct.pack_into('<QI', h, 0x20, media, 3)
    start = 24576
    for i, length in enumerate((2 * CHUNK, 4 * CHUNK, 2 * CHUNK + 8192)):
        off = 0x200 + i * 0x80
        h[off:off + 16] = LINUX if i == 2 else uuid.UUID('48465300-0000-11aa-aa11-00306543ecac').bytes
        h[off + 16:off + 32] = uuid.UUID(ROOT_UUID if i == 2 else f'1000000{i}-1111-4222-8333-444444444444').bytes
        struct.pack_into('<QQQ', h, off + 32, start, start + length, 0x3000000000000 if i == 1 else 0)
        start += length
    for index in range(1024):
        struct.pack_into('<H', h, 0x800 + index * 2, 0xf000 if index == 0 else 0xf111)
    for owner, physical in enumerate(((1, 2), (3, 4, 5, 8), (10, 6, 7))):
        for logical, index in enumerate(physical):
            struct.pack_into('<H', h, 0x800 + index * 2, owner << 12 | logical)
    return crc(h)


class LwvmCParser(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cc = shutil.which('cc')
        if not cc:
            raise unittest.SkipTest('C compiler is required for the real parser tests')
        cls.temporary = tempfile.TemporaryDirectory()
        cls.work = Path(cls.temporary.name)
        cls.binary = cls.work / 'orchard-lwvm'
        subprocess.run([cc, '-std=c11', '-O2', '-Wall', '-Wextra', '-Werror',
                        str(ROOT / 'boot/lwvm/lwvm.c'), str(ROOT / 'boot/lwvm/main.c'),
                        '-o', str(cls.binary)], check=True)

    @classmethod
    def tearDownClass(cls):
        cls.temporary.cleanup()

    def invoke(self, primary=None, backup=None, root=ROOT_UUID, media=MEDIA):
        primary = header() if primary is None else primary
        backup = primary if backup is None else backup
        image = self.work / 'media.img'
        with image.open('wb') as stream:
            stream.truncate(media)
            stream.write(primary)
            stream.seek(CHUNK - 4096)
            stream.write(backup)
        return subprocess.run([str(self.binary), 'inspect', '--disk', str(image),
                               '--root', root, '--json'], capture_output=True, text=True)

    def reject(self, h, message, **kwargs):
        result = self.invoke(crc(h), **kwargs)
        self.assertNotEqual(result.returncode, 0, result.stdout)
        self.assertIn(message, result.stderr)

    def test_noncontiguous_map_uses_physical_chunks_and_clips_the_final_chunk(self):
        result = self.invoke()
        self.assertEqual(result.returncode, 0, result.stderr)
        receipt = json.loads(result.stdout)
        self.assertEqual(receipt['bytes'], 40960)
        self.assertEqual(receipt['partition_index'], 2)
        self.assertEqual(receipt['extents'], [
            {'start_sector': 0, 'sectors': 32, 'physical_sector': 320},
            {'start_sector': 32, 'sectors': 48, 'physical_sector': 192}])
        self.assertEqual(receipt['disk'], {'major': None, 'minor': None})

    def test_headers_must_both_have_good_crc_and_match(self):
        h = header()
        h[100] ^= 1
        self.assertIn('CRC mismatch', self.invoke(h).stderr)
        h = header()
        h[100] ^= 1
        self.assertIn('copies disagree', self.invoke(backup=crc(h)).stderr)
        self.assertIn('signature', self.invoke(bytearray(4096)).stderr)

    def test_requested_identity_type_and_encryption_attributes(self):
        self.assertIn('not found', self.invoke(root='99999999-1234-4321-8765-123456789abc').stderr)
        self.assertIn('invalid disk, root UUID', self.invoke(root='not-a-uuid').stderr)
        for offset, value in ((0x300, b'\1' * 16), (0x330, struct.pack('<Q', 1))):
            h = header()
            h[offset:offset + len(value)] = value
            self.reject(h, 'Linux type and zero attributes')

    def test_duplicate_hole_extra_and_foreign_chunks_are_rejected(self):
        for index, word, message in ((7, 0x2001, 'duplicate'), (7, 0xf111, 'incomplete'),
                                     (11, 0x2003, 'outside'), (11, 0x3000, 'outside'),
                                     (0, 0x2000, 'outside'), (11, 0xffff, 'special')):
            h = header()
            struct.pack_into('<H', h, 0x800 + index * 2, word)
            self.reject(h, message)

    def test_partition_descriptors_count_uuid_bounds_and_overlap(self):
        for count in (0, 9, 0xffffffff):
            h = header()
            struct.pack_into('<I', h, 0x28, count)
            self.reject(h, 'count')
        h = header()
        h[0x310:0x320] = h[0x210:0x220]
        self.reject(h, 'duplicate LwVM partition UUID')
        for start, end in ((0, 4096), (24577, 40960), (24576, MEDIA + 4096), (24576, 24576)):
            h = header()
            struct.pack_into('<QQ', h, 0x320, start, end)
            self.reject(h, 'descriptor')
        h = header()
        struct.pack_into('<QQ', h, 0x320, 24576, 65536)
        self.reject(h, 'overlapping')

    def test_declared_media_must_match_actual_image(self):
        h = header()
        struct.pack_into('<Q', h, 0x20, MEDIA - 4096)
        self.reject(h, 'media identity or length')

    def test_geometry_does_not_assume_a_64gb_unit(self):
        # A smaller non-power-of-two medium keeps the same chunk geometry.
        media = MEDIA - 4096
        h = header(media)
        struct.pack_into('<H', h, 0x800 + 1023 * 2, 0xf3ff)
        result = self.invoke(crc(h), media=media)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(json.loads(result.stdout)['chunk_bytes'], CHUNK)

    def test_map_refuses_regular_image_before_ioctl(self):
        self.invoke()
        result = subprocess.run([str(self.binary), 'map', '--disk', str(self.work / 'media.img'),
                                 '--root', ROOT_UUID], capture_output=True, text=True)
        self.assertNotEqual(result.returncode, 0)
        self.assertIn('block device', result.stderr)
