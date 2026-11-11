"""Extract the A10 GPU firmware from a legacy prelinked kernelcache.

AGXFirmwareKextG9PRTBuddy keeps the image in __TEXT.__const: its build
variant as a C string ("Release"), then the image to the end of the section,
an ARM32 binary loaded at address 0 whose first word is the reset vector's
branch. The kext's code reaches it with PC-relative addresses, so there is no
pointer record to follow; the section layout is checked instead, and the
caller pins the image's SHA-256.
"""
import re

from orchard.apple.prelink import Kernelcache, named

G9P = 'com.apple.AGXFirmwareKextG9PRTBuddy'
CONST = ('__TEXT', '__const')
VARIANT = re.compile(rb'\0*[\x20-\x7e]+\0')


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
