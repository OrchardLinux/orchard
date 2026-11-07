#!/usr/bin/env python3
"""A distribution's root as a block image: the form the installer writes to a partition.

`OUTPUT/root.blocks.zst` holds the image's allocated blocks, `OUTPUT/root.map.json`
where they go, and `OUTPUT/root-image.json` the release manifest's `[root]` table.
"""
import argparse
import os
from pathlib import Path

from ..buildenv import build, native, run
from ..hardware import ROOT

IMAGE = 'localhost/orchard-root-image:latest'


def make(rootfs, output, *, hardware=None):
    recipe = Path(hardware or ROOT) / 'boot/root-image'
    rootfs, output = Path(rootfs).resolve(), Path(output).resolve()
    output.mkdir(parents=True, exist_ok=True)
    build(IMAGE, recipe / 'Containerfile', recipe, arch='arm64')
    command = ['python3', '/recipe/build-inside.py', '/inputs/' + rootfs.name, '/out']
    if native() and os.geteuid() != 0:
        command = ['sudo', '-n', '-E', *command]    # the tree keeps its owners
    # In memory: this is gigabytes, and a Podman machine's disk file never shrinks again.
    run(IMAGE, command, [(recipe, '/recipe', True), (rootfs.parent, '/inputs', True), (output, '/out', False)],
        root=True, arch='arm64', memory='12g')
    return output / 'root-image.json'


def main():
    parser = argparse.ArgumentParser(description=__doc__.split('\n\n')[0])
    parser.add_argument('rootfs', type=Path, help='the root as a compressed tar')
    parser.add_argument('--output', type=Path, required=True)
    args = parser.parse_args()
    print(make(args.rootfs, args.output))


if __name__ == '__main__':
    main()
