"""SSH access over the serial login: a simulated console; never touches a device."""
import re
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from orchard import ssh_access
from orchard.serial import SerialError


class FakeConsole:
    """An agetty login, then a shell that runs what it is sent in a home directory."""

    def __init__(self, home, user='tablet', password='secret', shell_open=False):
        self.home, self.user, self.password = home, user, password
        self.state = 'shell' if shell_open else 'login'
        self.pending = bytearray(b'\r\nOrchard tablet ttyGS0\r\n\r\ntablet login: ' if not shell_open else b'$ ')
        self.typed_user = None
        self.log = []

    def write(self, data, deadline):
        for line in data.decode().split('\r'):
            if line or data == b'\r':
                self._line(line)

    def _line(self, line):
        self.log.append((self.state, line))
        if self.state == 'login':
            if line:
                self.typed_user, self.state = line, 'password'
                self.pending += b'Password: '
            else:
                self.pending += b'\r\ntablet login: '
        elif self.state == 'password':
            if self.typed_user == self.user and line == self.password:
                self.state = 'shell'
                self.pending += b'\r\nLast login: today\r\n[tablet@orchard ~]$ '
            else:
                self.state = 'login'
                self.pending += b'\r\nLogin incorrect\r\ntablet login: '
        elif self.state == 'shell':
            if line == 'exit':
                self.state = 'login'
                self.pending += b'logout\r\n\r\ntablet login: '
                return
            self.pending += line.encode() + b'\r\n'     # echo
            result = subprocess.run(['sh', '-c', line], capture_output=True, env={'HOME': str(self.home)})
            self.pending += result.stdout.replace(b'\n', b'\r\n') + b'[tablet@orchard ~]$ '

    def until(self, pattern, deadline, limit=65536):
        match = pattern.search(bytes(self.pending))
        if not match:
            raise SerialError('Serial response timed out; command outcome unknown')
        data = bytes(self.pending[:match.end()])
        del self.pending[:match.end()]
        return data, match


class Grant(unittest.TestCase):
    def setUp(self):
        work = tempfile.TemporaryDirectory()
        self.addCleanup(work.cleanup)
        self.home = Path(work.name)
        self.keys = ['ssh-ed25519 ' + 'A' * 68, 'ssh-ed25519 ' + 'B' * 68]

    def sign_in(self, console, user='tablet', password='secret'):
        session = ssh_access.Console(console)
        session.login(user, password, timeout=5)
        return session

    def test_adds_each_key_once_with_private_permissions(self):
        console = FakeConsole(self.home)
        session = self.sign_in(console)
        self.assertEqual(session.run(ssh_access.append_keys(self.keys)), 0)
        self.assertEqual(session.run(ssh_access.append_keys(self.keys[:1])), 0)
        session.logout()
        keys = (self.home / '.ssh/authorized_keys')
        self.assertEqual(keys.read_text().splitlines(), self.keys)
        self.assertEqual(keys.stat().st_mode & 0o777, 0o600)
        self.assertEqual((self.home / '.ssh').stat().st_mode & 0o777, 0o700)
        self.assertEqual(console.state, 'login')

    def test_a_wrong_password_is_refused_clearly(self):
        with self.assertRaisesRegex(ValueError, 'refused the login'):
            self.sign_in(FakeConsole(self.home), password='wrong')

    def test_an_open_shell_is_ended_before_signing_in(self):
        console = FakeConsole(self.home, shell_open=True)
        self.sign_in(console)
        self.assertIn(('shell', 'exit'), console.log)
        self.assertEqual(console.state, 'shell')

    def test_the_password_is_sent_only_at_the_password_prompt(self):
        console = FakeConsole(self.home)
        session = self.sign_in(console)
        session.run(ssh_access.append_keys(self.keys))
        self.assertEqual([state for state, line in console.log if line == 'secret'], ['password'])

    def test_grant_remembers_the_user_and_forgets_the_old_host_key(self):
        console = FakeConsole(self.home)
        port = mock.MagicMock()
        port.__enter__.return_value = console
        device = {'mode': 'system', 'serial_port': '/dev/cu.usbmodem1'}
        with mock.patch.object(ssh_access, 'target', return_value=device), \
                mock.patch.object(ssh_access, 'SerialPort', return_value=port), \
                mock.patch.object(ssh_access.computer_key, 'public_keys', return_value=self.keys), \
                mock.patch.object(ssh_access.computer_key, 'user', return_value=None), \
                mock.patch.object(ssh_access.computer_key, 'remember_user') as remember, \
                mock.patch.object(ssh_access.computer_key, 'ssh_argv') as argv:
            said = []
            user = ssh_access.grant(password_fn=lambda prompt: 'secret', input_fn=lambda prompt: 'tablet',
                                    output=said.append)
        self.assertEqual(user, 'tablet')
        remember.assert_called_once_with('tablet')
        argv.assert_called_once_with('tablet', forget=True)
        self.assertNotIn('secret', ''.join(said))
        self.assertEqual((self.home / '.ssh/authorized_keys').read_text().splitlines(), self.keys)

    def test_a_bad_user_name_is_refused_before_the_console(self):
        with mock.patch.object(ssh_access, 'target', return_value={'serial_port': 'x'}), \
                mock.patch.object(ssh_access.computer_key, 'user', return_value=None):
            with self.assertRaisesRegex(ValueError, 'Not a user name'):
                ssh_access.grant(input_fn=lambda prompt: 'root; rm', password_fn=lambda prompt: '')


if __name__ == '__main__':
    unittest.main()
