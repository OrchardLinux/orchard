#!/usr/bin/env python3
"""Combine loader, boot arguments, DTBs, kernel and initramfs into an m1n1 image.

m1n1 boots with the DTB whose root compatible names the device it runs on, so
one image carries the DTB of every device it supports. The image is m1n1's
own payload format and nothing more; this script needs only Python's standard
library and runs by path, without orchard:

  python3 producers/assemble.py --bootargs 'console=tty0 rdinit=/init' \
      --loader m1n1.bin --dtb t8010-j71b.dtb --kernel Image.gz \
      --initramfs initramfs.cpio.gz --output linux.bin

The same bytes by hand (docs/ram-development.md, "Assembling by hand"):
m1n1.bin, the optional logo, the line "chosen.bootargs=ARGS\n", every DTB,
Image.gz, then "m1n1_initramfs", the initramfs size as 4 bytes
little-endian, the initramfs.
"""
import argparse
import hashlib
import json
import struct
from pathlib import Path


INITRAMFS_MAGIC = b'm1n1_initramfs'
# m1n1's custom logo (payload.c): this magic, then RGBA flattened on black,
# 256x256 and then 128x128. m1n1 draws it instead of its own logo, but only
# as the first payload after m1n1 itself.
LOGO_MAGIC = b'm1n1_logo_256128'
LOGO_BYTES = len(LOGO_MAGIC) + 4 * (256 * 256 + 128 * 128)


def initramfs_header(path):
    size = path.stat().st_size
    if not 0 < size <= 0xffffffff:
        raise ValueError('Initramfs size exceeds m1n1 framing limits')
    # m1n1 passes this payload directly to Linux instead of expanding gzip in
    # its tiny loader inflater. Linux performs the normal initramfs unpack.
    return INITRAMFS_MAGIC + struct.pack('<I', size)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    given = parser.add_mutually_exclusive_group(required=True)
    given.add_argument('--bootargs', help='kernel command line')
    given.add_argument('--manifest', type=Path, help='boot profile: JSON with "bootargs": "chosen.bootargs=...\\n"')
    for name in ('loader', 'kernel', 'initramfs', 'output'):
        parser.add_argument('--' + name, type=Path, required=True)
    parser.add_argument('--dtb', type=Path, action='append', required=True, help='repeat once per device')
    parser.add_argument('--logo', type=Path, help='m1n1 custom logo payload (m1n1_logo_256128, RGBA 256x256 then 128x128)')
    args = parser.parse_args()
    if args.bootargs is not None:
        if '\n' in args.bootargs or '\r' in args.bootargs:
            parser.error('boot arguments are one line')
        bootargs = f'chosen.bootargs={args.bootargs}\n'.encode()
    else:
        bootargs = json.loads(args.manifest.read_text())['bootargs'].encode()
    if not bootargs.startswith(b'chosen.bootargs=') or not bootargs.endswith(b'\n'):
        parser.error('invalid device boot arguments')
    if args.output.exists():
        parser.error('output already exists; choose a new artifact name')
    if args.logo is not None:
        if not args.logo.is_file():
            parser.error(f'missing logo: {args.logo}')
        with args.logo.open('rb') as stream:
            if stream.read(len(LOGO_MAGIC)) != LOGO_MAGIC or args.logo.stat().st_size != LOGO_BYTES:
                parser.error(f'{args.logo} is not an m1n1 logo payload ({LOGO_MAGIC.decode()}, {LOGO_BYTES} bytes)')
    logo = [('logo', args.logo)] if args.logo is not None else []
    inputs = [('loader', args.loader), *logo, ('bootargs', bootargs), *(('dtb', dtb) for dtb in args.dtb),
              ('kernel', args.kernel), ('initramfs', args.initramfs)]
    for name, source in inputs:
        if not isinstance(source, bytes) and not source.is_file():
            parser.error(f'missing {name}: {source}')
    inputs.insert(-1, ('initramfs_header', initramfs_header(args.initramfs)))
    args.output.parent.mkdir(parents=True, exist_ok=True)
    components = []
    offset = 0
    with args.output.open('xb') as result:
        for name, source in inputs:
            digest = hashlib.sha256()
            size = 0
            if isinstance(source, bytes):
                blocks = (source,)
            else:
                def chunks(path):
                    with path.open('rb') as stream:
                        while block := stream.read(1024 * 1024):
                            yield block
                blocks = chunks(source)
            for block in blocks:
                result.write(block)
                digest.update(block)
                size += len(block)
            components.append(dict(name=name, offset=offset, bytes=size, sha256=digest.hexdigest()))
            offset += size
    receipt = args.output.with_suffix(args.output.suffix + '.json')
    receipt.write_text(json.dumps({'image_sha256': hashlib.sha256(args.output.read_bytes()).hexdigest(),
                                   'initramfs_encoding': 'm1n1_initramfs',
                                   'components': components}, indent=2) + '\n')
    print(f'Built {args.output}; component receipt: {receipt}')


if __name__ == '__main__':
    main()
