# ppm2png.py: helpers for src/test.sh (Python standard library only).
#   python3 ppm2png.py png IN.ppm OUT.png    P6 PPM to PNG
#   python3 ppm2png.py diff A.ppm B.ppm      largest per-channel difference
import struct
import sys
import zlib


def read_ppm(path):
    data = open(path, 'rb').read()
    fields, i = [], 0
    while len(fields) < 4:
        while data[i:i + 1].isspace():
            i += 1
        j = i
        while not data[j:j + 1].isspace():
            j += 1
        fields.append(data[i:j])
        i = j
    w, h = int(fields[1]), int(fields[2])
    return w, h, data[i + 1:i + 1 + w * h * 3]


def write_png(path, w, h, pixels):
    raw = b''.join(b'\0' + pixels[y * w * 3:(y + 1) * w * 3] for y in range(h))

    def chunk(kind, body):
        return struct.pack('>I', len(body)) + kind + body + struct.pack('>I', zlib.crc32(kind + body) & 0xffffffff)
    with open(path, 'wb') as f:
        f.write(b'\x89PNG\r\n\x1a\n' + chunk(b'IHDR', struct.pack('>IIBBBBB', w, h, 8, 2, 0, 0, 0)) +
                chunk(b'IDAT', zlib.compress(raw, 9)) + chunk(b'IEND', b''))


if sys.argv[1] == 'png':
    write_png(sys.argv[3], *read_ppm(sys.argv[2]))
elif sys.argv[1] == 'diff':
    a, b = read_ppm(sys.argv[2]), read_ppm(sys.argv[3])
    assert a[:2] == b[:2], 'sizes differ'
    print(max(abs(x - y) for x, y in zip(a[2], b[2])))
