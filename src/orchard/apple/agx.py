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
"""
import hashlib
import re

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
