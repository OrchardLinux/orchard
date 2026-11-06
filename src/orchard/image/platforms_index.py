#!/usr/bin/env python3
"""What an orchard build offers a distribution: finished platform layers, the install tools, and platforms.toml.

A distribution names an orchard version and takes its kernels, loaders and
install tools from these files, so it publishes only its root. Files are named
relative to wherever platforms.toml was fetched from, because a nightly's tag
isn't known while it is built.
"""
import argparse
import gzip
import io
from pathlib import Path
import tarfile

from . import platform
from .integrity import sha
from .publish import toml

INSTALL_TOOLS = ('orchard-write', 'dropbear', 'resize2fs', 'tune2fs')
LOADERS = ('pongo.bin', 'm1n1.bin')


def receipt(path):
    return {'file': path.name, 'bytes': path.stat().st_size, 'sha256': sha(path)}


def write_install_tools(tools, output):
    """The static programs the installer adds to a kernel's initrd, as a reproducible archive."""
    with output.open('xb') as raw, gzip.GzipFile(fileobj=raw, mode='wb', filename='', mtime=0) as compressed, \
            tarfile.open(fileobj=compressed, mode='w|', format=tarfile.PAX_FORMAT) as archive:
        for name in INSTALL_TOOLS:
            data = (tools / name).read_bytes()
            member = tarfile.TarInfo(name)
            member.size, member.mode = len(data), 0o755
            member.uname = member.gname = 'root'
            archive.addfile(member, io.BytesIO(data))


def build(directory, *, kernels, tools, commit):
    """Write every arm64 platform's layer, the install tools and platforms.toml into DIRECTORY.

    KERNELS holds PLATFORM/components/kernel for each platform; TOOLS the static
    tools. The loaders must already be in DIRECTORY, where they are published.
    """
    layers = {name: platform.build(name, directory, bundle=kernels / name / 'components/kernel',
                                   busybox=tools / 'busybox')
              for name in platform.platforms()}
    write_install_tools(tools, directory / 'install-tools.tar.gz')
    document = {'schema': 1, 'kind': 'orchard.platforms', 'commit': commit,
                'loaders': {path.stem: receipt(path) for path in (directory / name for name in LOADERS)},
                'install_tools': receipt(directory / 'install-tools.tar.gz'),
                'platforms': layers}
    (directory / 'platforms.toml').write_text(toml(document))
    return document


def main():
    parser = argparse.ArgumentParser(description=__doc__.split('\n\n')[0])
    parser.add_argument('directory', type=Path, help='the published files; everything is written here')
    parser.add_argument('--kernels', type=Path, required=True, help='holds PLATFORM/components/kernel')
    parser.add_argument('--tools', type=Path, required=True, help="orchard's static tools")
    parser.add_argument('--commit', required=True)
    args = parser.parse_args()
    build(args.directory, kernels=args.kernels, tools=args.tools, commit=args.commit)


if __name__ == '__main__':
    main()
