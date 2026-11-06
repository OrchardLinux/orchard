"""What a distribution build gets from orchard, as one JSON file: the distribution contract's entry.

  python -m orchard.inputs --output inputs.json [--platform NAME ...] [--kernel-dir PLATFORM=DIR ...]
                           [--m1n1 FILE] [--pongo FILE] [--tools DIR]

A distribution's build runs orchard's producers as commands from an orchard
snapshot (a checkout, the newest nightly's tooling, or a release's
orchard-source.tar.gz unpacked) with PYTHONPATH=SNAPSHOT/src, and imports none
of orchard's Python. This command resolves everything the distribution takes
finished: per platform its kernel bundle (Image.gz, every board's DTB,
modules; the nightly's when its source key matches, else built), its boards
and their DTBs, and the shared m1n1, Pongo and static tools, plus the
snapshot's revision, and under "produced" how each component was got (the
commands that built it, or the nightly it was downloaded from; the steps
`orchard build --explain` records). Paths point into orchard's cache and stay valid for the
build. The other producers of the contract (docs/distribution-contract.md)
take these paths as arguments.

Without --platform: every platform a distribution root serves (arm64).
Components given explicitly (--kernel-dir, --m1n1, --pongo, --tools, e.g.
from a release's component bundles in CI) are used as they are: with all of
them given, nothing is fetched or built.
"""
import argparse
import json
from pathlib import Path
import sys

SCHEMA = 1


TOOLS = ('busybox', 'dropbear', 'dropbearkey', 'wpa_supplicant', 'wpa_cli', 'iw', 'orchard-write', 'resize2fs', 'tune2fs')


def resolve(platforms=None, explicit=None, *, hardware=None, cache=None, shared=None, kernel_tree=None):
    """The inputs document for PLATFORMS (default: every arm64 platform).

    SHARED: {'m1n1', 'pongo', 'tools'} given explicitly; the rest is resolved.
    KERNEL_TREE: a developer's kernel tree to build every selected platform's kernel from.
    """
    from .build import prepare_shared
    from .hardware import ROOT, revision
    from .image import platform
    hardware = Path(hardware or ROOT)
    explicit = explicit or {}
    known = platform.platforms(hardware)
    selected = list(platforms or known)
    unknown = sorted(set(selected) - set(known)) + sorted(set(explicit) - set(selected))
    if unknown:
        raise ValueError(f'Unknown or unselected platform {", ".join(unknown)}; known: {", ".join(known)}')
    given = {key: Path(value) for key, value in (shared or {}).items() if value is not None}
    found = {} if len(given) == 3 else prepare_shared(hardware, cache)
    m1n1 = given.get('m1n1') or found['loader']
    pongo = given.get('pongo') or found['pongo']
    tools = given.get('tools') or Path(found['tools'])
    for path in (m1n1, pongo, *(tools / name for name in TOOLS)):
        if not Path(path).is_file():
            raise ValueError(f'Missing component {path}')
    try:
        orchard = revision()
    except ValueError:
        orchard = {'commit': None, 'modified': None}
    def bundle(name):
        if name in explicit:
            return explicit[name]
        if kernel_tree is not None:
            from .build import prepare_kernel
            return prepare_kernel(hardware, cache, device=sorted(platform.boards(name, hardware))[0],
                                  kernel_tree=kernel_tree)
        return platform.kernel(name, hardware=hardware, cache=cache)
    return {
        'schema': SCHEMA,
        'orchard': orchard,
        'platforms': {name: {'kernel': str(Path(bundle(name)).resolve()),
                             'boards': platform.boards(name, hardware)}
                      for name in selected},
        'shared': {'m1n1': str(Path(m1n1).resolve()), 'pongo': str(Path(pongo).resolve()),
                   'tools': {name: str((tools / name).resolve()) for name in TOOLS}},
    }


def main():
    parser = argparse.ArgumentParser(description=__doc__.split('\n\n')[0])
    parser.add_argument('--output', type=Path, required=True, help='the JSON document to write')
    parser.add_argument('--platform', action='append', help='a platform to build for; repeat (default: every one)')
    parser.add_argument('--kernel-dir', action='append', default=[], metavar='PLATFORM=DIRECTORY',
                        help="a platform's staged kernel bundle instead of orchard's")
    parser.add_argument('--m1n1', type=Path, help="orchard's m1n1 to use as it is")
    parser.add_argument('--pongo', type=Path, help='the Pongo to use as it is')
    parser.add_argument('--tools', type=Path, help='a directory with the static tools to use as they are')
    parser.add_argument('--kernel-tree', type=Path, help='build the kernels from this kernel tree '
                        '(default: ORCHARD_KERNEL_TREE, which orchard build --kernel-tree sets)')
    args = parser.parse_args()
    explicit = {}
    for value in args.kernel_dir:
        name, separator, path = value.partition('=')
        if not separator:
            parser.error('--kernel-dir takes PLATFORM=DIRECTORY')
        explicit[name] = Path(path)
    try:
        import os
        tree = args.kernel_tree or (Path(os.environ['ORCHARD_KERNEL_TREE']) if os.environ.get('ORCHARD_KERNEL_TREE')
                                    else None)
        from . import explain
        explain.start()
        try:
            document = resolve(args.platform, explicit, shared={'m1n1': args.m1n1, 'pongo': args.pongo,
                                                                'tools': args.tools}, kernel_tree=tree)
        finally:
            steps = explain.stop()
    except ValueError as error:
        sys.exit(str(error))
    # How each component was got: built (the commands), reused from the cache or downloaded from a nightly.
    document['produced'] = steps
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(document, indent=2, sort_keys=True) + '\n')
    print(f'orchard inputs for {", ".join(document["platforms"])}: {args.output}')


if __name__ == '__main__':
    main()
