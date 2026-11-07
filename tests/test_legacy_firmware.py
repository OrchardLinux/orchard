"""Legacy firmware pipeline: generated fixtures only; no Apple bytes or keys."""
import hashlib
import json
from pathlib import Path
import plistlib
import random
import struct
import tempfile
import unittest
from unittest.mock import patch
import zlib

from orchard import recipe
from orchard.apple import aes, firmware_keys, hfs, legacy_images, remote_ipsw, sgx, sparse_deflate, trx, udif
from test_remote_ipsw import Response, server


def sha(data):
    return hashlib.sha256(data).hexdigest()


class Bytes:
    def __init__(self, data):
        self.data, self.size, self.requests = data, len(data), []

    def read(self, offset, length):
        if not 0 <= offset <= offset + length <= self.size:
            raise ValueError('Fixture read out of bounds')
        self.requests.append((offset, length))
        return self.data[offset:offset + length]


def encrypt(key, iv, data):
    """Small independent forward AES-128 for generated encrypted fixtures."""
    def xtime(x):
        return ((x << 1) ^ (0x11b if x & 128 else 0)) & 255
    keys = list(key)
    rcon = 1
    for i in range(10):
        word = [aes.SBOX[x] for x in keys[-3:] + keys[-4:-3]]
        word[0] ^= rcon
        rcon = xtime(rcon)
        for j in range(16):
            keys.append(keys[-16] ^ word[j % 4])
            word[j % 4] = keys[-1]
    result, previous = bytearray(), iv
    for pos in range(0, len(data), 16):
        s = [a ^ b ^ c for a, b, c in zip(data[pos:pos + 16], previous, keys[:16])]
        for round in range(1, 11):
            s = [aes.SBOX[s[((column + row) % 4) * 4 + row]] for column in range(4) for row in range(4)]
            if round != 10:
                mixed = []
                for c in range(4):
                    a, b, d, e = s[c * 4:c * 4 + 4]
                    total = a ^ b ^ d ^ e
                    mixed += [a ^ total ^ xtime(a ^ b), b ^ total ^ xtime(b ^ d),
                              d ^ total ^ xtime(d ^ e), e ^ total ^ xtime(e ^ a)]
                s = mixed
            s = [a ^ b for a, b in zip(s, keys[round * 16:(round + 1) * 16])]
        previous = bytes(s)
        result += previous
    return bytes(result)


def encrypted_dmg(data, key):
    import hmac
    header = bytearray(512)
    header[:8] = b'encrcdsa'
    struct.pack_into('>I', header, 8, 2)
    struct.pack_into('>IQQ', header, 52, 512, len(data), 512)
    out = bytearray(header)
    for i in range(0, len(data), 512):
        iv = hmac.new(key[16:], struct.pack('>I', i // 512), hashlib.sha1).digest()[:16]
        out += encrypt(key[:16], iv, data[i:i + 512].ljust(512, b'\0'))
    return bytes(out)


def img3(macho, key, iv):
    stream = b''.join(b'\xff' + macho[p:p + 8] for p in range(0, len(macho), 8))
    plain = (b'complzss' + struct.pack('>III', zlib.adler32(macho), len(macho), len(stream))).ljust(384, b'\0') + stream
    cipher = encrypt(key, iv, plain.ljust((len(plain) + 15) & ~15, b'\0'))
    tag = struct.pack('<4sII', b'ATAD', len(cipher) + 12, len(plain)) + cipher
    return struct.pack('<4s4I', b'3gmI', len(tag) + 20, len(tag), 0, 0) + tag


def trx_fixture(code=b'firmware', old=b''):
    blob = bytearray(struct.pack('<4s6I', b'HDR0', 28 + len(code) + len(old), 0, 1, len(code), 0, len(old)) + code + old)
    struct.pack_into('<I', blob, 8, zlib.crc32(blob[12:]) ^ 0xffffffff)
    return bytes(blob)


def segment(name, va, offset, size, sections=()):
    result = struct.pack('<II16s8I', 1, 56 + 68 * len(sections), name.encode(), va, size,
                         offset, size, 7, 7, len(sections), 0)
    for section, address, length in sections:
        result += struct.pack('<16s16s9I', section.encode(), name.encode(), address, length, 0, 0, 0, 0, 0, 0, 0)
    return result


def macho(commands):
    return struct.pack('<7I', 0xfeedface, 12, 0, 2, len(commands), sum(map(len, commands)), 0) + b''.join(commands)


def sgx_fixture():
    blob = bytearray(0x4000)
    top = macho([segment('__PRELINK_TEXT', 0x8000, 0x1000, 0x3000)])
    blob[:len(top)] = top
    embedded = macho([segment('__TEXT', 0x8000, 0, 0x1000,
                             [('__cstring', 0x8300, 13), ('__const', 0x8400, 4)]),
                      segment('__DATA', 0x9000, 0x1000, 0x1000, [('__data', 0x9100, 8)])])
    blob[0x1000:0x1000 + len(embedded)] = embedded
    blob[0x1300:0x130d] = b'SGXDriver543\0'
    blob[0x1400:0x1404] = b'PDS!'
    blob[0x2100:0x2108] = b'GPU-CODE'
    return bytes(blob)


def fork(size, start, count):
    return struct.pack('>QII', size, 0, count) + struct.pack('>II', start, count) + bytes(56)


def catalog_key(parent, name):
    text = name.encode('utf-16-be')
    return struct.pack('>HIH', 6 + len(text), parent, len(text) // 2) + text


def node(kind, records, next=0):
    blob = bytearray(512)
    struct.pack_into('>IIbBHH', blob, 0, next, 0, kind, 1, len(records), 0)
    pos = 14
    for i, record in enumerate(records):
        struct.pack_into('>H', blob, 510 - i * 2, pos)
        blob[pos:pos + len(record)] = record
        pos += len(record)
    struct.pack_into('>H', blob, 510 - len(records) * 2, pos)
    assert pos <= 510 - len(records) * 2
    return bytes(blob)


def hfs_fixture(files):
    # One file per leaf, an index with repeated parent prefixes, and a root header.
    count = len(files)
    blob = bytearray((8 + count * 3) * 512)
    header = bytearray(512)
    header[:2] = b'H+'
    struct.pack_into('>II', header, 40, 512, len(blob) // 512)
    header[272:352] = fork((count + 2) * 512, 4, count + 2)
    blob[1024:1536] = header
    tree = bytearray(512)
    struct.pack_into('>I', tree, 16, 1)
    struct.pack_into('>H', tree, 32, 512)
    blob[2048:2560] = tree
    indices = []
    for i, (name, data) in enumerate(sorted(files.items())):
        assert len(data) <= 1024
        record = bytearray(248)
        struct.pack_into('>H', record, 0, 2)
        struct.pack_into('>I', record, 8, 100 + i)
        block = 6 + count + 2 * i
        record[88:168] = fork(len(data), block, 2)
        key = catalog_key(2, name)
        indices.append(key + struct.pack('>I', i + 2))
        leaf = node(-1, [key + record], next=i + 3 if i + 1 < count else 0)
        blob[(6 + i) * 512:(7 + i) * 512] = leaf
        blob[block * 512:block * 512 + len(data)] = data
    blob[2560:3072] = node(0, indices)
    return bytes(blob)


def dmg_fixture(data):
    compressed = zlib.compress(data)
    mish = bytearray(244)
    mish[:4] = b'mish'
    struct.pack_into('>QQQ', mish, 8, 0, len(data) // 512, 0)
    struct.pack_into('>I', mish, 200, 1)
    struct.pack_into('>IIQQQQ', mish, 204, 0x80000005, 0, 0, len(data) // 512, 0, len(compressed))
    plist = plistlib.dumps({'resource-fork': {'blkx': [{'Name': 'Apple_HFS', 'Data': bytes(mish)}]}})
    trailer = bytearray(512)
    trailer[:4] = b'koly'
    struct.pack_into('>QQ', trailer, 216, len(compressed), len(plist))
    return compressed + plist + trailer


class LegacyFirmware(unittest.TestCase):
    def test_aes_known_vectors_all_key_sizes(self):
        plain = bytes.fromhex('00112233445566778899aabbccddeeff')
        for size, cipher in [(16, '69c4e0d86a7b0430d8cdb78070b4c55a'),
                             (24, 'dda97ca4864cdfe06eaf70a0ec0d7191'),
                             (32, '8ea2b7ca516745bfeafc49904b496089')]:
            self.assertEqual(aes.decrypt_cbc(bytes(range(size)), bytes(16), bytes.fromhex(cipher)), plain)
        key, iv = bytes(range(16)), bytes(reversed(range(16)))
        data = bytes(range(64))
        self.assertEqual(aes.decrypt_cbc(key, iv, encrypt(key, iv, data)), data)
        for key, iv, data in [(b'x', bytes(16), bytes(16)), (bytes(16), b'', bytes(16)), (bytes(16), bytes(16), b'x')]:
            with self.assertRaises(ValueError):
                aes.decrypt_cbc(key, iv, data)

    def test_encrypted_dmg_random_reads_and_bad_headers(self):
        data = random.Random(21).randbytes(2053)
        key = bytes(range(36))
        source = Bytes(encrypted_dmg(data, key))
        image = legacy_images.Encrcdsa(source, key)
        for start, size in [(0, 12), (510, 1027), (2050, 3), (2053, 0), (0, 2053)]:
            self.assertEqual(image.read(start, size), data[start:start + size])
        with self.assertRaises(ValueError):
            image.read(2053, 1)
        with self.assertRaises(ValueError):
            legacy_images.Encrcdsa(source, b'x')

    def test_img3_padding_lzss_and_bad_key(self):
        data, key, iv = sgx_fixture(), bytes(range(16)), bytes(16)
        blob = img3(data, key, iv)
        self.assertEqual(legacy_images.img3_kernelcache(blob, key, iv), data)
        for bad in [blob[:-1], blob[:20] + struct.pack('<4sII', b'ATAD', 0, 0) + blob[32:]]:
            with self.assertRaises(ValueError):
                legacy_images.img3_kernelcache(bad, key, iv)
        with self.assertRaises(ValueError):
            legacy_images.img3_kernelcache(blob, bytes(16), iv)

    def test_sparse_deflate_variable_blocks_and_prefix(self):
        for strategy in (zlib.Z_DEFAULT_STRATEGY, zlib.Z_FIXED):
            data = b'header' * 20000 + random.Random(17).randbytes(250000)
            compressor = zlib.compressobj(6, zlib.DEFLATED, -15, strategy=strategy)
            raw = compressor.compress(data) + compressor.flush()
            source = Bytes(raw)
            image = sparse_deflate.StoredDeflate(source, len(data))
            for offset, length in [(len(data) - 10, 10), (120000, 70000), (0, 150000), (1, 0)]:
                self.assertEqual(image.read(offset, length), data[offset:offset + length])
            self.assertLess(sum(n for _, n in source.requests), len(data))
        with self.assertRaises(ValueError):
            image.read(-1, 1)

    def test_sparse_stored_blocks_skip_payloads(self):
        chunks = [b'x' * 16391, b'y' * 16387, b'z' * 16]
        raw = b''.join(struct.pack('<BHH', int(i == 2), len(c), len(c) ^ 65535) + c for i, c in enumerate(chunks))
        source = Bytes(raw)
        image = sparse_deflate.StoredDeflate(source, sum(map(len, chunks)))
        self.assertEqual(image.read(image.size - 16, 16), chunks[-1])
        self.assertLess(sum(n for _, n in source.requests), 4200)
        with self.assertRaises(ValueError):
            sparse_deflate.StoredDeflate(Bytes(b'\x01\x01\x00\x00\x00x'), 1)
        with self.assertRaises(ValueError):
            sparse_deflate.StoredDeflate(Bytes(zlib.compress(b'compressible' * 1000)[2:-4]), 12000)

    def test_sparse_header_prefetch_follows_lengths_not_predictions(self):
        rng = random.Random(9)
        chunks = [rng.randbytes(rng.randint(16380, 16400)) for _ in range(300)]
        # A sudden change defeats prediction and must trigger a safe refetch.
        chunks[140] = b'x' * 60000
        data = b''.join(chunks)
        raw = b''.join(struct.pack('<BHH', int(i == len(chunks) - 1), len(c), len(c) ^ 65535) + c
                       for i, c in enumerate(chunks))

        class Multi(Bytes):
            batches = 0

            def read_ranges(self, ranges):
                self.batches += 1
                return [self.read(*pair) for pair in ranges]

        source = Multi(raw)
        image = sparse_deflate.StoredDeflate(source, len(data))
        self.assertEqual(image.read(len(data) - 12, 12), data[-12:])
        self.assertGreater(source.batches, 2)
        self.assertLess(source.batches, 16)
        self.assertLess(sum(n for _, n in source.requests), len(data) // 20)

    def test_remote_multipart_ranges_are_bounded_and_validated(self):
        from email.message import Message
        blob = bytes(range(256))

        def response(mutation=None, status=206):
            headers = Message()
            headers['Content-Type'] = 'multipart/byteranges; boundary=test-boundary'
            body = b''
            for first, last in [(3, 7), (100, 106)]:
                body += (b'\r\n--test-boundary\r\nContent-Type: application/octet-stream\r\n' +
                         f'Content-Range: bytes {first}-{last}/256\r\n\r\n'.encode() + blob[first:last + 1])
            body += b'\r\n--test-boundary--\r\n'
            return Response(mutation(body) if mutation else body, status, headers)

        remote = object.__new__(remote_ipsw.RemoteFile)
        remote.url, remote.size, remote.downloaded, remote.on_bytes = 'https://example.invalid', 256, 0, None
        with patch.object(remote_ipsw, 'open_url', lambda req: response()):
            self.assertEqual(remote.read_ranges([(3, 5), (100, 7)]), [blob[3:8], blob[100:107]])
        for mutation in [lambda b: b.replace(b'/256', b'/255'), lambda b: b[:-20],
                         lambda b: b.replace(b'100-106', b'101-107'), lambda b: b + bytes(4000)]:
            with patch.object(remote_ipsw, 'open_url', lambda req: response(mutation)):
                with self.assertRaises(ValueError):
                    remote.read_ranges([(3, 5), (100, 7)])
        with patch.object(remote_ipsw, 'open_url', lambda req: response(status=200)):
            with self.assertRaises(ValueError):
                remote.read_ranges([(3, 5), (100, 7)])

    def test_trx_crc_nvram_and_placeholder(self):
        fw = trx_fixture(old=bytes(28))
        result = trx.add_nvram(fw, b'# comment\n board = test # comment\n', '02:00:00:00:00:00')
        self.assertIn(b'board=test\0macaddr=02:00:00:00:00:00\0\0', result)
        self.assertEqual(struct.unpack_from('<I', result, 8)[0], zlib.crc32(result[12:]) ^ 0xffffffff)
        words = (len(result) - 28 - len(b'firmware') - 4) // 4
        self.assertEqual(struct.unpack_from('<I', result, len(result) - 4)[0], ((~words & 65535) << 16) | words)
        for bad in [b'x', fw[:-1], fw[:8] + bytes(4) + fw[12:], trx_fixture(old=b'x')]:
            with self.assertRaises(ValueError):
                trx.add_nvram(bad, b'x=1', '02:00:00:00:00:00')
        with self.assertRaises(ValueError):
            trx.add_nvram(fw, b'macaddr=00:00:00:00:00:01', '02:00:00:00:00:00')
        # Bytes the header length counts past offsets[0] + offsets[2] are dropped, as Cascadia does.
        tail = bytearray(trx_fixture())
        tail += b'trailer'
        struct.pack_into('<I', tail, 4, len(tail))
        struct.pack_into('<I', tail, 8, zlib.crc32(tail[12:]) ^ 0xffffffff)
        self.assertEqual(trx.add_nvram(bytes(tail), b'x=1', '02:00:00:00:00:00'),
                         trx.add_nvram(trx_fixture(), b'x=1', '02:00:00:00:00:00'))
        # Any other bootstrap address would escape the boot-time replacement.
        with self.assertRaisesRegex(ValueError, 'placeholder'):
            trx.add_nvram(fw, b'x=1', '02:10:5a:05:00:03')

    def test_sgx_container_layout(self):
        output = sgx.extract(sgx_fixture(), 'TEST')
        self.assertEqual(struct.unpack_from('<8s7I16s', output),
                         (b'SGX543FW', 1, 64, 8, 72, 4, 0x9100, 0x8400, b'TEST' + bytes(12)))
        self.assertEqual(output[64:], b'GPU-CODEPDS!')
        for bad in [b'bad', sgx_fixture()[:100], sgx_fixture().replace(b'SGXDriver543', b'OtherDriver!')]:
            with self.assertRaises(ValueError):
                sgx.extract(bad, 'TEST')

    def test_hfs_index_seeks_first_equal_parent(self):
        files = {'alpha': b'a', 'beta': b'b', 'gamma': b'c'}
        volume = hfs.Volume(Bytes(hfs_fixture(files)))
        for name, data in files.items():
            self.assertEqual(volume.read(name), data)
        for name in ('missing', '../alpha', ''):
            with self.assertRaises(ValueError):
                volume.read(name)

    def test_hfs_inline_and_resource_compression(self):
        expected = b'compressed fixture' * 8
        for kind in (3, 4):
            image = bytearray(hfs_fixture({'file': b''}))
            record_at = 6 * 512 + 14 + len(catalog_key(2, 'file'))
            image[record_at + 41] |= 0x20
            attribute = b'fpmc' + struct.pack('<IQ', kind, len(expected))
            if kind == 3:
                attribute += zlib.compress(expected)
            else:
                compressed = zlib.compress(expected)
                resource = struct.pack('>I', 16).ljust(20, b'\0')
                resource += struct.pack('<III', 1, 12, len(compressed)) + compressed
                image[7 * 512:7 * 512 + len(resource)] = resource
                image[record_at + 168:record_at + 248] = fork(len(resource), 7, 1)
            start = len(image) // 512
            tree = bytearray(512)
            struct.pack_into('>I', tree, 16, 1)
            struct.pack_into('>H', tree, 32, 512)
            name = 'com.apple.decmpfs'.encode('utf-16-be')
            key = struct.pack('>HHIIH', 12 + len(name), 0, 100, 0, len(name) // 2) + name
            value = struct.pack('>4I', 0x10, 0, 0, len(attribute)) + attribute
            image += tree + node(-1, [key + value])
            image[1024 + 352:1024 + 432] = fork(1024, start, 2)
            struct.pack_into('>I', image, 1024 + 44, len(image) // 512)
            self.assertEqual(hfs.Volume(Bytes(image)).read('file'), expected)

    def test_hfs_overflow_extents_and_catalog_cycle(self):
        expected = b'x' * 512 + b'ending'
        image = bytearray(hfs_fixture({'file': expected}))
        record_at = 6 * 512 + 14 + len(catalog_key(2, 'file'))
        image[record_at + 88:record_at + 168] = fork(len(expected), 7, 1)
        start = len(image) // 512
        tree = bytearray(512)
        struct.pack_into('>I', tree, 16, 1)
        struct.pack_into('>H', tree, 32, 512)
        key = struct.pack('>HBBII', 10, 0, 0, 100, 1)
        image += tree + node(-1, [key + struct.pack('>II', 8, 1) + bytes(56)])
        image[1024 + 192:1024 + 272] = fork(1024, start, 2)
        struct.pack_into('>I', image, 1024 + 44, len(image) // 512)
        self.assertEqual(hfs.Volume(Bytes(image)).read('file'), expected)
        struct.pack_into('>I', image, 6 * 512, 2)  # catalog leaf points to itself
        with self.assertRaisesRegex(ValueError, 'cycle'):
            hfs.Volume(Bytes(image)).read('missing')

    def test_udif_to_hfs(self):
        image = udif.Udif(Bytes(dmg_fixture(hfs_fixture({'firmware': b'test data'}))))
        self.assertEqual(hfs.Volume(image).read('firmware'), b'test data')
        self.assertEqual(udif.adc(b'\x82abc\x00\x02', 6), b'abcabc')
        with self.assertRaises(ValueError):
            udif.adc(b'\x00\x00', 3)

    def test_key_file_scope_permissions_and_redacted_errors(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / 'keys.json'
            document = {'schema': 1, 'ipsws': {'archive-hash': {
                'root.dmg': {'kind': 'encrcdsa', 'key': bytes(range(36)).hex()},
                'kernel': {'kind': 'img3', 'key': bytes(16).hex(), 'iv': bytes(16).hex()}}}}
            path.write_text(json.dumps(document))
            path.chmod(0o600)
            store = firmware_keys.KeyStore('archive-hash', path)
            self.assertEqual(store.get('root.dmg', 'encrcdsa'), bytes(range(36)))
            self.assertEqual(store.get('kernel', 'img3'), (bytes(16), bytes(16)))
            with self.assertRaises(ValueError):
                firmware_keys.KeyStore('other-hash', path).get('root.dmg', 'encrcdsa')
            path.chmod(0o644)
            with self.assertRaisesRegex(ValueError, 'owner-private'):
                firmware_keys.KeyStore('archive-hash', path).get('kernel', 'img3')
            link = Path(directory) / 'link'
            link.symlink_to(path)
            with self.assertRaises(ValueError):
                firmware_keys.KeyStore('archive-hash', link).get('kernel', 'img3')

    def test_recipe_auxiliary_inputs_are_checked_and_selected(self):
        fw, nv = trx_fixture(), b'board=test'
        result = trx.add_nvram(fw, nv, '02:00:00:00:00:00')
        output = {'destination': 'lib/firmware/wifi', 'sha256': sha(result), 'steps': [
            {'step': 'trx-add-nvram@1', 'input': 'fw', 'nvram': 'nv', 'placeholder_mac': '02:00:00:00:00:00'}]}
        document = {'schema': 2, 'inputs': {'fw': {'sha256': sha(fw)}, 'nv': {'sha256': sha(nv)}}, 'outputs': [output]}
        recipe.supported(document)
        self.assertEqual(recipe.needed_inputs(document), ['fw', 'nv'])
        self.assertEqual(recipe.needed_inputs(document, []), [])
        self.assertEqual(recipe.run(document, {'fw': fw, 'nv': nv}.__getitem__), {'lib/firmware/wifi': result})
        with self.assertRaisesRegex(ValueError, 'input differs'):
            recipe.run(document, {'fw': fw, 'nv': b'wrong'}.__getitem__)
        output['steps'][0]['nvram'] = 'unknown'
        with self.assertRaisesRegex(ValueError, 'Unknown recipe input'):
            recipe.supported(document)

    def test_all_four_recipe_outputs_from_generated_inputs(self):
        document = json.loads((Path(__file__).resolve().parents[1] / 'devices/p105/firmware.json').read_text())
        hcd = b'\x4c\xfc\x05' + bytes(4) + b'x' + b'\x4e\xfc\x04' + bytes(4)
        inputs = {'P105.mtprops': b'generated touch data', 'borg.trx': trx_fixture(),
                  'borg-t-st.txt': b'board=test', 'BlueTool': b'binary prefix' + hcd + b'trailing',
                  'kernelcache': sgx_fixture()}
        expected = [inputs['P105.mtprops'],
                    trx.add_nvram(inputs['borg.trx'], inputs['borg-t-st.txt'], '02:00:00:00:00:00'),
                    hcd, sgx.extract(inputs['kernelcache'], '12H321')]
        for name, data in inputs.items():
            document['inputs'][name]['sha256'] = sha(data)
        for output, data in zip(document['outputs'], expected):
            if 'pending' not in output:
                output['sha256'] = sha(data)
        # Wi-Fi waits for a measurement with real inputs: made, never installed.
        self.assertEqual([o['destination'] for o in recipe.pending(document)], ['lib/firmware/brcm/brcmfmac4334.bin'])
        self.assertEqual(len(recipe.pinned(document)), 3)
        recipe.check(document)
        seen = []

        def read(name):
            seen.append(name)
            return inputs[name]

        result = recipe.run(document, read)
        self.assertEqual(list(result.values()), expected)
        self.assertEqual(sorted(seen), sorted(inputs))
        seen.clear()
        recipe.run(document, read, document['outputs'][2:3])
        self.assertEqual(seen, ['BlueTool'])
        document['outputs'][0]['sha256'] = '0' * 64
        with self.assertRaisesRegex(ValueError, 'output differs'):
            recipe.run(document, read)
        del document['inputs']['borg.trx']['sha256']
        with self.assertRaisesRegex(ValueError, 'reads unpinned inputs'):
            recipe.supported(document)
        document['outputs'][1]['sha256'] = '0' * 64
        with self.assertRaisesRegex(ValueError, 'either sha256 or pending'):
            recipe.supported(document)

    def test_builds_install_pinned_outputs_and_measure_pending_ones(self):
        from orchard import firmware

        class Progress:
            phases = []

            def phase(self, text):
                self.phases.append(text)

        made = []

        def produce(profile, document, outputs, clock, progress):
            made.append([o['destination'] for o in outputs])
            return {o['destination']: o['destination'].encode() for o in outputs}

        pinned = {'destination': 'lib/firmware/a', 'sha256': sha(b'lib/firmware/a'), 'steps': []}
        waiting = {'destination': 'lib/firmware/b', 'pending': 'not measured', 'steps': []}
        document = {'inputs': {}, 'outputs': [pinned, waiting]}
        with tempfile.TemporaryDirectory() as directory, \
                patch.object(firmware, 'recipe_for', lambda *a, **k: document), \
                patch.object(firmware, 'cache_root', lambda: Path(directory)), \
                patch.object(firmware, '_produce', produce):
            outputs = firmware.ensure('test', None, Progress())
            self.assertEqual(sorted(p.name for p in outputs.iterdir()), [pinned['sha256']])
            self.assertTrue(firmware.cached('test'))
            self.assertIn('Not installing lib/firmware/b: its hash is pending verification', Progress.phases)
            self.assertEqual(firmware.measure('test', None, Progress()), {'lib/firmware/b': sha(b'lib/firmware/b')})
            self.assertEqual(made, [['lib/firmware/a'], ['lib/firmware/b']])
            self.assertEqual(sorted(p.name for p in outputs.iterdir()), [pinned['sha256']])

    def test_remote_encrypted_root_and_img3(self):
        # Exercise range ZIP -> sparse DEFLATE -> AES -> UDIF -> HFS, plus IMG3.
        key, iv = bytes(range(16)), bytes(16)
        root_key = bytes(range(36))
        image = encrypted_dmg(dmg_fixture(hfs_fixture({'firmware': b'fixture firmware'})), root_key)
        # zipfile level 0 is needed here: an encrypted tiny fixture can otherwise
        # remain a compressed block rather than the legacy IPSW's stored tail.
        import io
        import zipfile
        buffer = io.BytesIO()
        manifest = plistlib.dumps({'BuildIdentities': [{'Info': {'DeviceClass': 'p105ap'},
            'Manifest': {'KernelCache': {'Info': {'Path': 'kernelcache'}}}}]})
        with zipfile.ZipFile(buffer, 'w', compression=zipfile.ZIP_DEFLATED, compresslevel=0) as archive:
            archive.writestr('root.dmg', image)
            archive.writestr('BuildManifest.plist', manifest)
            archive.writestr('kernelcache', img3(b'\xce\xfa\xed\xfe' + bytes(28), key, iv))
        blob = buffer.getvalue()

        class Keys:
            def get(self, name, kind):
                return root_key if kind == 'encrcdsa' else (key, iv)

        with patch.object(remote_ipsw, 'open_url', server(blob, [])):
            source = remote_ipsw.Ipsw('https://example.invalid/fw', len(blob), keys=Keys())
            self.assertEqual(source.system_file('firmware'), b'fixture firmware')
            self.assertEqual(source.kernelcache('p105ap'), b'\xce\xfa\xed\xfe' + bytes(28))


if __name__ == '__main__':
    unittest.main()
