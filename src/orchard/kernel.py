"""CLI-owned kernel source preparation and cached-component composition.

Hardware is an unchanged input: use its existing full kernel, module, DTB and
staging recipes. Cache policy and module-SDK handling do not live in hardware.
Hardware builds one kernel per platform (kernel/series/<platform>); a device
names its platform and DTB in devices/<device>/device.toml.
"""
from contextlib import contextmanager
import fcntl
import hashlib
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile

from . import sources
from .kernel_sdk import export as export_sdk


def _series(hardware, *args):
    return subprocess.check_output([sys.executable, str(Path(hardware) / 'kernel/series.py'), *args],
                                   text=True)


def descriptor(hardware, device):
    """The platform descriptor's kernel table (platforms/<platform>.toml): arch,
    image, dtbs and the repository paths of upstream, series and config."""
    return json.loads(_series(hardware, '--platform', device))


def pin(hardware, device):
    lock = Path(hardware) / descriptor(hardware, device)['upstream']
    return dict(line.split('=', 1) for line in lock.read_text().splitlines() if '=' in line)


def layout(hardware, device):
    """(platform, dtb) for a device, from hardware's kernel series."""
    from .hardware import device as described
    if 'kernel' not in described(device, hardware):
        raise ValueError(f'{device} has no kernel yet: its device.toml describes identity only')
    platform, dtb = _series(hardware, '--resolve', device).split()
    if dtb == '-':
        raise ValueError(f'{device} is a kernel platform, not a device')
    return platform, dtb


def shared_tree(hardware):
    """Whether hardware's kernel/build keeps one tree per platform in the Orchard cache."""
    builder = Path(hardware) / 'kernel/build'
    return builder.is_file() and '--work-dir' in builder.read_text()


def work_directory(hardware, device, tree=None):
    """The incremental kernel tree hardware's kernel/build compiles in.

    Current hardware shares one tree per platform and kernel commit between all
    checkouts, in the Orchard cache; older hardware keeps it in the checkout.
    Never reset it: it holds editable source and compiler output. A
    developer's kernel tree (TREE) builds inside itself, TREE/.orchard-out/
    PLATFORM, which git ignores there: its output lives and goes with the tree.
    """
    platform = layout(hardware, device)[0]
    if tree is not None:
        return Path(tree).resolve() / OUT / platform
    if shared_tree(hardware):
        return sources.cache_root() / 'kernel-work' / f"{platform}-{pin(hardware, device)['commit'][:12]}"
    return Path(hardware) / 'kernel/.work' / platform


OUT = '.orchard-out'  # a developer kernel tree's build output, inside the tree


def ignore_output(tree):
    """Make the tree's git ignore OUT (its info/exclude), so builds never show in status or in its key."""
    exclude = Path(os.fsdecode(subprocess.check_output(['git', '-C', str(tree), 'rev-parse', '--git-path',
                                                        'info/exclude']).strip()))
    if not exclude.is_absolute():
        exclude = Path(tree) / exclude
    line = f'/{OUT}/'
    text = exclude.read_text() if exclude.is_file() else ''
    if line not in text.splitlines():
        exclude.parent.mkdir(parents=True, exist_ok=True)
        exclude.write_text(text + ('' if text.endswith('\n') or not text else '\n') + line + '\n')


def check_tree(hardware, device, tree):
    """A developer's kernel tree: a git checkout with the platform's defconfig."""
    tree = Path(tree).expanduser().resolve()
    described = descriptor(hardware, device)
    defconfig = tree / 'arch' / described['arch'] / 'configs' / described['defconfig']
    if not (tree / 'Makefile').is_file() or not defconfig.is_file():
        raise ValueError(f'{tree} is not a kernel tree with arch/{described["arch"]}/configs/'
                         f'{described["defconfig"]} (the {described["platform"]} defconfig)')
    if subprocess.run(['git', '-C', str(tree), 'rev-parse', '--git-dir'], capture_output=True).returncode:
        raise ValueError(f'{tree} is not a git checkout; orchard identifies a kernel tree by its content')
    ignore_output(tree)
    return tree


def _source(hardware, cache, device, tree):
    """(source, compiler output): orchard's patched tree, or the developer's TREE as it is."""
    if tree is None:
        return prepare_source(hardware, cache, device)
    return Path(tree).resolve(), work_directory(hardware, device, tree) / 'output'


def _identity(source, tree):
    return sources.working_tree(source, force=tree is None)


@contextmanager
def tree_lock(hardware, device, tree=None):
    """Exclusive use of the kernel tree, shared with kernel/build run by anyone.

    kernel/build takes the same lock unless ORCHARD_KERNEL_WORK_LOCKED names
    its tree, so the CLI holds it across source preparation and the build.
    """
    work = work_directory(hardware, device, tree)
    work.mkdir(parents=True, exist_ok=True)
    with (work / '.lock').open('a') as lock:
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            print(f'Waiting for another kernel build to finish with {work} ...', file=sys.stderr, flush=True)
            fcntl.flock(lock, fcntl.LOCK_EX)
        previous = os.environ.get('ORCHARD_KERNEL_WORK_LOCKED')
        os.environ['ORCHARD_KERNEL_WORK_LOCKED'] = str(work)
        try:
            yield work
        finally:
            if previous is None:
                os.environ.pop('ORCHARD_KERNEL_WORK_LOCKED', None)
            else:
                os.environ['ORCHARD_KERNEL_WORK_LOCKED'] = previous
            fcntl.flock(lock, fcntl.LOCK_UN)


def tree_marks(hardware, device):
    """What applied-patches may hold: the kernel commit, as kernel/build writes it, or nothing (an older tree)."""
    return ((pin(hardware, device)['commit'] + '\n').encode(), b'')


def source_tree(hardware, cache, device):
    """Resolve the requested tree before cache lookup, without changing source.

    A clean checkout of an older managed patch set must not poison the new
    content-addressed key. Local edits still participate, or block a patch
    transition when they cannot be separated safely.
    """
    selected = pin(hardware, device)
    work = work_directory(hardware, device)
    source = work / 'source'
    expected = tree_marks(hardware, device)
    declared = sources.declared_tree(selected['url'], selected['commit'], cache=cache, donor=source)
    if not (source / '.git').exists():
        return declared
    if (sources.git(source, 'rev-parse', 'HEAD').decode() != selected['commit'] or
            sources.git(source, 'remote', 'get-url', 'origin').decode() != selected['url']):
        raise ValueError('Local kernel revision/remote differs from upstream.lock; sources were not reset')
    current = sources.working_tree(source)
    marker = work / 'applied-patches'
    baseline = work / 'prepared-tree'
    if marker.exists() and marker.read_bytes() not in expected:
        if current == declared or (baseline.exists() and current == baseline.read_text().strip()):
            return declared
        raise ValueError('The kernel tree was prepared for another state and has local edits; '
                         'refusing to discard edits or select an unrelated cached kernel')
    if marker.exists() and current == declared and not baseline.exists():
        # Upgrade legacy metadata on a verified clean cache hit, without
        # touching source/index/compiler output or invoking a producer.
        baseline.write_text(declared + '\n')
    return current


def prepare_source(hardware, cache, device):
    hardware = Path(hardware)
    selected = pin(hardware, device)
    work = work_directory(hardware, device)
    source = work / 'source'
    work.mkdir(parents=True, exist_ok=True)
    (work / 'output').mkdir(exist_ok=True)
    if not source.exists():
        sources.checkout(selected['url'], selected['commit'], source, cache=cache)
    if (sources.git(source, 'rev-parse', 'HEAD').decode() != selected['commit'] or
            sources.git(source, 'remote', 'get-url', 'origin').decode() != selected['url']):
        raise ValueError('Local kernel revision/remote differs from upstream.lock; sources were not reset')
    expected = tree_marks(hardware, device)
    marker = work / 'applied-patches'
    baseline = work / 'prepared-tree'
    declared = sources.declared_tree(selected['url'], selected['commit'], cache=cache, donor=source)
    if not marker.exists() or marker.read_bytes() not in expected:
        # source_tree refuses a tree of another state that has edits.
        source_tree(hardware, cache, device)
        marker.write_bytes(expected[0])
        baseline.write_text(declared + '\n')
    # Safely migrate an older workspace only when its source is exactly declared.
    if sources.working_tree(source) == declared:
        baseline.write_text(declared + '\n')
    return source, work / 'output'


def build_base(hardware, cache, device, output, tree=None):
    """One-time fill using hardware's existing full producer, not a new API.

    Retain only the kernel/in-tree modules and an SDK. The producer also builds
    external modules/final DTB on this first fill; those outputs are not in the
    reusable base. Subsequent device-layer edits bypass the full producer.
    """
    from .build import command
    hardware, output = Path(hardware), Path(output)
    source, compiler_output = _source(hardware, cache, device, tree)
    identity = _identity(source, tree)
    with tempfile.TemporaryDirectory(prefix='base-', dir=work_directory(hardware, device, tree)) as temporary:
        work = Path(temporary)
        extra = ['--work-dir', str(work_directory(hardware, device, tree))] if shared_tree(hardware) or tree else []
        if tree is not None:
            extra += ['--kernel-tree', str(source)]
        command(hardware / 'kernel/build', device, '--resume', *extra, '--output', work / 'full')
        command(sys.executable, hardware / 'kernel/stage-modules.py',
                '--build', compiler_output, '--output', work / 'in-tree')
        output.mkdir(parents=True)
        for name in (descriptor(hardware, device)['image'], 'kernel.release'):
            shutil.copy2(work / 'full' / name, output / name)
        shutil.copytree(work / 'full/dtbs', output / 'dtbs')
        shutil.copytree(work / 'in-tree/lib', output / 'lib')
        export_sdk(compiler_output, output / 'sdk', descriptor(hardware, device)['arch'])
        (output / 'inputs.json').write_text(json.dumps({'source_tree': identity}) + '\n')
    if _identity(source, tree) != identity:
        raise ValueError('Kernel source changed during build; refusing result')


def build_device(hardware, cache, device, base, output, tree=None):
    """Select the device DTB; build any external modules with a PRIVATE cached-SDK copy."""
    from .build import command
    hardware, base, output = map(Path, (hardware, base, output))
    platform_name, dtb = layout(hardware, device)
    described = descriptor(hardware, device)
    source, _ = _source(hardware, cache, device, tree)
    identity = _identity(source, tree)
    if json.loads((base / 'inputs.json').read_text())['source_tree'] != identity:
        raise ValueError('Cached kernel source identity differs; rebuild the base')
    # A developer's tree brings its own defconfig; orchard's config is for its own tree.
    if tree is None and (base / 'sdk/.config').read_bytes() != (hardware / described['config']).read_bytes():
        raise ValueError('Cached kernel configuration differs; rebuild the base')
    release = (base / 'kernel.release').read_text().strip()
    if not release or '/' in release or len(release.split()) != 1:
        raise ValueError('Invalid kernel release')
    if (base / 'sdk/include/config/kernel.release').read_text().strip() != release:
        raise ValueError('Cached module SDK release differs')
    with tempfile.TemporaryDirectory(prefix='modules-', dir=work_directory(hardware, device, tree)) as temporary:
        work = Path(temporary)
        sdk = work / 'output'
        shutil.copytree(base / 'sdk', sdk)
        # The unchanged hardware staging recipe scans the compiler-output tree.
        # Restore in-tree modules at their ORIGINAL relative build paths there,
        # so it can merge external overrides without needing a new hardware API.
        for module in (base / 'lib/modules' / release / 'kernel').rglob('*.ko'):
            if module.is_symlink():
                raise ValueError('Symlink in cached in-tree modules')
            target = sdk / module.relative_to(base / 'lib/modules' / release / 'kernel')
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(module, target)
        command(sys.executable, Path(hardware) / 'producers/buildenv.py', 'build',
                '--file', hardware / 'kernel/Containerfile', '--context', hardware / 'kernel',
                '--image', 'localhost/orchard-hardware-kernel-toolchain:bookworm')
        external = []
        if (hardware / 'kernel/modules' / device).is_dir():
            command(hardware / 'kernel/build-modules', '--source', source, '--build', sdk,
                    '--modules', hardware / 'kernel/modules' / device,
                    '--localversion', '-orchard-' + platform_name, '--arch', described['arch'],
                    '--output', work / 'external')
            external = ['--external', work / 'external']
        command(sys.executable, hardware / 'kernel/stage-modules.py', '--build', sdk,
                *external, '--output', work / 'staged')
        output.mkdir(parents=True)
        for name in (described['image'], 'kernel.release'):
            shutil.copy2(base / name, output / name)
        shutil.copy2(base / 'dtbs' / (dtb + '.dtb'), output / (device + '.dtb'))
        # Every board of the platform: an installed root serves all of them.
        shutil.copytree(base / 'dtbs', output / 'dtbs')
        shutil.copytree(work / 'staged/lib', output / 'lib')
    if _identity(source, tree) != identity:
        raise ValueError('Kernel source changed during module build; refusing result')
