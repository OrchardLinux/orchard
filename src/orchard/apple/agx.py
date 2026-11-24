"""Extract Apple GPU firmware from a legacy prelinked kernelcache.

A10 (G9P): AGXFirmwareKextG9PRTBuddy keeps the image in __TEXT.__const: its build
variant as a C string ("Release"), then the image to the end of the section,
an ARM32 binary loaded at address 0 whose first word is the reset vector's
branch. The kext's code reaches it with PC-relative addresses, so there is no
pointer record to follow; the section layout is checked instead, and the
caller pins the image's SHA-256.

A9 (G5P): AGXFirmwareKextG5P's __TEXT.__const is the whole firmware, a META
LDR command stream the driver loads into the GPU's own processor. The LDR has
no header to check here, and the driver's firmware layout belongs to one
image, so its size and SHA-256 are pinned: the C0 image of iPadOS 16.7.10.

A8 (G4P): AGXFirmwareKextG4P holds the same kind of META LDR, and AGXG4P's
data holds the border colour table the driver loads beside it. The iOS 12
kernelcaches that carry the tested image have no per-kext load addresses in
their prelink metadata, so both are found by their structure: every LDR whose
records walk cleanly, and the one 0x800-byte table that opens with 0x440 zero
bytes and then the word 0x02040000. The recipe's output hash picks the image,
as for BlueTool's patchrams.
"""
import hashlib
import re
import struct

from orchard.apple.prelink import Kernelcache, named

G9P = 'com.apple.AGXFirmwareKextG9PRTBuddy'
CONST = ('__TEXT', '__const')
VARIANT = re.compile(rb'\0*[\x20-\x7e]+\0')

G5P = 'com.apple.AGXFirmwareKextG5P'
G5P_SIZE = 119112
G5P_SHA256 = '33ae20cbbe2498e4f119ba4e9a55467e4e0192d4c86d5bf1903adce63ffa5dca'


def extract(blob: bytes) -> bytes:
    kernel = Kernelcache(blob)
    section = named(kernel.kext_sections(G9P, 'A10 GPU firmware'), CONST)[CONST]
    const = kernel.read(section['addr'], section['size'])
    variant = VARIANT.match(const)
    if not variant:
        raise ValueError('No build variant before the GPU firmware')
    image = const[variant.end():]
    # An ARM "b" (condition always) as the reset vector, on a whole number of words
    if len(image) < 4096 or len(image) % 4 or image[3] != 0xea:
        raise ValueError('The GPU firmware does not start with a reset vector')
    return image


def extract_g5p(blob: bytes) -> bytes:
    kernel = Kernelcache(blob)
    section = named(kernel.kext_sections(G5P, 'G5P firmware'), CONST)[CONST]
    if section['size'] != G5P_SIZE:
        raise ValueError('Unsupported G5P C0 firmware size')
    data = kernel.read(section['addr'], section['size'])
    if hashlib.sha256(data).hexdigest() != G5P_SHA256:
        raise ValueError('Unsupported G5P C0 firmware digest')
    return data


LDR_MAGIC = 0x01AA5500
G4P_BORDER_SIZE = 0x800
G4P_BORDER_MARK = (0x440, 0x02040000)  # the table's first nonzero word and its offset


def ldr_length(blob: bytes) -> int:
    """The length of the META LDR at the start of BLOB: the end of its last L1 record or L2 block."""
    if len(blob) < 16:
        raise ValueError('Truncated LDR header')
    magic, code, pos, _, _ = struct.unpack_from('<IIIHH', blob)
    if magic != LDR_MAGIC:
        raise ValueError('Not a META LDR')
    end, seen = max(16, code), set()
    while pos != 0xFFFFFFFF:
        if pos in seen or pos + 8 > len(blob):
            raise ValueError('Bad LDR record list')
        seen.add(pos)
        command, size, following = struct.unpack_from('<HHI', blob, pos)
        if size < 12 or size % 4 or pos + size > len(blob):
            raise ValueError('Bad LDR L1 record')
        words = struct.unpack_from('<%dI' % ((size - 12) // 4), blob, pos + 8)
        if command & 0x10 or command in (3, 5):
            index = 0
        elif command in (0, 4):
            index = 1 if command == 0 else 2
        else:
            raise ValueError('Unknown LDR L1 command %#x' % command)
        if len(words) <= index:
            raise ValueError('Bad LDR L1 record')
        block = words[index]
        if block + 4 > len(blob):
            raise ValueError('Bad LDR L2 offset')
        block_size = struct.unpack_from('<H', blob, block + 2)[0]
        if block_size < 6 or block + block_size > len(blob):
            raise ValueError('Bad LDR L2 block')
        end = max(end, pos + size, block + block_size)
        pos = following
    return end


def _pick(candidates, expected, what):
    """The candidate whose SHA-256 the recipe pins; without a pin, the only candidate."""
    if expected is None:
        if len(set(candidates)) != 1:
            raise ValueError(f'{len(set(candidates))} {what} candidates and no pinned hash to choose one')
        return candidates[0]
    for candidate in candidates:
        if hashlib.sha256(candidate).hexdigest() == expected:
            return candidate
    raise ValueError(f'Pinned {what} not found in the kernelcache')


def extract_g4p_ldr(blob: bytes, expected) -> bytes:
    """The A8 GPU firmware (AGXFirmwareKextG4P's META LDR), byte for byte."""
    magic, candidates = struct.pack('<I', LDR_MAGIC), []
    at = blob.find(magic)
    while at >= 0:
        try:
            candidates.append(blob[at:at + ldr_length(blob[at:])])
        except ValueError:
            pass
        at = blob.find(magic, at + 4)
    return _pick(candidates, expected, 'G4P firmware')


def extract_g4p_border(blob: bytes, expected) -> bytes:
    """The A8 GPU's border colour table: 0x800 bytes of AGXG4P's data."""
    offset, word = G4P_BORDER_MARK
    needle, candidates = struct.pack('<I', word), []
    at = blob.find(needle)
    while at >= 0:
        start = at - offset
        if start >= 0 and start % 4 == 0 and start + G4P_BORDER_SIZE <= len(blob) and \
                not any(blob[start:at]):
            candidates.append(blob[start:start + G4P_BORDER_SIZE])
        at = blob.find(needle, at + 1)
    return _pick(candidates, expected, 'G4P border colour table')
