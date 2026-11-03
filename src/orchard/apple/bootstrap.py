"""Descriptor-selected Pongo bootstrap tools; host checks never touch a device."""
from pathlib import Path
import os
import platform
import re
import shutil

from orchard.integrity import digest

NAMES = ('palera1n', 'checkra1n-a7')
# Official 0.1337.3 tools used for the measured A7 handoff. Never substitute
# the newer palera1n-embedded checkra1n merely because it has the same name.
CHECKRA1N_SHA256 = {
    'aarch64': '078053c11e9a301f0e71b87b6b854e6c8a69c87972325dd0c90deeeba1c3cb70',
    'x86_64': '53154d87594cf67c56659f24547f1b5bb5336f84ea58c2d42f25a7b2df81a964',
}


def name(description):
    value = description.get('boot', {}).get('bootstrap', 'palera1n')
    if value not in NAMES:
        raise ValueError('Unknown Pongo bootstrap: ' + str(value))
    return value


def checkra1n_a7():
    """Find and verify the supported host binary, without executing it."""
    machine = platform.machine()
    machine = 'aarch64' if machine == 'arm64' else machine
    if platform.system() != 'Linux' or machine not in CHECKRA1N_SHA256:
        raise ValueError('checkra1n-a7 currently requires a Linux aarch64 or x86_64 host')
    tool = shutil.which('checkra1n-0.1337.3')
    if not tool:
        local = Path.home() / '.local/bin/checkra1n-0.1337.3'
        if local.is_file() and os.access(local, os.X_OK):
            tool = str(local)
    if not tool:
        raise ValueError('A7 boot needs the pinned checkra1n-0.1337.3 host tool on PATH or in ~/.local/bin')
    if digest(Path(tool)) != CHECKRA1N_SHA256[machine]:
        raise ValueError('checkra1n-0.1337.3 host checksum mismatch; no bootstrap was started')
    return tool


def require_direct_pi(location, *, root=Path('/sys/bus/usb/devices'),
                      model_file=Path('/proc/device-tree/model')):
    """Keep the measured Pi 4 A7 path direct; do not assume a bus/port number."""
    try:
        model = model_file.read_bytes().rstrip(b'\0\r\n')
    except FileNotFoundError:
        return  # Other Linux hosts do not expose this board identity.
    if not model.startswith(b'Raspberry Pi 4 Model B'):
        return
    if not isinstance(location, str) or not re.fullmatch(r'\d+-\d+(?:\.\d+)*', location):
        raise ValueError('Cannot verify the A7 direct Pi USB location')

    def identity(path):
        return tuple((path/key).read_text().strip().lower()
                     for key in ('idVendor', 'idProduct'))
    try:
        parent = (root/location).resolve(strict=True).parent
        if identity(parent) == ('1d6b', '0002'):
            return
        if (identity(parent) == ('2109', '3431') and
                identity(parent.parent) == ('1d6b', '0002')):
            return
    except OSError as error:
        raise ValueError('Cannot verify the A7 direct Pi USB topology') from error
    raise ValueError('A7 boot on Pi 4 requires a direct Pi USB port; move only the iPad cable from the hub')
