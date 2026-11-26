"""The image a 32-bit iBoot starts as its kernel is made from synthetic inputs, with no device access."""
import importlib.util
import struct
import unittest

from orchard.apple import lzss
from orchard.hardware import ROOT

spec = importlib.util.spec_from_file_location('m1n1_armv7', ROOT / 'producers/m1n1_armv7.py')
producer = importlib.util.module_from_spec(spec)
spec.loader.exec_module(producer)


def macho(text=b'\xfe' * 0x3000):
    """A 32-bit ARM Mach-O shaped as m1n1-armv7.ld lays it out: an empty __LAST at the end of the file."""
    def segment(name, vmaddr, vmsize, fileoff, filesize):
        return struct.pack('<II16s8I', 1, 56, name, vmaddr, vmsize, fileoff, filesize, 7, 5, 0, 0)
    size = 0x1000 + len(text)
    commands = (segment(b'__TEXT', 0x80002000, len(text), 0x1000, len(text)) +
                segment(b'__LAST', 0x80001000 + size + 0x30000, 0, size, 0))
    header = struct.pack('<7I', 0xfeedface, 12, 9, 2, 2, len(commands), 1) + commands
    return header.ljust(0x1000, b'\0') + text


def zimage(size=0x200):
    data = bytearray(size)
    struct.pack_into('<III', data, 0x24, 0x016f2818, 0, size)
    return bytes(data)


def dtb(size=0x80):
    return b'\xd0\x0d\xfe\xed' + struct.pack('>I', size) + bytes(size - 8)


def unwrap(image, kind):
    """The DATA of an IMG3, after checking the container as iBoot reads it."""
    magic, total, tags_size, signed, ident = struct.unpack_from('<4sIII4s', image, 0)
    assert magic == b'3gmI' and total == len(image) and tags_size == signed == total - 20
    assert ident[::-1] == kind.encode()
    at, found = 20, {}
    while at < total:
        tag, size, used = struct.unpack_from('<4sII', image, at)
        assert size >= 12 and size % 4 == 0 and used <= size - 12
        found[tag[::-1]] = image[at + 12:at + 12 + used]
        at += size
    assert at == total and found[b'TYPE'][::-1] == kind.encode()
    return found[b'DATA']


class ImageTests(unittest.TestCase):
    def test_a_ram_boot_appends_the_payload_to_m1n1(self):
        kernel, tree, initramfs = zimage(), dtb(), b'\x1f\x8b' + bytes(301)
        image, ramdisk, receipt = producer.build(macho(), bootargs='console=tty0', dtbs=[tree],
                                                 kernel=kernel, initramfs=initramfs)
        self.assertIsNone(ramdisk)
        packed = unwrap(image, 'krnl')
        self.assertEqual(packed[:8], b'complzss')
        loaded = bytes(lzss.decompress(packed))
        self.assertEqual(len(loaded), receipt['macho_bytes'])
        base = macho()
        self.assertEqual(loaded[:28], base[:28])
        at = producer.last_segment(base)
        vmaddr, vmsize, fileoff, filesize = struct.unpack_from('<4I', loaded, at + 24)
        payload = loaded[len(base):]
        # iBoot loads exactly the payload, page rounded, where m1n1 looks for it
        self.assertEqual((fileoff, filesize), (len(base), len(payload)))
        self.assertEqual(vmsize, (len(payload) + 4095) & -4096)
        self.assertEqual(payload, b'chosen.bootargs=console=tty0\n' + tree + kernel +
                         b'm1n1_initramfs' + struct.pack('<I', len(initramfs)) + initramfs + bytes(8))

    def test_a_large_payload_can_travel_as_the_ramdisk(self):
        kernel, tree = zimage(0x202), dtb()
        image, ramdisk, receipt = producer.build(macho(), bootargs='quiet', dtbs=[tree], kernel=kernel,
                                                 split=True)
        loaded = bytes(lzss.decompress(unwrap(image, 'krnl')))
        self.assertEqual(loaded[len(macho()):], b'chosen.bootargs=quiet\n' + bytes(8))
        # the kernel is padded so that what follows it stays word aligned
        self.assertEqual(unwrap(ramdisk, 'rdsk'), tree + kernel + bytes(2) + bytes(8))
        self.assertEqual(receipt['ramdisk_image']['bytes'], len(ramdisk))

    def test_m1n1_alone_waits_for_the_host_or_starts_the_flash(self):
        image, _, _ = producer.build(macho())
        self.assertEqual(bytes(lzss.decompress(unwrap(image, 'krnl')))[len(macho()):], bytes(8))
        image, _, _ = producer.build(macho(), autoboot=True)
        self.assertEqual(bytes(lzss.decompress(unwrap(image, 'krnl')))[len(macho()):], b'autoboot=1\n' + bytes(8))

    def test_every_length_survives_the_stored_lzss_stream(self):
        for size in (0, 1, 7, 8, 9, 4095, 4096, 70001):
            data = bytes((index * 7 + size) & 0xff for index in range(size))
            self.assertEqual(bytes(lzss.decompress(producer.lzss_stored(data))), data, size)

    def test_wrong_inputs_are_refused(self):
        good = macho()
        cases = [
            (dict(kernel=zimage()), 'device tree'),
            (dict(dtbs=[dtb()], kernel=b'\0' * 0x40), 'zImage'),
            (dict(dtbs=[dtb()], kernel=zimage() + dtb()), 'appended'),
            (dict(dtbs=[b'not a tree' * 8], kernel=zimage()), 'flattened'),
            (dict(initramfs=b'x'), 'for a kernel'),
            (dict(autoboot=True, dtbs=[dtb()], kernel=zimage()), 'autoboot'),
            (dict(split=True), 'ramdisk'),
            (dict(chosen=[('boot=args', 'x')]), 'Unusable'),
        ]
        for arguments, message in cases:
            with self.assertRaisesRegex(ValueError, message):
                producer.build(good, **arguments)
        with self.assertRaisesRegex(ValueError, '32-bit ARM Mach-O'):
            producer.build(b'\xcf\xfa\xed\xfe' + bytes(64))
        carried, _, _ = producer.build(good)
        with self.assertRaisesRegex(ValueError, 'already carries'):
            producer.build(bytes(lzss.decompress(unwrap(carried, 'krnl'))))


if __name__ == '__main__':
    unittest.main()
