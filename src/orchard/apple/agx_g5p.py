"""Extract the unmodified G5P C0 META LDR from the user's prelinked kernelcache."""
import hashlib
from orchard.apple.prelink import Kernelcache, named

BUNDLE = 'com.apple.AGXFirmwareKextG5P'
CONST = ('__TEXT', '__const')
SIZE = 119112
SHA256 = '33ae20cbbe2498e4f119ba4e9a55467e4e0192d4c86d5bf1903adce63ffa5dca'


def extract(blob):
    kernel = Kernelcache(blob)
    section = named(kernel.kext_sections(BUNDLE, 'G5P firmware'), CONST)[CONST]
    if section['size'] != SIZE:
        raise ValueError('Unsupported G5P C0 firmware size')
    data = kernel.read(section['addr'], section['size'])
    if hashlib.sha256(data).hexdigest() != SHA256:
        raise ValueError('Unsupported G5P C0 firmware digest')
    return data
