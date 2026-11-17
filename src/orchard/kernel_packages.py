"""The installed system's kernel package, from the Orchard package repository (OrchardLinux/packages).

The installer works like pacstrap installing `linux`: the host downloads the
newest published `linux-orchard-PLATFORM` of the root's base, as it downloads
the platform layer, uploads it, and the root's own package manager installs it
(install_device.py). So every system owns its kernel through its package
manager and updates it with the system's ordinary update.

Bases: pacman (Arch Linux ARM; the repository `orchard`, release repo-ARCH,
checked against the index's sha256) and apk (Alpine; the release named ARCH,
checked against the index's checksum of the package's control part). The
packages are built in OrchardLinux/packages with makepkg and abuild; nothing
here builds one.
"""
import base64
import gzip
import hashlib
import io
import json
from pathlib import Path
import re
import tarfile
import zlib

from .http import open_url

REPOSITORY = 'https://github.com/OrchardLinux/packages/releases/download'
# The repositories' architecture names for orchard's kernel architectures (platform.json).
ARCH = {'pacman': {'arm64': 'aarch64', 'arm': 'armv7h'}, 'apk': {'arm64': 'aarch64', 'arm': 'armv7'}}
INDEX = {'pacman': 'repo-{arch}/orchard.db', 'apk': '{arch}/APKINDEX.tar.gz'}
# Which package manager a root has: what install_device.py looks for in the new root.
DETECT = ('if [ -x /newroot/usr/bin/pacman ]; then echo pacman; '
          'elif [ -x /newroot/sbin/apk ] || [ -x /newroot/usr/bin/apk ]; then echo apk; fi')


def layer_platform(layer):
    """(platform, kernel architecture) from a platform layer's platform.json."""
    with tarfile.open(layer, mode='r:gz') as archive:
        for member in archive:
            if member.name == 'usr/lib/orchard/platform.json' and member.isfile():
                description = json.loads(archive.extractfile(member).read())
                return description['platform'], description.get('architecture', 'arm64')
    raise ValueError('The platform layer has no usr/lib/orchard/platform.json')


def pacman_entry(index, name):
    """{file, version, sha256} of NAME in a pacman index (orchard.db), or None."""
    with tarfile.open(fileobj=io.BytesIO(index)) as archive:
        for member in archive:
            if member.name.endswith('/desc') and member.isfile():
                lines = archive.extractfile(member).read().decode().split('\n')
                value = lambda key: lines[lines.index(key) + 1] if key in lines else None
                if value('%NAME%') == name:
                    return {'file': value('%FILENAME%'), 'version': value('%VERSION%'),
                            'sha256': value('%SHA256SUM%')}
    return None


def apk_entry(index, name):
    """{file, version, identity} of NAME in an APKINDEX.tar.gz, or None. identity: the C: field."""
    # A signed index is the signature's tar, then the index's, one gzip stream each.
    with tarfile.open(fileobj=io.BytesIO(gzip.decompress(index)), ignore_zeros=True) as archive:
        for member in archive:
            if member.name == 'APKINDEX' and member.isfile():
                for block in archive.extractfile(member).read().decode().split('\n\n'):
                    fields = dict(line.split(':', 1) for line in block.splitlines() if ':' in line)
                    if fields.get('P') == name:
                        return {'file': f'{name}-{fields["V"]}.apk', 'version': fields['V'],
                                'identity': fields.get('C')}
    return None


def apk_identity(data):
    """apk's identity of a package file: Q1 + base64(SHA-1 of its control stream, as compressed).

    A package is [signature stream] control stream, data stream; the control stream is the one
    whose tar holds .PKGINFO.
    """
    offset = 0
    while offset < len(data):
        decompressor = zlib.decompressobj(31)
        content = decompressor.decompress(data[offset:])
        end = len(data) - len(decompressor.unused_data)
        if b'.PKGINFO' in content[:512]:
            return 'Q1' + base64.b64encode(hashlib.sha1(data[offset:end]).digest()).decode()
        offset = end
    raise ValueError('The apk package has no .PKGINFO')


def resolve(base, platform, architecture):
    """The repository's newest linux-orchard-PLATFORM for BASE: its entry and URL, or None if none is published."""
    arch = ARCH[base][architecture]
    name = 'linux-orchard-' + platform
    directory = INDEX[base].format(arch=arch)
    try:
        with open_url(f'{REPOSITORY}/{directory}', timeout=60) as response:
            index = response.read()
    except OSError:
        return None
    entry = (pacman_entry if base == 'pacman' else apk_entry)(index, name)
    if entry is None or not re.fullmatch(r'[A-Za-z0-9@._+-]+', entry['file'] or ''):
        return None
    release = directory.rsplit('/', 1)[0]
    return {**entry, 'name': name, 'base': base, 'url': f'{REPOSITORY}/{release}/{entry["file"]}'}


def download(entry, directory):
    """ENTRY's file into DIRECTORY, checked against the index; returns its path."""
    with open_url(entry['url'], timeout=600) as response:
        data = response.read()
    if entry['base'] == 'pacman':
        if not entry.get('sha256') or hashlib.sha256(data).hexdigest() != entry['sha256']:
            raise ValueError(f'{entry["file"]} differs from the package index')
    elif not entry.get('identity') or apk_identity(data) != entry['identity']:
        raise ValueError(f'{entry["file"]} differs from the package index')
    path = Path(directory) / entry['file']
    path.write_bytes(data)
    return path
