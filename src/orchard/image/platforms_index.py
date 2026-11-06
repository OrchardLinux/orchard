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
import json
from pathlib import Path
import shutil
import tarfile
import tempfile
import tomllib

from . import platform
from .integrity import sha
from .publish import toml

INSTALL_TOOLS = ('orchard-write', 'dropbear', 'resize2fs', 'tune2fs')
LOADERS = ('pongo.bin', 'm1n1.bin')
REPOSITORY = 'OrchardLinux/orchard'


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


def receipts(document):
    """Every file platforms.toml names."""
    return [*document['loaders'].values(), document['install_tools'], *document['platforms'].values()]


def build(directory, *, kernels, tools, commit, loaders=None):
    """Write the layers of KERNELS ({platform: bundle}), the install tools and platforms.toml into DIRECTORY.

    TOOLS is the directory of static tools. LOADERS ({'pongo': Path, 'm1n1': Path}) are copied
    there; by default they're already in DIRECTORY, where they are published.
    """
    for name, path in (loaders or {}).items():
        shutil.copyfile(path, directory / f'{name}.bin')
    layers = {name: platform.build(name, directory, bundle=bundle, busybox=tools / 'busybox')
              for name, bundle in kernels.items()}
    write_install_tools(tools, directory / 'install-tools.tar.gz')
    document = {'schema': 1, 'kind': 'orchard.platforms', 'commit': commit,
                'loaders': {path.stem: receipt(path) for path in (directory / name for name in LOADERS)},
                'install_tools': receipt(directory / 'install-tools.tar.gz'),
                'platforms': layers}
    (directory / 'platforms.toml').write_text(toml(document))
    return document


def from_inputs(directory, inputs):
    """platforms.toml and its files, from a document of orchard.inputs: this build's own kernels."""
    with tempfile.TemporaryDirectory() as temporary:
        tools = Path(temporary)
        for name, path in inputs['shared']['tools'].items():
            shutil.copyfile(path, tools / name)
        return build(directory, kernels={name: Path(row['kernel']) for name, row in inputs['platforms'].items()},
                     tools=tools, commit=(inputs.get('orchard') or {}).get('commit') or 'unknown',
                     loaders={name: inputs['shared'][name] for name in ('pongo', 'm1n1')})


def fetch(url, path, entry=None):
    """URL into PATH, checked against ENTRY's receipt when given."""
    from urllib.request import urlopen
    with urlopen(url, timeout=60) as response, path.open('xb') as output:
        shutil.copyfileobj(response, output, 1 << 20)
    if entry and sha(path) != entry['sha256']:
        raise ValueError(f'{url} differs from its receipt')


def from_release(directory, tag, platforms=None, *, repository=REPOSITORY):
    """platforms.toml and its files from a published orchard build, by its immutable tag.

    PLATFORMS keeps only those; the index then records base_url, so orchard.image.publish links
    these files instead of hosting copies.
    """
    base = f'https://github.com/{repository}/releases/download/{tag}'
    with tempfile.TemporaryDirectory() as temporary:
        index = Path(temporary) / 'platforms.toml'
        fetch(base + '/platforms.toml', index)
        document = tomllib.loads(index.read_text())
    if document.get('schema') != 1 or document.get('kind') != 'orchard.platforms':
        raise ValueError(f'{tag} has no platforms.toml this orchard reads')
    if platforms:
        missing = sorted(set(platforms) - set(document['platforms']))
        if missing:
            raise ValueError(f'{tag} lacks {", ".join(missing)}')
        document['platforms'] = {name: document['platforms'][name] for name in platforms}
    for entry in receipts(document):
        fetch(base + '/' + entry['file'], directory / entry['file'], entry)
    document['base_url'] = base
    (directory / 'platforms.toml').write_text(toml(document))
    return document


def main():
    parser = argparse.ArgumentParser(description=__doc__.split('\n\n')[0])
    parser.add_argument('directory', type=Path, help='everything is written here')
    source = parser.add_mutually_exclusive_group(required=True)
    source.add_argument('--release', metavar='TAG', help='take it finished from this published orchard build')
    source.add_argument('--inputs', type=Path, help='build it from a document of orchard.inputs')
    source.add_argument('--kernels', type=Path, help='build it from PLATFORM/components/kernel below this directory')
    parser.add_argument('--tools', type=Path, help="with --kernels: orchard's static tools")
    parser.add_argument('--commit', help='with --kernels: the commit they were built from')
    parser.add_argument('--platform', action='append', help='with --release: only this platform; repeat')
    args = parser.parse_args()
    args.directory.mkdir(parents=True, exist_ok=True)
    if args.release:
        from_release(args.directory, args.release, args.platform)
    elif args.inputs:
        from_inputs(args.directory, json.loads(args.inputs.read_text()))
    else:
        if not (args.tools and args.commit):
            parser.error('--kernels needs --tools and --commit')
        build(args.directory, kernels={name: args.kernels / name / 'components/kernel' for name in platform.platforms()},
              tools=args.tools, commit=args.commit)


if __name__ == '__main__':
    main()
