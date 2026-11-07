# SPDX-License-Identifier: GPL-2.0-only
"""Public templates and private provision receipts for offline iOS starts."""
import copy
import hashlib
import json
import os
from pathlib import Path
import shutil
import sys
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import patch
import zlib

from orchard import cascadia_payload as image, hardware, recovery_identity
from orchard.apple import ios_identity, ios_launcher
from test_cascadia_loader import PROFILE, kernel, loader, tree


BOOT = '11111111-1111-4111-8111-111111111111'
NONCE = '0123456789abcdef0123456789abcdef'


def template():
    described = tree()
    described['properties']['compatible'] = b'apple,p105ap\0'
    policy = {'schema': 2, 'sources': ['host-usb', 'ios-device'], 'profile': 'p105',
              'compatibles': ['apple,p105ap'], 'chip': 0x8942, 'board': 10}
    return image.assemble(loader(), kernel(), image.write_fdt(described), b'cpio', PROFILE,
                          'console=tty0 ' + recovery_identity.encode_parameter(
                              recovery_identity.POLICY_PARAMETER, policy))[0]


class FixedIdentityTemplate(unittest.TestCase):
    def setUp(self):
        self.profile = ios_launcher.boot_profile('p105')

    def test_preparation_keeps_linux_bytes_and_reserves_one_bounded_slot(self):
        original = template()
        padded, identity = ios_identity.prepare_template(original, self.profile)
        self.assertEqual((padded, identity), ios_identity.prepare_template(original, self.profile))
        self.assertEqual(identity['public_input_sha256'], image.digest(original))
        self.assertEqual(identity['template_sha256'], image.digest(padded))
        first, second = image.configuration(original), image.configuration(padded)
        self.assertEqual(original[first['kernel_offset']:first['dtb_offset']],
                         padded[second['kernel_offset']:second['dtb_offset']])
        self.assertEqual(original[first['initrd_offset']:], padded[second['initrd_offset']:])
        at, length = identity['slot_offset'], identity['slot_bytes']
        self.assertEqual(padded[at:at + length], b' ' * length)
        fields = {'schema': 1, 'source': 'ios-device', 'profile': 'p105', 'ecid': 123,
                  'chip': 0x8942, 'board': 10, 'handoff_id': NONCE,
                  'template_sha256': image.digest(padded)}
        token = recovery_identity.encode_parameter(recovery_identity.HANDOFF_PARAMETER, fields).encode()
        bound = bytearray(padded)
        bound[at:at + len(token)] = token
        self.assertEqual(image.configuration(bound), second)
        dtb = bound[second['dtb_offset']:second['dtb_offset'] + second['dtb_size']]
        root, _, _ = image.read_fdt(dtb)
        chosen = next(child for child in root['children'] if child['name'] == 'chosen')
        bootargs = chosen['properties']['bootargs'][:-1]
        self.assertLessEqual(len(bootargs), 1023)
        self.assertEqual(recovery_identity.parameters(bytes(bootargs))[recovery_identity.HANDOFF_PARAMETER], fields)

    def test_host_only_policy_wrong_board_and_command_line_growth_are_refused(self):
        raw = template()
        with self.assertRaisesRegex(ValueError, 'recipe'):
            ios_identity.prepare_template(raw, {**self.profile, 'board': 11})
        policy = {'schema': 1, 'source': 'host-usb', 'profile': 'p105',
                  'compatibles': ['apple,p105ap'], 'chip': 0x8942, 'board': 10}
        dtb = tree()
        dtb['properties']['compatible'] = b'apple,p105ap\0'
        host, _ = image.assemble(loader(), kernel(), image.write_fdt(dtb), b'cpio', PROFILE,
            recovery_identity.encode_parameter(recovery_identity.POLICY_PARAMETER, policy))
        with self.assertRaisesRegex(ValueError, 'does not allow'):
            ios_identity.prepare_template(host, self.profile)
        policy = image.host_identity_policy(raw)
        long, _ = image.assemble(loader(), kernel(), image.write_fdt(dtb), b'cpio', PROFILE,
            'x=' + 'x' * 650 + ' ' + recovery_identity.encode_parameter(recovery_identity.POLICY_PARAMETER, policy))
        with self.assertRaisesRegex(ValueError, 'command-line bound'):
            ios_identity.prepare_template(long, self.profile)

    def test_owner_legacy_staging_image_retains_exact_bytes(self):
        raw = bytes.fromhex('c0000cf1') + bytes(40000)
        self.assertEqual(ios_identity.prepare_template(raw, self.profile), (raw, None))


@unittest.skipUnless(sys.platform == 'darwin' and shutil.which('xcrun'),
                     'Identity-enabled launcher checks need Apple clang and codesign')
class NativeIdentityBuild(unittest.TestCase):
    def test_identity_enabled_helper_builds_for_ios9_armv7(self):
        profile = ios_launcher.boot_profile('p105')
        bundle, identity = ios_identity.prepare_template(template(), profile)
        with TemporaryDirectory(prefix='orchard-ios-origin-build-') as directory:
            root = Path(directory)
            (root / 'bundle').write_bytes(bundle)
            (root / 'ibec').write_bytes(b'synthetic checked ibec input')
            (root / 'kloader').write_bytes(b'synthetic checked kloader input')
            shim = {'mutable_crc_offset': 24, 'boot_image_crc32': f'{zlib.crc32(bundle):08x}'}
            result = ios_launcher.build_launcher(root / 'bundle', root / 'ibec', root / 'kloader',
                                                 root / 'out', profile, identity=identity, shim=shim)
        self.assertEqual(result['architecture'], 'armv7')
        self.assertEqual(result['minimum_ios'], '9.0')
        self.assertEqual(set(result['source_files']), {
            'ios-start.c', 'ios-launcher.m', 'ios-identity-bind.h', 'ios-live-identity.h', 'producer'})


class ProvisionedOfflineResume(unittest.TestCase):
    def setUp(self):
        self.work = TemporaryDirectory()
        self.addCleanup(self.work.cleanup)
        self.folder = Path(self.work.name)
        self.folder.chmod(0o700)
        self.profile = ios_launcher.boot_profile('p105')
        self.data, self.identity = ios_identity.prepare_template(template(), self.profile)
        self.endpoint = {'profile': 'p105', 'ecid': 123, 'boot_id': BOOT,
                         'handoff_id': NONCE, 'identity_source': 'ios-device'}
        self.receipt = self.folder / 'installation.json'
        self.files = []
        for key, data in (('boot_path', self.data), ('ibec_path', b'prepared ibec'),
                          ('kloader_path', b'signed loader'), ('helper_path', b'signed helper')):
            name = 'files/' + key
            path = self.folder / name
            path.parent.mkdir(mode=0o700, exist_ok=True)
            path.write_bytes(data)
            path.chmod(0o600)
            self.files.append({'source': name, 'destination': self.profile[key],
                               'bytes': len(data), 'sha256': image.digest(data)})
        self.record = {'schema': 1, 'kind': 'ios-local-file-launcher', 'device': 'p105',
                       'machine': self.profile['machine'], 'boot_recipe_sha256': ios_launcher.recipe_digest('p105'),
                       'identity': self.identity, 'files': self.files}
        self.save()

    def save(self):
        self.receipt.write_text(json.dumps(self.record))
        self.receipt.chmod(0o600)

    def test_explicit_private_provision_accepts_an_offline_start_without_host_upload(self):
        expected = ios_launcher.provision_handoff('p105', self.endpoint, self.receipt)
        self.assertEqual(expected.fields(), {'identity_source': 'ios-device', 'handoff_id': NONCE,
            'template_sha256': self.identity['template_sha256'], 'chip': 0x8942, 'board': 10})
        from orchard.apple import cascadia
        with patch.dict(os.environ, {'ORCHARD_IOS_LAUNCHER': str(self.folder)}), \
             patch.object(cascadia, 'tool') as irecovery, patch.object(cascadia, 'DeviceState') as upload_scope:
            self.assertEqual(cascadia.recovery_handoff('p105', self.endpoint), expected)
        irecovery.assert_not_called()
        upload_scope.assert_not_called()

    def test_unknown_receipt_and_wrong_origin_cannot_start_a_host_session(self):
        with patch.dict(os.environ, {'ORCHARD_IOS_LAUNCHER': ''}), self.assertRaisesRegex(ValueError, 'Select the private'):
            ios_launcher.provision_handoff('p105', self.endpoint)
        for change in ({'identity_source': None}, {'identity_source': 'host-usb'}, {'profile': 'j71b'}):
            with self.subTest(change=change), self.assertRaises(ValueError):
                ios_launcher.provision_handoff('p105', {**self.endpoint, **change}, self.receipt)

    def test_hash_path_slot_recipe_and_inventory_mismatches_are_refused(self):
        original = copy.deepcopy(self.record)
        cases = (
            {'boot_recipe_sha256': '0' * 64},
            {'identity': {**self.identity, 'template_sha256': '0' * 64}},
            {'identity': {**self.identity, 'slot_offset': len(self.data)}},
            {'files': self.files[:1]},
            {'files': [{**self.files[0], 'source': '../escape'}, *self.files[1:]]},
            {'files': [{**self.files[0], 'sha256': '0' * 64}, *self.files[1:]]},
        )
        for changed in cases:
            self.record = {**original, **changed}
            self.save()
            with self.subTest(fields=tuple(changed)), self.assertRaises(ValueError):
                ios_launcher.provision_handoff('p105', self.endpoint, self.receipt)
        self.record = original
        self.save()
        self.receipt.chmod(0o644)
        with self.assertRaisesRegex(ValueError, 'private regular'):
            ios_launcher.provision_handoff('p105', self.endpoint, self.receipt)


if __name__ == '__main__':
    unittest.main()
