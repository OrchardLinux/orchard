"""Runtime authorization preserves an explicitly permitted on-device origin."""
import importlib.util
import json
from pathlib import Path
import sys
import tempfile
import unittest
from unittest import mock

import test_host_identity_handoff as fixtures

ROOT = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location('ios_origin_device_fixture', ROOT/'runtime/recovery/device.py')
device = importlib.util.module_from_spec(spec)
with mock.patch.dict(sys.modules, {'identity': fixtures.identity}):
    spec.loader.exec_module(device)


class IosRuntimeAuthorization(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.record = {'profile': 'p105', 'ecid': 123, 'boot_id': fixtures.BOOT,
                       'identity_source': 'ios-device', 'handoff_id': 'a'*32,
                       'template_sha256': 'b'*64, 'chip': 0x8942, 'board': 0x0a}
        self.request = {**self.record, 'public_key': 'ssh-ed25519 ' + 'A'*68}
        self.directory = self.root/'run/recovery'
        self.directory.mkdir(parents=True)
        (self.directory/'dropbear.pid').write_text('123')
        (self.directory/'ssh_host_ed25519_key.pub').write_text('ssh-ed25519 ' + 'B'*68)
        (self.root/'proc/123').mkdir(parents=True)
        (self.root/'proc/123/cmdline').write_bytes(b'/usr/sbin/dropbear\0')

    def path(self, value):
        if value in ('/run/recovery', '/proc'):
            return self.root/value.lstrip('/')
        return Path(value)

    def test_bootstrap_returns_ios_source_after_exact_guarded_request(self):
        with mock.patch.object(device, 'device', return_value=self.record), \
                mock.patch.object(device, 'Path', side_effect=self.path), \
                mock.patch.object(device.subprocess, 'run') as authorize, \
                mock.patch.object(device.subprocess, 'Popen') as spawn:
            result = device.bootstrap(self.request)
        self.assertEqual({key: result[key] for key in self.record}, self.record)
        self.assertEqual(result['host_key'], 'ssh-ed25519 ' + 'B'*68)
        authorize.assert_called_once_with(['/sbin/authorize-key'],
            input=(self.request['public_key']+'\n').encode(), check=True, capture_output=True)
        spawn.assert_not_called()

    def test_missing_or_changed_handoff_stops_before_key_or_service_commands(self):
        for key, value in (('identity_source', 'host-usb'), ('handoff_id', 'c'*32),
                           ('template_sha256', 'd'*64), ('chip', True), ('board', False)):
            for request in ({**self.request, key: value},
                            {name: value for name, value in self.request.items() if name != key}):
                with self.subTest(key=key, present=key in request), \
                        mock.patch.object(device, 'device', return_value=self.record), \
                        mock.patch.object(device.subprocess, 'run') as authorize, \
                        mock.patch.object(device.subprocess, 'Popen') as spawn:
                    with self.assertRaisesRegex(ValueError, 'handoff differs'):
                        device.bootstrap(request)
                    authorize.assert_not_called()
                    spawn.assert_not_called()

    def test_status_preserves_on_device_provenance(self):
        release = self.root/'release.json'
        release.write_text(json.dumps({'release': 'public-fixture'}))
        with mock.patch.object(device, 'device', return_value=self.record), \
                mock.patch.object(device, 'RELEASE', release):
            self.assertEqual(device.status({}), {'release': 'public-fixture', **self.record})


if __name__ == '__main__':
    unittest.main()
