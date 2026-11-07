# SPDX-License-Identifier: GPL-2.0-only
"""A host-bound recovery keeps its attempt identity through serial and SSH."""
import base64
from contextlib import nullcontext
import json
import os
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import Mock, patch

from orchard.apple import cascadia
from orchard.contracts import Connection, RecoveryHandoff
from orchard.state import BootState, DeviceState, private_directory
from orchard.transports import ssh


BOOT = '11111111-1111-4111-8111-111111111111'
NONCE = '0123456789abcdef0123456789abcdef'
TEMPLATE = 'a' * 64


class HandoffChecks(unittest.TestCase):
    def setUp(self):
        self.handoff = RecoveryHandoff(NONCE, TEMPLATE, 0x8942, 10)
        self.scope = BootState(DeviceState('p105', 123), BOOT)
        self.endpoint = {'profile': 'p105', 'ecid': 123, 'boot_id': BOOT,
                         'location': 'port', 'serial_port': '/dev/fake', 'handoff_id': NONCE}
        self.identity = {'profile': 'p105', 'ecid': 123, 'boot_id': BOOT, **self.handoff.fields()}

    def test_identity_requires_nonce_template_provenance_and_exact_integer_ids(self):
        self.assertTrue(self.handoff.matches(self.identity))
        for field, value in (('identity_source', 'adt'), ('handoff_id', 'f' * 32),
                             ('template_sha256', 'b' * 64), ('chip', True), ('board', False)):
            with self.subTest(field=field):
                self.assertFalse(self.handoff.matches({**self.identity, field: value}))
                missing = dict(self.identity)
                del missing[field]
                self.assertFalse(self.handoff.matches(missing))
        for nonce in ('0' * 32, 'A' * 32, 'f' * 31):
            with self.subTest(nonce=nonce), self.assertRaises(ValueError):
                RecoveryHandoff(nonce, TEMPLATE, 0x8942, 10)

    def test_serial_bootstrap_checks_provenance_before_accepting_key(self):
        result = {**self.identity, 'host': '172.16.42.1', 'port': 22,
                  'host_key': 'ssh-ed25519 ' + 'A' * 68}
        def response(value):
            return json.dumps({'protocol': 1, 'event': 'result', 'operation': 'bootstrap', 'result': value})
        self.assertEqual(ssh.bootstrap_reply(0, response(result), self.scope, self.handoff)[:2],
                         ('172.16.42.1', 22))
        for field in self.handoff.fields():
            wrong = dict(result)
            del wrong[field]
            with self.subTest(field=field), self.assertRaisesRegex(ValueError, 'host-bound handoff'):
                ssh.bootstrap_reply(0, response(wrong), self.scope, self.handoff)

    def test_usb_nonce_change_stops_rpc_before_dispatch(self):
        for value in (None, 'f' * 32):
            current = {**self.endpoint, 'handoff_id': value}
            check = ssh.endpoint_check(self.scope, self.endpoint, Mock(return_value=current), self.handoff)
            connection = Connection(['ssh'], self.scope, check, self.handoff)
            from orchard.protocol import Client
            with patch('orchard.protocol.subprocess.run') as launch:
                with self.subTest(value=value), self.assertRaisesRegex(ValueError, 'changed'):
                    Client(connection, Mock()).call('start')
                launch.assert_not_called()

    def test_ios_origin_change_stops_rpc_even_with_the_same_boot_and_nonce(self):
        ios = RecoveryHandoff(NONCE, TEMPLATE, 0x8942, 10, 'ios-device')
        endpoint = {**self.endpoint, 'identity_source': 'ios-device'}
        for source in (None, 'host-usb'):
            check = ssh.endpoint_check(self.scope, endpoint,
                Mock(return_value={**endpoint, 'identity_source': source}), ios)
            from orchard.protocol import Client
            with patch('orchard.protocol.subprocess.run') as launch:
                with self.subTest(source=source), self.assertRaisesRegex(ValueError, 'changed'):
                    Client(Connection(['ssh'], self.scope, check, ios), Mock()).call('start')
                launch.assert_not_called()

    def test_authenticated_status_checks_template_before_yielding_connection(self):
        check = Mock()
        connection = Connection(['ssh'], self.scope, check, self.handoff)
        for change in ({'identity_source': 'adt'}, {'handoff_id': None}, {'template_sha256': 'b' * 64}, {'board': 11}):
            with patch.object(ssh, 'Client') as client:
                client.return_value.call.return_value = {**self.identity, **change}
                with self.subTest(change=change), self.assertRaisesRegex(ValueError, 'host-bound handoff'):
                    ssh.verify_peer(connection)
        check.assert_not_called()
        with patch.object(ssh, 'Client') as client:
            client.return_value.call.return_value = self.identity
            ssh.verify_peer(connection)
        check.assert_called_once()

    def test_session_rejects_unknown_or_changed_handoff_before_credentials(self):
        for handoff, endpoint in ((None, self.endpoint),
                                  (self.handoff, {**self.endpoint, 'handoff_id': None})):
            with patch.object(ssh.subprocess, 'run') as launch:
                with self.subTest(handoff=handoff), self.assertRaisesRegex(ValueError, 'handoff'):
                    with ssh.session(self.scope.device, Mock(return_value=endpoint), handoff=handoff):
                        self.fail('Unbound session opened')
                launch.assert_not_called()

    def test_session_echoes_bound_identity_and_verifies_peer_before_yield(self):
        def keygen(argv, **kwargs):
            path = Path(argv[-1])
            path.write_text('private fixture')
            path.with_suffix('.pub').write_text('ssh-ed25519 ' + 'A' * 68 + ' fixture\n')
            return Mock(returncode=0)
        captured = []
        def bootstrap(command, **kwargs):
            captured.append(json.loads(base64.urlsafe_b64decode(command.rsplit(' ', 1)[1])))
            result = {**self.identity, 'host': '172.16.42.1', 'port': 22,
                      'host_key': 'ssh-ed25519 ' + 'A' * 68}
            return 0, json.dumps({'protocol': 1, 'event': 'result', 'operation': 'bootstrap', 'result': result})
        with TemporaryDirectory() as temporary, patch.dict(os.environ, {'ORCHARD_HOME': temporary}), \
             patch.object(ssh.subprocess, 'run', side_effect=keygen), \
             patch.object(ssh, 'SerialPort') as serial, patch.object(ssh, 'wait_authenticated'), \
             patch.object(ssh, 'one_connection', return_value=nullcontext(['ssh'])), \
             patch.object(ssh, 'Client') as client:
            serial.return_value.__enter__.return_value.run.side_effect = bootstrap
            client.return_value.call.return_value = self.identity
            with ssh.session(self.scope.device, Mock(return_value=self.endpoint),
                             expected_boot=BOOT, handoff=self.handoff) as connection:
                self.assertEqual(connection.handoff, self.handoff)
                client.return_value.call.assert_called_once_with('status', timeout=8)
        self.assertEqual({key: captured[0][key] for key in self.handoff.fields()}, self.handoff.fields())


class CompletedHandoff(unittest.TestCase):
    def setUp(self):
        self.temp = TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        env = patch.dict(os.environ, {'ORCHARD_HOME': str(self.root)})
        env.start()
        self.addCleanup(env.stop)
        self.endpoint = {'profile': 'p105', 'ecid': 123, 'boot_id': BOOT, 'handoff_id': NONCE}
        self.handoff = RecoveryHandoff(NONCE, TEMPLATE, 0x8942, 10)
        folder = private_directory(DeviceState('p105', 123).root / 'boot-attempts')
        self.path = folder / (NONCE + '.json')
        self.record = {'schema': 1, 'method': 'recovery-autogo', 'phase': 'complete',
                       'payload_format': 'cascadia-cb32', 'profile': 'p105', 'ecid': 123,
                       'boot_id': BOOT, 'loader_bytes': cascadia.LOADER_TRIGGER_BYTES,
                       'loader_sha256': 'b' * 64,
                       'inter_upload_settle_seconds': cascadia.RECOVERY_REARM_SECONDS,
                       'loader_revalidated': True, **self.handoff.fields()}

    def save(self):
        self.path.write_text(json.dumps(self.record))
        self.path.chmod(0o600)

    def test_completed_private_receipt_recovers_exact_handoff(self):
        self.save()
        self.assertEqual(cascadia.recovery_handoff('p105', self.endpoint), self.handoff)
        self.assertIsNone(cascadia.recovery_handoff('p105', {**self.endpoint, 'handoff_id': None}))

    def test_unknown_incomplete_or_changed_receipt_cannot_resume(self):
        with self.assertRaisesRegex(ValueError, 'private completed'):
            cascadia.recovery_handoff('p105', self.endpoint)
        for change in ({'phase': 'awaiting-linux'}, {'identity_source': 'adt'}, {'handoff_id': 'f' * 32},
                       {'ecid': 124}, {'profile': 'j71b'}, {'loader_bytes': 0x8000},
                       {'inter_upload_settle_seconds': 0.5},
                       {'inter_upload_settle_seconds': 1}, {'loader_revalidated': False},
                       {'loader_revalidated': 1}, {'loader_revalidated': 'true'},
                       {'loader_sha256': 'B' * 64},
                       {'boot_id': '22222222-2222-4222-8222-222222222222'}):
            original = self.record
            self.record = {**original, **change}
            self.save()
            with self.subTest(change=change), self.assertRaisesRegex(ValueError, 'handoff receipt'):
                cascadia.recovery_handoff('p105', self.endpoint)
            self.record = original
        for field in ('loader_bytes', 'loader_sha256', 'inter_upload_settle_seconds',
                      'loader_revalidated'):
            original = self.record
            self.record = dict(original)
            del self.record[field]
            self.save()
            with self.subTest(missing=field), self.assertRaisesRegex(ValueError, 'handoff receipt'):
                cascadia.recovery_handoff('p105', self.endpoint)
            self.record = original

    def test_linked_or_public_receipt_is_refused(self):
        self.save()
        self.path.chmod(0o644)
        with self.assertRaisesRegex(ValueError, 'private completed'):
            cascadia.recovery_handoff('p105', self.endpoint)
        target = self.path.with_suffix('.other')
        self.path.rename(target)
        self.path.symlink_to(target)
        with self.assertRaisesRegex(ValueError, 'private completed'):
            cascadia.recovery_handoff('p105', self.endpoint)


if __name__ == '__main__':
    unittest.main()
