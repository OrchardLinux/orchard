"""Inspect little-endian ELF32/ELF64 dependencies without executing target code."""
import struct


def _parts(data):
    if data[:4] != b'\x7fELF':
        return None
    if len(data) < 16 or data[5:7] != b'\x01\x01' or data[4] not in (1, 2):
        raise ValueError('Unsupported ELF encoding')
    width = data[4]
    header_size, program_size = (52, 32) if width == 1 else (64, 56)
    if len(data) < header_size:
        raise ValueError('Incomplete ELF header')
    machine = struct.unpack_from('<H', data, 18)[0]
    offset = struct.unpack_from('<I' if width == 1 else '<Q', data, 28 if width == 1 else 32)[0]
    size, count = struct.unpack_from('<HH', data, 42 if width == 1 else 54)
    if count and (size < program_size or offset < header_size or offset + size * count > len(data)):
        raise ValueError('ELF program headers exceed the file')
    segments = []
    for index in range(count):
        position = offset + size * index
        if width == 1:
            kind, file_offset, virtual, _, file_size, _, _, _ = struct.unpack_from('<8I', data, position)
        else:
            kind, _, file_offset, virtual, _, file_size, _, _ = struct.unpack_from('<II6Q', data, position)
        if file_offset + file_size > len(data):
            raise ValueError('ELF segment exceeds the file')
        segments.append((kind, virtual, file_offset, file_size))
    return width, machine, segments


def architecture(data):
    """Return Orchard's architecture for an ARM executable or shared object."""
    parsed = _parts(data)
    if parsed is None:
        raise ValueError('Expected an ELF executable')
    width, machine, _ = parsed
    if width == 2 and machine == 183:
        return 'arm64'
    if width == 1 and machine == 40:
        flags = struct.unpack_from('<I', data, 36)[0]
        if flags >> 24 != 5 or not flags & 0x400 or flags & 0x200:
            raise ValueError('ARM recovery requires EABI5 hard-float executables')
        return 'arm'
    raise ValueError('Unsupported recovery ELF architecture')


def require_static(data):
    """Reject an interpreter or shared-library dependency in an initrd tool."""
    parsed = _parts(data)
    if parsed is None:
        raise ValueError('Expected a static ELF executable')
    if any(kind == 3 for kind, _, _, _ in parsed[2]) or needed(data):
        raise ValueError('Installed root helpers must be static ELF executables')


def needed(data):
    parsed = _parts(data)
    if parsed is None:
        return set()
    width, _, segments = parsed
    dynamic = [segment for segment in segments if segment[0] == 2]
    if not dynamic:
        return set()
    if len(dynamic) != 1:
        raise ValueError('ELF has multiple dynamic tables')
    _, _, offset, length = dynamic[0]
    step, form = (8, '<II') if width == 1 else (16, '<QQ')
    if length % step:
        raise ValueError('Incomplete ELF dynamic entry')
    names, strings, string_size = [], None, None
    for entry in range(offset, offset + length, step):
        tag, value = struct.unpack_from(form, data, entry)
        if tag == 0:
            break
        if tag == 1:
            names.append(value)
        elif tag == 5:
            strings = value
        elif tag == 10:
            string_size = value
    if not names:
        return set()
    if strings is None:
        raise ValueError('ELF dependencies have no string table')
    for kind, virtual, file_offset, file_size in segments:
        if kind == 1 and virtual <= strings < virtual + file_size:
            start = file_offset + strings - virtual
            length = file_offset + file_size - start
            if string_size is not None:
                if string_size > length:
                    raise ValueError('ELF string table exceeds its load segment')
                length = string_size
            result = set()
            for name in names:
                if name >= length:
                    raise ValueError('ELF dependency offset exceeds the string table')
                end = data.find(b'\0', start + name, start + length)
                if end < 0:
                    raise ValueError('Unterminated ELF dependency name')
                try:
                    text = data[start + name:end].decode('ascii')
                except UnicodeDecodeError as error:
                    raise ValueError('Non-ASCII ELF dependency name') from error
                if not text or '/' in text or text in ('.', '..'):
                    raise ValueError('Invalid ELF dependency name')
                result.add(text)
            return result
    raise ValueError('ELF string table is outside load segments')
