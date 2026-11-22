"""BlueTool's embedded Bluetooth firmware; board recipes and selections are callers' policy."""
import hashlib

from orchard.apple.macho import macho

WRITE_RAM, LAUNCH_RAM = 0xfc4c, 0xfc4e


def _stream_end(data, start):
    """End of the HCD command stream at start: Write RAM records up to Launch RAM."""
    end = start
    while end + 3 <= len(data):
        opcode = int.from_bytes(data[end:end + 2], 'little')
        length = data[end + 2]
        if opcode not in (WRITE_RAM, LAUNCH_RAM) or end + 3 + length > len(data):
            break
        # Newer streams open with a BRCMcfgS record and may follow each other directly.
        if end != start and data[end + 7:end + 15] == b'BRCMcfgS':
            break
        end += 3 + length
        if opcode == LAUNCH_RAM:
            break
    return end


def hcd_from_binary(data, expected):
    """Select only the SHA-pinned HCD command stream embedded in BlueTool.

    Streams are found by their record structure, so both the newer ones that
    open with a BRCMcfgS record and older ones without it (BCM4350) are found.
    """
    offset = 0
    while True:
        start = data.find(b'\x4c\xfc', offset)
        if start < 0:
            raise ValueError('Pinned Bluetooth firmware not found in BlueTool')
        end = _stream_end(data, start)
        if end == start:
            offset = start + 1
            continue
        if hashlib.sha256(data[start:end]).hexdigest() == expected:
            return data[start:end]
        offset = end


def regulatory_from_binary(data, address, size, expected=None):
    """Read a pinned, single-command regulatory table at its native VM address.

    The recipe records the address/length from the board's native registration.
    Both the BlueTool input and table output are hash-pinned by the recipe runner.
    Only file-backed bytes in one unambiguous Mach-O segment are accepted.
    """
    if address < 0 or not 1 <= size <= 255:
        raise ValueError('Invalid single-command Bluetooth regulatory range')
    _, segments, _ = macho(data)
    matches = [segment for segment in segments
               if segment['vm'] <= address and
               address + size <= segment['vm'] + segment['size']]
    if len(matches) != 1:
        raise ValueError('Bluetooth regulatory range must map to one file-backed segment')
    segment = matches[0]
    offset = segment['offset'] + address - segment['vm']
    if offset + size > len(data):
        raise ValueError('Truncated Bluetooth regulatory table')
    table = data[offset:offset + size]
    if expected is not None and hashlib.sha256(table).hexdigest() != expected:
        raise ValueError('Bluetooth regulatory table differs from pinned hash')
    return table
