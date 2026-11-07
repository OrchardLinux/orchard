"""Seek in a DEFLATE member with a compressed prefix and stored blocks.

Encrypted legacy DMGs are effectively incompressible. ZIP's DEFLATE encoder
emits stored blocks after compressing the DMG header. Index their five-byte
headers, not their payloads. Block lengths are NOT a fixed 16 KiB stride.
Multipart requests prefetch small windows around predicted header locations;
the prediction is only an I/O optimisation. Every actual boundary is followed
from the preceding block's validated length, with a fresh fetch on a miss.
Other DEFLATE layouts fail closed, never falling back to a full DMG download.
"""
import bisect
import math
import struct
import zlib


class Bits:
    def __init__(self, source):
        self.source, self.bit = source, 0
        self.base, self.buffer = 0, b''

    def get(self, count):
        value = 0
        for i in range(count):
            pos = self.bit // 8
            if pos >= 1 << 20:
                raise ValueError('DEFLATE prefix exceeds 1 MiB')
            if not self.base <= pos < self.base + len(self.buffer):
                self.base = pos
                self.buffer = self.source.read(pos, min(4096, self.source.size - pos))
            if not self.buffer:
                raise ValueError('Truncated DEFLATE prefix')
            value |= ((self.buffer[pos - self.base] >> (self.bit % 8)) & 1) << i
            self.bit += 1
        return value


def huffman(lengths):
    counts = [lengths.count(n) for n in range(16)]
    counts[0] = 0
    code, next_code, table = 0, {}, {}
    for n in range(1, 16):
        code = (code + counts[n - 1]) << 1
        if code + counts[n] > 1 << n:
            raise ValueError('Oversubscribed DEFLATE Huffman tree')
        next_code[n] = code
    for symbol, n in enumerate(lengths):
        if n:
            code = next_code[n]
            reverse = int(f'{code:0{n}b}'[::-1], 2)
            table[n, reverse] = symbol
            next_code[n] += 1

    def decode(bits):
        code = 0
        for n in range(1, 16):
            code |= bits.get(1) << (n - 1)
            if (n, code) in table:
                return table[n, code]
        raise ValueError('Invalid DEFLATE Huffman code')
    return decode


def first_stored(source):
    bits = Bits(source)
    while True:
        final, kind = bits.get(1), bits.get(2)
        if kind == 0:
            return (bits.bit + 7) // 8 - 1, final
        if kind == 3:
            raise ValueError('Reserved DEFLATE block type')
        if kind == 1:
            literal = huffman([8] * 144 + [9] * 112 + [7] * 24 + [8] * 8)
            distance = huffman([5] * 32)
        else:
            nl, nd, nc = bits.get(5) + 257, bits.get(5) + 1, bits.get(4) + 4
            if nl > 286:
                raise ValueError('Invalid DEFLATE literal count')
            order = (16, 17, 18, 0, 8, 7, 9, 6, 10, 5, 11, 4, 12, 3, 13, 2, 14, 1, 15)
            lengths = [0] * 19
            for i in range(nc):
                lengths[order[i]] = bits.get(3)
            decode = huffman(lengths)
            lengths = []
            while len(lengths) < nl + nd:
                symbol = decode(bits)
                if symbol < 16:
                    lengths.append(symbol)
                elif symbol == 16:
                    if not lengths:
                        raise ValueError('DEFLATE repeat without a predecessor')
                    lengths.extend([lengths[-1]] * (bits.get(2) + 3))
                elif symbol == 17:
                    lengths.extend([0] * (bits.get(3) + 3))
                else:
                    lengths.extend([0] * (bits.get(7) + 11))
            if len(lengths) != nl + nd or not lengths[256]:
                raise ValueError('Invalid DEFLATE code lengths')
            literal, distance = huffman(lengths[:nl]), huffman(lengths[nl:])
        while True:
            symbol = literal(bits)
            if symbol == 256:
                break
            if symbol > 256:
                if symbol > 285:
                    raise ValueError('Invalid DEFLATE length code')
                bits.get(0 if symbol < 265 or symbol == 285 else (symbol - 261) // 4)
                dist = distance(bits)
                if dist > 29:
                    raise ValueError('Invalid DEFLATE distance code')
                bits.get(0 if dist < 4 else dist // 2 - 1)
        if final:
            raise ValueError('DMG is fully compressed; sparse access is unsupported')


class StoredDeflate:
    def __init__(self, source, size):
        self.source, self.size = source, size
        at, final = first_stored(source)
        # The first stored header can share a byte with the preceding block;
        # its type bits were checked by first_stored, not by the byte's low bits.
        length, complement = struct.unpack('<HH', source.read(at + 1, 4))
        if length ^ complement != 0xffff:
            raise ValueError('Invalid first stored DEFLATE block')
        inflater = zlib.decompressobj(-15)
        self.prefix = inflater.decompress(source.read(0, at + 5), 16 << 20)
        if inflater.unconsumed_tail or len(self.prefix) >= 16 << 20:
            raise ValueError('DEFLATE prefix expands beyond 16 MiB')
        self.blocks = []
        self.starts = []
        self.end = len(self.prefix)
        self.finished = False
        self.windows, self.window_starts = [], []
        self._append(at, length, final)

    def _append(self, at, length, final):
        if at + 5 + length > self.source.size or self.end + length > self.size:
            raise ValueError('Stored DEFLATE block exceeds member')
        if length:
            self.starts.append(self.end)
            self.blocks.append((at + 5, length))
        self.end += length
        self.next = at + 5 + length
        self.finished = bool(final)
        if final and (self.end != self.size or self.next != self.source.size):
            raise ValueError('DEFLATE member size differs')

    def _header(self):
        if self.windows:
            i = bisect.bisect_right(self.window_starts, self.next) - 1
            if i >= 0:
                start, data = self.windows[i]
                at = self.next - start
                if at + 5 <= len(data):
                    return data[at:at + 5]
        if hasattr(self.source, 'read_ranges') and self.blocks:
            # Predict from recent measured lengths; never treat the prediction
            # as an offset mapping. A window miss simply starts another batch.
            recent = self.blocks[-32:]
            # A short flush block or a long outlier must not poison 32 batches.
            stride = sorted(n + 5 for _, n in recent)[len(recent) // 2]
            ranges = [(self.next, 5)]
            previous_end = self.next + 5
            for i in range(1, 128):
                radius = 32 + 8 * math.isqrt(i)
                middle = self.next + round(i * stride)
                start = max(previous_end, middle - radius)
                end = min(self.source.size, middle + radius + 5)
                if start + 5 > end:
                    break
                ranges.append((start, end - start))
                previous_end = end
            if len(ranges) > 1:
                data = self.source.read_ranges(ranges)
                self.windows = [(start, part) for (start, _), part in zip(ranges, data)]
                self.window_starts = [start for start, _ in self.windows]
                return data[0]
        return self.source.read(self.next, 5)

    def read(self, offset, length):
        if not 0 <= offset <= offset + length <= self.size:
            raise ValueError('Read outside DEFLATE member')
        if not length:
            return b''
        stop = offset + length
        while self.end < stop:
            if self.finished:
                raise ValueError('Truncated DEFLATE member')
            raw = self._header()
            kind, size, complement = struct.unpack('<BHH', raw)
            if kind & 6 or size ^ complement != 0xffff:
                raise ValueError('Non-stored block in sparse DEFLATE tail')
            self._append(self.next, size, kind & 1)
        out = bytearray(self.prefix[offset:min(stop, len(self.prefix))])
        offset += len(out)
        while offset < stop:
            i = bisect.bisect_right(self.starts, offset) - 1
            base, size = self.blocks[i]
            within = offset - self.starts[i]
            take = min(stop - offset, size - within)
            out += self.source.read(base + within, take)
            offset += take
        return bytes(out)
