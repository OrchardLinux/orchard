# SPDX-License-Identifier: GPL-2.0-only
"""Prepare a fixed-size on-device identity slot without altering Linux inputs."""
import hashlib
import struct

from orchard import recovery_identity
from orchard import cascadia_payload as cascadia


def property_span(data, wanted_path, wanted_name):
    """Locate a property in an already validated FDT, excluding its padding."""
    cascadia.read_fdt(data)
    _, _, tree_at, strings_at, _, _, _, _, strings_size, tree_size = cascadia.FDT_HEADER.unpack_from(data)
    names = data[strings_at:strings_at + strings_size]
    at, end, stack, matches = tree_at, tree_at + tree_size, [], []
    while at < end:
        token = struct.unpack_from('>I', data, at)[0]
        at += 4
        if token == 1:
            name, at = cascadia._cstring(data, at, end)
            at = cascadia.aligned(at, 4)
            stack.append(name)
        elif token == 2:
            stack.pop()
        elif token == 3:
            length, name_at = struct.unpack_from('>II', data, at)
            at += 8
            name, _ = cascadia._cstring(names, name_at, len(names))
            if tuple(stack) == wanted_path and name == wanted_name:
                matches.append((at, length))
            at = cascadia.aligned(at + length, 4)
        elif token == 9:
            break
    if len(matches) != 1:
        raise ValueError('Expected one fixed-slot FDT property')
    return matches[0]


def prepare_template(payload, profile):
    """Retain a public input hash and create one deterministic launch template."""
    if payload[cascadia.CONFIG_OFFSET:cascadia.CONFIG_OFFSET + 4] != b'CB32':
        return payload, None  # Owner-supplied pre-CB32 standalone boot images.
    policy = cascadia.host_identity_policy(payload)
    if policy is None:
        return payload, None
    if not recovery_identity.allows_source(policy, 'ios-device'):
        raise ValueError('This public recovery does not allow an iOS device identity')
    if (policy['profile'], policy['chip'], policy['board']) != (
            profile['profile'], profile['chip'], profile['board']):
        raise ValueError('Recovery identity policy differs from the iOS boot recipe')
    values = cascadia.configuration(payload)
    dtb = payload[values['dtb_offset']:values['dtb_offset'] + values['dtb_size']]
    root, _, _ = cascadia.read_fdt(dtb)
    chosen = next(child for child in root['children'] if child['name'] == 'chosen')
    original = chosen['properties']['bootargs'][:-1].decode('ascii')
    largest = recovery_identity.encode_parameter(recovery_identity.HANDOFF_PARAMETER,
        {'schema': 1, 'source': 'ios-device', 'profile': profile['profile'],
         'ecid': (1 << 64) - 1, 'chip': profile['chip'], 'board': profile['board'],
         'handoff_id': 'f' * 32, 'template_sha256': 'f' * 64})
    slot_bytes = len(largest)
    bootargs = original + ' ' + ' ' * slot_bytes
    if len(bootargs.encode('ascii')) > recovery_identity.MAX_CMDLINE_BYTES:
        raise ValueError('The iOS identity slot exceeds the ARM command-line bound')
    loader = bytearray(payload[:values['kernel_offset']])
    pristine = dict(values)
    for key in ('dtb_offset', 'dtb_size', 'initrd_offset', 'initrd_size', 'image_size', 'crc32'):
        pristine[key] = 0
    loader[cascadia.CONFIG_OFFSET:cascadia.CONFIG_OFFSET + cascadia.CONFIG.size] = cascadia.CONFIG.pack(
        *(pristine[key] for key in cascadia.CONFIG_FIELDS))
    described = {'schema': 1, **{key: values[key] for key in cascadia.PROFILE_FIELDS - {'schema'}}}
    output, _ = cascadia.assemble(bytes(loader), payload[values['kernel_offset']:values['dtb_offset']],
        dtb, payload[values['initrd_offset']:], described, bootargs)
    framed = cascadia.configuration(output)
    tree = output[framed['dtb_offset']:framed['dtb_offset'] + framed['dtb_size']]
    value_at, value_bytes = property_span(tree, ('', 'chosen'), 'bootargs')
    slot_at = framed['dtb_offset'] + value_at + len(original) + 1
    if value_bytes != len(bootargs) + 1 or output[slot_at:slot_at + slot_bytes] != b' ' * slot_bytes:
        raise ValueError('The prepared identity slot differs from the checked FDT')
    return output, {'schema': 1, 'identity_source': 'ios-device',
                    'public_input_sha256': hashlib.sha256(payload).hexdigest(),
                    'template_sha256': hashlib.sha256(output).hexdigest(),
                    'template_bytes': len(output), 'slot_offset': slot_at, 'slot_bytes': slot_bytes,
                    'profile': profile['profile'], 'chip': profile['chip'], 'board': profile['board']}


def elf_word_offset(data, name, base, length):
    """Find the generated mutable CRC word inside the checked shim ELF."""
    if len(data) < 52 or data[:7] != b'\x7fELF\x01\x01\x01' or struct.unpack_from('<H', data, 18)[0] != 40:
        raise ValueError('Expected an ARM32 shim ELF')
    at = struct.unpack_from('<I', data, 32)[0]
    size, count = struct.unpack_from('<HH', data, 46)
    if size != 40 or not count or at + count * size > len(data):
        raise ValueError('Invalid shim ELF section table')
    sections = [struct.unpack_from('<10I', data, at + index * size) for index in range(count)]
    found = []
    for section in sections:
        if section[1] != 2:
            continue
        offset, size, link, entry = section[4], section[5], section[6], section[9]
        if entry != 16 or link >= count or size % entry or offset + size > len(data):
            raise ValueError('Invalid shim ELF symbols')
        strings = sections[link]
        if strings[1] != 3 or strings[4] + strings[5] > len(data):
            raise ValueError('Invalid shim ELF symbol strings')
        names = data[strings[4]:strings[4] + strings[5]]
        for cursor in range(offset, offset + size, entry):
            named, address, bytes_, info, _, index = struct.unpack_from('<IIIBBH', data, cursor)
            if named >= len(names) or names.find(b'\0', named) < 0:
                raise ValueError('Invalid shim ELF symbol name')
            if names[named:names.find(b'\0', named)] == name.encode('ascii'):
                if bytes_ != 4 or info & 15 != 1 or not 0 < index < count or address % 4:
                    raise ValueError('The shim CRC symbol is not one word')
                found.append(address - base)
    if len(found) != 1 or not 0 <= found[0] <= length - 4:
        raise ValueError('The shim CRC word exceeds its checked padding')
    return found[0]
