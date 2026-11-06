#!/usr/bin/env python3
# SPDX-License-Identifier: GPL-2.0-only
"""Assemble an ARM32 iBEC payload with an external initramfs described by its DTB.

The loader occupies the first 32 KiB. Its checked configuration tells the
loader where to copy the initramfs before entering the unchanged zImage.
This module uses only the standard library and also runs by file path.
"""
import argparse
import hashlib
import json
from pathlib import Path
import struct
import sys
import tomllib
import zlib

from . import recovery_identity as identity_protocol


CONFIG_OFFSET = 0x7f00
CONFIG = struct.Struct('<4s16I')
CONFIG_FIELDS = ('magic', 'version', 'header_size', 'load_address', 'kernel_offset',
                 'dtb_offset', 'dtb_size', 'initrd_offset', 'initrd_size',
                 'initrd_address', 'image_size', 'watchdog_address', 'l2_address',
                 'ram_start', 'ram_end', 'initrd_limit', 'crc32')
PROFILE_FIELDS = frozenset(('schema', 'load_address', 'kernel_offset', 'watchdog_address',
                            'l2_address', 'ram_start', 'ram_end', 'initrd_address', 'initrd_limit'))
FDT_HEADER = struct.Struct('>10I')


def digest(data):
    return hashlib.sha256(data).hexdigest()


def aligned(value, size=8):
    return (value + size - 1) & -size


def profile(path):
    return validate_profile(tomllib.loads(Path(path).read_text()))


def validate_profile(result):
    if set(result) != PROFILE_FIELDS or result['schema'] != 1:
        raise ValueError('Expected the schema-1 Cascadia handoff profile')
    if any(type(value) is not int or not 0 <= value <= 0xffffffff for value in result.values()):
        raise ValueError('Handoff addresses must be unsigned 32-bit integers')
    first, end = result['ram_start'], result['ram_end']
    base, target, limit = result['load_address'], result['initrd_address'], result['initrd_limit']
    if (result['kernel_offset'] != 0x8000 or base != first or base % 0x8000000 or
            not first <= base < base + 0x8000 < target < limit <= end or target % 4096):
        raise ValueError('Handoff memory ranges overlap or have invalid alignment')
    if any(result[key] % 4 for key in ('watchdog_address', 'l2_address')):
        raise ValueError('Handoff register addresses must be word aligned')
    return result


def configuration(raw):
    if len(raw) < CONFIG_OFFSET + CONFIG.size:
        raise ValueError('Loader lacks its configuration record')
    result = dict(zip(CONFIG_FIELDS, CONFIG.unpack_from(raw, CONFIG_OFFSET)))
    if (result['magic'], result['version'], result['header_size']) != (b'CB32', 1, CONFIG.size):
        raise ValueError('Unknown loader configuration format')
    return result


def verify_loader(binary, described, elf=None):
    """Reject metadata before ARM code and a loader built for another profile."""
    if (not CONFIG_OFFSET + CONFIG.size <= len(binary) <= 0x8000 or
            binary[:4] != bytes.fromhex('c0000cf1')):
        raise ValueError('Expected the ARM CPSID instruction at loader byte zero')
    config = configuration(binary)
    for field in PROFILE_FIELDS - {'schema'}:
        if config[field] != described[field]:
            raise ValueError('Loader configuration differs from the handoff profile: ' + field)
    if any(config[field] for field in ('dtb_offset', 'dtb_size', 'initrd_offset', 'initrd_size',
                                        'image_size', 'crc32')):
        raise ValueError('Expected a pristine loader before payload assembly')
    if elf is None:
        return
    if len(elf) < 52 or elf[:7] != b'\x7fELF\x01\x01\x01':
        raise ValueError('Expected little-endian ELF32')
    if struct.unpack_from('<HHI', elf, 16) != (2, 40, 1):
        raise ValueError('Expected an ARM executable')
    if struct.unpack_from('<I', elf, 24)[0] != described['load_address']:
        raise ValueError('ELF entry differs from the upload address')
    offset = struct.unpack_from('<I', elf, 32)[0]
    size, count = struct.unpack_from('<HH', elf, 46)
    if size != 40 or not count or offset + size * count > len(elf):
        raise ValueError('Invalid loader section table')
    allocated = []
    for index in range(count):
        _, kind, flags, address, source, length, *_ = struct.unpack_from('<10I', elf, offset + index * size)
        if not flags & 2 or not length:
            continue
        relative = address - described['load_address']
        if (kind == 8 or relative < 0 or source + length > len(elf) or
                relative + length > len(binary) or binary[relative:relative + length] != elf[source:source + length]):
            raise ValueError('ELF allocated section differs from the raw loader')
        if relative != CONFIG_OFFSET and relative + length > 0x6000:
            raise ValueError('ELF section overlaps the loader stack or configuration')
        allocated.append((relative, flags, length))
    if not allocated or min(allocated)[0] != 0 or not min(allocated)[1] & 4:
        raise ValueError('Loader starts with non-executable metadata')
    if not any(start == CONFIG_OFFSET and length == CONFIG.size for start, _, length in allocated):
        raise ValueError('ELF lacks the configuration section')


def _cstring(data, offset, end):
    if not 0 <= offset < end:
        raise ValueError('FDT string offset exceeds its block')
    stop = data.find(b'\0', offset, end)
    if stop < 0:
        raise ValueError('Unterminated FDT string')
    try:
        return data[offset:stop].decode('ascii'), stop + 1
    except UnicodeDecodeError as error:
        raise ValueError('FDT names must be ASCII') from error


def read_fdt(data):
    """Return a checked tree and reservation list, preserving every property."""
    if len(data) < FDT_HEADER.size:
        raise ValueError('Truncated FDT header')
    magic, total, tree_at, strings_at, reserve_at, version, last, cpu, strings_size, tree_size = FDT_HEADER.unpack_from(data)
    if magic != 0xd00dfeed or total != len(data) or version < 17 or last > 17:
        raise ValueError('Unsupported FDT header')
    if (tree_at % 4 or reserve_at % 8 or
            tree_size <= 0 or any(start < 40 or size < 0 or start + size > total
                for start, size in ((tree_at, tree_size), (strings_at, strings_size))) or
            not 40 <= reserve_at <= total - 16):
        raise ValueError('Invalid FDT block bounds')
    intervals = [(tree_at, tree_at + tree_size), (strings_at, strings_at + strings_size)]
    if max(intervals[0][0], intervals[1][0]) < min(intervals[0][1], intervals[1][1]):
        raise ValueError('FDT blocks overlap')
    reservations, at = [], reserve_at
    while True:
        if at + 16 > total or any(max(at, start) < min(at + 16, end) for start, end in intervals):
            raise ValueError('FDT reservation map is truncated or overlaps another block')
        address, length = struct.unpack_from('>QQ', data, at)
        at += 16
        if address == length == 0:
            break
        if not length or address + length > 1 << 64:
            raise ValueError('Invalid FDT reservation')
        reservations.append((address, length))
    names = data[strings_at:strings_at + strings_size]
    at, end, stack, root, finished = tree_at, tree_at + tree_size, [], None, False
    while at < end:
        if at + 4 > end:
            raise ValueError('Truncated FDT token')
        token = struct.unpack_from('>I', data, at)[0]
        at += 4
        if token == 1:
            name, at = _cstring(data, at, end)
            at = aligned(at, 4)
            if at > end or len(stack) >= 128 or '/' in name:
                raise ValueError('Invalid FDT node')
            node = {'name': name, 'properties': {}, 'children': []}
            if stack:
                if not name or any(child['name'] == name for child in stack[-1]['children']):
                    raise ValueError('Duplicate or empty FDT child node')
                stack[-1]['children'].append(node)
            elif root is not None or name:
                raise ValueError('Expected exactly one unnamed FDT root')
            else:
                root = node
            stack.append(node)
        elif token == 2:
            if not stack:
                raise ValueError('Unbalanced FDT end-node')
            stack.pop()
        elif token == 3:
            if not stack or at + 8 > end or stack[-1]['children']:
                raise ValueError('Invalid FDT property position')
            length, name_at = struct.unpack_from('>II', data, at)
            at += 8
            name, _ = _cstring(names, name_at, len(names))
            if not name or name in stack[-1]['properties'] or at + length > end:
                raise ValueError('Invalid or repeated FDT property')
            stack[-1]['properties'][name] = data[at:at + length]
            at = aligned(at + length, 4)
        elif token == 4:
            continue
        elif token == 9:
            if stack or root is None or at != end:
                raise ValueError('FDT ends before its nodes or has trailing tokens')
            finished = True
            break
        else:
            raise ValueError('Unknown FDT token')
    if not finished:
        raise ValueError('FDT lacks its end token')
    return root, reservations, cpu


def check_compatible(data, expected):
    """Bind a board profile to the supplied DTB before payload assembly."""
    tree, _, _ = read_fdt(data)
    value = tree['properties'].get('compatible', b'')
    if not value or not value.endswith(b'\0'):
        raise ValueError('Cascadia DTB has no complete compatible list')
    try:
        observed = set(value[:-1].decode('ascii').split('\0'))
    except UnicodeDecodeError as error:
        raise ValueError('Cascadia DTB compatible is invalid') from error
    if not observed.intersection(expected):
        raise ValueError('Cascadia DTB compatible differs from the selected board')


def write_fdt(root, reservations=(), cpu=0):
    names, offsets, tree = bytearray(), {}, bytearray()
    def integer(value):
        tree.extend(struct.pack('>I', value))
    def node(item):
        integer(1)
        tree.extend(item['name'].encode('ascii') + b'\0')
        tree.extend(b'\0' * (-len(tree) % 4))
        for name, value in item['properties'].items():
            if name not in offsets:
                offsets[name] = len(names)
                names.extend(name.encode('ascii') + b'\0')
            integer(3)
            tree.extend(struct.pack('>II', len(value), offsets[name]))
            tree.extend(value)
            tree.extend(b'\0' * (-len(tree) % 4))
        for child in item['children']:
            node(child)
        integer(2)
    node(root)
    integer(9)
    # A valid tree may have no properties; retain a one-byte strings block.
    if not names:
        names.append(0)
    reserve = b''.join(struct.pack('>QQ', *row) for row in reservations) + b'\0' * 16
    tree_at = 40 + len(reserve)
    strings_at = tree_at + len(tree)
    return (FDT_HEADER.pack(0xd00dfeed, strings_at + len(names), tree_at, strings_at,
                            40, 17, 16, cpu, len(names), len(tree)) + reserve + tree + names)


def chosen(data, bootargs, start, size):
    if not isinstance(bootargs, str) or any(char in bootargs for char in ('\0', '\n', '\r')):
        raise ValueError('Kernel arguments must be one line')
    encoded = bootargs.encode('utf-8')
    if len(encoded) > identity_protocol.MAX_CMDLINE_BYTES:
        raise ValueError('Kernel arguments exceed the ARM command-line bound')
    root, reservations, cpu = read_fdt(data)
    cell_bytes = root['properties'].get('#address-cells', struct.pack('>I', 2))
    if len(cell_bytes) != 4 or (cells := struct.unpack('>I', cell_bytes)[0]) not in (1, 2):
        raise ValueError('Expected one or two root address cells')
    selected = next((item for item in root['children'] if item['name'] == 'chosen'), None)
    if selected is None:
        selected = {'name': 'chosen', 'properties': {}, 'children': []}
        root['children'].append(selected)
    properties = selected['properties']
    width = cells * 4
    previous_start = properties.get('linux,initrd-start', b'')
    previous_end = properties.get('linux,initrd-end', b'')
    if len(previous_start) == len(previous_end) == width:
        old_start, old_end = int.from_bytes(previous_start, 'big'), int.from_bytes(previous_end, 'big')
        reservations = [row for row in reservations if row != (old_start, old_end - old_start)]
    if any(max(start, address) < min(start + size, address + length) for address, length in reservations):
        raise ValueError('Initramfs destination overlaps reserved memory')
    properties['bootargs'] = encoded + b'\0'
    properties['linux,initrd-start'] = start.to_bytes(width, 'big')
    properties['linux,initrd-end'] = (start + size).to_bytes(width, 'big')
    reservations.append((start, size))
    return write_fdt(root, reservations, cpu)


def _policy_compatible(data, policy):
    check_compatible(data, policy['compatibles'])
    tree, _, _ = read_fdt(data)
    raw = tree['properties']['compatible']
    if not raw.endswith(b'\0') or b'\0\0' in raw:
        raise ValueError('Recovery DTB compatible list is incomplete')
    if raw.split(b'\0')[0].decode('ascii') not in policy['compatibles']:
        raise ValueError('First DTB compatible differs from the recovery identity policy')


def decompression_bound(kernel, dtb_size, described):
    """Conservatively bound the v6.12 ARM decompressor's relocated workspace.

    The zImage table describes its inflated image and BSS. The low upload
    address overlaps the output, so reserve room for a relocated compressed
    image as well as DTB growth, stack and malloc space above that output.
    Unknown table formats are refused before a payload is written.
    """
    if len(kernel) < 0x3c or len(kernel) % 8:
        raise ValueError('zImage must end on an eight-byte boundary for its appended DTB')
    if dtb_size > 0x100000:
        raise ValueError('DTB exceeds the ARM decompressor workspace bound')
    if struct.unpack_from('<II', kernel, 0x30) != (0x04030201, 0x45454545):
        raise ValueError('Expected the little-endian zImage size-table header')
    start = struct.unpack_from('<I', kernel, 0x28)[0]
    table_at = struct.unpack_from('<I', kernel, 0x38)[0] - start
    if table_at < 0x3c or table_at + 28 > len(kernel):
        raise ValueError('zImage size table exceeds the kernel')
    count, magic, size_at, bss_size, text_offset, malloc_size, end = struct.unpack_from('<7I', kernel, table_at)
    if count != 6 or magic != 0x5a534c4b or end or size_at < 0x3c or size_at + 4 > len(kernel):
        raise ValueError('Unsupported zImage size table')
    inflated = struct.unpack_from('<I', kernel, size_at)[0]
    if not inflated or text_offset < 0x8000 or malloc_size > 0x100000:
        raise ValueError('Invalid zImage decompression parameters')
    # head.S allows a DTB to grow by 50%, bounded at 1 MiB. Include the
    # decompressor relocation/code/stack alignment with an extra 128 KiB.
    dtb_workspace = min(max(aligned(dtb_size + dtb_size // 2), 0x8000), 0x100000)
    bound = (described['ram_start'] + text_offset + inflated + bss_size +
             aligned(len(kernel) + dtb_workspace + malloc_size + 0x20000, 4096))
    if bound > described['initrd_address']:
        raise ValueError('Kernel decompression workspace overlaps the initramfs destination')
    return bound


def assemble(loader, kernel, dtb, initramfs, described, bootargs, *, elf=None, identity_policy=None):
    validate_profile(described)
    verify_loader(loader, described, elf)
    if (len(kernel) < 0x30 or struct.unpack_from('<I', kernel, 0x24)[0] != 0x016f2818):
        raise ValueError('Expected an ARM zImage')
    start, end = struct.unpack_from('<II', kernel, 0x28)
    if end <= start or end - start != len(kernel):
        raise ValueError('zImage length differs from its header or contains appended bytes')
    if not initramfs or len(initramfs) > described['initrd_limit'] - described['initrd_address']:
        raise ValueError('Initramfs exceeds its reserved destination')
    if identity_policy is not None:
        policy = identity_protocol.validate_policy(identity_policy)
        if identity_protocol.parameters(bootargs):
            raise ValueError('Recovery template already contains identity parameters')
        _policy_compatible(dtb, policy)
        bootargs = bootargs + ' ' + identity_protocol.encode_parameter(identity_protocol.POLICY_PARAMETER, policy)
    tree = chosen(dtb, bootargs, described['initrd_address'], len(initramfs))
    expanded_end = decompression_bound(kernel, len(tree), described)
    kernel_at = described['kernel_offset']
    dtb_at = kernel_at + len(kernel)
    initrd_at = aligned(dtb_at + len(tree))
    image_size = initrd_at + len(initramfs)
    if described['load_address'] + image_size > described['initrd_address']:
        raise ValueError('Uploaded payload overlaps the initramfs destination')
    values = configuration(loader)
    values.update(dtb_offset=dtb_at, dtb_size=len(tree), initrd_offset=initrd_at,
                  initrd_size=len(initramfs), image_size=image_size)
    record = CONFIG.pack(*(values[field] for field in CONFIG_FIELDS))
    values['crc32'] = zlib.crc32(record[:-4])
    framed_loader = bytearray(loader)
    framed_loader[CONFIG_OFFSET:CONFIG_OFFSET + CONFIG.size] = CONFIG.pack(*(values[field] for field in CONFIG_FIELDS))
    components, image = [], bytearray()
    def append(name, payload):
        if payload:
            components.append({'name': name, 'offset': len(image), 'bytes': len(payload), 'sha256': digest(payload)})
            image.extend(payload)
    append('loader', framed_loader)
    append('loader-padding', b'\0' * (kernel_at - len(image)))
    append('kernel', kernel)
    append('kernel-padding', b'\0' * (dtb_at - len(image)))
    append('dtb', tree)
    append('dtb-padding', b'\0' * (initrd_at - len(image)))
    append('initramfs', initramfs)
    return bytes(image), {'schema': 1, 'architecture': 'arm', 'loader_format': 'cascadia',
                         'image_sha256': digest(image), 'initramfs_encoding': 'cascadia-dtb',
                         'components': components, 'load_address': described['load_address'],
                         'kernel_entry': described['load_address'] + kernel_at,
                         'dtb_address': described['load_address'] + dtb_at,
                         'kernel_decompression_bound': expanded_end,
                         'initramfs_address': described['initrd_address'],
                         'input_loader_sha256': digest(loader), 'input_dtb_sha256': digest(dtb)}


def _payload_parts(data):
    """Validate a CB32 bundle before inspecting or binding its identity."""
    values = configuration(data)
    if (values['image_size'] != len(data) or values['kernel_offset'] != 0x8000 or
            data[:4] != bytes.fromhex('c0000cf1') or
            values['crc32'] != zlib.crc32(data[CONFIG_OFFSET:CONFIG_OFFSET + CONFIG.size - 4])):
        raise ValueError('Invalid Cascadia payload configuration')
    kernel_at, dtb_at, dtb_size = (values[key] for key in ('kernel_offset', 'dtb_offset', 'dtb_size'))
    initrd_at, initrd_size = values['initrd_offset'], values['initrd_size']
    if (not kernel_at + 0x3c <= dtb_at < dtb_at + dtb_size <= initrd_at or
            initrd_at != aligned(dtb_at + dtb_size) or initrd_at + initrd_size != len(data) or
            any(data[dtb_at + dtb_size:initrd_at])):
        raise ValueError('Invalid Cascadia payload component ranges')
    described = {'schema': 1, **{key: values[key] for key in PROFILE_FIELDS - {'schema'}}}
    validate_profile(described)
    if values['load_address'] + len(data) > values['initrd_address']:
        raise ValueError('Cascadia payload overlaps its initramfs destination')
    tree, _, _ = read_fdt(data[dtb_at:dtb_at + dtb_size])
    selected = next((child for child in tree['children'] if child['name'] == 'chosen'), None)
    raw = selected['properties'].get('bootargs', b'') if selected else b''
    if not raw.endswith(b'\0') or b'\0' in raw[:-1]:
        raise ValueError('Cascadia payload lacks one complete bootargs value')
    try:
        bootargs = raw[:-1].decode('utf-8')
    except UnicodeError as error:
        raise ValueError('Cascadia bootargs are not UTF-8') from error
    return values, described, bootargs


def host_identity_policy(data):
    """Return the declared host-bound recovery policy of an unbound template."""
    values, _, bootargs = _payload_parts(data)
    if 'orchard.recovery-' not in bootargs:
        return None
    parameters = identity_protocol.parameters(bootargs)
    if identity_protocol.HANDOFF_PARAMETER in parameters:
        raise ValueError('A public recovery template already contains an identity handoff')
    policy = parameters.get(identity_protocol.POLICY_PARAMETER)
    if policy is None:
        raise ValueError('Recovery identity parameters lack a policy')
    start = values['dtb_offset']
    _policy_compatible(data[start:start + values['dtb_size']], policy)
    return policy


def bind_host_identity(data, *, ecid, chip, board, profile, handoff_id, template_sha256):
    """Bind a guarded host USB observation to one complete, revalidated bundle."""
    policy = host_identity_policy(data)
    if policy is None:
        raise ValueError('Payload does not declare host-bound recovery identity')
    if (profile, chip, board) != (policy['profile'], policy['chip'], policy['board']):
        raise ValueError('Selected device differs from the recovery identity policy')
    if template_sha256 != digest(data):
        raise ValueError('Recovery template digest differs before identity binding')
    handoff = identity_protocol.validate_handoff({'schema': 1, 'source': 'host-usb',
        'profile': profile, 'ecid': ecid, 'chip': chip, 'board': board,
        'handoff_id': handoff_id, 'template_sha256': template_sha256})
    values, described, bootargs = _payload_parts(data)
    loader = bytearray(data[:values['kernel_offset']])
    pristine = dict(values)
    for key in ('dtb_offset', 'dtb_size', 'initrd_offset', 'initrd_size', 'image_size', 'crc32'):
        pristine[key] = 0
    loader[CONFIG_OFFSET:CONFIG_OFFSET + CONFIG.size] = CONFIG.pack(*(pristine[key] for key in CONFIG_FIELDS))
    bootargs += ' ' + identity_protocol.encode_parameter(identity_protocol.HANDOFF_PARAMETER, handoff)
    output, _ = assemble(bytes(loader), data[values['kernel_offset']:values['dtb_offset']],
        data[values['dtb_offset']:values['dtb_offset'] + values['dtb_size']],
        data[values['initrd_offset']:], described, bootargs)
    _payload_parts(output)
    return output


def main():
    parser = argparse.ArgumentParser(description=__doc__.split('\n\n')[0])
    given = parser.add_mutually_exclusive_group(required=True)
    given.add_argument('--bootargs')
    given.add_argument('--manifest', type=Path)
    for name in ('loader', 'kernel', 'initramfs', 'profile', 'output'):
        parser.add_argument('--' + name, type=Path, required=True)
    parser.add_argument('--loader-elf', type=Path)
    parser.add_argument('--identity-policy', type=Path, help='descriptor-selected public recovery identity policy')
    parser.add_argument('--dtb', action='append', type=Path, required=True)
    parser.add_argument('--logo', type=Path)
    args = parser.parse_args()
    if len(args.dtb) != 1:
        parser.error('A Cascadia payload takes exactly one device tree')
    if args.logo:
        parser.error('Cascadia payloads do not implement the m1n1 logo format')
    receipt = args.output.with_suffix(args.output.suffix + '.json')
    if args.output.exists() or receipt.exists():
        parser.error('Choose new image and receipt paths')
    try:
        bootargs = args.bootargs
        if args.manifest:
            bootargs = json.loads(args.manifest.read_text())['bootargs']
            prefix = 'chosen.bootargs='
            if not bootargs.startswith(prefix) or not bootargs.endswith('\n'):
                raise ValueError('Invalid boot-argument manifest')
            bootargs = bootargs[len(prefix):-1]
        data, document = assemble(args.loader.read_bytes(), args.kernel.read_bytes(), args.dtb[0].read_bytes(),
                                  args.initramfs.read_bytes(), profile(args.profile), bootargs,
                                  elf=args.loader_elf.read_bytes() if args.loader_elf else None,
                                  identity_policy=identity_protocol.validate_policy(identity_protocol.object_json(args.identity_policy.read_bytes()))
                                  if args.identity_policy else None)
    except (ValueError, KeyError) as error:
        parser.error(str(error))
    document['profile_sha256'] = digest(args.profile.read_bytes())
    args.output.parent.mkdir(parents=True, exist_ok=True)
    with args.output.open('xb') as output:
        output.write(data)
    with receipt.open('x') as output:
        json.dump(document, output, indent=2, sort_keys=True)
        output.write('\n')
    print(f'Built {args.output}; component receipt: {receipt}')


if __name__ == '__main__':
    main()
