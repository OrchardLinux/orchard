"""Device data as orchard's producers read it: descriptors, chips, firmware recipes.

data_root says where the data is; `root` selects another checkout's.
"""
from functools import cache
import os
from pathlib import Path
import re
import subprocess
import tomllib

# The repository root: devices/, kernel/, boot/ and schemas/ sit beside src/.
ROOT = Path(__file__).resolve().parents[2]


@cache
def data_root():
    """Where device data is: ORCHARD_DATA, else this checkout's own files."""
    override = os.environ.get('ORCHARD_DATA')
    if override:
        path = Path(override).expanduser()
        if not (path / 'devices').is_dir():
            raise ValueError(f'ORCHARD_DATA has no devices/: {path}')
        return path
    return ROOT


# What a release ships for installers that read only the release (board-data.tar.gz).
BOARD_DATA = ('devices', 'chips', 'platforms', 'features.toml', 'routes.toml')


def board_data_files(root=None):
    """{relative path: Path} of the board data a release ships, sorted; ROOT (default: data_root())."""
    root = Path(root or data_root())
    files = {}
    for name in BOARD_DATA:
        path = root / name
        for item in (sorted(p for p in path.rglob('*') if p.is_file()) if path.is_dir() else [path]):
            files[item.relative_to(root).as_posix()] = item
    return files


def device_path(profile, root=None):
    if not isinstance(profile, str) or not re.fullmatch(r'[a-z0-9][a-z0-9._-]*', profile):
        raise ValueError('Invalid hardware profile')
    path = Path(root or data_root()) / 'devices' / profile / 'device.toml'
    if not path.is_file() or path.is_symlink():
        raise ValueError('Hardware support is not installed for ' + profile)
    return path


def payload_profile(profile, kind, root=None):
    """The board-owned profile for a source-built payload loader."""
    if kind not in ('cascadia',):
        raise ValueError('Unsupported payload profile: ' + str(kind))
    path = device_path(profile, root).with_name(kind + '.toml')
    if not path.is_file() or path.is_symlink():
        raise ValueError(f'{profile} has no {kind} handoff profile')
    return path


def profiles(root=None):
    """Every device this hardware data describes."""
    devices = Path(root or data_root()) / 'devices'
    return sorted(path.parent.name for path in devices.glob('*/device.toml')
                  if not path.is_symlink() and not path.parent.is_symlink())


def device(profile, root=None):
    return tomllib.loads(device_path(profile, root).read_text())


def chip_architecture(described, root=None):
    """The architecture of a board's chip (chips/*.toml: arm64 or arm), or None for an unknown chip."""
    chips = Path(root) if root else data_root()
    for path in (chips / 'chips').glob('*.toml'):
        chip = tomllib.loads(path.read_text())
        if chip['cpid'] == described.get('match', {}).get('dfu', {}).get('chip'):
            return chip['architecture']
    return None


def root_architecture(described, root=None):
    """The architecture of the root a board runs, as a distribution index names it: armv7 on a 32-bit chip, else aarch64."""
    return 'armv7' if chip_architecture(described, root) == 'arm' else 'aarch64'


def firmware_path(profile, root=None):
    descriptor = device_path(profile, root)
    described = device(profile, root)
    if 'firmware' not in described:
        raise ValueError(f'{profile} has no firmware recipe yet')
    name = described['firmware']['recipe']
    if ('\\' in name or ':' in name or any(part in ('', '.', '..') for part in name.split('/'))):
        raise ValueError('Unsafe firmware recipe resource')
    path = descriptor.parent
    for part in name.split('/'):
        path = path / part
        if path.is_symlink():
            raise ValueError('Linked firmware recipe refused')
    if not path.is_file():
        raise ValueError('Firmware recipe resource is missing')
    return path


def revision():
    """The orchard commit this checkout is at, for a release's build receipt.

    {'commit': SHA, 'modified': bool}; modified means the checkout differed
    from that commit, so the commit alone does not reproduce the build.
    """
    def git(*arguments):
        return subprocess.run(['git', '-C', str(ROOT), *arguments], capture_output=True,
                              text=True, check=True).stdout.strip()
    try:
        commit = git('rev-parse', 'HEAD')
        modified = bool(git('status', '--porcelain', '--untracked-files=no'))
    except (OSError, subprocess.CalledProcessError) as error:
        raise ValueError(f'{ROOT} is not an orchard Git checkout; its revision is unknown') from error
    return {'commit': commit, 'modified': modified}
