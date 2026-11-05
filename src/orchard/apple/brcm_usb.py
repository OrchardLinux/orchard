# SPDX-License-Identifier: GPL-2.0-or-later
"""BCM43241 B5 USB firmware conversion using private J72 board inputs.

TRX/NVRAM layout follows Project Cascadia scripts/trx-add-nvram.py at
c20b85ebb951c704d7e3a664c378dde8276e3928 (rzxvx/Project-Cascadia).
The strict packer is the J72 device-tested implementation from
thisiscamk/ipad-air-j72-linux, code/wifi/usb-firmware/j72_pack_wifi_trx.py.
No firmware, board settings or reference MAC is included.
"""
import hashlib
import os
from pathlib import Path
import re
import stat
import struct
import zlib

HEADER = 28
RAM = 0x90000


def crc(image):
    return zlib.crc32(image[12:]) ^ 0xffffffff


def nvram(text, mac):
    if len(mac) != 6 or mac[0] & 1 or mac == bytes(6) or mac == b'\xff' * 6:
        raise ValueError('A real unicast six-byte owner MAC is required')
    text.encode('ascii', errors='strict')
    if '\0' in text:
        raise ValueError('NVRAM input contains NUL')
    values = {}
    for raw in text.splitlines():
        line = raw.split('#', 1)[0].strip()
        if not line:
            continue
        if '=' not in line:
            raise ValueError('NVRAM line lacks a value')
        key, value = (part.strip() for part in line.split('=', 1))
        if not re.fullmatch(r'[A-Za-z0-9_.:/-]+', key) or key in values:
            raise ValueError('Invalid or duplicate NVRAM key')
        if any(ord(c) < 32 or ord(c) > 126 for c in value):
            raise ValueError('NVRAM value contains a control character')
        values[key] = value
    if not values:
        raise ValueError('Empty board NVRAM')
    address = ':'.join(f'{v:02x}' for v in mac)
    if 'macaddr' in values and values['macaddr'].lower() != address:
        raise ValueError('Board MAC differs from owner MAC')
    values['macaddr'] = address
    body = ('\0'.join(f'{key}={value}' for key, value in values.items()) + '\0\0').encode('ascii')
    body = body.ljust((len(body) + 3) & ~3, b'\0')
    words = len(body) // 4
    if words > 0xffff:
        raise ValueError('NVRAM exceeds token range')
    return body + struct.pack('<I', words | ((~words & 0xffff) << 16))


def pack(firmware, text, mac):
    if not HEADER <= len(firmware) <= RAM:
        raise ValueError('Firmware length outside ROM RAM bound')
    magic, length, checksum, flags, fwlen, jump, nvlen = struct.unpack_from('<7I', firmware)
    if magic != 0x30524448 or length != len(firmware) or flags != 0x10020:
        raise ValueError('Unexpected J72 TRX header')
    if crc(firmware) != checksum:
        raise ValueError('Original firmware CRC failed')
    if not fwlen or fwlen > length - HEADER or jump >= fwlen or nvlen:
        raise ValueError('Invalid firmware offsets or input already has NVRAM')
    if any(firmware[HEADER + fwlen:]):
        raise ValueError('Non-padding firmware trailer requires review')
    board = nvram(text, mac)
    image = bytearray(firmware[:HEADER + fwlen] + board)
    if len(image) > RAM:
        raise ValueError('Combined image exceeds observed ROM RAM')
    struct.pack_into('<I', image, 4, len(image))
    struct.pack_into('<I', image, 24, len(board))
    struct.pack_into('<I', image, 8, crc(image))
    assert crc(image) == struct.unpack_from('<I', image, 8)[0]
    return bytes(image)



DESTINATION = 'lib/firmware/brcm/brcmfmac43241-j72-usb.bin'


def _read_private(path, maximum):
    """Read bounded regular inputs without following a final symlink."""
    with os.fdopen(os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK), 'rb') as stream:
        if not stat.S_ISREG(os.fstat(stream.fileno()).st_mode):
            raise ValueError('Firmware inputs must be regular files')
        data = stream.read(maximum + 1)
    if len(data) > maximum:
        raise ValueError('Private firmware input exceeds its format bound')
    return data


def prepare(firmware, board_nvram, mac_file, output):
    """Create an owner-private J72 USB firmware mixin, without device access.

    The MAC is a separate six-byte binary input, never an argument or a
    public recipe parameter. This radio's USB download protocol needs it in
    the appended board settings; its output is per-unit and stays private.
    Existing paths are never replaced, including dangling symlinks.
    """
    from orchard.firmware import pack_mixin

    image = pack(_read_private(firmware, RAM),
                 _read_private(board_nvram, RAM).decode('ascii'),
                 _read_private(mac_file, 6))
    output = Path(output)
    # mkdir is exclusive: no exists()/open() race or overwrite of a mixin.
    output.mkdir(mode=0o700)
    prepared = output / 'prepared'
    target = prepared / 'firmware' / DESTINATION
    target.parent.mkdir(parents=True, mode=0o700)
    with os.fdopen(os.open(target, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600), 'wb') as stream:
        stream.write(image)
    files = {DESTINATION: {'bytes': len(image), 'sha256': hashlib.sha256(image).hexdigest()}}
    archive, receipt = pack_mixin(prepared, {'schema': 1, 'profile': 'j72', 'files': files}, output)
    return archive, receipt
