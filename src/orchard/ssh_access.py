"""SSH access to an installed system that was installed without it.

Every installed system offers a login on its USB serial console. This signs
in there once, as the system's user with their password, and adds this
computer's keys (orchard.computer_key) to that user's authorized_keys, as the
installer does when SSH logins are allowed. After that `orchard shell`,
`exec`, `push`, `pull` and plain `ssh USER@172.16.42.1` work. The password is
sent only to the serial login and never stored or logged.
"""
import getpass
import re
import secrets
import shlex
import time

from . import computer_key
from .serial import SerialError, SerialPort

LOGIN = re.compile(rb'login: $')
PASSWORD = re.compile(rb'[Pp]assword: ?$')
REFUSED = re.compile(rb'Login incorrect|Authentication failure|Permission denied')
USER = re.compile(r'[a-z_][a-z0-9_-]{0,31}')


def append_keys(keys):
    """A POSIX shell command adding KEYS to the user's authorized_keys, each once."""
    lines = ''.join(f'grep -qxF {shlex.quote(key)} ~/.ssh/authorized_keys 2>/dev/null || '
                    f'echo {shlex.quote(key)} >> ~/.ssh/authorized_keys; ' for key in keys)
    return ('umask 077; mkdir -p ~/.ssh && chmod 700 ~/.ssh && touch ~/.ssh/authorized_keys && ' + lines +
            'chmod 600 ~/.ssh/authorized_keys')


class Console:
    """A login session on an installed system's serial console."""

    def __init__(self, port):
        self.port = port

    def _await(self, patterns, deadline):
        """The index of the first of PATTERNS the console shows; raises on a refused login."""
        combined = re.compile(b'|'.join(b'(' + p.pattern + b')' for p in (REFUSED, *patterns)), re.M)
        _, match = self.port.until(combined, deadline)
        if match.group(1):
            raise ValueError('The system refused the login: check the user name and password')
        return next(index for index in range(len(patterns)) if match.group(index + 2))

    def login(self, user, password, timeout=30):
        deadline = time.monotonic() + timeout
        # A fresh line brings up the login prompt, or a shell left open from before.
        self.port.write(b'\r', deadline)
        token = 'OMA_' + secrets.token_hex(8)
        ready = re.compile(rb'(?:\r?\n)' + token.encode() + rb'\r?\n')
        first = self._await([LOGIN, PASSWORD, re.compile(rb'[$#%>] ?$')], deadline)
        if first == 2:
            # Someone's shell is open on the console: end it and sign in as USER.
            self.port.write(b'exit\r', deadline)
            first = self._await([LOGIN], deadline)
        if first == 1:
            # A password prompt left from an earlier attempt: fail it and start over.
            self.port.write(b'\r', deadline)
            first = self._await([LOGIN], deadline)
        self.port.write(user.encode() + b'\r', deadline)
        self._await([PASSWORD], deadline)
        self.port.write(password.encode() + b'\r', deadline)
        # The shell's prompt is the user's own: wait for a marker this session prints instead.
        while True:
            self.port.write(f"printf '\\n%s\\n' {token}\r".encode(), deadline)
            try:
                combined = re.compile(REFUSED.pattern + b'|' + ready.pattern)
                _, match = self.port.until(combined, min(deadline, time.monotonic() + 3))
            except SerialError:
                if time.monotonic() >= deadline:
                    raise
                continue
            if REFUSED.search(match.group(0)):
                raise ValueError('The system refused the login: check the user name and password')
            return

    def run(self, command, timeout=30):
        """COMMAND's exit code in the signed-in shell."""
        deadline = time.monotonic() + timeout
        token = 'OMA_' + secrets.token_hex(12)
        line = f"sh -c {shlex.quote(command)}; printf '\\n%s:%s\\n' {token}_DONE \"$?\"\r"
        self.port.write(line.encode(), deadline)
        _, match = self.port.until(re.compile(rb'(?:\r?\n)' + token.encode() + rb'_DONE:([0-9]{1,3})\r?\n'),
                                   deadline)
        return int(match.group(1))

    def logout(self):
        try:
            self.port.write(b'exit\r', time.monotonic() + 5)
        except SerialError:
            pass


def target():
    """The one running installed system on USB, with its serial console."""
    from .apple import usb
    systems = [item for item in usb.devices() if item['mode'] == 'system']
    if len(systems) != 1:
        raise ValueError('Connect exactly one running installed system (none or several found)')
    if not systems[0].get('serial_port'):
        raise ValueError('The installed system shows no USB serial console')
    return systems[0]


def grant(user=None, *, password_fn=getpass.getpass, input_fn=input, output=print):
    """Add this computer's keys to USER's authorized_keys over the serial console; returns USER."""
    device = target()
    user = user or computer_key.user() or input_fn('User on the device: ').strip()
    if not USER.fullmatch(user or ''):
        raise ValueError('Not a user name: ' + repr(user))
    password = password_fn(f'Password of {user} on the device: ')
    keys = computer_key.public_keys()

    def check():
        from .apple import usb
        if not any(item.get('serial_port') == device['serial_port'] and item['mode'] == 'system'
                   for item in usb.devices()):
            raise ValueError('The system disconnected')
    with SerialPort(device['serial_port'], check) as port:
        console = Console(port)
        console.login(user, password)
        try:
            code = console.run(append_keys(keys))
        finally:
            console.logout()
    if code != 0:
        raise ValueError(f'Adding the keys failed on the device (exit {code})')
    computer_key.remember_user(user)
    # Its host key may be new since an earlier install: learn it again on the first connection.
    computer_key.ssh_argv(user, forget=True)
    output(f'SSH access for {user} added from this computer ({len(keys)} key{"s" * (len(keys) != 1)}). '
           f'Try: orchard shell, or ssh {user}@172.16.42.1')
    return user
