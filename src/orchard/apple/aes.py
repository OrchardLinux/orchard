"""AES-CBC decryption (FIPS-197), standard library only.

Old Apple firmware wraps its images in AES: IMG3 payloads in AES-128/192/256
CBC, FileVault-style root filesystem images in AES-128 CBC per chunk. This is
the textbook table-driven inverse cipher; it is slow next to a native library
(a few MB/s) but needs nothing installed, and the firmware it reads is tens
of MB at most.
"""
import struct


def _tables():
    sbox, inv = [0] * 256, [0] * 256
    p = q = 1
    while True:  # walk the multiplicative group with generator 3
        p ^= ((p << 1) ^ (0x1B if p & 0x80 else 0)) & 0xFF
        q ^= q << 1
        q ^= q << 2
        q ^= q << 4
        q &= 0xFF
        if q & 0x80:
            q ^= 0x09
        s = q ^ ((q << 1 | q >> 7) & 0xFF) ^ ((q << 2 | q >> 6) & 0xFF) ^ \
            ((q << 3 | q >> 5) & 0xFF) ^ ((q << 4 | q >> 4) & 0xFF) ^ 0x63
        sbox[p], inv[s] = s, p
        if p == 1:
            break
    sbox[0], inv[0x63] = 0x63, 0

    def mul(a, b):
        r = 0
        while b:
            if b & 1:
                r ^= a
            a = ((a << 1) ^ (0x1B if a & 0x80 else 0)) & 0xFF
            b >>= 1
        return r

    td0 = [mul(s, 14) << 24 | mul(s, 9) << 16 | mul(s, 13) << 8 | mul(s, 11) for s in inv]
    rot = [[(t >> (8 * k) | t << (32 - 8 * k)) & 0xFFFFFFFF for t in td0] for k in (1, 2, 3)]
    # InvMixColumns of one word, for the decryption key schedule.
    imc = lambda w: (td0[sbox[w >> 24]] ^ rot[0][sbox[w >> 16 & 0xFF]] ^
                     rot[1][sbox[w >> 8 & 0xFF]] ^ rot[2][sbox[w & 0xFF]])
    return sbox, inv, td0, rot[0], rot[1], rot[2], imc


SBOX, INV_SBOX, TD0, TD1, TD2, TD3, _INV_MIX = _tables()


class Aes:
    def __init__(self, key):
        if len(key) not in (16, 24, 32):
            raise ValueError('AES key must be 16, 24 or 32 bytes')
        nk = len(key) // 4
        self.rounds = nk + 6
        w = list(struct.unpack(f'>{nk}I', key))
        rcon = 1
        for i in range(nk, 4 * (self.rounds + 1)):
            t = w[i - 1]
            if i % nk == 0:
                t = ((SBOX[t >> 16 & 0xFF] << 24 | SBOX[t >> 8 & 0xFF] << 16 |
                      SBOX[t & 0xFF] << 8 | SBOX[t >> 24]) ^ (rcon << 24))
                rcon = ((rcon << 1) ^ (0x1B if rcon & 0x80 else 0)) & 0xFF
            elif nk > 6 and i % nk == 4:
                t = (SBOX[t >> 24] << 24 | SBOX[t >> 16 & 0xFF] << 16 |
                     SBOX[t >> 8 & 0xFF] << 8 | SBOX[t & 0xFF])
            w.append(w[i - nk] ^ t)
        # Equivalent inverse cipher: round keys reversed, inner ones InvMixColumn'ed.
        keys = [w[4 * r:4 * r + 4] for r in range(self.rounds, -1, -1)]
        self.keys = [keys[0]] + [[_INV_MIX(x) for x in k] for k in keys[1:-1]] + [keys[-1]]

    def decrypt_block(self, s0, s1, s2, s3):
        k = self.keys[0]
        s0, s1, s2, s3 = s0 ^ k[0], s1 ^ k[1], s2 ^ k[2], s3 ^ k[3]
        t0, t1, t2, t3 = TD0, TD1, TD2, TD3
        for k in self.keys[1:-1]:
            s0, s1, s2, s3 = (
                t0[s0 >> 24] ^ t1[s3 >> 16 & 0xFF] ^ t2[s2 >> 8 & 0xFF] ^ t3[s1 & 0xFF] ^ k[0],
                t0[s1 >> 24] ^ t1[s0 >> 16 & 0xFF] ^ t2[s3 >> 8 & 0xFF] ^ t3[s2 & 0xFF] ^ k[1],
                t0[s2 >> 24] ^ t1[s1 >> 16 & 0xFF] ^ t2[s0 >> 8 & 0xFF] ^ t3[s3 & 0xFF] ^ k[2],
                t0[s3 >> 24] ^ t1[s2 >> 16 & 0xFF] ^ t2[s1 >> 8 & 0xFF] ^ t3[s0 & 0xFF] ^ k[3])
        k, inv = self.keys[-1], INV_SBOX
        return (
            (inv[s0 >> 24] << 24 | inv[s3 >> 16 & 0xFF] << 16 | inv[s2 >> 8 & 0xFF] << 8 | inv[s1 & 0xFF]) ^ k[0],
            (inv[s1 >> 24] << 24 | inv[s0 >> 16 & 0xFF] << 16 | inv[s3 >> 8 & 0xFF] << 8 | inv[s2 & 0xFF]) ^ k[1],
            (inv[s2 >> 24] << 24 | inv[s1 >> 16 & 0xFF] << 16 | inv[s0 >> 8 & 0xFF] << 8 | inv[s3 & 0xFF]) ^ k[2],
            (inv[s3 >> 24] << 24 | inv[s2 >> 16 & 0xFF] << 16 | inv[s1 >> 8 & 0xFF] << 8 | inv[s0 & 0xFF]) ^ k[3])

    def decrypt_cbc(self, data, iv):
        """Whole 16-byte blocks of data, CBC-chained from iv."""
        if len(data) % 16 or len(iv) != 16:
            raise ValueError('AES-CBC needs whole blocks and a 16-byte IV')
        words = struct.unpack(f'>{len(data) // 4}I', data)
        p0, p1, p2, p3 = struct.unpack('>4I', iv)
        out = [0] * len(words)
        block = self.decrypt_block
        for i in range(0, len(words), 4):
            c0, c1, c2, c3 = words[i:i + 4]
            d0, d1, d2, d3 = block(c0, c1, c2, c3)
            out[i:i + 4] = d0 ^ p0, d1 ^ p1, d2 ^ p2, d3 ^ p3
            p0, p1, p2, p3 = c0, c1, c2, c3
        return struct.pack(f'>{len(out)}I', *out)


def decrypt_cbc(key, iv, data):
    return Aes(key).decrypt_cbc(data, iv)
