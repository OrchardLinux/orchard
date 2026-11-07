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
from .elf import architecture, require_static
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


def build(directory, *, kernels, tools, commit, loaders=None, platform_inputs=None):
    """Write the layers of KERNELS ({platform: bundle}), the install tools and platforms.toml into DIRECTORY.

    TOOLS is the directory of static tools. LOADERS ({'pongo': Path, 'm1n1': Path}) are copied
    there; by default they're already in DIRECTORY, where they are published.
    """
    for name, path in (loaders or {}).items():
        shutil.copyfile(path, directory / f'{name}.bin')
    layers = {}
    for name, bundle in kernels.items():
        options = {'bundle': bundle, 'busybox': tools / 'busybox'}
        options.update((platform_inputs or {}).get(name, {}))
        layers[name] = platform.build(name, directory, **options)
    write_install_tools(tools, directory / 'install-tools.tar.gz')
    document = {'schema': 1, 'kind': 'orchard.platforms', 'commit': commit,
                'loaders': {path.stem: receipt(path) for path in (directory / name for name in LOADERS)},
                'install_tools': receipt(directory / 'install-tools.tar.gz'),
                'platforms': layers}
    (directory / 'platforms.toml').write_text(toml(document))
    return document


def input_plan(inputs):
    """Validate every selected input before the index writes any output.

    The block-image installer currently publishes one m1n1 and one native
    install-tools archive. Cascadia's ARMv7 launch contract remains separate.
    """
    from ..hardware import ROOT
    from ..kernel import descriptor
    from ..ram import boards_by_platform

    if not isinstance(inputs, dict) or inputs.get('schema', 1) != 1:
        raise ValueError('Expected schema-1 Orchard inputs')
    rows = inputs.get('platforms')
    if not isinstance(rows, dict) or not rows:
        raise ValueError('Orchard inputs name no platforms')
    known = boards_by_platform(ROOT)
    if set(rows) - set(known):
        raise ValueError('Unknown platform in Orchard inputs')
    if inputs.get('architecture', 'arm64') != 'arm64':
        raise ValueError('The platform index requires the ARM64 m1n1 install contract; use the ARMv7 platform producer directly')
    shared = inputs.get('shared', {})
    if not isinstance(shared, dict):
        raise ValueError('Invalid shared Orchard inputs')

    def regular(value, role):
        if not isinstance(value, str) or not value or not Path(value).is_absolute():
            raise ValueError(role + ' needs an absolute file path')
        path = Path(value)
        if path.is_symlink() or not path.is_file():
            raise ValueError(role + ' needs a regular file')
        return path

    def executable(value, role):
        path = regular(value, role)
        data = path.read_bytes()
        if architecture(data) != 'arm64':
            raise ValueError(role + ' differs from the ARM64 root architecture')
        require_static(data)
        return path

    kernels, options, tool_sets, loaders = {}, {}, [], []
    for name, row in rows.items():
        if not isinstance(row, dict):
            raise ValueError('Invalid platform input record: ' + name)
        described = descriptor(ROOT, name)
        if (row.get('architecture', described['arch']) != 'arm64' or described['arch'] != 'arm64' or
                row.get('image', described['image']) != described['image']):
            raise ValueError('Platform ' + name + ' differs from the ARM64 root architecture or kernel image')
        board_dtbs = row.get('boards')
        all_boards = known[name]
        if (not isinstance(board_dtbs, dict) or not board_dtbs or
                any(board not in all_boards or dtb != all_boards[board]
                    for board, dtb in board_dtbs.items())):
            raise ValueError('Platform ' + name + ' has an invalid board-to-DTB selection')
        if board_dtbs == all_boards:
            device = None
        elif len(board_dtbs) == 1:
            device = next(iter(board_dtbs))
        else:
            raise ValueError('A platform layer requires one selected board or every board')
        formats = {platform.root_format(ROOT, name, device=board) for board in board_dtbs}
        if len(formats) != 1 or row.get('root_format', 'gpt') != next(iter(formats)):
            raise ValueError('Platform ' + name + ' root format differs from its selected boards')
        root_format = next(iter(formats))
        loader = row['loader'] if 'loader' in row else {'kind': 'm1n1', 'file': shared.get('m1n1')}
        if not isinstance(loader, dict) or loader.get('kind') != 'm1n1':
            raise ValueError('The ARM64 platform index requires an m1n1 loader')
        loaders.append(regular(loader.get('file'), name + ' loader'))
        tools = row['tools'] if 'tools' in row else shared.get('tools')
        if not isinstance(tools, dict):
            raise ValueError('Platform ' + name + ' has no native tool record')
        selected_tools = {tool: executable(tools.get(tool), name + ' ' + tool)
                          for tool in (*INSTALL_TOOLS, 'busybox')}
        layer = {'busybox': selected_tools['busybox'], 'root_architecture': 'arm64', 'device': device}
        if root_format == 'lwvm':
            mapper = executable(tools.get('lwvm'), name + ' LwVM mapper')
            dropbearkey = executable(tools.get('dropbearkey'), name + ' dropbearkey')
            if selected_tools['dropbear'].parent != dropbearkey.parent:
                raise ValueError('The LwVM rescue tools must share a directory')
            layer.update(lwvm=mapper, rescue_tools=dropbearkey.parent)
        elif tools.get('lwvm') is not None:
            raise ValueError('A GPT platform cannot supply an LwVM mapper')
        bundle = row.get('kernel')
        if (not isinstance(bundle, str) or not Path(bundle).is_absolute() or
                Path(bundle).is_symlink() or not Path(bundle).is_dir()):
            raise ValueError('Platform ' + name + ' needs a staged kernel directory')
        kernels[name], options[name] = Path(bundle), layer
        tool_sets.append(selected_tools)
    if any(sha(loader) != sha(loaders[0]) for loader in loaders[1:]):
        raise ValueError('One platform index requires the same m1n1 loader for every selected platform')
    for tools in tool_sets[1:]:
        if any(sha(tools[name]) != sha(tool_sets[0][name]) for name in INSTALL_TOOLS):
            raise ValueError('One platform index requires the same install tools for every selected platform')
    return kernels, options, tool_sets[0], {'pongo': regular(shared.get('pongo'), 'Pongo'), 'm1n1': loaders[0]}


def from_inputs(directory, inputs):
    """platforms.toml and its files from the exact selected Orchard input records."""
    kernels, options, selected_tools, loaders = input_plan(inputs)
    with tempfile.TemporaryDirectory() as temporary:
        tools = Path(temporary)
        for name, path in selected_tools.items():
            shutil.copyfile(path, tools / name)
        Path(directory).mkdir(parents=True, exist_ok=True)
        return build(directory, kernels=kernels,
                     tools=tools, commit=(inputs.get('orchard') or {}).get('commit') or 'unknown',
                     loaders=loaders, platform_inputs=options)


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
    if args.release:
        args.directory.mkdir(parents=True, exist_ok=True)
        from_release(args.directory, args.release, args.platform)
    elif args.inputs:
        from_inputs(args.directory, json.loads(args.inputs.read_text()))
    else:
        if not (args.tools and args.commit):
            parser.error('--kernels needs --tools and --commit')
        args.directory.mkdir(parents=True, exist_ok=True)
        build(args.directory, kernels={name: args.kernels / name / 'components/kernel' for name in platform.platforms()},
              tools=args.tools, commit=args.commit)


if __name__ == '__main__':
    main()
