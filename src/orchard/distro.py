"""A distribution checkout as an orchard project: build its release, boot its recovery.

`orchard.toml` with a [release] table at the root of a distribution's
checkout makes it one (orchard.project; the older distro.toml is still read):

    name = "omarchy-a"          # the distro of its index.toml
    [release]
    build = "tools/build"       # its build entry, relative to the checkout
    selects-platform = true     # optional: the entry takes --platform

In that checkout `orchard build` runs the entry as

    ENTRY --version VERSION --output out/releases/VERSION [--platform NAME ...]

With selects-platform the entry builds only the platforms it is given
(--platform all: every one): orchard passes --platform/--device, else
orchard.local.toml, else the attached board's platform, and never all
unless asked. Without it the entry builds every platform, as before.

with orchard's Python (a `.py` entry) or directly (anything else, with
ORCHARD_PYTHON set). The entry builds what is missing and writes a release
directory: the root image, per platform a recovery and orchard's layer,
`release.toml` and the `index.toml` that pins it. orchard then checks the
index and its files and records the build in out/releases/latest.json.

`orchard run` builds, then boots that release's recovery for the attached
board from RAM, without installing; `orchard install` with no source installs
the last build. The distribution owns how its image is made; orchard owns
the release format, the device data and the device.
"""
import datetime
import hashlib
import json
import os
from pathlib import Path
import re
import shlex
import shutil
import subprocess
import sys

import tomllib

DISTRO = 'distro.toml'
LATEST = 'out/releases/latest.json'
_NAME = re.compile(r'[a-z0-9][a-z0-9._-]*')
_VERSION = re.compile(r'[0-9A-Za-z][0-9A-Za-z._+-]{0,127}')


def find(directory):
    """The distribution checkout DIRECTORY is in (the nearest orchard.toml with [release], or the older
    distro.toml, up to its repository's root), or None."""
    from . import project
    return project.find_release(directory)


def settings(root):
    """ROOT's orchard.toml [release] (or the older distro.toml), checked: {'name': str, 'build': Path}."""
    from . import project
    kind, name, value, path = project.read(Path(root))
    if kind != 'release':
        raise ValueError(f'{root} has no orchard.toml with [release]')
    value = {**value, 'name': name}
    unknown = set(value) - {'name', 'build', 'selects-platform', 'selects-device'}
    if unknown:
        raise ValueError(f'{path}: unknown settings {", ".join(sorted(unknown))}; '
                         'expected name, build, selects-platform, selects-device')
    selects = value.get('selects-platform', False)
    if not isinstance(selects, bool):
        raise ValueError(f'{path}: selects-platform must be true or false')
    selects_device = value.get('selects-device', False)
    if not isinstance(selects_device, bool) or (selects_device and not selects):
        raise ValueError(f'{path}: selects-device requires selects-platform and must be true or false')
    name, build = value.get('name'), value.get('build')
    if not isinstance(name, str) or not _NAME.fullmatch(name):
        raise ValueError(f'{path}: name must be the distro name of its index, e.g. "omarchy-a"')
    if (not isinstance(build, str) or not build or build.startswith('/') or '\\' in build
            or any(part in ('', '.', '..') for part in build.split('/'))):
        raise ValueError(f'{path}: build must be a path inside the checkout, e.g. "tools/build"')
    entry = Path(root) / build
    if entry.is_symlink() or not entry.is_file():
        raise ValueError(f'{path}: the build entry {build} is not a file')
    if entry.suffix != '.py' and not os.access(entry, os.X_OK):
        raise ValueError(f'{path}: the build entry {build} is not executable')
    return {'name': name, 'build': entry, 'selects_platform': selects, 'selects_device': selects_device}


def default_version(now=None):
    """dev-YYYYMMDD-HHMMSS (UTC): a new local version for every build."""
    now = now or datetime.datetime.now(datetime.timezone.utc)
    return now.strftime('dev-%Y%m%d-%H%M%S')


def build(root, *, version=None, platforms=None, kernel_tree=None, device=None):
    """Run ROOT's build entry into out/releases/VERSION, check the release, record it; returns its index.

    PLATFORMS: a list of platform names or 'all', passed as --platform to an
    entry that selects platforms (orchard.toml [release] selects-platform = true); None
    for an entry that builds every platform.
    """
    from .hardware import ROOT
    from .manifests import resolve_local, verify_local_receipts
    root = Path(root).resolve()
    distro = settings(root)
    version = version or default_version()
    if not _VERSION.fullmatch(version):
        raise ValueError('A version is letters, digits and . _ + -, starting with a letter or digit')
    output = root / 'out/releases' / version
    if output.exists() or output.is_symlink():
        raise ValueError(f'{output} exists; choose another --version')
    output.parent.mkdir(parents=True, exist_ok=True)
    entry = distro['build']
    argv = [sys.executable, str(entry)] if entry.suffix == '.py' else [str(entry)]
    argv += ['--version', version, '--output', str(output)]
    if platforms is not None:
        if not distro['selects_platform']:
            raise ValueError(f'{distro["name"]}\'s build entry builds every platform and takes no --platform; '
                             'update the distribution (orchard.toml [release] selects-platform = true)')
        argv += [part for name in ([platforms] if platforms == 'all' else platforms)
                 for part in ('--platform', name)]
    elif distro['selects_platform']:
        raise ValueError(f'{distro["name"]} selects its platforms: choose with --platform, --device, '
                         'orchard.local.toml or the attached board')
    if device is not None:
        if not distro['selects_device']:
            raise ValueError('Distribution build entry does not accept --device')
        argv += ['--device', device]
    print('+', ' '.join(argv), flush=True)
    environment = {**os.environ, 'ORCHARD_PYTHON': sys.executable, 'ORCHARD_CHECKOUT': str(ROOT)}
    tree = ''
    if kernel_tree is not None:
        # orchard.inputs, which the build script runs, builds the kernels from this tree.
        environment['ORCHARD_KERNEL_TREE'] = str(kernel_tree)
        print(f'Kernel from the tree {kernel_tree}', flush=True)
        tree = f'ORCHARD_KERNEL_TREE={shlex.quote(str(kernel_tree))} '
    # What orchard runs, as the plain command: the distribution composes the rest itself.
    (output.parent / f'{version}.commands.sh').write_text(
        '#!/bin/sh\n# What orchard build ran for this release (--explain prints it); the build script\n'
        '# runs orchard\'s producers itself, from the snapshot ORCHARD_CHECKOUT names; how orchard.inputs\n'
        '# got each component (built, reused, or downloaded from a nightly) is in its JSON, under "produced".\nset -eu\n'
        f'cd {shlex.quote(str(root))}\n'
        f'{tree}ORCHARD_CHECKOUT={shlex.quote(str(ROOT))} ' + ' '.join(map(shlex.quote, argv)) + '\n')
    subprocess.run(argv, cwd=root, env=environment, check=True)
    index = output / 'index.toml'
    if not index.is_file():
        raise ValueError(f'{entry.relative_to(root)} wrote no {index.relative_to(root)}')
    from .manifests import load
    document = load(index)
    _, manifest = resolve_local(document, output)
    if manifest['distro'] != distro['name']:
        raise ValueError(f'The release is {manifest["distro"]!r}, but orchard.toml names {distro["name"]!r}')
    if manifest['version'] != version:
        raise ValueError(f'The release says version {manifest["version"]!r}, but was built as {version!r}')
    verify_local_receipts(document, output)
    record = root / LATEST
    record.write_text(json.dumps({'version': version, 'index': f'{version}/index.toml'}, indent=2) + '\n')
    print(f'{distro["name"]} {version}: {index}')
    return index


def latest(root):
    """The index of ROOT's last build, from out/releases/latest.json."""
    root = Path(root).resolve()
    record = root / LATEST
    if not record.is_file():
        raise ValueError(f'No build in {root}; run orchard build')
    value = json.loads(record.read_text())
    index = record.parent / value['index']
    if not index.is_file():
        raise ValueError(f'The last build {value["version"]} is gone; run orchard build')
    return index


def run(root, *, build_first=True, version=None, yes=False, progress=None, kernel_tree=None):
    """Build (unless BUILD_FIRST is false), then boot the release's installer system on the attached board from RAM.

    Nothing is installed: its session (USB network, SSH) is how to look
    around. Never retries a boot.
    """
    from .boot import confirm_boot
    from .devices import choose
    from .releases import resolve
    from .state import operation_lock
    if build_first:
        platforms = None
        if settings(root)['selects_platform']:
            # Only what the attached board needs: its platform's recovery and layer.
            from . import ram
            from .devices import attached_board
            from .hardware import ROOT
            board = attached_board()
            if board is None:
                raise ValueError('No described device is attached: connect it, then run again')
            platforms = [ram.resolve_platform(root, ROOT, device=board)]
        index = build(root, version=version, platforms=platforms, kernel_tree=kernel_tree,
                      **({'device': board} if settings(root)['selects_device'] else {}))
    else:
        index = latest(root)
    with operation_lock():
        adapter, device = choose()
        record, assets = resolve(str(index), adapter.PROFILE, progress, purpose='boot')
        what = 'recovery' if 'recovery' in assets else "kernel and installer system"
        print(f'{record["name"]} {record["release"]}: its {record["platform"]} {what}, from RAM; nothing is installed.')
        if not yes:
            confirm_boot()
        try:
            if 'recovery' in assets:
                adapter.boot(device, assets, record, progress)
            else:
                from .install_direct import HOST, start_for_inspection
                start_for_inspection(adapter, device, assets, device['ecid'], progress)
                print(f'On USB. Sign in with: ssh root@{HOST}')
        except (OSError, ValueError, RuntimeError, subprocess.SubprocessError, KeyboardInterrupt) as error:
            raise SystemExit(f'{error}\nStopped. Inspect device state before any new boot attempt.')
    return {'recovery_observed': True, 'release': record['release']}


def component(root, name, inputs, produce, *, artifact, extra=(), keep=2):
    """A build product of ROOT keyed by its inputs: ROOT/out/components/NAME/KEY/ARTIFACT.

    INPUTS are paths relative to ROOT (files or directories) and EXTRA more
    strings, such as other components' keys, that decide the result. With
    the same inputs the earlier result is reused; otherwise PRODUCE(path, work)
    makes ARTIFACT at path, which does not exist yet; work is a directory in
    orchard's cache for this name and key, kept between attempts. KEEP results
    per name stay. Returns (path, key).
    """
    from .build import cache_root, recipe_entries
    root = Path(root).resolve()
    if not _NAME.fullmatch(name) or '/' in artifact or artifact in ('', '.', '..'):
        raise ValueError('Invalid component name or artifact')
    entries = recipe_entries(root, inputs)
    if not entries:
        raise ValueError(f'{name}: no input files under {", ".join(inputs)}')
    key = hashlib.sha256(json.dumps({'inputs': entries, 'extra': list(extra)}, sort_keys=True).encode()).hexdigest()
    directory = root / 'out/components' / name
    target = directory / key[:16]
    if (target / artifact).exists():
        print(f'Reusing {name}: {target.relative_to(root)}', flush=True)
        os.utime(target)  # most recently used: kept first
        return target / artifact, key
    print(f'Building {name}: {target.relative_to(root)}', flush=True)
    partial = directory / f'.partial-{key[:16]}'
    if partial.exists():
        shutil.rmtree(partial)
    partial.mkdir(parents=True)
    work = cache_root() / 'work' / f'{settings(root)["name"]}-{name}' / key[:16]
    work.mkdir(parents=True, exist_ok=True)
    produce(partial / artifact, work)
    if not (partial / artifact).exists():
        raise ValueError(f'{name}: the build made no {artifact}')
    if target.exists():
        shutil.rmtree(target)
    os.replace(partial, target)
    older = sorted((path for path in directory.iterdir() if path.is_dir() and path != target
                    and not path.name.startswith('.')), key=lambda path: path.stat().st_mtime, reverse=True)
    for path in older[max(keep - 1, 0):]:
        shutil.rmtree(path)
    shutil.rmtree(work, ignore_errors=True)
    return target / artifact, key
