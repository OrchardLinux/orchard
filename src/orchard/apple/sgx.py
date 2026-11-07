"""SGX543 container, after rzxvx's Project Cascadia extract-sgx-firmware.py.

The recipe checks the complete container hash, including section addresses and
build ID: driver offsets are meaningful only for that exact kernel build.
"""
import struct


def segments(blob, base):
    if base < 0 or base + 28 > len(blob) or blob[base:base + 4] != b'\xce\xfa\xed\xfe':
        raise ValueError('Not a 32-bit Mach-O')
    count, size = struct.unpack_from('<II', blob, base + 16)
    pos, end = base + 28, base + 28 + size
    if end > len(blob) or count > size // 8:
        raise ValueError('Invalid Mach-O load commands')
    result = {}
    for _ in range(count):
        if pos + 8 > end:
            raise ValueError('Truncated load command')
        command, length = struct.unpack_from('<II', blob, pos)
        if length < 8 or pos + length > end:
            raise ValueError('Invalid load command size')
        if command == 1:
            if length < 56:
                raise ValueError('Truncated segment')
            name = blob[pos + 8:pos + 24].rstrip(b'\0').decode('ascii')
            va, vsize, offset, fsize = struct.unpack_from('<4I', blob, pos + 24)
            nsects = struct.unpack_from('<I', blob, pos + 48)[0]
            if 56 + 68 * nsects > length or name in result:
                raise ValueError('Invalid segment sections')
            sections = {}
            for i in range(nsects):
                at = pos + 56 + i * 68
                section = blob[at:at + 16].rstrip(b'\0').decode('ascii')
                addr, ssize, _ = struct.unpack_from('<3I', blob, at + 32)
                if not va <= addr <= addr + ssize <= va + vsize or section in sections:
                    raise ValueError('Invalid section range')
                sections[section] = (addr, ssize)
            result[name] = (va, offset, fsize, sections)
        pos += length
    return result


def extract(kernel, build):
    encoded = build.encode('ascii')
    if not encoded or len(encoded) > 16:
        raise ValueError('Invalid SGX build ID')
    top = segments(kernel, 0)
    if '__PRELINK_TEXT' not in top:
        raise ValueError('No prelinked kexts')
    va, start, size, _ = top['__PRELINK_TEXT']
    if start + size > len(kernel):
        raise ValueError('Truncated prelink text')

    def section(pair):
        address, length = pair
        offset = address - va + start
        if not start <= offset <= offset + length <= start + size:
            raise ValueError('Section outside prelink text')
        return kernel[offset:offset + length]

    matches = []
    for base in range(start, start + size - 27, 0x1000):
        if kernel[base:base + 4] != b'\xce\xfa\xed\xfe':
            continue
        segs = segments(kernel, base)
        strings = segs.get('__TEXT', (0, 0, 0, {}))[3].get('__cstring')
        if strings and b'SGXDriver543\0' in section(strings):
            matches.append(segs)
    if len(matches) != 1:
        raise ValueError('Expected exactly one IMGSGX543 kext')
    try:
        d = matches[0]['__DATA'][3]['__data']
        c = matches[0]['__TEXT'][3]['__const']
    except KeyError as exc:
        raise ValueError('Missing SGX firmware sections') from exc
    data, const = section(d), section(c)
    header = struct.pack('<8s7I16s', b'SGX543FW', 1, 64, len(data),
                         64 + len(data), len(const), d[0], c[0], encoded)
    return header.ljust(64, b'\0') + data + const
