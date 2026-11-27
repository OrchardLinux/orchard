#!/usr/bin/env python3
"""Take a platform's kernel from the newest nightly when nothing that decides it changed.

  ci/reuse_kernel.py PLATFORM --output DIRECTORY [--rebuild]

A kernel job asks this before it installs a compiler. The nightly's kernel is
taken when both hold:

- the platform's source key (ci/keys.py: upstream pin, series, patches,
  config, toolchain recipes) equals the one the nightly published in
  component-keys.json, and
- the build scripts the key leaves out (GUARDED) are the same files as in the
  nightly's orchard-source.tar.gz.

Then DIRECTORY gets kernel-components-PLATFORM.tar.gz (the kernel directory and
its toolchain lock, taken out of the nightly's PLATFORM-components.tar.gz) and
kernel-source-PLATFORM.tar.gz (the nightly's file, unchanged): what the compile
would have left there. Every download is checked against the nightly's
SHA256SUMS. Anything else (another key, a changed script, no nightly, no
network, a failed check, --rebuild) leaves DIRECTORY alone and the job compiles.

Prints the decision and, in GitHub Actions, sets the step outputs `key` (the
platform's source key) and `reused` (the nightly's name, or empty).
"""
import argparse
import hashlib
import importlib.util
import json
import os
from pathlib import Path
import tarfile
import tempfile

ROOT = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location('keys', ROOT / 'ci/keys.py')
keys = importlib.util.module_from_spec(spec)
spec.loader.exec_module(keys)

# What builds a kernel besides the inputs of its source key. A directory counts
# with everything below it; a path may be missing on both sides.
GUARDED = ('kernel/build', 'kernel/build-modules', 'kernel/stage-modules.py', 'kernel/series.py',
           'kernel/modules', 'producers/buildenv.py')
LIMIT = 2 * 1024 * 1024 * 1024


def guarded(name):
    return any(name == path or name.startswith(path + '/') for path in GUARDED)


def checkout_scripts(root):
    """{path: sha256} of the guarded files in the checkout ROOT."""
    found = {}
    for relative in GUARDED:
        path = root / relative
        for file in ([path] if path.is_file() else sorted(path.rglob('*')) if path.is_dir() else []):
            if file.is_file() and '__pycache__' not in file.parts:
                found[file.relative_to(root).as_posix()] = hashlib.sha256(file.read_bytes()).hexdigest()
    return found


def archived_scripts(archive):
    """{path: sha256} of the guarded files in a `git archive` of orchard."""
    found = {}
    with tarfile.open(archive) as tar:
        for member in tar:
            if member.isfile() and guarded(member.name):
                found[member.name] = hashlib.sha256(tar.extractfile(member).read()).hexdigest()
    return found


def download(url, path, checksum):
    digest, size = hashlib.sha256(), 0
    with keys.open_url(url, timeout=300) as response, path.open('xb') as out:
        while block := response.read(1024 * 1024):
            size += len(block)
            if size > LIMIT:
                raise ValueError(f'{path.name} is larger than expected')
            digest.update(block)
            out.write(block)
    if not checksum or digest.hexdigest() != checksum:
        raise ValueError(f'{path.name} does not match SHA256SUMS')


def kernel_artifact(bundle, output):
    """The kernel job's artifact, from a published platform bundle: its kernel and toolchain lock."""
    wanted = ('components/kernel/', 'components/kernel-toolchain-packages.lock')
    with tarfile.open(bundle) as source, tarfile.open(output, 'x:gz', compresslevel=6) as artifact:
        names = set()
        for member in source:
            if not member.name.startswith(wanted):
                continue
            if not (member.isfile() or member.isdir()) or '..' in Path(member.name).parts:
                raise ValueError(f'{bundle.name}: unexpected member {member.name}')
            artifact.addfile(member, source.extractfile(member) if member.isfile() else None)
            names.add(member.name)
    if 'components/kernel/kernel.release' not in names or wanted[1] not in names:
        raise ValueError(f'{bundle.name} has no complete kernel')


def reuse(platform, output, *, root=ROOT, releases=None):
    """The nightly's name when its kernel of PLATFORM now lies in OUTPUT; raises ValueError when it can't be used."""
    releases = releases or keys.RELEASES
    key = keys.source_key(Path(root), platform)
    try:
        tag = json.loads(keys.fetch(f'{releases}/{keys.TAG}/latest.json', 64 * 1024))['name']
        sums = keys.sums(keys.fetch(f'{releases}/{tag}/{keys.SUMS}', 1024 * 1024).decode())
        published = keys.fetch(f'{releases}/{tag}/{keys.KEYS}', 1024 * 1024)
    except (OSError, KeyError) as error:
        raise ValueError(f'no nightly to ask ({error})') from error
    if hashlib.sha256(published).hexdigest() != sums.get(keys.KEYS):
        raise ValueError(f'{tag}: {keys.KEYS} does not match its {keys.SUMS}')
    entry = json.loads(published).get('platforms', {}).get(platform) or {}
    if entry.get('key') != key:
        raise ValueError(f'{tag} has another kernel state')
    bundle, source = entry['asset'], f'kernel-source-{platform}.tar.gz'
    if '/' in bundle:
        raise ValueError(f'{tag}: unexpected asset name {bundle}')
    output.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix='reuse-kernel-', dir=output) as temporary:
        work = Path(temporary)
        try:
            download(f'{releases}/{tag}/orchard-source.tar.gz', work / 'orchard-source.tar.gz',
                     sums.get('orchard-source.tar.gz'))
            then, now = archived_scripts(work / 'orchard-source.tar.gz'), checkout_scripts(root)
            if then != now:
                changed = sorted(name for name in then.keys() | now.keys() if then.get(name) != now.get(name))
                raise ValueError(f'build scripts changed since {tag}: {", ".join(changed)}')
            download(f'{releases}/{tag}/{bundle}', work / bundle, sums.get(bundle))
            download(f'{releases}/{tag}/{source}', work / source, sums.get(source))
            kernel_artifact(work / bundle, work / f'kernel-components-{platform}.tar.gz')
        except (OSError, tarfile.TarError) as error:
            raise ValueError(f'{tag}: {error}') from error
        for name in (f'kernel-components-{platform}.tar.gz', source):
            (work / name).replace(output / name)
    return tag


def main():
    parser = argparse.ArgumentParser(description=__doc__.split('\n\n')[0])
    parser.add_argument('platform')
    parser.add_argument('--output', type=Path, required=True)
    parser.add_argument('--rebuild', action='store_true', help='do not ask the nightly; the job compiles')
    args = parser.parse_args()
    key, tag = keys.source_key(ROOT, args.platform), ''
    try:
        if args.rebuild:
            raise ValueError('a rebuild was asked for')
        tag = reuse(args.platform, args.output)
        message = f'{args.platform} kernel: from {tag}, source key {key[:12]} and build scripts unchanged'
    except ValueError as error:
        message = f'{args.platform} kernel: compiling, {error}'
    print(message, flush=True)
    if os.environ.get('GITHUB_OUTPUT'):
        with open(os.environ['GITHUB_OUTPUT'], 'a') as out:
            out.write(f'key={key}\nreused={tag}\n')
    if os.environ.get('GITHUB_STEP_SUMMARY'):
        with open(os.environ['GITHUB_STEP_SUMMARY'], 'a') as out:
            out.write(message + '\n')


if __name__ == '__main__':
    main()
