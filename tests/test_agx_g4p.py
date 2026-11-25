"""A8 (G4P) GPU firmware steps and the A8 boards' recipes. Synthetic bytes only."""
import hashlib
import json
from pathlib import Path
import struct
import unittest
from unittest.mock import patch

from orchard import firmware, recipe
from orchard.apple import agx

ROOT = Path(__file__).resolve().parents[1]
A8_BOARDS = ('n102', 'n56', 'n61', 'j96', 'j97')
LDR = 'lib/firmware/apple/gfx-t7000.bin'
BORDER = 'lib/firmware/apple/gfx-t7000-border.bin'


def sha(data):
    return hashlib.sha256(data).hexdigest()


def ldr(fill):
    """A META LDR: header, one L1 record (command 0) and its L2 block of FILL bytes."""
    header = struct.pack('<IIIHH', agx.LDR_MAGIC, 16, 16, 0, 0)
    record = struct.pack('<HHI', 0, 20, 0xFFFFFFFF) + struct.pack('<III', 0, 36, 0)  # 20 bytes, L2 at 36
    block = struct.pack('<HH', 0x8000, 4 + len(fill)) + fill
    return header + record + block


def border(seed):
    offset, word = agx.G4P_BORDER_MARK
    rest = bytes((seed + i) % 251 + 1 for i in range(agx.G4P_BORDER_SIZE - offset - 4))
    return bytes(offset) + struct.pack('<I', word) + rest


class G4PSteps(unittest.TestCase):
    def test_the_ldr_with_the_pinned_hash_is_returned_byte_for_byte(self):
        one, two = ldr(b'firmware one'), ldr(b'firmware two, longer')
        blob = b'\xff' * 7 + one + b'\x00' * 9 + two + b'tail'
        self.assertEqual(agx.extract_g4p_ldr(blob, sha(two)), two)
        self.assertEqual(agx.extract_g4p_ldr(blob, sha(one)), one)
        with self.assertRaisesRegex(ValueError, 'Pinned G4P firmware not found'):
            agx.extract_g4p_ldr(blob, '0' * 64)

    def test_without_a_pin_only_a_single_ldr_is_taken(self):
        one = ldr(b'firmware')
        self.assertEqual(agx.extract_g4p_ldr(b'junk' + one, None), one)
        with self.assertRaisesRegex(ValueError, '2 G4P firmware candidates'):
            agx.extract_g4p_ldr(one + ldr(b'other'), None)

    def test_a_broken_record_list_is_not_a_candidate(self):
        broken = bytearray(ldr(b'firmware'))
        struct.pack_into('<H', broken, 16, 0x7)  # an unknown L1 command
        with self.assertRaisesRegex(ValueError, '0 G4P firmware candidates'):
            agx.extract_g4p_ldr(bytes(broken), None)
        looped = bytearray(ldr(b'firmware'))
        struct.pack_into('<I', looped, 20, 16)  # the record points at itself
        with self.assertRaises(ValueError):
            agx.ldr_length(bytes(looped))

    def test_the_border_table_is_found_by_structure_and_hash(self):
        table = border(3)
        blob = b'\x01' * 12 + table + b'\x02' * 64
        self.assertEqual(agx.extract_g4p_border(blob, sha(table)), table)
        self.assertEqual(agx.extract_g4p_border(blob, None), table)
        other = border(9)
        with self.assertRaisesRegex(ValueError, 'Pinned G4P border colour table not found'):
            agx.extract_g4p_border(blob + other, sha(border(5)))
        self.assertEqual(agx.extract_g4p_border(blob + b'\x01' * 4 + other, sha(other)), other)

    def test_a_marker_after_nonzero_bytes_is_not_a_table(self):
        offset, word = agx.G4P_BORDER_MARK
        blob = b'\x01' * offset + struct.pack('<I', word) + bytes(agx.G4P_BORDER_SIZE)
        with self.assertRaisesRegex(ValueError, '0 G4P border colour table candidates'):
            agx.extract_g4p_border(blob, None)

    def test_the_steps_run_through_a_recipe(self):
        image, table = ldr(b'firmware'), border(1)
        blob = image + table
        document = {
            'schema': 2, 'device': 'iPod7,1',
            'ipsw': {'version': '12.5.8', 'build': '16H88', 'bytes': 1, 'sha256': '0' * 64,
                     'url': 'https://updates.cdn-apple.com/x.ipsw'},
            'inputs': {'kc': {'ipsw': {'kernelcache': True}, 'sha256': sha(blob)}},
            'outputs': [
                {'destination': LDR, 'sha256': sha(image),
                 'steps': [{'step': 'agx-g4p-ldr-from-kernelcache@1', 'input': 'kc'}]},
                {'destination': BORDER, 'sha256': sha(table),
                 'steps': [{'step': 'agx-g4p-border-from-kernelcache@1', 'input': 'kc'}]},
            ],
        }
        self.assertEqual(recipe.run(recipe.check(document), lambda name: blob), {LDR: image, BORDER: table})


class RestoreInputs(unittest.TestCase):
    RESTORE = {'version': '12.4', 'build': '16G77', 'bytes': 7, 'sha256': '1' * 64,
               'url': 'https://updates.cdn-apple.com/other.ipsw'}

    def document(self, data):
        return {
            'schema': 2, 'device': 'iPad5,1',
            'ipsw': {'version': '15.8.8', 'build': '19H422', 'bytes': 5, 'sha256': '0' * 64,
                     'url': 'https://updates.cdn-apple.com/own.ipsw'},
            'inputs': {
                'own': {'ipsw': {'kernelcache': True}, 'sha256': sha(b'own kernelcache')},
                'other': {'ipsw': {'kernelcache': 'n102ap', 'restore': self.RESTORE}, 'sha256': sha(data)},
            },
            'outputs': [{'destination': 'lib/firmware/a', 'sha256': sha(data),
                         'steps': [{'step': 'copy@1', 'input': 'other'}]},
                        {'destination': 'lib/firmware/b', 'sha256': sha(b'own kernelcache'),
                         'steps': [{'step': 'copy@1', 'input': 'own'}]}],
        }

    def test_an_input_from_another_restore_image_must_be_pinned(self):
        document = self.document(b'kc')
        recipe.check(document)
        del document['inputs']['other']['sha256']
        with self.assertRaisesRegex(ValueError, 'needs its sha256 and board'):
            recipe.supported(document)

    def test_each_input_reads_its_own_restore_image_and_board(self):
        opened = []

        class Ipsw:
            def __init__(self, url, size, on_bytes=None, keys=None):
                opened.append((url, size))
                self.url = url

            def kernelcache(self, board):
                return b'kc' if (self.url, board) == (RestoreInputs.RESTORE['url'], 'n102ap') else \
                    b'own kernelcache' if board == 'j96ap' else b'wrong'

        class Progress:
            def task(self, name):
                return self

            def phase(self, text):
                pass

            def reset_transfer(self):
                pass

        class Clock:
            def exclude(self, kind):
                from contextlib import nullcontext
                return nullcontext()

        document = self.document(b'kc')
        with patch('orchard.apple.remote_ipsw.Ipsw', Ipsw):
            result = firmware._produce('j96', document, document['outputs'], Clock(), Progress())
        self.assertEqual(result, {'lib/firmware/a': b'kc', 'lib/firmware/b': b'own kernelcache'})
        self.assertEqual(sorted(opened), [('https://updates.cdn-apple.com/other.ipsw', 7),
                                          ('https://updates.cdn-apple.com/own.ipsw', 5)])


class A8Recipes(unittest.TestCase):
    # The LDR of each board's source image: the iOS 12 boards' own; iPadOS 15's build is untested, so the
    # iPad mini 4 takes iOS 12.4's (16G77) from the iPod touch 6.
    LDRS = {'16H88': '5f99a8ab88ddd34f8f32c11adb21f6b2410bece3d23a9aee560ca80fe8e45f55',
            '16H22': '36bfc905aaf41baac1748849942c4b5f796b1d293e8171045ee45cf253150c68',
            '16G77': '0b60def67290391b4c6aa913d9a78a8105d6d9b03738175b8e5732689db1e5ba'}
    BORDER_SHA256 = '233ffc1d7db27d9f16db17e23a9ce33082b5a7b72061af3da510bca3c94aff3e'
    OWN_IMAGE = ('n102', 'n56', 'n61')

    def test_every_a8_board_installs_the_gpu_firmware_of_its_source_image(self):
        for board in A8_BOARDS:
            with self.subTest(board=board):
                document = recipe.check(json.loads((ROOT / f'devices/{board}/firmware.json').read_text()))
                outputs = {o['destination']: o for o in document['outputs']}
                steps = {LDR: 'agx-g4p-ldr-from-kernelcache@1', BORDER: 'agx-g4p-border-from-kernelcache@1'}
                for destination, step in steps.items():
                    output = outputs[destination]
                    name = output['steps'][0]['input']
                    self.assertEqual(output['steps'], [{'step': step, 'input': name}])
                    location = document['inputs'][name]['ipsw']
                    if board in self.OWN_IMAGE:
                        self.assertEqual(location, {'kernelcache': True})
                        build = document['ipsw']['build']
                    else:
                        self.assertEqual(location['kernelcache'], 'n102ap')
                        build = location['restore']['build']
                    expected = self.LDRS[build] if destination == LDR else self.BORDER_SHA256
                    self.assertEqual(output['sha256'], expected)


if __name__ == '__main__':
    unittest.main()
