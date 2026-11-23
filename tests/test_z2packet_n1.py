"""N1 firmware recipe contract, on synthetic bytes without Apple firmware."""
import hashlib
import plistlib
import struct
import unittest

from orchard import recipe
from orchard.apple import z2fw, z2packet


def fixture():
    payload = bytes(range(16))
    header = struct.pack('>H', len(payload) // 4) + bytes(4)
    packet = z2packet.PREFIX + header + struct.pack('>H', sum(header))
    packet += payload + z2fw.xe32(sum(payload))
    return {'C1F15,1': {'PreconstructedBootloadPacketType': 'Z2',
                       'Constructed Firmware': packet}}


def convert(document=None, **kwargs):
    params = {'profile': 'C1F15,1', 'calibration_address': 0x10009000,
              'calibration_size': 1024, 'prox_address': 0x10009600, 'prox_size': 128}
    return z2packet.convert_n1(plistlib.dumps(fixture() if document is None else document),
                                **(params | kwargs))


class N1PacketConversion(unittest.TestCase):
    def test_four_records_keep_firmware_verbatim_and_calibration_dynamic(self):
        records = z2fw.decode_container(convert())
        self.assertEqual([code for code, _ in records], [4, 1, 6, 1])
        self.assertEqual(struct.unpack('<11I', records[0][1]),
                         (0x818, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0x103))
        self.assertEqual(len(records[1][1]), 144)
        self.assertEqual(z2fw.decode_blob(records[1][1], hbpp_version=0x103),
                         (0x10009600, bytes(128)))
        with self.assertRaisesRegex(ValueError, 'length mismatch'):
            z2fw.decode_blob(records[1][1], hbpp_version=0x104)
        self.assertEqual(records[2][1], struct.pack('<III', 0x10009000, 1024, 0))
        self.assertEqual(records[3][1], fixture()['C1F15,1']['Constructed Firmware'])
        self.assertEqual(z2packet.convert(plistlib.dumps(fixture()), 'C1F15,1',
                                          0x10009000, 1024),
                         z2fw.container([(4, struct.pack('<11I', 0x18, *([0] * 10))),
                                          records[2], records[3]]))

    def test_invalid_profiles_and_wire_packets_are_refused(self):
        original = fixture()['C1F15,1']
        packet = original['Constructed Firmware']
        bad_checksum = packet[:-5] + bytes([packet[-5] ^ 1]) + packet[-4:]
        cases = [([], 'profile dictionary'), ({}, 'No preconstructed'),
                 ({'C1F15,1': []}, 'No preconstructed'),
                 ({'C1F15,1': original | {'PreconstructedBootloadPacketType': 'HBPP14'}},
                  'No preconstructed'),
                 ({'C1F15,1': original | {'Constructed Firmware': [packet]}},
                  'no constructed firmware'),
                 ({'C1F15,1': original | {'Firmware Config': {}}}, 'unsupported firmware config'),
                 ({'C1F15,1': original | {'Constructed Firmware': packet[:-1]}}, 'length mismatch'),
                 ({'C1F15,1': original | {'Constructed Firmware': bad_checksum}}, 'checksum')]
        for document, error in cases:
            with self.subTest(error=error), self.assertRaisesRegex(ValueError, error):
                convert(document)
        for offset in (0, 4, 6, 10):
            corrupt = bytearray(packet)
            corrupt[offset] ^= 1
            with self.subTest(offset=offset), self.assertRaises(ValueError):
                convert({'C1F15,1': original | {'Constructed Firmware': bytes(corrupt)}})

    def test_calibration_range_errors_refuse_before_output(self):
        for kwargs in ({'prox_size': 0}, {'prox_size': True}, {'prox_size': -4},
                       {'prox_size': 127}, {'prox_size': 0x40000},
                       {'prox_address': 0}, {'prox_address': 0x10009601},
                       {'prox_address': 0xfffffffc},
                       {'calibration_size': 0}, {'calibration_size': True},
                       {'calibration_size': 1023}, {'calibration_size': 0x40001},
                       {'calibration_size': 0x604}, {'calibration_address': 0x10009001},
                       {'calibration_address': 0x10009600}, {'calibration_address': 4}):
            with self.subTest(kwargs=kwargs), self.assertRaises(ValueError):
                convert(**kwargs)

    def test_normal_recipe_checks_both_hashes_and_exact_parameters(self):
        data, output = plistlib.dumps(fixture()), convert()
        step = {'step': 'z2fw-from-z2-packet@2', 'input': 'mtprops', 'profile': 'C1F15,1',
                'calibration_address': 0x10009000, 'calibration_size': 1024,
                'prox_address': 0x10009600, 'prox_size': 128}
        document = {'schema': 2, 'device': 'iPad4,2',
                    'ipsw': {'version': '12.5.7', 'build': '16H81', 'bytes': 1,
                             'sha256': '0' * 64, 'url': 'https://updates.cdn-apple.com/test.ipsw'},
                    'inputs': {'mtprops': {'ipsw': {'rootfs': 'test.mtprops'},
                                          'sha256': hashlib.sha256(data).hexdigest()}},
                    'outputs': [{'destination': 'lib/firmware/apple/test-n1.z2fw',
                                 'sha256': hashlib.sha256(output).hexdigest(), 'steps': [step]}]}
        recipe.check(document)
        self.assertEqual(recipe.run(document, lambda _: data),
                         {'lib/firmware/apple/test-n1.z2fw': output})
        with self.assertRaisesRegex(ValueError, 'input differs'):
            recipe.run(document, lambda _: b'tampered')
        document['outputs'][0]['sha256'] = '1' * 64
        with self.assertRaisesRegex(ValueError, 'output differs'):
            recipe.run(document, lambda _: data)
        for field in ('prox_address', 'prox_size'):
            value = step.pop(field)
            with self.assertRaisesRegex(ValueError, 'Wrong parameters'):
                recipe.check(document)
            step[field] = value
        step['prox_size'] = True
        with self.assertRaisesRegex(ValueError, 'must be int'):
            recipe.check(document)


if __name__ == '__main__':
    unittest.main()
