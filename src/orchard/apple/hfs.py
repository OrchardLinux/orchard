"""Read-only HFS+ firmware lookup, adapted from rzxvx / Project Cascadia.

Catalog, attributes and overflow extents are sought by numeric key prefix,
then scanned only within that prefix. This avoids both a full catalog scan
and approximating Apple's Unicode collation with Python's case folding.
"""
import struct
import zlib


def u16(data, offset):
    return struct.unpack_from('>H', data, offset)[0]


def u32(data, offset):
    return struct.unpack_from('>I', data, offset)[0]


def u64(data, offset):
    return struct.unpack_from('>Q', data, offset)[0]


class Fork:
    def __init__(self, volume, record, cnid=None, kind=0):
        if len(record) != 80:
            raise ValueError('Truncated HFS fork')
        self.volume, self.size = volume, u64(record, 0)
        self.extents = [struct.unpack_from('>II', record, 16 + 8 * i) for i in range(8)]
        covered = sum(n for _, n in self.extents)
        if covered * volume.bs < self.size:
            if cnid is None:
                raise ValueError('Fragmented HFS metadata bootstrap is unsupported')
            for key, value in volume.records(volume.extents, (kind, cnid),
                                              lambda k: (k[0], u32(k, 2))):
                if u32(key, 6) != covered or len(value) < 64:
                    raise ValueError('Missing HFS overflow extents')
                extra = [struct.unpack_from('>II', value, i * 8) for i in range(8)]
                self.extents.extend(extra)
                covered += sum(n for _, n in extra)
        if covered * volume.bs < self.size:
            raise ValueError('HFS extents do not cover the fork')
        if any(start + count > volume.blocks for start, count in self.extents):
            raise ValueError('HFS extent outside the volume')

    def read(self, offset, length):
        if not 0 <= offset <= offset + length <= self.size:
            raise ValueError('Read outside HFS fork')
        out = bytearray()
        for start, count in self.extents:
            span = count * self.volume.bs
            if offset >= span:
                offset -= span
                continue
            take = min(length, span - offset)
            out += self.volume.image.read(self.volume.base + start * self.volume.bs + offset, take)
            length -= take
            offset = 0
            if not length:
                break
        if length:
            raise ValueError('Short HFS fork')
        return bytes(out)

    def all(self):
        return self.read(0, self.size)


class Volume:
    def __init__(self, image, base=0):
        self.image, self.base = image, base
        header = image.read(base + 1024, 512)
        if header[:2] not in (b'H+', b'HX'):
            raise ValueError('Not an HFS+ volume')
        self.bs, self.blocks = u32(header, 40), u32(header, 44)
        if self.bs < 512 or self.bs & (self.bs - 1) or base + self.bs * self.blocks > image.size:
            raise ValueError('Invalid HFS volume geometry')
        self.extents = Fork(self, header[192:272])
        self.catalog = Fork(self, header[272:352], 4)
        self.attributes = Fork(self, header[352:432], 8)

    def records(self, fork, target, prefix):
        if not fork.size:
            return
        header = fork.read(0, 64)
        root, node_size = u32(header, 16), u16(header, 32)
        if node_size < 512 or node_size & (node_size - 1):
            raise ValueError('Invalid HFS B-tree node size')
        seen = set()

        def node(number):
            if number in seen:
                raise ValueError('HFS B-tree cycle')
            seen.add(number)
            raw = fork.read(number * node_size, node_size)
            count = u16(raw, 10)
            table = node_size - 2 * (count + 1)
            offsets = [u16(raw, node_size - 2 * (i + 1)) for i in range(count + 1)]
            if table < 14 or any(not 14 <= a < b <= table for a, b in zip(offsets, offsets[1:])):
                raise ValueError('Invalid HFS record offsets')
            records = []
            for a, b in zip(offsets, offsets[1:]):
                key_size = u16(raw, a)
                end = a + 2 + key_size
                if key_size < 6 or end > b:
                    raise ValueError('Invalid HFS key')
                records.append((raw[a + 2:end], raw[(end + 1) & ~1:b]))
            return raw, records

        number = root
        while number:
            raw, entries = node(number)
            kind = raw[8]
            if kind == 0:  # index; keep the child before the first equal prefix
                if not entries:
                    raise ValueError('Empty HFS index')
                chosen = entries[0][1]
                for key, value in entries:
                    if prefix(key) >= target:
                        break
                    chosen = value
                if len(chosen) < 4:
                    raise ValueError('Truncated HFS child pointer')
                number = u32(chosen, 0)
            elif kind == 255:
                for key, value in entries:
                    p = prefix(key)
                    if p > target:
                        return
                    if p == target:
                        yield key, value
                number = u32(raw, 0)
            else:
                raise ValueError('Unexpected HFS node kind')

    def lookup(self, path):
        names = path.strip('/').split('/')
        if not names or any(n in ('', '.', '..') for n in names):
            raise ValueError('Invalid HFS file path')
        parent = 2
        for i, name in enumerate(names):
            found = []
            for key, record in self.records(self.catalog, parent, lambda k: u32(k, 0)):
                length = u16(key, 4)
                if len(key) != 6 + 2 * length:
                    raise ValueError('Invalid HFS catalog name')
                if key[6:].decode('utf-16-be') == name:
                    found.append(record)
            if len(found) != 1:
                raise ValueError('HFS file not found or ambiguous: ' + path)
            record = found[0]
            last = i == len(names) - 1
            if len(record) < (248 if last else 88) or u16(record, 0) != (2 if last else 1):
                raise ValueError('Unexpected HFS catalog record: ' + path)
            parent = u32(record, 8)
        return record

    def xattr(self, cnid, name):
        for key, record in self.records(self.attributes, cnid, lambda k: u32(k, 2)):
            if len(key) < 12 or len(key) != 12 + u16(key, 10) * 2:
                raise ValueError('Invalid HFS attribute key')
            if key[12:].decode('utf-16-be') == name:
                if len(record) < 16 or u32(record, 0) != 0x10 or u32(record, 12) > len(record) - 16:
                    raise ValueError('Unsupported or truncated HFS attribute')
                return record[16:16 + u32(record, 12)]
        raise ValueError('HFS compression attribute missing')

    def read(self, path):
        record = self.lookup(path)
        cnid = u32(record, 8)
        if not record[41] & 0x20:
            return Fork(self, record[88:168], cnid).all()
        header = self.xattr(cnid, 'com.apple.decmpfs')
        if len(header) < 16 or header[:4] != b'fpmc':
            raise ValueError('Invalid decmpfs header')
        kind, size = struct.unpack_from('<IQ', header, 4)

        def inflate(data):
            return data[1:] if data[:1] == b'\xff' else zlib.decompress(data)

        if kind == 3:
            out = inflate(header[16:])
        elif kind == 4:
            resource = Fork(self, record[168:248], cnid, 255).all()
            base = u32(resource, 0) + 4
            count = struct.unpack_from('<I', resource, base)[0]
            if base + 4 + count * 8 > len(resource):
                raise ValueError('Invalid decmpfs resource table')
            chunks = []
            for i in range(count):
                offset, length = struct.unpack_from('<II', resource, base + 4 + i * 8)
                if offset < 4 + count * 8 or base + offset + length > len(resource):
                    raise ValueError('Invalid decmpfs resource chunk')
                chunks.append(inflate(resource[base + offset:base + offset + length]))
            out = b''.join(chunks)
        else:
            raise ValueError(f'Unsupported HFS decmpfs type {kind}')
        if len(out) != size:
            raise ValueError('HFS decompressed file size differs')
        return out
