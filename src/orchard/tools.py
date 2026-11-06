"""Host tools Orchard runs, fetched when needed and pinned by SHA-256. No device operations.

ORCHARD_PALERA1N or ORCHARD_CHECKRA1N selects another binary, unchecked, to test one.
"""
import hashlib
import os
from pathlib import Path
import platform
import shutil
import tempfile

from .cache import shared_cache_root

PALERA1N = 'https://github.com/palera1n/palera1n/releases/download/v2.4/'
CHECKRA1N = 'https://assets.checkra.in/downloads/preview/0.1337.3/'
TOOLS = {
    'palera1n': {
        'version': 'v2.4',
        'hosts': {
            ('Darwin', 'arm64'): (PALERA1N + 'palera1n-macos-universal', 9757024,
                                  'dcf2f827e89a12215685c55f0462cd7d0cbc0551e3acd340bf0216c274c2e83e'),
            ('Darwin', 'x86_64'): (PALERA1N + 'palera1n-macos-universal', 9757024,
                                   'dcf2f827e89a12215685c55f0462cd7d0cbc0551e3acd340bf0216c274c2e83e'),
            ('Linux', 'arm64'): (PALERA1N + 'palera1n-linux-arm64', 5059168,
                                 '0ddcc3e703709f9cf9a85eb7c598f7500e2028fc4c60f3085eecb0b2bcbb138b'),
            ('Linux', 'x86_64'): (PALERA1N + 'palera1n-linux-x86_64', 5042624,
                                  'ea531df933c0c8edab25ecb8f43faedd010177484482bb3744eaa4cf1cd48658'),
        },
    },
    # The unpatched official build. palera1n embeds the same version with patches of its own.
    'checkra1n': {
        'version': '0.1337.3',
        'hosts': {
            ('Darwin', 'arm64'): (CHECKRA1N + 'checkra1n-macos', 1161528,
                                  '3bc1a0247dd9782b18de8945e3bb3b2dc740208b8c78f78634b80a7804a9f9be'),
            ('Darwin', 'x86_64'): (CHECKRA1N + 'checkra1n-macos', 1161528,
                                   '3bc1a0247dd9782b18de8945e3bb3b2dc740208b8c78f78634b80a7804a9f9be'),
            ('Linux', 'arm64'): (CHECKRA1N + 'checkra1n-linux-arm64', 550664,
                                 '078053c11e9a301f0e71b87b6b854e6c8a69c87972325dd0c90deeeba1c3cb70'),
            ('Linux', 'x86_64'): (CHECKRA1N + 'checkra1n-linux-x86_64', 554232,
                                  '53154d87594cf67c56659f24547f1b5bb5336f84ea58c2d42f25a7b2df81a964'),
        },
    },
}
ARCH = {'x86_64': 'x86_64', 'amd64': 'x86_64', 'aarch64': 'arm64', 'arm64': 'arm64'}


def _pin(name):
    """(url, bytes, sha256) of the tool for this host."""
    key = platform.system(), ARCH.get(platform.machine().lower())
    try:
        return TOOLS[name]['hosts'][key]
    except KeyError:
        raise ValueError(f'{name} has no build for {platform.system()} {platform.machine()}') from None


def _sha256(path):
    digest = hashlib.sha256()
    with Path(path).open('rb') as stream:
        for block in iter(lambda: stream.read(1 << 20), b''):
            digest.update(block)
    return digest.hexdigest()


def _matches(path, sha256):
    path = Path(path)
    return path.is_file() and os.access(path, os.X_OK) and _sha256(path) == sha256


def _location(name):
    return shared_cache_root() / 'tools' / name / TOOLS[name]['version'] / name


def find(name):
    """The usable binary of this tool already on this host, or None; fetches nothing."""
    override = os.environ.get(f'ORCHARD_{name.upper()}')
    if override:
        return override
    sha256 = _pin(name)[2]
    candidates = [_location(name), shutil.which(name), Path.home() / '.local/bin' / name]
    return next((str(path) for path in candidates if path and _matches(path, sha256)), None)


def ensure_tool(name, say=None, progress=None, clock=None):
    """The path of this tool, fetched into the Orchard cache if this host doesn't have it."""
    existing = find(name)
    if existing:
        return existing
    from .releases import materialize
    url, size, sha256 = _pin(name)
    if say:
        say(f'Getting {name} {TOOLS[name]["version"]}')
    source = materialize({'url': url, 'bytes': size, 'sha256': sha256}, None, progress, clock, label=name)
    destination = _location(name)
    destination.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.NamedTemporaryFile(dir=destination.parent, delete=False) as output:
        partial = Path(output.name)
        try:
            with Path(source).open('rb') as stream:
                shutil.copyfileobj(stream, output)
            partial.chmod(0o755)
            os.replace(partial, destination)
        finally:
            partial.unlink(missing_ok=True)
    if not _matches(destination, sha256):
        destination.unlink(missing_ok=True)
        raise ValueError(f'{name} does not match its pinned SHA-256')
    return str(destination)
