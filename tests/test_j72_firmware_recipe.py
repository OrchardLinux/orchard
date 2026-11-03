"""The console-only J72 recipe does not fetch an unqualified touch output."""
from contextlib import nullcontext
import json
from pathlib import Path
import struct
import tempfile
import unittest
from unittest.mock import Mock, patch
import zlib

from orchard import firmware, recipe
from orchard.apple import apfs

ROOT = Path(__file__).resolve().parents[1]


class J72FirmwareRecipe(unittest.TestCase):
    def test_console_firmware_needs_no_download(self):
        description = recipe.check(json.loads((ROOT / 'devices/j72/firmware.json').read_text()))
        self.assertEqual(recipe.needed_inputs(description), [])
        self.assertEqual(recipe.run(description, Mock(side_effect=AssertionError('unexpected input'))), {})
        with tempfile.TemporaryDirectory() as directory, \
                patch.object(firmware, 'recipe_for', return_value=description), \
                patch.object(firmware, 'cache_root', return_value=Path(directory)), \
                patch('orchard.apple.remote_ipsw.Ipsw', side_effect=AssertionError('unexpected download')):
            clock, progress = Mock(), Mock()
            clock.exclude.return_value = nullcontext()
            output = firmware.ensure('j72', clock, progress, allow_download=False)
            self.assertTrue(output.is_dir())
            self.assertEqual(list(output.iterdir()), [])
            progress.phase.assert_called_once_with('Verified cached firmware')

    def test_existing_type4_resource_decoder_handles_compressed_and_stored_blocks(self):
        # Entirely synthetic, with the same two-block storage form as the input.
        first, second = b'a' * 65536, b'synthetic logical file tail'
        pieces = [zlib.compress(first), b'\xff' + second]
        base = 0x104
        table = bytearray(struct.pack('<I', len(pieces)))
        offset = 4 + 8 * len(pieces)
        for piece in pieces:
            table.extend(struct.pack('<II', offset, len(piece)))
            offset += len(piece)
        fork = struct.pack('>I', 0x100).ljust(base, b'\0') + table + b''.join(pieces)
        self.assertEqual(apfs._resource_fork(4, fork, len(first + second)), first + second)
        with self.assertRaisesRegex(ValueError, 'size mismatch'):
            apfs._resource_fork(4, fork, len(first + second) + 1)


if __name__ == '__main__':
    unittest.main()
