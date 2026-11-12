#!/usr/bin/env python3
"""Check the recovery tools' target ISA and static ELF linkage without running them."""
import argparse
from pathlib import Path
import stat
import struct


TARGETS = {'arm': (1, 40), 'arm64': (2, 183)}


def validate(data, arch):
    if arch not in TARGETS:
        raise ValueError('Unsupported recovery architecture: ' + arch)
    elf_class, machine = TARGETS[arch]
    if len(data) < 16 or data[:7] != b'\x7fELF' + bytes((elf_class, 1, 1)):
        raise ValueError('ELF class or byte order differs from target ' + arch)
    header = '<16sHHIIIIIHHHHHH' if elf_class == 1 else '<16sHHIQQQIHHHHHH'
    if len(data) < struct.calcsize(header):
        raise ValueError('Incomplete ELF header')
    fields = struct.unpack_from(header, data)
    if fields[1] not in (2, 3) or fields[2] != machine or fields[3] != 1:
        raise ValueError('ELF machine or executable type differs from target ' + arch)
    if fields[8] != struct.calcsize(header):
        raise ValueError('Invalid ELF header size')
    if arch == 'arm' and (fields[7] & 0xff000000 != 0x05000000 or
                          fields[7] & 0x600 != 0x400):
        raise ValueError('ARM recovery requires EABI5 hard-float executables')
    segment = '<IIIIIIII' if elf_class == 1 else '<IIQQQQQQ'
    offset, stride, count = fields[5], fields[9], fields[10]
    if stride != struct.calcsize(segment) or not count or offset + stride * count > len(data):
        raise ValueError('Invalid ELF program header table')
    dynamic = []
    for index in range(count):
        values = struct.unpack_from(segment, data, offset + index * stride)
        kind = values[0]
        start, size = (values[1], values[4]) if elf_class == 1 else (values[2], values[5])
        if start + size > len(data):
            raise ValueError('ELF segment exceeds the file')
        if kind == 3:
            raise ValueError('Recovery tools must be static: PT_INTERP is present')
        if kind == 2:
            dynamic.append((start, size))
    entry = '<iI' if elf_class == 1 else '<qQ'
    width = struct.calcsize(entry)
    for start, size in dynamic:
        if size % width:
            raise ValueError('Incomplete ELF dynamic table')
        for cursor in range(start, start + size, width):
            tag, _ = struct.unpack_from(entry, data, cursor)
            if tag == 0:
                break
            if tag == 1:
                raise ValueError('Recovery tools must be static: DT_NEEDED is present')


def check(path, arch):
    info = path.lstat()
    if not stat.S_ISREG(info.st_mode) or not info.st_mode & 0o111 or info.st_size > 64 * 1024 * 1024:
        raise ValueError('Expected a bounded regular executable: ' + str(path))
    validate(path.read_bytes(), arch)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--arch', choices=TARGETS, required=True)
    parser.add_argument('tools', nargs='+', type=Path)
    args = parser.parse_args()
    for path in args.tools:
        try:
            check(path, args.arch)
        except (OSError, ValueError, struct.error) as error:
            parser.exit(1, str(path) + ': ' + str(error) + '\n')


if __name__ == '__main__':
    main()
