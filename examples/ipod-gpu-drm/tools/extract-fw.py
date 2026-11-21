#!/usr/bin/env python3
"""Make the A8 GPU firmware files from an Apple kernelcache you fetch yourself.

Usage:
  extract-fw.py OUT_DIR                          fetch the tested kernelcache (iPod touch 6, iOS 12.4 16G77)
                                                 from Apple's public IPSW over HTTP ranges (~15 MB, not the
                                                 whole 2.8 GB IPSW)
  extract-fw.py OUT_DIR --ipsw URL BYTES BOARD   the same from another IPSW, e.g. an iPad mini 4's (BOARD j96ap)
  extract-fw.py OUT_DIR --kernelcache FILE       a decompressed kernelcache you already have
                                                 (e.g. `ipsw download ipsw --device iPod7,1 --build 16G77 --kernel`)

Writes:
  OUT_DIR/gfx-t7000.bin         the GPU firmware: AGXFirmwareKextG4P's META LDR image, byte for byte
  OUT_DIR/gfx-t7000-border.bin  BorderColorTable's first 0x800 bytes from AGXG4P's data; powervr's A8
                                variant loads them beside the firmware and refuses to probe without them

The A8 GPU is the same on every T7000 board, and the driver was written and tested against the firmware
of iOS 12.4 (16G77) for iPod7,1: that LDR's SHA-256 is pinned below. Another kernelcache's LDR is written
too, with a warning that it is not the tested one. The border table is found by its pinned SHA-256; if a
kernelcache does not hold that table, use the default (iPod) kernelcache for every A8 board.

The kernelcache is decompressed by orchard's own code (src/orchard/apple). Nothing from Apple is stored in
this repository.
"""
import hashlib
import os
import struct
import sys

ROOT = os.path.abspath(os.path.join(os.path.dirname(os.path.abspath(__file__)), '..', '..', '..'))
sys.path.insert(0, os.path.join(ROOT, 'src'))

TESTED_IPSW = ('https://updates.cdn-apple.com/2019SummerFCS/fullrestores/041-86563/'
               '9E4DEC4C-A743-11E9-87E4-CD93DBB619B1/iPodtouch_12.4_16G77_Restore.ipsw', 2813180896, 'n102ap')
LDR_MAGIC = 0x01AA5500
TESTED_LDR_SHA256 = '0b60def67290391b4c6aa913d9a78a8105d6d9b03738175b8e5732689db1e5ba'
TABLE_LEN = 0x800
TABLE_WORD = (0x440, 0x02040000)  # a word of the table, to find candidates before hashing them
TABLE_SHA256 = '233ffc1d7db27d9f16db17e23a9ce33082b5a7b72061af3da510bca3c94aff3e'


def fail(msg):
    raise SystemExit('extract-fw: ' + msg)


def ldr_length(blob):
    """The META LDR image's length: the end of its last L1 record or L2 block."""
    if len(blob) < 16:
        raise ValueError('truncated header')
    magic, code, data, _, _ = struct.unpack_from('<IIIHH', blob)
    if magic != LDR_MAGIC:
        raise ValueError('not a META LDR')
    end, pos, seen = max(16, code), data, set()
    while pos != 0xFFFFFFFF:
        if pos in seen or pos + 8 > len(blob):
            raise ValueError('bad record list')
        seen.add(pos)
        cmd, size, nxt = struct.unpack_from('<HHI', blob, pos)
        if size < 12 or size % 4 or pos + size > len(blob):
            raise ValueError('bad L1 record')
        words = struct.unpack_from('<%dI' % ((size - 12) // 4), blob, pos + 8)
        if cmd & 0x10 or cmd in (3, 5):
            l2 = words[0]
        elif cmd == 0:
            l2 = words[1]
        elif cmd == 4:
            l2 = words[2]
        else:
            raise ValueError('unknown L1 command %#x' % cmd)
        if l2 + 4 > len(blob):
            raise ValueError('bad L2 offset')
        l2size = struct.unpack_from('<H', blob, l2 + 2)[0]
        if l2size < 6 or l2 + l2size > len(blob):
            raise ValueError('bad L2 block')
        end = max(end, pos + size, l2 + l2size)
        pos = nxt
    return end


def find_ldr(kc):
    found = []
    magic = struct.pack('<I', LDR_MAGIC)
    at = kc.find(magic)
    while at >= 0:
        try:
            found.append(kc[at:at + ldr_length(kc[at:])])
        except ValueError:
            pass
        at = kc.find(magic, at + 4)
    if not found:
        fail('no META LDR (GPU firmware) in this kernelcache')
    tested = [f for f in found if hashlib.sha256(f).hexdigest() == TESTED_LDR_SHA256]
    if tested:
        return tested[0], True
    if len(found) > 1:
        fail('%d META LDR images and none is the tested one; use the default kernelcache' % len(found))
    return found[0], False


def find_table(kc):
    off, word = TABLE_WORD
    needle = struct.pack('<I', word)
    at = kc.find(needle)
    while at >= 0:
        start = at - off
        if start >= 0 and start % 16 == 0:
            table = kc[start:start + TABLE_LEN]
            if hashlib.sha256(table).hexdigest() == TABLE_SHA256:
                return table
        at = kc.find(needle, at + 1)
    fail('this kernelcache does not hold the known BorderColorTable; run extract-fw.py OUT_DIR without '
         'options (the iPod touch 6 kernelcache serves every A8 board)')


def main(argv):
    if len(argv) == 1:
        source = TESTED_IPSW
    elif len(argv) == 5 and argv[1] == '--ipsw':
        source = (argv[2], int(argv[3]), argv[4].lower())
    elif len(argv) == 3 and argv[1] == '--kernelcache':
        source = argv[2]
    else:
        fail('usage: extract-fw.py OUT_DIR [--ipsw URL BYTES BOARD | --kernelcache FILE]')
    out = argv[0]
    if isinstance(source, str):
        kc = open(source, 'rb').read()
        if kc[:4] != b'\xcf\xfa\xed\xfe':
            fail('%s is not a decompressed 64-bit Mach-O kernelcache' % source)
    else:
        from orchard.apple.remote_ipsw import Ipsw
        url, size, board = source
        print('extract-fw: reading the %s kernelcache from %s' % (board, url))
        kc = Ipsw(url, size).kernelcache(board)

    ldr, tested = find_ldr(kc)
    table = find_table(kc)
    os.makedirs(out, exist_ok=True)
    for name, data in (('gfx-t7000.bin', ldr), ('gfx-t7000-border.bin', table)):
        path = os.path.join(out, name)
        with open(path, 'wb') as f:
            f.write(data)
        print('extract-fw: %s, %d bytes, sha256 %s' % (path, len(data), hashlib.sha256(data).hexdigest()))
    if tested:
        print('extract-fw: the GPU firmware is the tested one (iOS 12.4 16G77, iPod touch 6)')
    else:
        print('extract-fw: WARNING: the GPU firmware is not the tested one (iOS 12.4 16G77, iPod touch 6); '
              'the driver may not boot it. Without options this tool fetches the tested one.')


if __name__ == '__main__':
    main(sys.argv[1:])
