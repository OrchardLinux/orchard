"""m1n1 proxy transport: framing checksums and the boot manager reply."""
import unittest

from orchard.transports import m1n1


class Transport(unittest.TestCase):
    def test_checksum_matches_m1n1(self):
        # proxyclient's UartInterface.checksum for b'abc'
        self.assertEqual(m1n1.checksum(b'abc'), 3020375102)

    def test_boot_entries_text_from_m1n1(self):
        text = ('contract 1\nentry omarchy-4d6c-6.18\ntitle Omarchy\ninitrd /a\ninitrd /b\nroot 4d6c\n\n'
                'entry broken\nproblem needs exactly one linux\nproblem efi is not supported\n\n')
        self.assertEqual(m1n1.parse_boot_entries(text), [
            ('omarchy-4d6c-6.18', {'title': ['Omarchy'], 'initrd': ['/a', '/b'], 'root': ['4d6c']}, []),
            ('broken', {}, ['needs exactly one linux', 'efi is not supported']),
        ])
        self.assertEqual(m1n1.parse_boot_entries('contract 1\n'), [])
        with self.assertRaisesRegex(m1n1.M1n1Error, 'another boot contract: contract 2'):
            m1n1.parse_boot_entries('contract 2\n')
        with self.assertRaisesRegex(m1n1.M1n1Error, 'exactly one XBOOTLDR'):
            m1n1.parse_boot_entries('contract 1\nerror Expected exactly one XBOOTLDR boot partition, found 0\n')


if __name__ == '__main__':
    unittest.main()
