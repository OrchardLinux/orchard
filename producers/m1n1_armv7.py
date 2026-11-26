#!/usr/bin/env python3
"""Make the image a 32-bit iBoot starts as its kernel: the ARMv7 m1n1 with its payload.

On Apple's 32-bit chips (A5-A6X) m1n1 is started by a patched iBEC the way iBoot starts
XNU: the host sends Apple's device tree and this image, then `bootx`. m1n1 gets iBoot's
boot arguments and device tree that way, as on the 64-bit boards. The image is an IMG3
of type `krnl` holding the Mach-O m1n1-armv7.macho (boot/m1n1/build) in Apple's LZSS
container, unencrypted and unsigned.

The payload is m1n1's own format, as producers/assemble.py writes it for the 64-bit
boards: variables, the boards' DTBs, the kernel (a zImage here) and the framed
initramfs. It is appended to the Mach-O, whose last segment (`__LAST`) is sized to it,
so iBoot loads it behind m1n1. With --ramdisk-output, DTBs, kernel and initramfs go
into a second IMG3 of type `rdsk` instead, for iBoot's `ramdisk` command: m1n1 looks
for payloads there as well, and iBoot's limit for a kernel image no longer bounds the
initramfs. Variables stay with m1n1 either way.

Without a kernel the image is m1n1 alone: with --autoboot it starts the default boot
entry of the device's flash, without it m1n1 waits for the host on its USB proxy.

This script needs only Python's standard library and runs by path, without orchard:

  python3 producers/m1n1_armv7.py --m1n1 m1n1-armv7.macho --bootargs 'console=tty0' \\
      --dtb s5l8942x-p105.dtb --kernel zImage --initramfs initramfs.cpio.gz \\
      --output kernelcache.img3
"""
import argparse
import hashlib
import json
import struct
import zlib
from pathlib import Path


INITRAMFS_MAGIC = b'm1n1_initramfs'
MH_MAGIC, CPU_TYPE_ARM, LC_SEGMENT = 0xfeedface, 12, 1
ZIMAGE_MAGIC = 0x016f2818
FDT_MAGIC = b'\xd0\x0d\xfe\xed'
LZSS_HEADER = 0x180
PAGE = 4096


def digest(data):
    return hashlib.sha256(data).hexdigest()


def last_segment(macho):
    """Offset of the `__LAST` segment command of a 32-bit ARM Mach-O, and where its file data starts."""
    if len(macho) < 28:
        raise ValueError('m1n1 is not a Mach-O file')
    magic, cpu, _, _, ncmds, sizeofcmds, _ = struct.unpack_from('<7I', macho, 0)
    if magic != MH_MAGIC or cpu != CPU_TYPE_ARM or 28 + sizeofcmds > len(macho):
        raise ValueError('m1n1 is not a 32-bit ARM Mach-O: use m1n1-armv7.macho')
    at = 28
    for _ in range(ncmds):
        if at + 8 > 28 + sizeofcmds:
            raise ValueError('Truncated Mach-O load commands')
        command, size = struct.unpack_from('<II', macho, at)
        if size < 8 or at + size > 28 + sizeofcmds:
            raise ValueError('Invalid Mach-O load command')
        if command == LC_SEGMENT and size >= 56 and macho[at + 8:at + 24].rstrip(b'\0') == b'__LAST':
            vmaddr, vmsize, fileoff, filesize = struct.unpack_from('<4I', macho, at + 24)
            if vmsize or filesize or fileoff != len(macho) or vmaddr % PAGE:
                raise ValueError('m1n1 already carries a payload, or is not as boot/m1n1/build makes it')
            return at
        at += size
    raise ValueError('m1n1 has no __LAST segment for a payload')


def with_payload(macho, payload):
    """The Mach-O with PAYLOAD behind it and its last segment sized to it; zeros end the payload."""
    payload += bytes(8)
    at = last_segment(macho)
    image = bytearray(macho + payload)
    struct.pack_into('<I', image, at + 28, (len(payload) + PAGE - 1) & -PAGE)   # vmsize
    struct.pack_into('<I', image, at + 36, len(payload))                        # filesize
    return bytes(image)


def lzss_stored(data):
    """DATA in Apple's `complzss` container, every byte a literal: any LZSS reader takes it.

    A literal run is a flag byte with all eight bits set and the eight bytes. That costs an
    eighth more bytes than the data, and none of a compressor's time for images of tens of MiB.
    """
    groups = (len(data) + 7) // 8
    stream = bytearray(len(data) + groups)
    stream[0::9] = b'\xff' * groups
    for k in range(8):
        part = data[k::8]
        stream[k + 1::9] = part
    header = struct.pack('>4s4sIIII', b'comp', b'lzss', zlib.adler32(data), len(data), len(stream), 1)
    return header.ljust(LZSS_HEADER, b'\0') + bytes(stream)


def img3(kind, data):
    """An unencrypted, unsigned IMG3 of type KIND (four characters, as Apple writes them) around DATA."""
    if len(kind) != 4:
        raise ValueError('IMG3 types have four characters')
    ident = kind.encode()[::-1]
    type_tag = struct.pack('<4sII', b'EPYT', 32, 4) + ident + bytes(16)
    padded = data + bytes(-len(data) % 4)
    data_tag = struct.pack('<4sII', b'ATAD', 12 + len(padded), len(data)) + padded
    tags = type_tag + data_tag
    return struct.pack('<4sIII', b'3gmI', 20 + len(tags), len(tags), len(tags)) + ident + tags


def check_kernel(data):
    if len(data) < 0x30 or struct.unpack_from('<I', data, 0x24)[0] != ZIMAGE_MAGIC:
        raise ValueError('The kernel is not a 32-bit ARM zImage')
    start, end = struct.unpack_from('<II', data, 0x28)
    if end - start != len(data):
        raise ValueError('The zImage file is not as long as its header says (an appended DTB?)')


def check_dtb(data):
    if len(data) < 40 or data[:4] != FDT_MAGIC or struct.unpack_from('>I', data, 4)[0] != len(data):
        raise ValueError('Not a flattened device tree')


def variables(bootargs=None, chosen=(), autoboot=False):
    lines = []
    if bootargs is not None:
        chosen = [('bootargs', bootargs), *chosen]
    for name, value in chosen:
        if not name or '=' in name or '\n' in name + value or len(name) > 56 or len(value) > 1000:
            raise ValueError(f'Unusable /chosen value for m1n1: {name}')
        lines.append(f'chosen.{name}={value}\n')
    if autoboot:
        lines.append('autoboot=1\n')
    return ''.join(lines).encode()


def boot_files(dtbs=(), kernel=None, initramfs=None):
    """DTBs, kernel and framed initramfs in m1n1's payload format."""
    for dtb in dtbs:
        check_dtb(dtb)
    parts = list(dtbs)
    if kernel is not None:
        check_kernel(kernel)
        if not dtbs:
            raise ValueError('A kernel needs the device tree of every board it is for')
        # m1n1 reads a zImage's length from its header; pad so that what follows stays aligned
        parts.append(kernel + bytes(-len(kernel) % 4))
    elif dtbs or initramfs is not None:
        raise ValueError('Device trees and an initramfs are for a kernel')
    if initramfs is not None:
        if not 0 < len(initramfs) <= 0xffffffff:
            raise ValueError('Initramfs size exceeds m1n1 framing limits')
        parts += [INITRAMFS_MAGIC, struct.pack('<I', len(initramfs)), initramfs]
    return b''.join(parts)


def build(m1n1, *, bootargs=None, chosen=(), autoboot=False, dtbs=(), kernel=None, initramfs=None,
          split=False):
    """(kernel image, ramdisk image or None, receipt) for iBoot's `bootx`."""
    if autoboot and kernel is not None:
        raise ValueError('--autoboot starts the system on the flash; it takes no kernel')
    head = variables(bootargs, chosen, autoboot)
    files = boot_files(dtbs, kernel, initramfs)
    ramdisk = None
    if split:
        if not files:
            raise ValueError('Nothing to put into a ramdisk image')
        ramdisk = img3('rdsk', files + bytes(8))
        files = b''
    macho = with_payload(m1n1, head + files)
    image = img3('krnl', lzss_stored(macho))
    receipt = {'m1n1': digest(m1n1), 'macho_bytes': len(macho), 'payload_bytes': len(head) + len(files),
               'kernel_image': {'type': 'krnl', 'bytes': len(image), 'sha256': digest(image)}}
    if ramdisk is not None:
        receipt['ramdisk_image'] = {'type': 'rdsk', 'bytes': len(ramdisk), 'sha256': digest(ramdisk)}
    return image, ramdisk, receipt


def main():
    parser = argparse.ArgumentParser(description=__doc__.split('\n\n')[0])
    parser.add_argument('--m1n1', required=True, type=Path, help='m1n1-armv7.macho from boot/m1n1/build')
    parser.add_argument('--bootargs', help='Linux boot arguments (/chosen bootargs)')
    parser.add_argument('--chosen', action='append', default=[], metavar='NAME=VALUE',
                        help='another /chosen string; repeat')
    parser.add_argument('--autoboot', action='store_true',
                        help='start the default boot entry of the flash instead of a kernel given here')
    parser.add_argument('--dtb', action='append', default=[], type=Path, help='a board DTB; repeat')
    parser.add_argument('--kernel', type=Path, help='zImage')
    parser.add_argument('--initramfs', type=Path)
    parser.add_argument('--output', required=True, type=Path, help='the IMG3 for bootx')
    parser.add_argument('--ramdisk-output', type=Path,
                        help='put DTBs, kernel and initramfs into this IMG3 for the ramdisk command')
    args = parser.parse_args()
    chosen = []
    for item in args.chosen:
        name, found, value = item.partition('=')
        if not found:
            parser.error('--chosen takes NAME=VALUE')
        chosen.append((name, value))
    for path in (args.output, args.ramdisk_output):
        if path is not None and (path.exists() or path.is_symlink()):
            parser.error(f'Output exists: {path}')
    try:
        image, ramdisk, receipt = build(
            args.m1n1.read_bytes(), bootargs=args.bootargs, chosen=chosen, autoboot=args.autoboot,
            dtbs=[path.read_bytes() for path in args.dtb],
            kernel=args.kernel.read_bytes() if args.kernel else None,
            initramfs=args.initramfs.read_bytes() if args.initramfs else None,
            split=args.ramdisk_output is not None)
    except ValueError as error:
        parser.error(str(error))
    args.output.write_bytes(image)
    if ramdisk is not None:
        args.ramdisk_output.write_bytes(ramdisk)
    print(json.dumps(receipt, indent=2, sort_keys=True))


if __name__ == '__main__':
    main()
