"""Synthetic binary-format regression vectors; no firmware or device data."""
import struct
import unittest

from orchard.apple.adt_tree import parse, u32s


def property_record(name, value):
    return name.encode().ljust(32, b'\0') + struct.pack('<I', len(value)) + value + b'\0' * (-len(value) % 4)


class BinaryCodecs(unittest.TestCase):
    def test_adt_tree_names_properties_and_children(self):
        child = struct.pack('<II', 1, 0) + property_record('name', b'child\0')
        root = (struct.pack('<II', 2, 1) + property_record('name', b'device-tree\0') +
                property_record('words', struct.pack('<II', 1, 0xffffffff)) + child)
        tree = parse(root)
        self.assertEqual([path for path, _ in tree.walk()], ['/device-tree', '/device-tree/child'])
        self.assertEqual(u32s(tree.properties['words']), [1, 0xffffffff])

    def test_adt_rejects_duplicate_truncated_and_excessive_properties(self):
        prop = property_record('name', b'root\0')
        for blob in (b'', struct.pack('<II', 4097, 0),
                     struct.pack('<II', 1, 0) + prop[:-1],
                     struct.pack('<II', 2, 0) + prop + prop):
            with self.subTest(blob=blob[:8]), self.assertRaises(ValueError):
                parse(blob)
        with self.assertRaises(ValueError):
            u32s(b'abc')


if __name__ == '__main__':
    unittest.main()
