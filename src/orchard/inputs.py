"""What a distribution build gets from orchard, as one JSON file: the distribution contract's entry.

  python -m orchard.inputs --output inputs.json [--platform NAME ...] --kernel-dir PLATFORM=DIR ...
                           --m1n1 FILE --pongo FILE --tools DIR [--loader BOARD=FILE ...]

A distribution's build runs orchard's producers as commands from an orchard
snapshot (a checkout, or a release's orchard-source.tar.gz unpacked) with
PYTHONPATH=SNAPSHOT/src, and imports none of orchard's Python. This command
writes down everything the distribution takes finished: per platform its
kernel bundle (Image.gz, every board's DTB, modules), its boards and their
DTBs, and the shared m1n1, Pongo and static tools, plus the snapshot's
revision. The other producers of the contract
(docs/distribution-contract.md) take these paths as arguments.

Every component is given: --kernel-dir per platform, --m1n1, --pongo and
--tools, e.g. from a release's component bundles in CI. Nothing is fetched or
built here; `orchard build` (the orchard command, OrchardLinux/tools) builds
or downloads them and writes the same document itself, as `orchard inputs`.

Without --platform: every platform a distribution root serves (arm64).
With 32-bit platforms (--platform apple-armv7) the document is that root's:
"architecture": "arm", ARMv7 tools under "shared", and per platform, instead
of m1n1 and Pongo, the Cascadia loader of each board that has one ("loaders",
board to file; --loader BOARD=FILE). One document is one root architecture:
arm64 and ARMv7 platforms do not mix.
"""
import argparse
import json
from pathlib import Path
import sys

SCHEMA = 1


TOOLS = ('busybox', 'dropbear', 'dropbearkey', 'wpa_supplicant', 'wpa_cli', 'iw', 'orchard-write', 'orchard-install-usb',
         'resize2fs', 'tune2fs')


def _missing(what):
    return ValueError(f'{what} is not given. `orchard build` resolves a build\'s inputs itself; '
                      'by hand, run `orchard inputs` (the orchard command, OrchardLinux/tools) or pass every component')


def _revision():
    from .hardware import revision
    try:
        return revision()
    except ValueError:
        return {'commit': None, 'modified': None}


def resolve(platforms=None, explicit=None, *, hardware=None, shared=None, loaders=None):
    """The inputs document for PLATFORMS (default: every arm64 platform).

    EXPLICIT: {platform: its staged kernel bundle}. SHARED: {'m1n1', 'pongo', 'tools'}.
    LOADERS: ARMv7 only, {board: Cascadia loader}.
    """
    from .hardware import ROOT
    from .image import platform
    hardware = Path(hardware or ROOT)
    explicit = explicit or {}
    known = platform.platforms(hardware)
    armv7 = platform.platforms(hardware, architecture='arm')
    selected = list(platforms or known)
    if selected and set(selected) <= set(armv7):
        return _resolve_armv7(selected, explicit, hardware=hardware, shared=shared, loaders=loaders)
    if set(selected) & set(armv7):
        raise ValueError('One document is one root architecture: choose arm64 or ARMv7 platforms, not both')
    if loaders:
        raise ValueError('Cascadia loaders are for ARMv7 platforms')
    unknown = sorted(set(selected) - set(known)) + sorted(set(explicit) - set(selected))
    if unknown:
        raise ValueError(f'Unknown or unselected platform {", ".join(unknown)}; known: {", ".join(known)}')
    given = {key: Path(value) for key, value in (shared or {}).items() if value is not None}
    for key, option in (('m1n1', '--m1n1'), ('pongo', '--pongo'), ('tools', '--tools')):
        if key not in given:
            raise _missing(option)
    m1n1, pongo, tools = given['m1n1'], given['pongo'], given['tools']
    for path in (m1n1, pongo, *(tools / name for name in TOOLS)):
        if not Path(path).is_file():
            raise ValueError(f'Missing component {path}')
    return {
        'schema': SCHEMA,
        'orchard': _revision(),
        'platforms': {name: {'kernel': str(Path(_bundle(name, explicit)).resolve()),
                             'boards': platform.boards(name, hardware)}
                      for name in selected},
        'shared': {'m1n1': str(Path(m1n1).resolve()), 'pongo': str(Path(pongo).resolve()),
                   'tools': {name: str((tools / name).resolve()) for name in TOOLS}},
    }


def _bundle(name, explicit):
    if name not in explicit:
        raise _missing(f'--kernel-dir {name}=DIRECTORY')
    return explicit[name]


def _resolve_armv7(selected, explicit, *, hardware, shared, loaders):
    """The ARMv7 root's document: its kernels, its static tools and its boards' Cascadia loaders."""
    from .hardware import payload_profile

    def has_profile(board, hardware):
        try:
            payload_profile(board, 'cascadia', hardware)
        except ValueError:
            return False
        return True
    from .image import platform
    unknown = sorted(set(explicit) - set(selected))
    if unknown:
        raise ValueError(f'Unselected platform {", ".join(unknown)}')
    given = {key: value for key, value in (shared or {}).items() if value is not None}
    if set(given) - {'tools'}:
        raise ValueError('ARMv7 platforms take no m1n1 or Pongo: their boards start the Cascadia loader')
    if 'tools' not in given:
        raise _missing('--tools')
    boards = {name: platform.boards(name, hardware) for name in selected}
    tools = Path(given['tools'])
    loaders = {board: Path(path) for board, path in (loaders or {}).items()}
    strays = sorted(set(loaders) - {board for rows in boards.values() for board in rows})
    if strays:
        raise ValueError(f'{", ".join(strays)}: not a board of {", ".join(selected)}')
    rows = {}
    for name in selected:
        found = {}
        for board in sorted(boards[name]):
            if board in loaders:
                found[board] = loaders[board]
            elif has_profile(board, hardware):
                raise _missing(f'--loader {board}=FILE')
        rows[name] = {'kernel': str(Path(_bundle(name, explicit)).resolve()),
                      'boards': boards[name], 'loaders': {board: str(Path(path).resolve()) for board, path in found.items()}}
    for path in (*(tools / name for name in TOOLS), *(Path(path) for row in rows.values() for path in row['loaders'].values())):
        if not Path(path).is_file():
            raise ValueError(f'Missing component {path}')
    return {'schema': SCHEMA, 'orchard': _revision(), 'architecture': 'arm', 'platforms': rows,
            'shared': {'tools': {name: str((tools / name).resolve()) for name in TOOLS}}}


def main():
    parser = argparse.ArgumentParser(description=__doc__.split('\n\n')[0])
    parser.add_argument('--output', type=Path, required=True, help='the JSON document to write')
    parser.add_argument('--platform', action='append', help='a platform to build for; repeat (default: every one)')
    parser.add_argument('--kernel-dir', action='append', default=[], metavar='PLATFORM=DIRECTORY',
                        help="a platform's staged kernel bundle instead of orchard's")
    parser.add_argument('--m1n1', type=Path, help="orchard's m1n1 to use as it is")
    parser.add_argument('--pongo', type=Path, help='the Pongo to use as it is')
    parser.add_argument('--tools', type=Path, help='a directory with the static tools to use as they are')
    parser.add_argument('--loader', action='append', default=[], metavar='BOARD=FILE',
                        help="ARMv7: a board's Cascadia loader to use as it is; repeat")
    parser.add_argument('--kernel-tree', type=Path, help=argparse.SUPPRESS)
    args = parser.parse_args()
    if args.kernel_tree:
        parser.error('--kernel-tree: `orchard build --kernel-tree DIR` builds the kernels of a tree')
    explicit = {}
    for value in args.kernel_dir:
        name, separator, path = value.partition('=')
        if not separator:
            parser.error('--kernel-dir takes PLATFORM=DIRECTORY')
        explicit[name] = Path(path)
    loaders = {}
    for value in args.loader:
        board, separator, path = value.partition('=')
        if not separator or not board or not path:
            parser.error('--loader takes BOARD=FILE')
        loaders[board] = Path(path)
    try:
        document = resolve(args.platform, explicit, shared={'m1n1': args.m1n1, 'pongo': args.pongo,
                                                            'tools': args.tools}, loaders=loaders)
    except ValueError as error:
        sys.exit(str(error))
    # How each component was got; everything here was given, so nothing was produced.
    document['produced'] = []
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(document, indent=2, sort_keys=True) + '\n')
    print(f'orchard inputs for {", ".join(document["platforms"])}: {args.output}')


if __name__ == '__main__':
    main()
