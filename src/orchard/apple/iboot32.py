# SPDX-License-Identifier: GPL-2.0-only
"""Private IMG3 preparation for a descriptor-pinned ARM32 recovery stage.

The plaintext packer follows Project Cascadia's img3pack.py. The owner supplies
its already patched raw image; both that image and Apple's original IMG3
container must match the device boot recipe before any output is created.
"""
import hashlib
import json
import os
from pathlib import Path
import re
import struct

from orchard import hardware
from orchard.cache import cache_root
from orchard.integrity import digest

HEADER = struct.Struct('<4sIII4s')
TAG = struct.Struct('<4sII')


def tags(blob):
    if len(blob) < HEADER.size:
        raise ValueError('Truncated IMG3 header')
    magic, full, unpacked, signature, ident = HEADER.unpack_from(blob)
    if magic != b'3gmI' or full != len(blob) or unpacked != full - HEADER.size or signature > unpacked:
        raise ValueError('IMG3 header does not describe the complete file')
    entries, at = [], HEADER.size
    while at < len(blob):
        if at + TAG.size > len(blob):
            raise ValueError('Truncated IMG3 tag')
        name, length, size = TAG.unpack_from(blob, at)
        if length < TAG.size or size > length - TAG.size or at + length > len(blob):
            raise ValueError('IMG3 tag exceeds its container')
        entries.append((name, blob[at + TAG.size:at + TAG.size + size], blob[at:at + length]))
        at += length
    if sum(name == b'ATAD' for name, _, _ in entries) != 1:
        raise ValueError('IMG3 must contain exactly one DATA tag')
    return ident, entries


def pack_plain(template, raw):
    """Replace DATA, remove KBAG and clear sigCheckArea, preserving other tags."""
    ident, entries = tags(template)
    data = bytearray()
    for name, _, original in entries:
        if name == b'GABK':
            continue
        if name == b'ATAD':
            padded = raw + bytes(-len(raw) % 16)
            data.extend(TAG.pack(name, TAG.size + len(padded), len(raw)) + padded)
        else:
            data.extend(original)
    result = HEADER.pack(b'3gmI', HEADER.size + len(data), len(data), 0, ident) + data
    _, checked = tags(result)
    if any(name == b'GABK' for name, _, _ in checked):
        raise ValueError('Plaintext IMG3 retains an encryption tag')
    if next(payload for name, payload, _ in checked if name == b'ATAD') != raw:
        raise ValueError('Plaintext IMG3 payload differs')
    return result


def thumb_branch(source, target):
    delta = target - (source + 4)
    if delta % 2 or not -(1 << 24) <= delta < 1 << 24:
        raise ValueError('Thumb branch exceeds its aligned range')
    value = delta & ((1 << 25) - 1)
    sign, i1, i2 = value >> 24, value >> 23 & 1, value >> 22 & 1
    return struct.pack('<HH', 0xf000 | sign << 10 | value >> 12 & 0x3ff,
                       0x9000 | ((~(i1 ^ sign)) & 1) << 13 | ((~(i2 ^ sign)) & 1) << 11 | value >> 1 & 0x7ff)


def recipe(profile):
    document = hardware.boot_recipe(profile)
    selected = document.get('methods', {}).get('recovery-autogo')
    if not isinstance(selected, dict):
        raise ValueError('This board has no recovery-autogo recipe')
    build = selected.get('build')
    candidates = [row for row in document.get('firmware', []) if row.get('ipsw', {}).get('build') == build]
    if len(candidates) != 1 or 'iBEC' not in candidates[0].get('inputs', {}):
        raise ValueError('Recovery method lacks one exact firmware input')
    selected = dict(selected, device=document['device'], template_sha256=candidates[0]['inputs']['iBEC']['sha256'])
    prepared = selected.get('prepared_ibec', {})
    if (not isinstance(prepared, dict) or type(prepared.get('bytes')) is not int or prepared['bytes'] <= 0
            or not re.fullmatch('[0-9a-f]{64}', prepared.get('sha256', ''))):
        raise ValueError('Recovery method needs a pinned prepared plaintext iBEC')
    for field in ('image_base', 'autogo_hook', 'autogo_trampoline', 'payload_address'):
        if not isinstance(selected.get(field), str) or not re.fullmatch('0x[0-9a-f]+', selected[field]):
            raise ValueError('Recovery recipe needs a checked address: ' + field)
        selected[field] = int(selected[field], 16)
    if type(selected.get('maximum_payload_bytes')) is not int or not 0x8000 < selected['maximum_payload_bytes'] <= 64 * (1 << 20):
        raise ValueError('Recovery method needs a bounded payload size')
    return selected


def verify_raw(raw, selected):
    expected = selected['prepared_ibec']
    if len(raw) != expected['bytes'] or hashlib.sha256(raw).hexdigest() != expected['sha256']:
        raise ValueError('Prepared plaintext iBEC differs from the recipe')
    base, hook, target = (selected[key] for key in ('image_base', 'autogo_hook', 'autogo_trampoline'))
    if not 0 <= hook <= len(raw) - 4 or not 0 <= target < len(raw):
        raise ValueError('Autogo hook exceeds the prepared image')
    if raw[hook:hook + 4] != thumb_branch(base + hook, base + target):
        raise ValueError('Prepared iBEC lacks the checked autogo hook')
    if len(raw) < 0x24 or struct.unpack_from('<I', raw, 0x20)[0] & ~0xfffff != base:
        raise ValueError('Prepared iBEC base differs from the recipe')


def directory(profile, selected=None):
    selected = recipe(profile) if selected is None else selected
    return cache_root() / 'boot' / profile / 'recovery-autogo' / selected['build']


def prepare(profile, template, raw, output):
    selected = recipe(profile)
    template, raw, output = Path(template), Path(raw), Path(output)
    if template.is_symlink() or raw.is_symlink() or not template.is_file() or not raw.is_file():
        raise ValueError('Firmware inputs must be regular files')
    template_bytes = template.read_bytes()
    if hashlib.sha256(template_bytes).hexdigest() != selected['template_sha256']:
        raise ValueError('Original IMG3 template differs from the firmware recipe')
    plaintext = raw.read_bytes()
    verify_raw(plaintext, selected)
    result = pack_plain(template_bytes, plaintext)
    if output.exists() or output.is_symlink():
        raise ValueError('Choose a new private prepared-boot directory')
    output.mkdir(parents=True, mode=0o700)
    stage = output / 'ibec.img3'
    with stage.open('xb') as stream:
        stream.write(result)
    stage.chmod(0o600)
    report = {'schema': 1, 'kind': 'recovery-autogo-inputs', 'profile': profile,
              'device': selected['device'], 'build': selected['build'], 'format': 'img3-plaintext',
              'source_template_sha256': selected['template_sha256'],
              'source_raw_sha256': selected['prepared_ibec']['sha256'],
              'ibec': {'file': 'ibec.img3', 'bytes': len(result), 'sha256': hashlib.sha256(result).hexdigest()},
              'device_operations': 0, 'on_device_qualified': False}
    (output / 'prepared.json').write_text(json.dumps(report, indent=2) + '\n')
    (output / 'prepared.json').chmod(0o600)
    return report


def verified(profile, root=None):
    selected = recipe(profile)
    root = directory(profile, selected) if root is None else Path(root)
    if (root.is_symlink() or not root.is_dir() or root.stat().st_uid != os.getuid()
            or root.stat().st_mode & 0o077):
        raise ValueError('Prepared-boot directory must be private and owned by this user')
    path, receipt = root / 'ibec.img3', root / 'prepared.json'
    if any(item.is_symlink() or not item.is_file() or item.stat().st_uid != os.getuid()
           or item.stat().st_mode & 0o077 for item in (path, receipt)):
        raise ValueError('Prepare the board\'s private recovery-autogo inputs before booting')
    record = json.loads(receipt.read_text())
    row = record.get('ibec', {})
    if (record.get('schema') != 1 or record.get('kind') != 'recovery-autogo-inputs'
            or record.get('profile') != profile or record.get('device') != selected['device']
            or record.get('build') != selected['build'] or record.get('format') != 'img3-plaintext'
            or record.get('source_template_sha256') != selected['template_sha256']
            or record.get('source_raw_sha256') != selected['prepared_ibec']['sha256']
            or row.get('file') != path.name or row.get('bytes') != path.stat().st_size
            or row.get('sha256') != digest(path)):
        raise ValueError('Prepared recovery stage differs from its checked receipt')
    blob = path.read_bytes()
    _, entries = tags(blob)
    if HEADER.unpack_from(blob)[3] or any(name == b'GABK' for name, _, _ in entries):
        raise ValueError('Prepared recovery stage must be plaintext')
    verify_raw(next(payload for name, payload, _ in entries if name == b'ATAD'), selected)
    return path, selected, record
