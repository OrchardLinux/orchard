#!/usr/bin/env python3
"""The source keys of what a build publishes: component-keys.json.

A component's source key is a hash of the files that decide it: for a
platform's kernel the upstream lock (its commit of OrchardLinux/linux), the
config, the toolchain recipes, its boards' own modules, the descriptor's
build fields and the kernel producers' version; for m1n1 and the static tools their recipes
(producers/contract.toml names them). Anyone with a checkout computes them
without building anything; a build whose checkout gives the same key takes
the published component instead of making it.

  ci/keys.py --output component-keys.json PLATFORM ...

Python 3.11 and its standard library only.
"""
import argparse
import hashlib
import json
import os
from pathlib import Path
import subprocess
import sys
import tomllib

ROOT = Path(__file__).resolve().parents[1]
KEYS = 'component-keys.json'
SUMS = 'SHA256SUMS'
# The rolling pointer to the newest nightly, and where builds are published.
TAG = 'nightly'
RELEASES = 'https://github.com/OrchardLinux/orchard/releases/download'
# m1n1's published files, all from its one build.
M1N1_FILES = ['m1n1.bin', 'm1n1-idevice.macho', 'monitor-stub.macho', 'm1n1-armv7.macho']


def contract(root):
    with (root / 'producers/contract.toml').open('rb') as stream:
        return tomllib.load(stream)


def recipe_entries(root, paths, *, ignore=('.work', '.git', '__pycache__')):
    """{relative path: "mode:sha256"} for recipe files, in hashing order."""
    entries = {}
    for relative in paths:
        top = root / relative
        files = [top] if top.is_file() else []
        if top.is_dir():
            for directory, dirs, names in os.walk(top):
                dirs[:] = sorted(d for d in dirs if d not in ignore)
                files.extend(Path(directory) / name for name in sorted(names))
        for path in files:
            # Git records only the executable bit, so the key does too: builders
            # with different umasks then hash the same checkout the same way.
            mode = 0o755 if path.stat().st_mode & 0o100 else 0o644
            entries[str(path.relative_to(root))] = f'{mode}:{hashlib.sha256(path.read_bytes()).hexdigest()}'
    return entries


def _hash(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True).encode()).hexdigest()


def boards(root, platform):
    """The boards of PLATFORM, sorted: those whose device.toml names it as their kernel's."""
    found = []
    for path in sorted((root / 'devices').glob('*/device.toml')):
        with path.open('rb') as stream:
            if tomllib.load(stream).get('kernel', {}).get('platform') == platform:
                found.append(path.parent.name)
    return found


def source_key(root, platform):
    """The key of PLATFORM's compiled kernel, from the checkout's files alone."""
    def series(*args):
        return subprocess.check_output([sys.executable, str(root / 'kernel/series.py'), *args], text=True)
    described = json.loads(series('--platform', platform))
    paths = [described['upstream'], described['config'], 'kernel/Containerfile', 'kernel/install-toolchain']
    # The modules its boards bring are built into the same bundle.
    modules = contract(root).get('board-modules')
    if modules:
        paths += [f'{modules}/{board}' for board in boards(root, platform) if (root / modules / board).is_dir()]
    return _hash({
        'platform': platform,
        'build': {key: described[key] for key in ('arch', 'image', 'dtbs')},
        'recipes': recipe_entries(root, paths),
        'producer': contract(root)['kernel-producer'],
    })


def shared_keys(root):
    """{name: key} of the components every platform shares, from their recipes."""
    inputs = contract(root)['inputs']
    return {name: _hash({'recipes': recipe_entries(root, inputs[name])}) for name in ('m1n1', 'tools')}


def keys(root, platforms):
    shared = shared_keys(root)
    return {'schema': 1,
            'platforms': {platform: {'key': source_key(root, platform),
                                     'asset': f'{platform}-components.tar.gz'} for platform in platforms},
            'shared': {'m1n1': {'key': shared['m1n1'], 'asset': 'm1n1.bin', 'files': M1N1_FILES},
                       'tools': {'key': shared['tools'], 'asset': 'tool-components.tar.gz'}}}


def open_url(url, timeout=60):
    """URL over HTTPS, also after its redirects."""
    import urllib.request
    from urllib.parse import urlsplit

    def check(value):
        if urlsplit(value).scheme != 'https':
            raise ValueError(f'Not an HTTPS URL: {value}')

    class Redirect(urllib.request.HTTPRedirectHandler):
        def redirect_request(self, request, fp, code, message, headers, newurl):
            check(newurl)
            return super().redirect_request(request, fp, code, message, headers, newurl)

    check(url)
    return urllib.request.build_opener(Redirect()).open(url, timeout=timeout)


def fetch(url, limit):
    """A small published file, whole."""
    with open_url(url) as response:
        data = response.read(limit + 1)
    if len(data) > limit:
        raise ValueError(f'{url} is larger than expected')
    return data


def sums(text):
    """{name: sha256} of a SHA256SUMS file."""
    rows = {}
    for line in text.splitlines():
        parts = line.split()
        if len(parts) == 2 and len(parts[0]) == 64:
            rows[parts[1].removeprefix('./')] = parts[0]
    return rows


def main():
    parser = argparse.ArgumentParser(description=__doc__.split('\n\n')[0])
    parser.add_argument('--output', type=Path, required=True)
    parser.add_argument('platform', nargs='+')
    args = parser.parse_args()
    text = json.dumps(keys(ROOT, args.platform), indent=2, sort_keys=True) + '\n'
    args.output.write_text(text)
    print(text, end='')


if __name__ == '__main__':
    main()
