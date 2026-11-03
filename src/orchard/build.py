"""Shared component build orchestration and caching.

Invokes hardware-owned compiler recipes. No RAM-root composition, installer
policy or device access. The hardware/CLI ownership boundary remains provisional.
"""
from contextlib import contextmanager
import fcntl
import hashlib
import json
import os
import re
from pathlib import Path
import shutil
import stat
import subprocess
import sys
import tempfile

import tomllib

from . import explain, sources, kernel as kernels

# The kernel producers' version: kernel/build, kernel/build-modules,
# kernel/stage-modules.py, kernel/series.py, src/orchard/buildenv.py,
# src/orchard/kernel_sdk.py and orchard.kernel's build functions. Their text is
# not part of the cache key, so a refactor rebuilds nothing; raise this when a
# change alters what they produce (files, contents or layout of a kernel bundle).
KERNEL_PRODUCER = 2  # 2: only modules.order is staged, not stale .ko files


PAYLOAD = 'payload.toml'
LOCAL = 'orchard.local.toml'
NO_DEVICE = ('No device selected: pass --device BOARD, set device = "BOARD" in '
             'orchard.local.toml, or connect the device')
_BOARD = re.compile(r'[a-z0-9][a-z0-9_-]*')


def _toml(path, allowed):
    try:
        with path.open('rb') as stream:
            settings = tomllib.load(stream)
    except (OSError, ValueError) as error:
        raise ValueError(f'{path}: {error}') from error
    unknown = set(settings) - set(allowed)
    if unknown:
        raise ValueError(f'{path}: unknown settings {", ".join(sorted(unknown))}; expected {", ".join(allowed)}')
    return settings


PAYLOAD_DEFAULTS = {'transport': True, 'modules': False, 'firmware': False}


def payload_settings(directory):
    """DIRECTORY/orchard.toml's [payload] (or the older payload.toml): how this RAM payload is built. Optional.

    transport = false leaves the USB session (network, SSH) out; modules = true
    stages the kernel's modules without loading them; firmware = true adds the
    booted board's firmware when `orchard run` boots it.
    """
    from . import project
    kind, _, settings, path = project.read(Path(directory).expanduser().resolve())
    if kind == 'release':
        raise ValueError(f'{path} describes a distribution, not a RAM payload')
    unknown = set(settings) - set(PAYLOAD_DEFAULTS)
    if unknown:
        raise ValueError(f'{path}: unknown settings {", ".join(sorted(unknown))}; '
                         f'expected {", ".join(PAYLOAD_DEFAULTS)}')
    for name, value in settings.items():
        if not isinstance(value, bool):
            raise ValueError(f'{path}: {name} must be true or false')
    return {**PAYLOAD_DEFAULTS, **settings}


def local_settings(directory):
    """The nearest orchard.local.toml from DIRECTORY up to its repository's root.

    This checkout's own choices, never committed: sticky defaults for
    --device ("BOARD"), --platform ("PLATFORM" or "all") and --kernel-tree
    ("PATH", relative to the file), and orchard = "PATH" (the orchard checkout
    to use; read by the launcher).
    """
    directory = Path(directory).expanduser().resolve()
    for parent in (directory, *directory.parents):
        path = parent / LOCAL
        if path.exists():
            return _toml(path, ('device', 'platform', 'orchard', 'kernel-tree')), path
        if (parent / '.git').exists():
            break
    return {}, None


def resolve_device(directory, device=None):
    """--device, else this checkout's orchard.local.toml; None if neither chooses.

    Projects and distributions name no board, and neither does a per-person
    setting: many work on several boards, one checkout or attached device each.
    """
    if device is not None:
        return device
    settings, path = local_settings(directory)
    if 'device' in settings:
        if not isinstance(settings['device'], str) or not _BOARD.fullmatch(settings['device']):
            raise ValueError(f'{path}: invalid device name')
        return settings['device']
    return None


def configured_platform(directory):
    """This checkout's platform = "PLATFORM" or "all" (orchard.local.toml), or None."""
    settings, path = local_settings(directory)
    if 'platform' not in settings:
        return None
    if not isinstance(settings['platform'], str) or not _BOARD.fullmatch(settings['platform']):
        raise ValueError(f'{path}: invalid platform name')
    return settings['platform']


def resolve_kernel_tree(directory, tree=None):
    """--kernel-tree, else this checkout's kernel-tree (orchard.local.toml, relative to it); or None."""
    if tree is not None:
        return Path(tree).expanduser().resolve()
    settings, path = local_settings(directory)
    if 'kernel-tree' not in settings:
        return None
    if not isinstance(settings['kernel-tree'], str) or not settings['kernel-tree']:
        raise ValueError(f'{path}: kernel-tree must be a path')
    return (path.parent / Path(settings['kernel-tree']).expanduser()).resolve()


def configured_device(directory):
    """The device chosen for DIRECTORY's checkout (orchard.local.toml), or None."""
    return resolve_device(directory)


def digest(path):
    result = hashlib.sha256()
    with Path(path).open('rb') as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b''):
            result.update(block)
    return result.hexdigest()


def command(*argv, env=None):
    """Run a hardware recipe. Its Python is Orchard's own."""
    print('+', ' '.join(map(str, argv)), flush=True)
    explain.command(argv)
    subprocess.run(list(map(str, argv)), check=True,
                   env={**(os.environ if env is None else env), 'ORCHARD_PYTHON': sys.executable})


def recipe_entries(hardware, paths, *, ignore=('.work', '.git', '__pycache__')):
    """{relative path: "mode:sha256"} for recipe files, in hashing order."""
    entries = {}
    for relative in paths:
        root = hardware / relative
        files = [root] if root.is_file() else []
        if root.is_dir():
            for directory, dirs, names in os.walk(root):
                dirs[:] = sorted(d for d in dirs if d not in ignore)
                files.extend(Path(directory) / name for name in sorted(names))
        for path in files:
            entries[str(path.relative_to(hardware))] = f'{path.stat().st_mode & 0o777}:{digest(path)}'
    return entries


def recipe_key(hardware, paths, *, ignore=('.work', '.git', '__pycache__')):
    """Hash recipes, not build output or experiment files."""
    result = hashlib.sha256()
    for relative, entry in recipe_entries(hardware, paths, ignore=ignore).items():
        mode, checksum = entry.split(':')
        result.update(relative.encode() + b'\0')
        result.update(mode.encode() + b'\0')
        result.update(checksum.encode())
    return result.hexdigest()


def keyed(inputs):
    """Key callable over a dict of inputs; remembers them for the cache record and miss reports."""
    def key():
        key.inputs = inputs()
        return hashlib.sha256(json.dumps(key.inputs, sort_keys=True).encode()).hexdigest()
    return key


def cache_root():
    return sources.cache_root()


def kernel_base_inputs(hardware, device, cache, tree=None):
    """What determines a platform's compiled kernel; the same before and after source checkout.

    The source tree (upstream base plus the platform's patch series), the
    config, the descriptor's build fields, the toolchain image and the
    producer version. Not the text of the build scripts (see KERNEL_PRODUCER),
    and not descriptor fields that do not build anything (defconfig, branch,
    description). The base is per platform; devices of one platform share it.
    """
    platform = kernels.layout(hardware, device)[0]
    described = kernels.descriptor(hardware, device)
    if tree is not None:
        # A developer's tree: its content (its own defconfig included), not orchard's series or config.
        return {
            'platform': platform, 'source': 'kernel-tree',
            'build': {key: described[key] for key in ('arch', 'image', 'dtbs', 'defconfig')},
            'recipes': recipe_entries(hardware, ['kernel/Containerfile', 'kernel/install-toolchain']),
            'producer': KERNEL_PRODUCER,
            'tree': sources.working_tree(tree, force=False),
            'mode': os.environ.get('ORCHARD_BUILD_NATIVE', '0') + ':' + os.environ.get('ORCHARD_BUILD_ENV_ID', ''),
        }
    return {
        'platform': platform,
        'build': {key: described[key] for key in ('arch', 'image', 'dtbs')},
        'recipes': recipe_entries(hardware, [described['config'], 'kernel/Containerfile',
                                             'kernel/install-toolchain']),
        'producer': KERNEL_PRODUCER,
        'tree': kernels.source_tree(hardware, cache, device),
        'mode': os.environ.get('ORCHARD_BUILD_NATIVE', '0') + ':' + os.environ.get('ORCHARD_BUILD_ENV_ID', ''),
    }


def kernel_base_key(hardware, device, cache):
    return keyed(lambda: kernel_base_inputs(hardware, device, cache))()


def kernel_inputs(hardware, device, base_key):
    """Device layer: DTB selection and external drivers, reusing the compiled base.

    Only device.toml's [kernel] selection matters here, not feature states or caveats.
    """
    platform, dtb = kernels.layout(hardware, device)
    return {
        'base': base_key, 'platform': platform, 'dtb': dtb, 'producer': KERNEL_PRODUCER,
        'recipes': recipe_entries(hardware, ['kernel/modules/' + device]),
    }


def kernel_key(hardware, device, base_key):
    return keyed(lambda: kernel_inputs(hardware, device, base_key))()


_HELD_LOCKS = set()


@contextmanager
def component_lock(cache, *, exclusive=False):
    """Use of a component cache.

    Builds and launches share it, so they run side by side: a launch never
    waits for an unrelated build. What removes entries (orchard cache) takes
    it exclusively and waits until no build uses the cache. Two processes
    never build the same entry at once: cached_component locks that entry.
    Taking the lock again in the same process is refused as a programming
    error, since an exclusive request would deadlock on flock.
    """
    cache.mkdir(parents=True, exist_ok=True)
    key = (cache / '.lock').resolve()
    if key in _HELD_LOCKS:
        raise ValueError(f'{cache} is already locked by this build')
    mode = fcntl.LOCK_EX if exclusive else fcntl.LOCK_SH
    with key.open('a') as lock:
        try:
            fcntl.flock(lock, mode | fcntl.LOCK_NB)
        except BlockingIOError:
            print(f'Waiting for running Orchard builds to finish with {cache} ...' if exclusive else
                  f'Waiting for another Orchard process to finish changing {cache} ...',
                  file=sys.stderr, flush=True)
            fcntl.flock(lock, mode)
        _HELD_LOCKS.add(key)
        try:
            yield
        finally:
            _HELD_LOCKS.discard(key)
            fcntl.flock(lock, fcntl.LOCK_UN)


INPUTS_RECORD = 'key-inputs.json'
# One copy of each file content and mode, hard-linked into every entry that has it.
SHARED_FILES = '.objects'


def share_files(cache, root, records):
    """Replace files under ROOT by hard links to the cache's single copy of their content.

    Entries are never written after publication, so sharing is invisible to
    readers; reuse still verifies every file. A shared copy that no longer
    matches its name is replaced instead of spreading. Filesystems without
    hard links keep private copies.
    """
    store = cache / SHARED_FILES
    store.mkdir(exist_ok=True)
    for relative, checksum in records.items():
        path = root / relative
        shared = store / f'{checksum}-{stat.S_IMODE(path.lstat().st_mode):o}'
        try:
            if shared.exists() and os.path.samefile(shared, path):
                continue
            if shared.exists() and digest(shared) != checksum:
                shared.unlink()
            if not shared.exists():
                os.link(path, shared)
                continue
            replacement = path.with_name(path.name + '.orchard-share')
            os.link(shared, replacement)
            os.replace(replacement, path)
        except OSError:
            continue


def remove_unshared(cache):
    """Delete shared copies no entry links to any more."""
    store = cache / SHARED_FILES
    if store.is_dir():
        for shared in store.iterdir():
            if shared.lstat().st_nlink == 1:
                shared.unlink()


def _flatten(value, prefix=''):
    if not isinstance(value, dict):
        return {prefix: value}
    flat = {}
    for name, item in value.items():
        flat.update(_flatten(item, f'{prefix}.{name}' if prefix else name))
    return flat


def explain_miss(parent, inputs):
    """Inputs that differ from the newest recorded entry of this component, or None."""
    records = sorted((p for p in parent.glob('*/' + INPUTS_RECORD) if not p.parent.name.startswith('build-')),
                     key=lambda p: p.stat().st_mtime)
    if not records:
        return None
    previous = _flatten(json.loads(records[-1].read_text()))
    current = _flatten(inputs)
    changed = sorted(name for name in previous.keys() | current.keys()
                     if previous.get(name) != current.get(name))
    return records[-1].parent.name, changed


def cached_component(cache, name, key, produce):
    """Immutable component directories, checked before reuse and publication.
    Call under component_lock when building into the same cache. A key made by
    keyed() also records its inputs, so later misses can say what changed.
    One process builds an entry at a time; another one that wants the same
    entry waits for it and reuses it.
    """
    parent = cache / name
    parent.mkdir(parents=True, exist_ok=True)
    initial_key = key()
    target = parent / initial_key
    if target.exists():
        return _reuse(name, target)
    locks = cache / '.locks'
    locks.mkdir(exist_ok=True)
    with (locks / f'{name}-{initial_key}').open('a') as entry:
        try:
            fcntl.flock(entry, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            print(f'Waiting for another build of {name} ...', file=sys.stderr, flush=True)
            fcntl.flock(entry, fcntl.LOCK_EX)
        try:
            if target.exists():
                return _reuse(name, target)
            return _build(cache, parent, name, key, initial_key, target, produce)
        finally:
            fcntl.flock(entry, fcntl.LOCK_UN)


def _reuse(name, target):
    """A published entry, checked file by file before use."""
    records = json.loads((target / 'component.json').read_text())
    actual = {str(p.relative_to(target)) for p in target.rglob('*')
              if p.is_file() and p != target / 'component.json'}
    if actual != set(records):
        raise ValueError(f'Cached component inventory changed: {target}; remove it and rebuild')
    for relative, expected in records.items():
        path = target / relative
        if path.is_symlink() or digest(path) != expected:
            raise ValueError(f'Cached component changed: {path}; remove that component and rebuild')
    os.utime(target)  # last use, which `orchard cache --prune` keeps by
    print(f'Reusing {name}: {target}', flush=True)
    explain.reused(name, target)
    return target


def _build(cache, parent, name, key, initial_key, target, produce):
    inputs = getattr(key, 'inputs', None)
    print(f'Building {name} (cache miss): {target}', flush=True)
    if inputs is not None:
        reason = explain_miss(parent, inputs)
        if reason:
            previous, changed = reason
            shown = ', '.join(changed[:8]) + (f' and {len(changed) - 8} more' if len(changed) > 8 else '')
            print(f'  differs from newest cached {previous[:12]} in: {shown}', flush=True)
    with tempfile.TemporaryDirectory(prefix='build-', dir=parent) as temporary:
        stage = Path(temporary) / 'component'
        stage.mkdir()
        with explain.producing(stage):
            produce(stage)
        if inputs is not None:
            (stage / INPUTS_RECORD).write_text(json.dumps(inputs, indent=2, sort_keys=True) + '\n')
        records = {str(p.relative_to(stage)): digest(p) for p in sorted(stage.rglob('*')) if p.is_file()}
        (stage / 'component.json').write_text(json.dumps(records, indent=2) + '\n')
        if key() != initial_key:
            raise ValueError(f'{name} inputs changed during build; result was not cached')
        if target.exists():
            raise ValueError(f'Component appeared during build: {target}; inspect before rebuilding')
        share_files(cache, stage, records)
        stage.rename(target)
    return target


def _build_context(hardware, cache, device):
    if hardware is None:
        from .hardware import ROOT
        hardware = ROOT
    hardware = Path(hardware).resolve()
    cache = Path(cache).resolve() if cache is not None else cache_root()
    if not (hardware / 'kernel/build').is_file():
        raise ValueError(f'Not a hardware checkout: {hardware}')
    if not device or any(c not in 'abcdefghijklmnopqrstuvwxyz0123456789_-' for c in device):
        raise ValueError('Invalid device profile')
    return hardware, cache


def _check_kernel_recipe(hardware, device):
    if not (hardware / 'kernel/series.py').is_file():
        raise ValueError(f'Hardware checkout has no kernel series: {hardware}; update hardware')
    platform = kernels.layout(hardware, device)[0]
    if not (hardware / 'platforms' / (platform + '.toml')).is_file():
        raise ValueError(f'Missing platform descriptor: {hardware}/platforms/{platform}.toml')
    # series.py --platform refuses a descriptor whose series, config or upstream lock is missing.
    kernels.descriptor(hardware, device)


def _prepare_kernel(hardware, cache, device, tree=None):
    """Resolve one coherent kernel/DTB/module bundle under the component lock.

    TREE: a developer's kernel tree, built as it is; its results are cached
    apart from orchard's own kernels (kernel-tree-*), so they never displace them.
    """
    components = cache / 'components-v2'
    platform = kernels.layout(hardware, device)[0]
    prefix = 'kernel-tree-' if tree is not None else 'kernel-'
    base_key = keyed(lambda: kernel_base_inputs(hardware, device, cache, tree))
    described = kernels.descriptor(hardware, device)
    if tree is None:
        explain.note(f'kernel source: orchard\'s {platform} tree, its upstream lock plus kernel/series/{platform}, '
                     f'prepared in its cache.\nStandalone: OrchardLinux/linux branch {described.get("branch", "main")} '
                     f'regenerated from this orchard commit has the same content '
                     f'(git rev-parse HEAD^{{tree}}: see kernel-base key-inputs.json "tree").')
    else:
        explain.note(f'kernel source: the tree {tree} as it is, with arch/{described["arch"]}/configs/'
                     f'{described["defconfig"]}')
    def produce_base(stage):
        with kernels.tree_lock(hardware, device, tree):
            kernels.build_base(hardware, cache, device, stage / 'kernel', tree=tree)
    base = cached_component(components, prefix + 'base-' + platform, base_key, produce_base) / 'kernel'
    def produce_kernel(stage):
        with kernels.tree_lock(hardware, device, tree):
            kernels.build_device(hardware, cache, device, base, stage / 'kernel', tree=tree)
    directory = cached_component(components, prefix + device,
        keyed(lambda: kernel_inputs(hardware, device, base.parent.name)), produce_kernel)
    return directory / 'kernel'


def prepare_kernel(hardware=None, cache=None, *, device, kernel_tree=None):
    """Build/reuse a coherent kernel, DTB and modules, without boot/tool producers.

    Defaults to the active hardware package and normal shared Orchard cache.
    Assembly policy and explicit-component overrides belong to the caller.
    """
    hardware, cache = _build_context(hardware, cache, device)
    _check_kernel_recipe(hardware, device)
    tree = kernels.check_tree(hardware, device, kernel_tree) if kernel_tree is not None else None
    with component_lock(cache):
        return _prepare_kernel(hardware, cache, device, tree)


def has_loader_patches(hardware):
    """Hardware keeps its m1n1 patches in ordered layers under boot/m1n1/patches/."""
    return any((Path(hardware) / 'boot/m1n1/patches').rglob('*.patch'))


# What the static tools are built from; the rest of the recovery directory is
# recovery runtime and doesn't change them.
# What orchard's m1n1 (the quiet one every device boots) is built from.
M1N1_INPUTS = ['boot/m1n1', 'boot/quiet/m1n1.patch', 'src/orchard/buildenv.py']
TOOL_INPUTS = ['boot/initramfs/recovery/' + name for name in
               ('build-tools', 'build-tools-in-container', 'Containerfile',
                'install-toolchain', 'upstream.lock')] + ['src/orchard/buildenv.py']


def recipe_work(components, recipe, key):
    """Work directory of one recipe input set, shared by every checkout in the Orchard cache.

    Recipes keep sources and incremental output there. Use refreshes the
    directory's time, which `orchard cache --prune` keeps by. Caller holds the lock.
    """
    work = components.parent / 'work' / recipe / key
    if work.exists():
        os.utime(work)
    else:
        work.parent.mkdir(parents=True, exist_ok=True)
    return work


def _prepare_tools(hardware, components):
    """The hardware's static recovery tools (BusyBox, Dropbear, Wi-Fi tools). Caller holds the lock."""
    def produce_tools(stage):
        work = recipe_work(components, 'recovery-tools', recipe_key(hardware, TOOL_INPUTS))
        if not (work / 'bin/busybox').exists():
            command(hardware / 'boot/initramfs/recovery/build-tools',
                    env={**os.environ, 'OMA_RECOVERY_TOOLS_WORK': str(work)})
        for tool in (work / 'bin').iterdir():
            if tool.is_file() and not tool.is_symlink():
                shutil.copyfile(tool, stage / tool.name)
                (stage / tool.name).chmod(0o755)
    return cached_component(components, 'recovery-tools',
        lambda: recipe_key(hardware, TOOL_INPUTS), produce_tools)


def prepare_tools(hardware=None, cache=None, *, device):
    """The directory of static recovery tools for RAM images, built or reused."""
    from . import published
    hardware, cache = _build_context(hardware, cache, device)
    tools = published.tools(hardware, cache)
    if tools is not None:
        return tools
    with component_lock(cache):
        return _prepare_tools(hardware, cache / 'components-v2')


RELEASES = 'https://github.com/OrchardLinux/orchard/releases/download'
# What Pongo is built from; boot/quiet/m1n1.patch is m1n1's, not Pongo's.
PONGO_RECIPE = ['boot/quiet/build-pongo.py', 'boot/quiet/pongo.patch']


def released_pongo(hardware):
    """(tag, sha256) of the latest release's Pongo, if this checkout's recipe is unchanged since.

    Pongo needs macOS with Xcode and the iPhoneOS SDK to build, and it is the
    same for every device, so an unchanged recipe uses the released binary.
    """
    def git(*arguments):
        return subprocess.run(['git', '-C', str(hardware), *arguments], capture_output=True, text=True)
    described = git('describe', '--tags', '--abbrev=0')
    if described.returncode:
        return None
    tag = described.stdout.strip()
    if git('diff', '--quiet', tag, '--', *PONGO_RECIPE).returncode:
        return None
    from .http import open_url
    for sums in ('SHA256SUMS', 'launcher.SHA256SUMS'):  # releases before 0.2 list it with the launcher
        try:
            with open_url(f'{RELEASES}/{tag}/{sums}') as response:
                text = response.read(1024 * 1024).decode()
        except (OSError, ValueError):
            continue
        for line in text.splitlines():
            parts = line.split()
            if len(parts) == 2 and parts[1].removeprefix('./') == 'pongo.bin' and re.fullmatch(r'[0-9a-f]{64}', parts[0]):
                return tag, parts[0]
    return None


def _prepare_pongo(hardware, components):
    released = released_pongo(hardware)
    if released is not None:
        tag, checksum = released
        def fetch(stage):
            from .http import open_url
            print(f'Using the released Pongo of {tag}; its recipe is unchanged', flush=True)
            with open_url(f'{RELEASES}/{tag}/pongo.bin') as response:
                data = response.read(64 * 1024 * 1024 + 1)
            if len(data) > 64 * 1024 * 1024 or hashlib.sha256(data).hexdigest() != checksum:
                raise ValueError(f'The released Pongo of {tag} does not match its checksum')
            (stage / 'pongo.bin').write_bytes(data)
        return cached_component(components, 'pongo', lambda: 'release-' + checksum, fetch) / 'pongo.bin'

    def produce(stage):
        if sys.platform != 'darwin' or not shutil.which('xcrun'):
            raise ValueError('Pongo\'s recipe (boot/quiet/build-pongo.py, pongo.patch) differs from the latest release, and building it '
                             'needs macOS with Xcode and the iPhoneOS SDK. Pass --pongo FILE to use another, '
                             'or build on a Mac.')
        work = recipe_work(components, 'pongo', recipe_key(hardware, PONGO_RECIPE))
        argv = [sys.executable, hardware / 'boot/quiet/build-pongo.py',
                '--work-dir', work, '--output', stage / 'pongo.bin']
        if (work / 'source').exists():
            argv.append('--resume')
        command(*argv)
    return cached_component(components, 'pongo', lambda: recipe_key(hardware, PONGO_RECIPE), produce) / 'pongo.bin'


def _prepare_m1n1(hardware, components, device):
    def key():
        return recipe_key(hardware, M1N1_INPUTS)

    def produce(stage):
        # One tree per patch set: a changed series starts a fresh tree instead
        # of resuming one the recipe would refuse. Crates are shared.
        work = recipe_work(components, 'm1n1-' + device, key())
        # Quiet, as CI builds it: black, no m1n1 logo, until m1n1 paints the system's splash.
        argv = [hardware / 'boot/m1n1/build', device, '--output', stage / 'm1n1.bin',
                '--work-dir', work, '--cargo-dir', components.parent / 'work/.cargo-m1n1',
                '--extra-patch', hardware / 'boot/quiet/m1n1.patch', '--extra-cflags', '-DQUIET_BOOT']
        if (work / 'applied-patches').exists():
            argv.append('--resume')
        elif work.exists():
            shutil.rmtree(work)  # an earlier attempt stopped before its patches were applied
        command(*argv)
    return cached_component(components, 'm1n1-' + device, key, produce) / 'm1n1.bin'


def prepare_components(hardware=None, cache=None, *, device, kernel=None,
                       busybox=None, loader=None, pongo=None, kernel_tree=None):
    """Build/reuse components shared by experiments and larger images.

    Explicit component paths bypass their producers. The returned paths are not
    installer-specific. Callers supplying a different hardware tree must also
    arrange for its Python build dependencies to be importable.
    """
    hardware, cache = _build_context(hardware, cache, device)
    components = cache / 'components-v2'
    if kernel is None:
        _check_kernel_recipe(hardware, device)
    if kernel is not None and kernel_tree is not None:
        raise ValueError('Give a staged kernel (--kernel-dir) or a kernel tree (--kernel-tree), not both')
    tree = kernels.check_tree(hardware, device, kernel_tree) if kernel_tree is not None else None
    if loader is None and not has_loader_patches(hardware):
        raise ValueError(f'Missing loader patches for {device}; add a loader recipe or supply --loader FILE')
    with component_lock(cache):
        if pongo is None:
            pongo = _prepare_pongo(hardware, components)
        if kernel is None:
            kernel = _prepare_kernel(hardware, cache, device, tree)
        if busybox is None:
            busybox = _prepare_tools(hardware, components) / 'busybox'
        if loader is None:
            # One m1n1 starts every Apple device; the board only picks its DTB.
            loader = _prepare_m1n1(hardware, components, 'apple')
    return {name: Path(path).resolve() for name, path in
            dict(kernel=kernel, busybox=busybox, loader=loader, pongo=pongo).items()}


def prepare_shared(hardware=None, cache=None):
    """Pongo, orchard's m1n1 and the static recovery tools, which every platform shares, built or reused.

    {'pongo': Path, 'loader': m1n1.bin, 'tools': directory}; the tools
    directory holds busybox, dropbear, dropbearkey, wpa_supplicant, wpa_cli
    and iw. A distribution's splash is data for this m1n1 (assemble.py --logo),
    not a m1n1 of its own.
    """
    from . import published
    hardware, cache = _build_context(hardware, cache, 'apple')
    components = cache / 'components-v2'
    # The nightly's m1n1 and tools when built from the same recipes; built here otherwise.
    loader, tools = published.m1n1(hardware, cache), published.tools(hardware, cache)
    with component_lock(cache):
        return {'pongo': _prepare_pongo(hardware, components).resolve(),
                'loader': (loader or _prepare_m1n1(hardware, components, 'apple')).resolve(),
                'tools': (tools or _prepare_tools(hardware, components)).resolve()}


def prepare_loader(hardware=None, cache=None, *, device):
    """Pongo and the storage-capable m1n1 that orchard launch needs, built or reused from the cache."""
    hardware, cache = _build_context(hardware, cache, device)
    if not has_loader_patches(hardware):
        raise ValueError(f'Missing loader patches for {device}')
    components = cache / 'components-v2'
    with component_lock(cache):
        return {'pongo': _prepare_pongo(hardware, components).resolve(),
                'm1n1': _prepare_m1n1(hardware, components, device).resolve()}


