"""The one way to read device data: descriptors, features, boot methods, firmware recipes, loaders.

Nothing else opens devices/, methods.toml or features.toml. data_root says where
they are; `root` selects a checkout's data, for builds, which have to match their
own checkout rather than the newest catalog.
"""
from functools import cache
import json
import os
from pathlib import Path
import re
import subprocess
import tomllib

# The repository root: devices/, kernel/, boot/ and schemas/ sit beside src/.
ROOT = Path(__file__).resolve().parents[2]
# What this version of the code can do; data can name methods newer than this code.
IMPLEMENTED_METHODS = frozenset({'checkm8-pongo'})


def published():
    """Whether this code is a published orchard (a nightly, a release, a core), not a checkout."""
    from .cache import cache_root, shared_cache_root
    return any(parent.resolve() in ROOT.parents for parent in (shared_cache_root() / 'tooling', cache_root() / 'core'))


@cache
def data_root():
    """Where device data is: ORCHARD_DATA, a checkout's own files, else the published catalog."""
    override = os.environ.get('ORCHARD_DATA')
    if override:
        path = Path(override).expanduser()
        if not (path / 'devices').is_dir():
            raise ValueError(f'ORCHARD_DATA has no devices/: {path}')
        return path
    if not published():
        return ROOT
    from .catalog import current
    latest = current()
    if latest is not None:
        return latest['path']
    if (ROOT / 'devices').is_dir():  # a clone made offline still has the data of its own commit
        return ROOT
    raise ValueError('No device catalog yet: connect to the network once')


def data_revision():
    """What this run's device data is, for a log or a report."""
    path = data_root()
    return path.name[:12] if path.parent.name == 'catalog' else str(path)


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


def features():
    """Feature names and display labels for device.toml [features], in display order."""
    return tomllib.loads((data_root() / 'features.toml').read_text())


def methods():
    """Boot methods a device.toml can list, and what each needs (methods.toml)."""
    return tomllib.loads((data_root() / 'methods.toml').read_text())


def device(profile, root=None):
    return tomllib.loads(device_path(profile, root).read_text())


def install_blockers(profile, described=None):
    """Why this Orchard can't install on a board: [] when it can.

    Installation follows from what a board has, so experimental boards get it
    as soon as it could work, even if other features don't work yet.
    """
    described = device(profile) if described is None else described
    from . import launcher
    blockers = []
    if 'kernel' not in described:
        blockers.append('it has no kernel or device tree yet')
    listed = described.get('boot', {}).get('methods', [])
    if not any(_method_usable(profile, name) for name in listed):
        if any(launcher.delivers(name) and _method_listed(profile, name) for name in listed) and launcher.find() is None:
            blockers.append(f'it starts through {launcher.NAME}, which is not installed')
        else:
            blockers.append('this Orchard has no boot method for it yet')
    if 'firmware' not in described:
        blockers.append('it has no firmware recipe yet')
    if described.get('features', {}).get('storage_write', 'missing') == 'missing':
        blockers.append('its internal storage cannot be written yet')
    return blockers


def has_recovery(described, root=None):
    """Whether Orchard's recovery runs on a board: it follows from the board's kernel.

    Recovery is built per platform with every board's device tree, on the arm64 root
    distributions build, so 32-bit boards have none until a root of their own exists.
    """
    if 'kernel' not in described:
        return False
    chips = Path(root) if root else data_root()
    for path in (chips / 'chips').glob('*.toml'):
        chip = tomllib.loads(path.read_text())
        if chip['cpid'] == described['match']['dfu']['chip']:
            return chip['architecture'] == 'arm64'
    return False


def method_implemented(name):
    """Whether this Orchard can run boot method NAME: its own code, or orchard-launcher when installed."""
    from . import launcher
    return name in IMPLEMENTED_METHODS or (launcher.delivers(name) and launcher.find() is not None)


def _method_usable(profile, name):
    return method_implemented(name) and _method_listed(profile, name)


def _method_listed(profile, name):
    """Whether the board has what method NAME needs: its boot.json recipe, where the method takes one."""
    if not methods()[name]['boot_json']:
        return True
    recipe = device_path(profile).parent / 'boot.json'
    return recipe.is_file() and name in json.loads(recipe.read_text())['methods']


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


def loader(profile):
    """The prebuilt Pongo and storage-capable m1n1 that start this device, if packaged.

    `orchard launch` needs only these two: m1n1 then reads the device's boot
    partition (docs/boot-contract.md), so launching depends on no distro and
    on nothing an install left on the host. Both are built once for every
    Apple device (boot/m1n1/build apple, boot/quiet/build-pongo.py); the
    published wheel carries them in loaders/, a source checkout has none and
    the CLI builds them instead. Returns {'pongo': Path, 'm1n1': Path} or None.
    """
    device_path(profile)
    root = ROOT / 'loaders'
    files = {'pongo': root / 'pongo.bin', 'm1n1': root / 'm1n1.bin'}
    if all(path.is_file() and not path.is_symlink() for path in files.values()):
        return files
    return None


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
