"""orchard shell/exec/push/pull reach an installed system as its user."""
from pathlib import Path
import tempfile
import unittest
from unittest import mock

from orchard import computer_key, ram_session

SYSTEM = {'location': 1, 'mode': 'system', 'product': 'Omarchy A', 'serial_port': '/dev/cu.x',
          'boot_id': '00000000-0000-4000-8000-000000000001', 'profile': 'j71b'}
PAYLOAD = {**SYSTEM, 'mode': 'ram', 'product': 'Orchard gpu_001'}
STABLE_PAYLOAD = {**PAYLOAD, 'boot_id': None, 'usb_id': 'f09dfe4c604a'}
BOOT_A = '11111111-1111-4111-8111-111111111111'
BOOT_B = '22222222-2222-4222-8222-222222222222'


class SystemSession(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        patcher = mock.patch.object(computer_key, 'directory', return_value=Path(self.temp.name))
        patcher.start()
        self.addCleanup(patcher.stop)

    def test_an_installed_system_is_a_target_when_no_payload_runs(self):
        with mock.patch('orchard.apple.usb.devices', return_value=[SYSTEM]):
            self.assertEqual(ram_session.target()['mode'], 'system')
        with mock.patch('orchard.apple.usb.devices', return_value=[SYSTEM, PAYLOAD]):
            self.assertEqual(ram_session.target()['mode'], 'ram')

    def test_it_signs_in_as_the_user_its_install_allowed(self):
        computer_key.remember_user('tablet')
        with mock.patch.object(computer_key, 'identity', return_value=Path('/key')), \
                mock.patch.object(ram_session, 'wait_authenticated'):
            argv = ram_session.connect(SYSTEM)
        self.assertEqual(argv[-1], 'tablet@172.16.42.1')
        self.assertIn('StrictHostKeyChecking=accept-new', argv)

    def test_network_system_does_not_discover_usb(self):
        computer_key.remember_user('tablet')
        with mock.patch.object(computer_key, 'identity', return_value=Path('/key')), \
                mock.patch.object(ram_session, 'target', side_effect=AssertionError('USB used')):
            argv = ram_session.connect(host='192.168.0.19')
        self.assertEqual(argv[-1], 'tablet@192.168.0.19')
        self.assertIn('StrictHostKeyChecking=accept-new', argv)

    def test_network_ram_uses_ram_key_and_pins_host(self):
        with mock.patch.object(ram_session, '_directory', return_value=Path(self.temp.name)), \
                mock.patch.object(ram_session, 'identity', return_value=Path('/ram-key')), \
                mock.patch.object(ram_session, 'target', side_effect=AssertionError('USB used')):
            argv = ram_session.connect(host='192.168.0.19', ram=True)
        self.assertEqual(argv[-1], 'root@192.168.0.19')
        self.assertIn('/ram-key', argv)
        self.assertIn('StrictHostKeyChecking=accept-new', argv)
        self.assertTrue(any('network_known_hosts' in arg for arg in argv))

    def test_network_rejects_options_and_ambiguous_ram_user(self):
        for host in ('-oProxyCommand=bad', 'user@host', 'host name'):
            with self.assertRaisesRegex(ValueError, 'Invalid SSH host'):
                ram_session.connect(host=host)
        with self.assertRaisesRegex(ValueError, '--ram'):
            ram_session.connect(host='192.168.0.19', ram=True, user='tablet')

    def test_without_a_user_it_says_how_to_get_one(self):
        with self.assertRaisesRegex(ValueError, '--user'):
            ram_session.connect(SYSTEM)

    def ram_connect(self, reply, device=STABLE_PAYLOAD):
        with mock.patch.object(ram_session, '_directory', return_value=Path(self.temp.name)), \
                mock.patch.object(ram_session, 'identity', return_value=Path('/ram-key')), \
                mock.patch.object(ram_session, 'SerialPort') as port, \
                mock.patch.object(ram_session, 'wait_authenticated') as wait:
            port.return_value.__enter__.return_value.run.return_value = (0, reply)
            argv = ram_session.connect(device)
            command = port.return_value.__enter__.return_value.run.call_args.args[0]
            self.assertIn('/proc/sys/kernel/random/boot_id', command)
            self.assertIn('/run/orchard/ssh_host_ed25519_key.pub', command)
            self.assertIn('StrictHostKeyChecking=yes', argv)
            wait.assert_called_once()
            return argv

    def test_stable_usb_identity_gets_fresh_boot_and_key_from_acm_on_each_connection(self):
        # A second connect must not reuse a pin just because USB is unchanged.
        first = self.ram_connect(BOOT_A + '\nssh-ed25519 AAAA\n')
        pin_a = Path(self.temp.name) / f'known_hosts-{BOOT_A}'
        self.assertEqual(pin_a.read_text(), '172.16.42.1 ssh-ed25519 AAAA\n')
        self.assertEqual(pin_a.stat().st_mode & 0o777, 0o600)
        self.assertEqual(self.ram_connect(BOOT_A + '\nssh-ed25519 AAAA\n'), first)
        second = self.ram_connect(BOOT_B + '\nssh-ed25519 BBBB\n')
        self.assertNotEqual(first, second)
        self.assertFalse(pin_a.exists())
        self.assertEqual((Path(self.temp.name) / f'known_hosts-{BOOT_B}').read_text(),
                         '172.16.42.1 ssh-ed25519 BBBB\n')

    def test_legacy_ram_boot_tag_is_still_verified_against_acm(self):
        self.ram_connect(PAYLOAD['boot_id'] + '\nssh-ed25519 AAAA\n', device=PAYLOAD)
        with self.assertRaisesRegex(ValueError, 'disconnected or rebooted'):
            self.ram_connect(BOOT_B + '\nssh-ed25519 AAAA\n', device=PAYLOAD)

    def test_invalid_acm_identity_or_changed_key_does_not_reuse_a_pin(self):
        self.ram_connect(BOOT_A + '\nssh-ed25519 AAAA\n')
        for reply, error in [('garbage\nssh-ed25519 BBBB\n', 'Invalid RAM boot'),
                             ('../../escape\nssh-ed25519 BBBB\n', 'Invalid RAM boot'),
                             (BOOT_A + '\n', 'exactly one SSH host key'),
                             (BOOT_A + '\nssh-ed25519 BBBB\n', 'changed within the same boot')]:
            with self.subTest(reply=reply), self.assertRaisesRegex(ValueError, error):
                self.ram_connect(reply)
        self.assertEqual((Path(self.temp.name) / f'known_hosts-{BOOT_A}').read_text(),
                         '172.16.42.1 ssh-ed25519 AAAA\n')

    def test_stable_usb_check_rejects_another_unit_or_mode_at_the_same_port(self):
        check = ram_session._check(STABLE_PAYLOAD)
        with mock.patch('orchard.apple.usb.devices', return_value=[STABLE_PAYLOAD]):
            check()
        for change in ({'usb_id': 'fedcba987654'}, {'mode': 'system'},
                       {'profile': 'j71t'}, {'serial_port': '/dev/cu.other'}):
            with self.subTest(change=change), \
                    mock.patch('orchard.apple.usb.devices', return_value=[{**STABLE_PAYLOAD, **change}]), \
                    self.assertRaisesRegex(ValueError, 'disconnected or rebooted'):
                check()


if __name__ == '__main__':
    unittest.main()
