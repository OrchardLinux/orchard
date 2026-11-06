"""Checked LwVM snapshots and allocation plans, shared by host and recovery.

All offsets are bytes. UUIDs in LwVM descriptors use network byte order.
Planning operates on saved bytes; device access lives in install_lwvm.py.
"""
import base64
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
    if not shifted or shifted > 0xffffffff or chunk < HEADER_BYTES or media_bytes // chunk < 2:
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


def hfs_header(raw, *, unmounted=True):
    if len(raw) != 512 or (raw[:4] not in (b'H+\0\4', b'HX\0\5')):
        raise Error('Expected an HFS Plus or HFSX volume header')
    attributes = struct.unpack_from('>I', raw, 4)[0]
    block, total, free = struct.unpack_from('>III', raw, 40)
    if block < 512 or block & (block - 1) or not total or free > total:
        raise Error('HFS allocation geometry is invalid')
    if attributes & 0x800 or (unmounted and not attributes & 0x100):
        raise Error('HFS must be cleanly unmounted before allocation')
    return {'filesystem_bytes': block * total, 'volume_id': raw[104:112].hex()}


def check_proof(snapshot, proof):
    if proof.get('schema') != 1 or proof.get('kind') != 'orchard.hfs-shrink-proof':
        raise Error('A saved HFS shrink proof is required')
    if proof.get('header_sha256') != snapshot['header_sha256']:
        raise Error('HFS proof belongs to a different LwVM snapshot')
    data = partition(snapshot, proof.get('data_uuid', ''))
    if data['type'] != HFS_TYPE or data['name'] != 'Data' or sum(p['name'] == 'Data' for p in snapshot['partitions']) != 1:
        raise Error('HFS shrink proof must identify the unique Apple HFS Data partition')
    device = proof.get('device', {})
    if not isinstance(device.get('profile'), str) or not device['profile'] or type(device.get('ecid')) is not int or not 0 < device['ecid'] < 2**64:
        raise Error('Native HFS proof needs the device profile and ECID')
    fsck = proof.get('fsck', {})
    if fsck.get('unmounted') is not True or type(fsck.get('exit_code')) is not int or fsck['exit_code'] != 0:
        raise Error('HFS shrink needs a successful unmounted filesystem check')
    for name in ('primary', 'alternate'):
        try:
            native = base64.b64decode(proof['native_hfs'][name], validate=True)
        except (KeyError, ValueError, TypeError) as error:
            raise Error('HFS shrink proof is missing a saved native header') from error
        parsed = hfs_header(native)
        if parsed != {key: proof.get(key) for key in ('filesystem_bytes', 'volume_id')}:
            raise Error('Native HFS header differs from the shrink proof')
    try:
        confirmation = base64.b64decode(proof['native_hfs']['confirmation'], validate=True)
    except (KeyError, ValueError, TypeError) as error:
        raise Error('HFS shrink proof needs a confirming native size and identity read') from error
    if hfs_header(confirmation, unmounted=False) != {key: proof.get(key) for key in ('filesystem_bytes', 'volume_id')}:
        raise Error('Native HFS confirmation differs from the checked shrink')
    if int(proof['volume_id'], 16) == 0:
        raise Error('Native HFS volume identity is zero')
    size = proof.get('filesystem_bytes')
    if type(size) is not int or size < 4096 or size % HEADER_BYTES or size >= data['bytes']:
        raise Error('HFS proof leaves no aligned allocation tail')
    retained = -(-size // snapshot['chunk_bytes']) * snapshot['chunk_bytes']
    if retained >= data['bytes']:
        raise Error('HFS proof leaves no complete allocation chunk')
    return data, retained


def shrink_proof(primary, backup, data_uuid, native_primary, native_alternate, *, native_confirmation, device, fsck):
    """Record the final native unmounted preparation, without decrypting Data.

    The caller supplies the native reader/checker's result. This function cannot
    attest that an external filesystem check ran. Recovery checks its device
    binding and LwVM snapshot; encrypted Data has no live plaintext HFS view.
    """
    snapshot = parse(primary, backup)
    proof = {'schema': 1, 'kind': 'orchard.hfs-shrink-proof',
             'header_sha256': snapshot['header_sha256'], 'data_uuid': str(uuid.UUID(data_uuid)),
             'device': device, 'fsck': fsck, **hfs_header(native_primary),
             'native_hfs': {'primary': base64.b64encode(native_primary).decode('ascii'),
                            'alternate': base64.b64encode(native_alternate).decode('ascii'),
                            'confirmation': base64.b64encode(native_confirmation).decode('ascii')}}
    check_proof(snapshot, proof)
    return proof


def allocate(primary, backup, proof, linux_uuid):
    before = parse(primary, backup)
    if any(p['type'] == LINUX_TYPE for p in before['partitions']):
        raise Error('An existing Linux partition must be selected explicitly')
    data, retained = check_proof(before, proof)
    count = len(before['partitions'])
    if count == 8 or any(primary[0x200 + count * 128:0x280 + count * 128]):
        raise Error('No empty LwVM descriptor is available')
    identity = uuid.UUID(linux_uuid)
    if not identity.int or str(identity) in {p['partuuid'] for p in before['partitions']}:
        raise Error('Linux partition identity is zero or repeated')
    header = bytearray(primary)
    struct.pack_into('<I', header, 0x28, count + 1)
    struct.pack_into('<Q', header, 0x200 + (data['number'] - 1) * 128 + 40, data['begin'] + retained)
    descriptor = bytearray(128)
    descriptor[:16] = uuid.UUID(LINUX_TYPE).bytes
    descriptor[16:32] = identity.bytes
    struct.pack_into('<QQQ', descriptor, 32, data['begin'] + retained, data['end'], 0)
    descriptor[56:66] = 'Linux'.encode('utf-16-le')
    header[0x200 + count * 128:0x280 + count * 128] = descriptor
    first = retained // before['chunk_bytes']
    for logical, physical in data['chunks'].items():
        if logical >= first:
            struct.pack_into('<H', header, 0x800 + physical * 2, count << 12 | logical - first)
    struct.pack_into('<I', header, 0x2c, checksum(header))
    after = parse(bytes(header), bytes(header), before['media_bytes'])
    return bytes(header), partition(after, str(identity))


def b64(header):
    return base64.b64encode(header).decode('ascii')


def public_partition(part):
    return {key: value for key, value in part.items() if key != 'chunks'}


def install_io_policy(root_bytes):
    """Bound the logical provider's lifetime and explicit read callbacks.

    This is allocation-size-based headroom, not a prediction of NAND work.
    Exhaustion stops the provider; it never triggers a retry.
    """
    return {'max_page_reads': min(0xffffffff, 131072 + 8 * -(-root_bytes // 4096)),
            'max_io_time_ms': 3600000}


def unb64(value):
    try:
        raw = base64.b64decode(value, validate=True)
    except (ValueError, TypeError) as error:
        raise Error('Invalid saved LwVM header') from error
    if len(raw) != HEADER_BYTES:
        raise Error('Saved LwVM header must contain 4096 bytes')
    return raw


def allocation_plan(primary, backup, proof, linux_uuid):
    proposed, root = allocate(primary, backup, proof, linux_uuid)
    return {'action': 'allocate', 'before': b64(primary), 'after': b64(proposed),
            'before_sha256': digest(primary), 'after_sha256': digest(proposed),
            'shrink_proof': proof, 'root': public_partition(root)}


def validate_plan(plan, primary, backup, media_bytes):
    snapshot = parse(primary, backup, media_bytes)
    if primary != unb64(plan['before']) or snapshot['header_sha256'] != plan['before_sha256']:
        raise Error('Live LwVM pair differs from the saved plan')
    if plan['action'] == 'allocate':
        after, root = allocate(primary, backup, plan['shrink_proof'], plan['root']['partuuid'])
    elif plan['action'] == 'replace':
        after = primary
        root = partition(snapshot, plan['root']['partuuid'])
        if root['type'] != LINUX_TYPE or root['attributes']:
            raise Error('Only an unencrypted Linux partition can be replaced')
    else:
        raise Error('Unknown LwVM install action')
    if after != unb64(plan['after']) or digest(after) != plan['after_sha256'] or public_partition(root) != plan['root']:
        raise Error('LwVM plan fields differ from the checked allocation')
    return snapshot, after, root


def saved_file(path, size=None):
    """Bounded saved-file input; a device or linked path is refused."""
    import os
    import stat
    fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK | os.O_CLOEXEC)
    with os.fdopen(fd, 'rb') as source:
        info = os.fstat(source.fileno())
        if not stat.S_ISREG(info.st_mode) or info.st_size > 65536 or (size is not None and info.st_size != size):
            raise Error('Expected a bounded saved regular file')
        data = source.read(65537)
        after = os.fstat(source.fileno())
        if len(data) != info.st_size or (info.st_size, info.st_mtime_ns, info.st_ctime_ns) != (after.st_size, after.st_mtime_ns, after.st_ctime_ns):
            raise Error('Saved input changed while reading')
    return data


def main():
    """Produce owner-private proof and allocation documents from saved files."""
    import argparse
    import json
    import os
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest='action', required=True)
    proof = commands.add_parser('proof', help='record native HFS shrink/check and later size confirmation')
    for name in ('native-primary', 'native-alternate', 'native-confirmation', 'fsck-report', 'data-uuid', 'profile', 'ecid'):
        proof.add_argument('--' + name, required=True)
    allocation = commands.add_parser('plan', help='plan a saved LwVM allocation without device access')
    allocation.add_argument('--proof', required=True)
    allocation.add_argument('--linux-uuid', required=True)
    for command in (proof, allocation):
        for name in ('primary', 'backup', 'output'):
            command.add_argument('--' + name, required=True)
    args = parser.parse_args()
    primary, backup = (saved_file(getattr(args, name), HEADER_BYTES) for name in ('primary', 'backup'))
    if args.action == 'proof':
        result = shrink_proof(primary, backup, args.data_uuid, saved_file(args.native_primary, 512),
                              saved_file(args.native_alternate, 512), native_confirmation=saved_file(args.native_confirmation, 512),
                              device={'profile': args.profile, 'ecid': int(args.ecid, 0)},
                              fsck=json.loads(saved_file(args.fsck_report)))
    else:
        result = allocation_plan(primary, backup, json.loads(saved_file(args.proof)), args.linux_uuid)
    fd = os.open(args.output, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW | os.O_CLOEXEC, 0o600)
    with os.fdopen(fd, 'w') as output:
        json.dump(result, output, indent=2)
        output.write('\n')
        output.flush()
        os.fsync(output.fileno())


if __name__ == '__main__':
    main()
