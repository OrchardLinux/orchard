"""Published components: a distribution build takes the nightly's instead of building them.

A component's source key is a hash of the files that decide it: for a
platform's kernel the upstream lock, the series and every patch it applies,
the config, the toolchain recipes, the descriptor's build fields and
KERNEL_PRODUCER; for m1n1 and the static tools their recipes. Anyone with an
orchard checkout computes it without building anything. CI publishes the
keys of what it built (component-keys.json, listed in SHA256SUMS); a
distribution build whose checkout gives the same key downloads that asset,
checks it against SHA256SUMS and uses it. Any other key, no network, or
ORCHARD_PUBLISHED=off (ORCHARD_KERNEL=local for kernels only): it is built as
before. A published component is CI's build of exactly this state, not a
claim that a local build gives the same bytes (CI installs the toolchains
natively, a local build runs them in its containers).

  python -m orchard.published keys --output component-keys.json [PLATFORM ...]
"""
import argparse
import hashlib
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tarfile
import tempfile

KEYS = 'component-keys.json'
SUMS = 'SHA256SUMS'
TAG = 'nightly'
LIMIT = 2 * 1024 * 1024 * 1024
TOOLS = ('busybox', 'dropbear', 'dropbearkey', 'wpa_supplicant', 'wpa_cli', 'iw')


def source_inputs(hardware, platform):
    """What decides PLATFORM's compiled kernel, from the checkout's files alone."""
    from .build import KERNEL_PRODUCER, recipe_entries
    hardware = Path(hardware)
    def series(*args):
        return subprocess.check_output([sys.executable, str(hardware / 'kernel/series.py'), *args], text=True)
    described = json.loads(series('--platform', platform))
    patches = [str(Path(line).resolve().relative_to(hardware.resolve())) for line in series(platform).splitlines()]
    return {
        'platform': platform,
        'build': {key: described[key] for key in ('arch', 'image', 'dtbs')},
        'recipes': recipe_entries(hardware, [described['upstream'], described['series'], described['config'],
                                             'kernel/Containerfile', 'kernel/install-toolchain', *patches]),
        'producer': KERNEL_PRODUCER,
    }


def _hash(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True).encode()).hexdigest()


def source_key(hardware, platform):
    return _hash(source_inputs(hardware, platform))


def shared_keys(hardware):
    """{name: key} of the components every platform shares, from their recipes."""
    from .build import M1N1_INPUTS, TOOL_INPUTS, recipe_entries
    return {'m1n1': _hash({'recipes': recipe_entries(Path(hardware), M1N1_INPUTS)}),
            'tools': _hash({'recipes': recipe_entries(Path(hardware), TOOL_INPUTS)})}


def write_keys(hardware, platforms, output):
    shared = shared_keys(hardware)
    keys = {'schema': 1,
            'platforms': {platform: {'key': source_key(hardware, platform),
                                     'asset': f'{platform}-components.tar.gz'} for platform in platforms},
            'shared': {'m1n1': {'key': shared['m1n1'], 'asset': 'm1n1.bin'},
                       'tools': {'key': shared['tools'], 'asset': 'tool-components.tar.gz'}}}
    Path(output).write_text(json.dumps(keys, indent=2, sort_keys=True) + '\n')
    return keys


def _fetch(url, limit):
    from .http import open_url
    with open_url(url) as response:
        data = response.read(limit + 1)
    if len(data) > limit:
        raise ValueError(f'{url} is larger than expected')
    return data


def _sums(text):
    rows = {}
    for line in text.splitlines():
        parts = line.split()
        if len(parts) == 2 and len(parts[0]) == 64:
            rows[parts[1].removeprefix('./')] = parts[0]
    return rows


def _published(cache, name, what, key, select, unpack, *, tag=TAG):
    """The cache entry NAME of the published component WHAT, or None to build it.

    SELECT picks its {key, asset} row from component-keys.json; UNPACK(asset
    file, stage) fills the entry from the downloaded, checked asset.
    """
    if os.environ.get('ORCHARD_PUBLISHED') == 'off':
        return None
    from .build import RELEASES, cached_component, component_lock
    components = Path(cache) / 'components-v2'
    if not (components / name / key).is_dir():
        try:
            if tag == TAG:  # the pointer names the newest nightly
                tag = json.loads(_fetch(f'{RELEASES}/{TAG}/latest.json', 64 * 1024))['name']
            sums = _sums(_fetch(f'{RELEASES}/{tag}/{SUMS}', 1024 * 1024).decode())
            keys = _fetch(f'{RELEASES}/{tag}/{KEYS}', 1024 * 1024)
        except (OSError, ValueError, KeyError) as error:
            print(f'No published {what} ({error}); building it', flush=True)
            return None
        if hashlib.sha256(keys).hexdigest() != sums.get(KEYS):
            print(f'The {tag} {KEYS} does not match its {SUMS}; building {what}', flush=True)
            return None
        entry = select(json.loads(keys))
        if not entry or entry.get('key') != key:
            print(f'The {tag} {what} is of another state; building it', flush=True)
            return None
        asset, checksum = entry['asset'], sums.get(entry['asset'])
        if not checksum or '/' in asset:
            return None
    def produce(stage):
        print(f'Using the {tag} {what}: its source key matches this checkout', flush=True)
        # No command makes it: --explain (orchard.explain) says where it came from.
        from . import explain
        explain.note(f'{what}: downloaded from orchard\'s {tag} ({asset}, sha256 {checksum}), '
                     f'published for source key {key}; ORCHARD_PUBLISHED=off builds it instead')
        from .http import open_url
        with tempfile.TemporaryDirectory(prefix='published-', dir=stage.parent) as temporary:
            download = Path(temporary) / asset
            digest, size = hashlib.sha256(), 0
            with open_url(f'{RELEASES}/{tag}/{asset}', timeout=300) as response, download.open('wb') as out:
                while block := response.read(1024 * 1024):
                    size += len(block)
                    if size > LIMIT:
                        raise ValueError(f'{asset} is larger than expected')
                    digest.update(block)
                    out.write(block)
            if digest.hexdigest() != checksum:
                raise ValueError(f'{asset} does not match {SUMS}')
            unpack(download, stage, Path(temporary))
        (stage / 'published.json').write_text(json.dumps(
            {'tag': tag, 'asset': asset, 'sha256': checksum, 'source_key': key}, indent=2) + '\n')
    with component_lock(cache):
        return cached_component(components, name, lambda: key, produce)


def _extract(archive, prefix, into):
    """Regular files and directories under PREFIX of a component tarball, into INTO."""
    with tarfile.open(archive) as tar:
        members = [m for m in tar.getmembers() if m.name.startswith(prefix)]
        for member in members:
            if not (member.isfile() or member.isdir()) or '..' in Path(member.name).parts:
                raise ValueError(f'{archive.name}: unexpected member {member.name}')
        if not members:
            raise ValueError(f'{archive.name} has no {prefix}')
        tar.extractall(into, members=members, filter='data')


def kernel(hardware, cache, platform, *, tag=TAG):
    """PLATFORM's published kernel directory for this checkout's source key, or None to compile."""
    if os.environ.get('ORCHARD_KERNEL') == 'local':
        return None
    def unpack(archive, stage, work):
        _extract(archive, 'components/kernel/', work / 'x')
        shutil.move(work / 'x/components/kernel', stage / 'kernel')
    entry = _published(cache, f'kernel-published-{platform}', f'{platform} kernel', source_key(hardware, platform),
                       lambda keys: keys.get('platforms', {}).get(platform), unpack, tag=tag)
    return entry / 'kernel' if entry else None


def m1n1(hardware, cache, *, tag=TAG):
    """orchard's published m1n1.bin for this checkout's m1n1 recipe, or None to build it."""
    def unpack(download, stage, work):
        shutil.move(download, stage / 'm1n1.bin')
    entry = _published(cache, 'm1n1-published', 'm1n1', shared_keys(hardware)['m1n1'],
                       lambda keys: keys.get('shared', {}).get('m1n1'), unpack, tag=tag)
    return entry / 'm1n1.bin' if entry else None


def tools(hardware, cache, *, tag=TAG):
    """The published static tools directory for this checkout's tool recipes, or None to build them."""
    def unpack(archive, stage, work):
        _extract(archive, 'components/boot/', work / 'x')
        for name in TOOLS:
            if not (work / 'x/components/boot' / name).is_file():
                raise ValueError(f'{archive.name} lacks {name}')
            shutil.move(work / 'x/components/boot' / name, stage / name)
    entry = _published(cache, 'tools-published', 'static tools', shared_keys(hardware)['tools'],
                       lambda keys: keys.get('shared', {}).get('tools'), unpack, tag=tag)
    return entry if entry else None


def main():
    parser = argparse.ArgumentParser(description=__doc__.split('\n\n')[0])
    commands = parser.add_subparsers(dest='action', required=True)
    keys = commands.add_parser('keys', help=f'write {KEYS} for the given (default: every arm64) platform')
    keys.add_argument('--output', type=Path, required=True)
    keys.add_argument('platform', nargs='*')
    args = parser.parse_args()
    from .hardware import ROOT
    from .image.platform import platforms
    print(json.dumps(write_keys(ROOT, args.platform or platforms(), args.output), indent=2))


if __name__ == '__main__':
    main()
