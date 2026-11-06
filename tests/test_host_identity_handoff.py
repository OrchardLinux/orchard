"""Descriptor-bound public recovery identity; fixtures never access a device."""
import base64
import importlib.util
import json
import os
from pathlib import Path
import stat
import sys
import tempfile
import unittest
from unittest import mock

from orchard.apple import adt
from orchard import recovery_identity as codec

ROOT = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location('host_identity_fixture', ROOT/'runtime/recovery/identity.py')
identity = importlib.util.module_from_spec(spec)
with mock.patch.dict(sys.modules, {'adt': adt}):
    spec.loader.exec_module(identity)
BOOT = '11111111-1111-4111-8111-111111111111'
POLICY = {'schema': 1, 'source': 'host-usb', 'profile': 'p105',
          'compatibles': ['apple,p105ap'], 'chip': 0x8942, 'board': 0x0a}
HANDOFF = {'schema': 1, 'source': 'host-usb', 'profile': 'p105', 'ecid': 123,
           'chip': 0x8942, 'board': 0x0a, 'handoff_id': 'a'*32, 'template_sha256': 'b'*64}


def raw_token(name, raw):
    return name + '=' + base64.urlsafe_b64encode(raw).rstrip(b'=').decode()


class IdentityCodec(unittest.TestCase):
    def test_roundtrip_partial_and_complete_parameters(self):
        policy = codec.encode_parameter(codec.POLICY_PARAMETER, POLICY)
        handoff = codec.encode_parameter(codec.HANDOFF_PARAMETER, HANDOFF)
        self.assertEqual(codec.parameters('console=tty0'), {})
        self.assertEqual(codec.parameters('console=tty0 ' + policy), {codec.POLICY_PARAMETER: POLICY})
        self.assertEqual(codec.parameters((policy+' '+handoff+'\n').encode()),
                         {codec.POLICY_PARAMETER: POLICY, codec.HANDOFF_PARAMETER: HANDOFF})

    def test_arm_command_line_boundary_and_truncated_handoff(self):
        policy = codec.encode_parameter(codec.POLICY_PARAMETER, POLICY)
        handoff = codec.encode_parameter(codec.HANDOFF_PARAMETER, HANDOFF)
        tokens = policy + ' ' + handoff
        full = 'x' * (1023-len(tokens)-1) + ' ' + tokens
        self.assertEqual(len(full), 1023)
        self.assertEqual(codec.parameters(full + '\n'),
                         {codec.POLICY_PARAMETER: POLICY, codec.HANDOFF_PARAMETER: HANDOFF})
        with self.assertRaisesRegex(ValueError, 'bound'):
            codec.parameters('x' + full)
        truncated = ('x' * (1024-len(handoff)-1) + ' ' + handoff)[:1023]
        self.assertEqual(len(truncated), 1023)
        with self.assertRaises(ValueError):
            codec.parameters(truncated)

    def test_ambiguous_malformed_and_oversized_tokens_fail(self):
        policy = codec.encode_parameter(codec.POLICY_PARAMETER, POLICY)
        handoff = codec.encode_parameter(codec.HANDOFF_PARAMETER, HANDOFF)
        duplicate = json.dumps(HANDOFF).replace('"ecid": 123', '"ecid": 0, "ecid": 123').encode()
        for line in (policy+' '+policy, handoff+' '+handoff, codec.HANDOFF_PARAMETER,
                     policy+'=', codec.HANDOFF_PARAMETER+'=!',
                     raw_token(codec.HANDOFF_PARAMETER, duplicate),
                     raw_token(codec.HANDOFF_PARAMETER, b'{}\0'),
                     raw_token(codec.HANDOFF_PARAMETER, b'[]'),
                     'orchard.recovery-unknown=e30',
                     handoff+'\0', handoff+'\r', handoff+'\n\n', 'x'*1024):
            with self.subTest(line=line[:90]), self.assertRaises(ValueError):
                codec.parameters(line)
        # Equivalent decoded bytes with nonzero unused pad bits are rejected.
        with self.assertRaisesRegex(ValueError, 'canonical'):
            codec.parameters(codec.HANDOFF_PARAMETER+'=e31')

    def test_selected_fields_and_attempt_types_are_exact(self):
        changes = ({'schema': True}, {'schema': 2}, {'source': 'configuration'}, {'profile': ''},
                   {'ecid': True}, {'ecid': 0}, {'ecid': 2**64}, {'chip': True}, {'board': False},
                   {'handoff_id': '0'*32}, {'handoff_id': 'A'*32}, {'handoff_id': 'a'*31},
                   {'template_sha256': 'x'*64}, {'extra': 1})
        for change in changes:
            with self.subTest(change=change), self.assertRaises(ValueError):
                codec.encode_parameter(codec.HANDOFF_PARAMETER, {**HANDOFF, **change})
        for change in ({'compatibles': []}, {'compatibles': ['apple,p105ap']*2},
                       {'compatibles': [None]}, {'compatibles': ['other,p105']},
                       {'board': -1}, {'chip': 0}, {'schema': True}, {'extra': 1}):
            with self.subTest(change=change), self.assertRaises(ValueError):
                codec.encode_parameter(codec.POLICY_PARAMETER, {**POLICY, **change})


class HostIdentity(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        for name, relative in (('RUN', 'run'), ('DEV', 'dev'), ('MTD', 'mtd'),
                               ('BOOT_ID', 'boot-id'), ('COMPATIBLE', 'compatible'),
                               ('POLICY', 'policy'), ('CMDLINE', 'cmdline')):
            patch = mock.patch.object(identity, name, self.root/relative)
            patch.start()
            self.addCleanup(patch.stop)
        identity.BOOT_ID.write_text(BOOT)
        identity.COMPATIBLE.write_bytes(b'apple,p105ap\0apple,s5l8942x\0')
        identity.POLICY.write_text(json.dumps(POLICY, indent=2))
        identity.POLICY.chmod(0o644)
        self.command_line()
        original_lstat, original_fstat = Path.lstat, os.fstat

        def root_stat(value):
            fields = list(value)
            fields[4] = 0  # Emulate root-owned image files on a non-root build host.
            return os.stat_result(fields)

        def lstat(path):
            result = original_lstat(path)
            if path == identity.POLICY or path == identity.RUN/'identity.json':
                result = root_stat(result)
            return result

        patch = mock.patch.object(Path, 'lstat', lstat)
        patch.start()
        self.addCleanup(patch.stop)
        patch = mock.patch.object(os, 'fstat', lambda fd: root_stat(original_fstat(fd)))
        patch.start()
        self.addCleanup(patch.stop)

    def command_line(self, *, policy=POLICY, handoff=HANDOFF):
        tokens = ['console=tty0']
        if policy is not None:
            tokens.append(codec.encode_parameter(codec.POLICY_PARAMETER, policy))
        if handoff is not None:
            tokens.append(codec.encode_parameter(codec.HANDOFF_PARAMETER, handoff))
        identity.CMDLINE.write_text(' '.join(tokens)+'\n')

    def test_canonical_profile_live_board_attempt_and_private_cache(self):
        with mock.patch.object(identity, 'validate', side_effect=AssertionError('ADT fallback')):
            record = identity.initialize()
            self.assertEqual(record, {'profile': 'p105', 'ecid': 123, 'boot_id': BOOT,
                'identity_source': 'host-usb', 'handoff_id': 'a'*32,
                'template_sha256': 'b'*64, 'chip': 0x8942, 'board': 0x0a})
            self.assertEqual(identity.profile(), 'p105')
            self.assertEqual(identity.cached(), record)
            self.assertEqual(stat.S_IMODE((identity.RUN/'identity.json').stat().st_mode), 0o600)
            serial = identity.serial(record)
            self.assertTrue(serial.endswith(' BOARD:p105 HND:'+'a'*32))
            self.assertLessEqual(len(serial), 126)
            with self.assertRaisesRegex(ValueError, 'already published'):
                identity.initialize()

    def test_missing_handoff_or_policy_token_does_not_fall_back_to_adt(self):
        for policy, handoff in ((POLICY, None), (None, HANDOFF), (None, None)):
            self.command_line(policy=policy, handoff=handoff)
            with self.subTest(policy=policy, handoff=handoff), \
                    mock.patch.object(identity, 'validate', side_effect=AssertionError('ADT fallback')), \
                    self.assertRaisesRegex(ValueError, 'policy and host USB handoff'):
                identity.initialize()
            self.assertFalse((identity.RUN/'identity.json').exists())

    def test_descriptor_policy_and_live_first_compatible_must_match(self):
        for raw in (b'apple,p105\0', b'apple,j1\0', b'apple,s5l8942x\0apple,p105ap\0',
                    b'apple,p105ap', b'apple,p105ap\0\0'):
            identity.COMPATIBLE.write_bytes(raw)
            with self.subTest(raw=raw), self.assertRaises(ValueError):
                identity.initialize()
        identity.COMPATIBLE.write_bytes(b'apple,p105ap\0')
        self.command_line(policy={**POLICY, 'compatibles': ['apple,p105ap', 'apple,j1']})
        with self.assertRaisesRegex(ValueError, 'staged descriptor'):
            identity.initialize()
        for key, value in (('profile', 'j1'), ('chip', 0x8945), ('board', 1)):
            self.command_line(handoff={**HANDOFF, key: value})
            with self.subTest(key=key), self.assertRaisesRegex(ValueError, 'differs'):
                identity.initialize()

    def test_policy_must_be_bounded_root_owned_and_not_writable_or_linked(self):
        root_lstat = Path.lstat

        def unowned(path):
            result = root_lstat(path)
            if path == identity.POLICY:
                fields = list(result)
                fields[4] = 501
                result = os.stat_result(fields)
            return result

        with mock.patch.object(Path, 'lstat', unowned), \
                self.assertRaisesRegex(ValueError, 'protected regular'):
            identity.initialize()
        identity.POLICY.chmod(0o664)
        with self.assertRaisesRegex(ValueError, 'protected regular'):
            identity.initialize()
        identity.POLICY.chmod(0o644)
        identity.POLICY.write_bytes(b'x'*2049)
        with self.assertRaisesRegex(ValueError, 'protected regular'):
            identity.initialize()
        identity.POLICY.unlink()
        identity.POLICY.symlink_to(self.root/'missing')
        with self.assertRaisesRegex(ValueError, 'protected regular'):
            identity.initialize()

    def test_cached_record_rechecks_attempt_digest_board_and_boot(self):
        record = identity.initialize()
        for key, value in (('ecid', 124), ('handoff_id', 'c'*32), ('template_sha256', 'd'*64)):
            self.command_line(handoff={**HANDOFF, key: value})
            with self.subTest(key=key), self.assertRaisesRegex(ValueError, 'differs'):
                identity.cached()
        self.command_line()
        identity.BOOT_ID.write_text('22222222-2222-4222-8222-222222222222')
        with self.assertRaisesRegex(ValueError, 'differs'):
            identity.cached()
        identity.BOOT_ID.write_text(BOOT)
        cache = identity.RUN/'identity.json'
        cache.write_text(json.dumps({**record, 'ecid': 123.0}))
        with self.assertRaisesRegex(ValueError, 'differs'):
            identity.cached()
        cache.write_text(json.dumps(record).replace('"ecid": 123', '"ecid": 0, "ecid": 123'))
        with self.assertRaisesRegex(ValueError, 'repeats'):
            identity.cached()
        cache.write_text(json.dumps(record))
        identity.CMDLINE.unlink()
        with self.assertRaisesRegex(ValueError, 'unavailable'):
            identity.cached()

    def test_usb_serial_bound_is_checked_before_publication(self):
        policy = {**POLICY, 'profile': 'p'*32}
        identity.POLICY.write_text(json.dumps(policy))
        self.command_line(policy=policy, handoff={**HANDOFF, 'profile': policy['profile']})
        with self.assertRaisesRegex(ValueError, 'USB serial string limit'):
            identity.initialize()
        self.assertFalse((identity.RUN/'identity.json').exists())

    def test_no_policy_keeps_the_adt_requirement_even_with_handoff_tokens(self):
        identity.POLICY.unlink()
        identity.COMPATIBLE.write_bytes(b'apple,j71b\0')
        identity.MTD.write_text('')
        with self.assertRaisesRegex(ValueError, 'one live ADT'):
            identity.initialize()

    def test_unknown_or_malformed_command_line_never_publishes(self):
        valid = identity.CMDLINE.read_text()
        values = (valid+'orchard.recovery-unknown=e30', valid+codec.encode_parameter(codec.HANDOFF_PARAMETER, HANDOFF),
                  valid.replace('orchard.recovery-handoff=', 'orchard.recovery-handoff=='),
                  valid+'\0', 'x'*1024)
        for value in values:
            identity.CMDLINE.write_text(value)
            with self.subTest(value=value[:80]), self.assertRaises(ValueError):
                identity.initialize()
            self.assertFalse((identity.RUN/'identity.json').exists())


if __name__ == '__main__':
    unittest.main()
