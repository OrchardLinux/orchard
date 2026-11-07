"""Checked LwVM snapshots, from Shayne Legault's LwVM installer (shaynelegault/orchard#94).

All offsets are bytes. UUIDs in LwVM descriptors use network byte order.
"""
import hashlib
import struct
import uuid
import zlib

MAGIC = bytes.fromhex('6a9088cf8afd630ae351e24887e0b98b')
HEADER_BYTES = 4096
LINUX_TYPE = '0fc63daf-8483-4772-8e79-3d69d8477de4'
HFS_TYPE = '48465300-0000-11aa-aa11-00306543ecac'
SECTOR = 512


class Error(ValueError):
    pass


def digest(data):
    return hashlib.sha256(data).hexdigest()


def chunk_size(media_bytes):
    if type(media_bytes) is not int or media_bytes < 8192 or media_bytes % HEADER_BYTES:
        raise Error('LwVM medium size is unsupported')
    shifted = (media_bytes - 1) >> 10
    chunk = 1 << shifted.bit_length()
    if not shifted or shifted > 0xffffffff or chunk < 2 * HEADER_BYTES or media_bytes // chunk < 2:
        raise Error('LwVM chunk geometry is unsupported')
    return chunk


def checksum(header):
    checked = bytearray(header)
    checked[0x2c:0x30] = bytes(4)
    return zlib.crc32(checked)


def parse(primary, backup, media_bytes=None):
    if len(primary) != HEADER_BYTES or primary != backup or primary[:16] != MAGIC:
        raise Error('LwVM header pair is incomplete or disagrees')
    if checksum(primary) != struct.unpack_from('<I', primary, 0x2c)[0]:
        raise Error('LwVM header CRC differs')
    size, count = struct.unpack_from('<QI', primary, 0x20)
    chunk = chunk_size(size)
    if media_bytes is not None and size != media_bytes:
        raise Error('LwVM medium size differs from the provider')
    if not any(primary[0x10:0x20]):
        raise Error('LwVM medium identity is zero')
    if not 1 <= count <= 8:
        raise Error('LwVM descriptor count is unsupported')
    parts, identities = [], set()
    for index in range(count):
        raw = primary[0x200 + index * 128:0x280 + index * 128]
        kind, identity = uuid.UUID(bytes=raw[:16]), uuid.UUID(bytes=raw[16:32])
        begin, end, flags = struct.unpack_from('<QQQ', raw, 32)
        if not kind.int or not identity.int or str(identity) in identities:
            raise Error('LwVM partition identity is zero or repeated')
        if begin < HEADER_BYTES or begin >= end or end > size or begin % HEADER_BYTES or end % HEADER_BYTES:
            raise Error('LwVM partition range is invalid')
        if any(begin < p['end'] and end > p['begin'] for p in parts):
            raise Error('LwVM virtual partition ranges overlap')
        identities.add(str(identity))
        parts.append({'number': index + 1, 'type': str(kind), 'partuuid': str(identity),
                      'begin': begin, 'end': end, 'bytes': end - begin, 'attributes': flags,
                      'name': raw[56:128].decode('utf-16-le', 'strict').rstrip('\0'),
                      'chunks': {}, 'extents': []})
    words = struct.unpack_from('<1024H', primary, 0x800)
    for physical, word in enumerate(words):
        owner, logical = word >> 12, word & 0xfff
        if owner == 15:
            if not ((physical == 0 and word == 0xf000) or
                    (0 < physical < size // chunk and word == 0xf111) or
                    (physical >= size // chunk and word == 0xf3ff)):
                raise Error('LwVM has an unknown allocation sentinel')
            continue
        if owner >= count or physical == 0 or physical >= size // chunk:
            raise Error('LwVM chunk owner or physical address is invalid')
        owned = parts[owner]['chunks']
        if logical in owned:
            raise Error('LwVM logical chunk is aliased')
        owned[logical] = physical
    for part in parts:
        needed = (part['bytes'] + chunk - 1) // chunk
        if set(part['chunks']) != set(range(needed)):
            raise Error('LwVM partition chunk coverage is incomplete')
        for logical, physical in sorted(part['chunks'].items()):
            length = min(chunk, part['bytes'] - logical * chunk)
            offset = physical * chunk
            if offset + length > size:
                raise Error('LwVM partition extent exceeds the medium')
            extents = part['extents']
            if extents and extents[-1]['physical'] + extents[-1]['bytes'] == offset:
                extents[-1]['bytes'] += length
            else:
                extents.append({'logical': logical * chunk, 'physical': offset, 'bytes': length})
    return {'media_bytes': size, 'chunk_bytes': chunk, 'backup_offset': chunk - HEADER_BYTES,
            'header_sha256': digest(primary), 'partitions': parts}


def partition(snapshot, identity):
    identity = str(uuid.UUID(identity))
    found = [p for p in snapshot['partitions'] if p['partuuid'] == identity]
    if len(found) != 1:
        raise Error('LwVM partition identity was not found')
    return found[0]


FREE_CHUNK = 0xf111


def free_runs(header, snapshot):
    """[(first physical chunk, chunks)] of unallocated chunks stored back to back, longest first.

    Only a run can hold a Linux partition the kernel's LwVM reader adds: it
    takes a partition whose chunks are in order and contiguous.
    """
    words = struct.unpack_from('<1024H', header, 0x800)
    runs, start = [], None
    for physical in range(1, snapshot['media_bytes'] // snapshot['chunk_bytes'] + 1):
        free = physical < snapshot['media_bytes'] // snapshot['chunk_bytes'] and words[physical] == FREE_CHUNK
        if free and start is None:
            start = physical
        elif not free and start is not None:
            runs.append((start, physical - start))
            start = None
    return sorted(runs, key=lambda run: (-run[1], run[0]))


def virtual_room(snapshot):
    """(first byte, bytes) of the LwVM virtual space after every partition, where a new entry's range goes."""
    begin = max([HEADER_BYTES, *(p['end'] for p in snapshot['partitions'])])
    begin = -(-begin // HEADER_BYTES) * HEADER_BYTES
    return begin, snapshot['media_bytes'] - begin


def free_space(header, snapshot):
    """[(first chunk, chunks)] a new Linux entry could take: free runs, cut to the virtual room left."""
    if len(snapshot['partitions']) >= 8:
        return []
    _, room = virtual_room(snapshot)
    most = room // snapshot['chunk_bytes']
    return [(first, min(count, most)) for first, count in free_runs(header, snapshot) if min(count, most) > 0]


def allocate_free(primary, backup, linux_uuid, first, chunks):
    """A new header with a Linux entry over the free chunks FIRST..FIRST+CHUNKS-1, and that entry.

    Nothing else changes: the entry is appended, its virtual range follows
    every other partition's, and the chunks are mapped to it in order. The
    result is parsed again before it is returned.
    """
    before = parse(primary, backup)
    count = len(before['partitions'])
    if count == 8 or any(primary[0x200 + count * 128:0x280 + count * 128]):
        raise Error('No empty LwVM descriptor is available')
    if not any(start <= first and first + chunks <= start + length for start, length in free_space(primary, before)):
        raise Error('The chosen chunks are not free LwVM space')
    identity = uuid.UUID(linux_uuid)
    if not identity.int or str(identity) in {p['partuuid'] for p in before['partitions']}:
        raise Error('Linux partition identity is zero or repeated')
    begin, _ = virtual_room(before)
    end = begin + chunks * before['chunk_bytes']
    header = bytearray(primary)
    struct.pack_into('<I', header, 0x28, count + 1)
    descriptor = bytearray(128)
    descriptor[:16] = uuid.UUID(LINUX_TYPE).bytes
    descriptor[16:32] = identity.bytes
    struct.pack_into('<QQQ', descriptor, 32, begin, end, 0)
    descriptor[56:66] = 'Linux'.encode('utf-16-le')
    header[0x200 + count * 128:0x280 + count * 128] = descriptor
    for logical in range(chunks):
        struct.pack_into('<H', header, 0x800 + (first + logical) * 2, count << 12 | logical)
    struct.pack_into('<I', header, 0x2c, checksum(header))
    after = parse(bytes(header), bytes(header), before['media_bytes'])
    return bytes(header), partition(after, str(identity))
