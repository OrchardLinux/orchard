"""Broadcom USB TRX + NVRAM, after rzxvx's Project Cascadia trx-add-nvram.py."""
import struct
import zlib

# The address public Wi-Fi firmware carries in place of a unit's own: locally
# administered, all zero otherwise, so it is plainly not a real address. At
# boot, the platform layer replaces it on every interface that reports it
# (system/defaults/usr/lib/udev/rules.d/70-orchard-wifi-address.rules).
PLACEHOLDER_MAC = '02:00:00:00:00:00'


def add_nvram(firmware, text, placeholder_mac):
    if placeholder_mac != PLACEHOLDER_MAC:
        raise ValueError(f'Wi-Fi firmware must carry the placeholder address {PLACEHOLDER_MAC}, '
                         'which the platform layer replaces at boot')
    if len(firmware) < 28:
        raise ValueError('Truncated TRX')
    magic, size, crc, flags, code_size, offset1, old_size = struct.unpack_from('<4s6I', firmware)
    # The header's length covers more than firmware and NVRAM in Apple's
    # images; like Cascadia, take offsets[0] bytes of firmware and check only
    # the NVRAM slot offsets[2] describes.
    if (magic != b'HDR0' or not 28 <= size <= len(firmware) or
            28 + code_size + old_size > len(firmware)):
        raise ValueError('Invalid TRX header or lengths')
    if zlib.crc32(firmware[12:size]) ^ 0xffffffff != crc:
        raise ValueError('TRX checksum differs')
    if any(firmware[28 + code_size:28 + code_size + old_size]):
        raise ValueError('TRX already contains nonzero NVRAM')
    lines = []
    for raw in text.decode('ascii').splitlines():
        line = raw.split('#', 1)[0].strip()
        if not line or '=' not in line:
            continue
        key, value = (part.strip() for part in line.split('=', 1))
        if '\0' in line or not key:
            raise ValueError('Invalid NVRAM entry')
        if key == 'macaddr':
            raise ValueError('Recipe NVRAM must not carry a device MAC')
        lines.append(key + '=' + value)
    lines.append('macaddr=' + placeholder_mac)
    body = ('\0'.join(lines) + '\0').encode('ascii')
    body = body.ljust((len(body) + 4) & ~3, b'\0')
    words = len(body) // 4
    if words > 0xffff:
        raise ValueError('NVRAM is too large')
    nvram = body + struct.pack('<I', ((~words & 0xffff) << 16) | words)
    image = bytearray(firmware[:28 + code_size] + nvram)
    struct.pack_into('<I', image, 4, len(image))
    struct.pack_into('<I', image, 24, len(nvram))
    struct.pack_into('<I', image, 8, zlib.crc32(image[12:]) ^ 0xffffffff)
    return bytes(image)
