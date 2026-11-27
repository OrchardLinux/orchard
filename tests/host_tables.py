"""Partition tables as a host plans them, for the tests of what runs on the device: a GPT with
the plan's partitions, and synthetic LwVM headers (32-bit boards)."""
import struct
import uuid
import zlib

from install_files import install_device as D
import lwvm

XBOOTLDR, ROOT_ARM64 = D.XBOOTLDR, D.ROOT_ARM64
ENTRY_SIZE = 128


def entry_bytes(kind, partuuid, start, blocks, name):
    return (uuid.UUID(kind).bytes_le + uuid.UUID(partuuid).bytes_le +
            struct.pack('<QQQ', start, start + blocks - 1, 0) + name.encode('utf-16-le')[:72].ljust(72, b'\0'))


def new_tables(plan, block, header, entries):
    """The plan's partitions in this GPT: [(lba, bytes)] of both copies, the backup first, and their numbers."""
    size = struct.unpack_from('<I', header, 12)[0]
    current, alternate = struct.unpack_from('<QQ', header, 24)
    entries_lba, count, entry_size = struct.unpack_from('<QII', header, 72)
    if entry_size != ENTRY_SIZE:
        raise D.InstallError('gpt', f'Unsupported GPT entry size {entry_size}')
    table = bytearray(entries)
    _, partitions, _ = D.parse_gpt('disk', block, header, entries)
    removed = plan.get('remove')
    if removed:
        table[(removed['number'] - 1) * entry_size:removed['number'] * entry_size] = bytes(entry_size)
    keep = [p for p in partitions if not removed or p['number'] != removed['number']]
    numbers = D.free_numbers(keep, 2)
    if max(numbers) > count:
        raise D.InstallError('gpt', 'The partition table is full')
    created = {}
    for spec, kind, name in ((plan['boot'], XBOOTLDR, 'boot'), (plan['root'], ROOT_ARM64, plan['root']['label'])):
        if spec is plan['boot'] and not spec['create']:
            continue
        number = numbers.pop(0)
        table[(number - 1) * entry_size:number * entry_size] = entry_bytes(kind, spec['partuuid'], spec['start'],
                                                                           spec['blocks'], name)
        created[spec['partuuid']] = number
    table = bytes(table)

    def sealed(this, other, where):
        raw = bytearray(header[:size])
        struct.pack_into('<QQ', raw, 24, this, other)
        struct.pack_into('<Q', raw, 72, where)
        struct.pack_into('<I', raw, 88, zlib.crc32(table))
        struct.pack_into('<I', raw, 16, 0)
        struct.pack_into('<I', raw, 16, zlib.crc32(raw))
        return bytes(raw).ljust(block, b'\0')
    last_usable = struct.unpack_from('<Q', header, 48)[0]
    return [(last_usable + 1, table), (alternate, sealed(alternate, current, last_usable + 1)),
            (entries_lba, table), (current, sealed(current, alternate, entries_lba))], created


MEDIA = 64_000_000_000            # the 64 GB model's disk
CHUNK = lwvm.chunk_size(MEDIA)    # 64 MiB
HFS = uuid.UUID(lwvm.HFS_TYPE).bytes
LINUX = uuid.UUID(lwvm.LINUX_TYPE).bytes
SYSTEM_ID, DATA_ID, LINUX_ID = ('11111111-2222-4333-8444-555555555555', '22222222-3333-4444-8555-666666666666',
                                '33333333-4444-4555-8666-777777777777')
# (name, type, GUID, attributes, physical chunks in logical order)
PARTS = (('System', HFS, SYSTEM_ID, 0, list(range(1, 21))),
         ('Data', HFS, DATA_ID, 0x3000000000000, list(range(21, 801))),
         ('Linux', LINUX, LINUX_ID, 0, list(range(801, 953))))


def header(parts=PARTS):
    """An LwVM header for PARTS, chunks given per partition in logical order."""
    h = bytearray(lwvm.HEADER_BYTES)
    h[:16] = lwvm.MAGIC
    h[16:32] = uuid.UUID('aaaaaaaa-bbbb-4ccc-8ddd-eeeeeeeeeeee').bytes
    struct.pack_into('<QI', h, 0x20, MEDIA, len(parts))
    begin = lwvm.HEADER_BYTES
    for index in range(1024):
        struct.pack_into('<H', h, 0x800 + index * 2, 0xf000 if index == 0 else 0xf111 if index < MEDIA // CHUNK else 0xf3ff)
    for index, (name, kind, guid, flags, chunks) in enumerate(parts):
        at = 0x200 + index * 0x80
        size = len(chunks) * CHUNK
        h[at:at + 16] = kind
        h[at + 16:at + 32] = uuid.UUID(guid).bytes
        struct.pack_into('<QQQ', h, at + 32, begin, begin + size, flags)
        h[at + 56:at + 56 + 2 * len(name)] = name.encode('utf-16-le')
        for logical, physical in enumerate(chunks):
            struct.pack_into('<H', h, 0x800 + physical * 2, index << 12 | logical)
        begin += size
    struct.pack_into('<I', h, 0x2c, 0)
    struct.pack_into('<I', h, 0x2c, zlib.crc32(h))
    return bytes(h)


def kernel_view(parts=PARTS):
    """What the kernel's LwVM reader adds: contiguous partitions only, numbered as their entries."""
    return {index + 1: (chunks[0] * CHUNK // 512, len(chunks) * CHUNK // 512)
            for index, (_, _, _, _, chunks) in enumerate(parts) if chunks == list(range(chunks[0], chunks[0] + len(chunks)))}


# Data's entry shrunk to end at chunk 600, as the resize tool will leave it: chunks 601..952 are free.
SHRUNK = (PARTS[0], ('Data', HFS, DATA_ID, 0x3000000000000, list(range(21, 601))))
