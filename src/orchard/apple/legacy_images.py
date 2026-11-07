"""Legacy encrypted image wrappers, after rzxvx / Project Cascadia.

Keys are supplied by the caller, not derived or embedded in this module.
"""
import hashlib
import hmac
import struct

from .aes import Aes
from . import lzss


class Encrcdsa:
    def __init__(self, source, key):
        header = source.read(0, 72)
        if header[:8] != b'encrcdsa' or struct.unpack_from('>I', header, 8)[0] != 2:
            raise ValueError('Unsupported encrypted DMG header')
        if len(key) != 36:
            raise ValueError('Encrypted DMG needs a 36-byte AES/HMAC key')
        self.bs, self.size, self.base = struct.unpack_from('>IQQ', header, 52)
        if (not self.bs or self.bs % 16 or self.bs > 1 << 20 or self.base < 72 or
                self.base + ((self.size + self.bs - 1) // self.bs) * self.bs > source.size):
            raise ValueError('Invalid encrypted DMG geometry')
        self.source, self.aes, self.mac = source, Aes(key[:16]), key[16:]
        self.cache = {}

    def read(self, offset, length):
        if not 0 <= offset <= offset + length <= self.size:
            raise ValueError('Read outside encrypted DMG')
        out = bytearray()
        # Fetch adjoining ciphertext chunks together, not one HTTP request per sector.
        first = offset // self.bs
        last = (offset + length + self.bs - 1) // self.bs
        if not length:
            return b''
        missing = [n for n in range(first, last) if n not in self.cache]
        if missing:
            start, stop = missing[0], missing[-1] + 1
            raw = self.source.read(self.base + start * self.bs, (stop - start) * self.bs)
            if len(self.cache) > 256:
                self.cache.clear()
            for n in range(start, stop):
                iv = hmac.new(self.mac, struct.pack('>I', n), hashlib.sha1).digest()[:16]
                at = (n - start) * self.bs
                self.cache[n] = self.aes.decrypt_cbc(raw[at:at + self.bs], iv)
        for n in range(first, last):
            # Cache clearing above must not evict the requested leading/trailing chunks.
            if n not in self.cache:
                raw = self.source.read(self.base + n * self.bs, self.bs)
                iv = hmac.new(self.mac, struct.pack('>I', n), hashlib.sha1).digest()[:16]
                self.cache[n] = self.aes.decrypt_cbc(raw, iv)
            begin = max(offset - n * self.bs, 0)
            end = min(offset + length - n * self.bs, self.bs)
            out += self.cache[n][begin:end]
        return bytes(out)


def img3_kernelcache(blob, key, iv):
    if len(blob) < 20 or blob[:4] != b'3gmI':
        raise ValueError('Not an IMG3 container')
    total, data_size = struct.unpack_from('<II', blob, 4)
    if total != len(blob) or data_size > total - 20:
        raise ValueError('Invalid IMG3 size')
    position, payloads = 20, []
    while position < total:
        if position + 12 > total:
            raise ValueError('Truncated IMG3 tag')
        tag, size, plain_size = struct.unpack_from('<4sII', blob, position)
        if size < 12 or position + size > total or plain_size > size - 12:
            raise ValueError('Invalid IMG3 tag size')
        if tag == b'ATAD':
            payloads.append((blob[position + 12:position + size], plain_size))
        position += size
    if len(payloads) != 1:
        raise ValueError('Expected one IMG3 DATA tag')
    cipher, plain_size = payloads[0]
    plain = Aes(key).decrypt_cbc(cipher, iv)[:plain_size]
    if plain[:8] != b'complzss':
        raise ValueError('IMG3 kernelcache did not decrypt to complzss')
    macho = lzss.decompress(plain)
    if macho[:4] != b'\xce\xfa\xed\xfe':
        raise ValueError('IMG3 kernelcache is not a 32-bit Mach-O')
    return macho
