"""Inspect and prune Orchard's directories: the shared build cache and the store's downloads."""
from contextlib import contextmanager
from datetime import datetime
import fcntl
import json
import os
from pathlib import Path
import platform
import shutil
import subprocess

from .build import INPUTS_RECORD, cache_root, component_lock, remove_unshared, share_files


def _size(path):
    total = 0
    for directory, _, names in os.walk(path):
        for name in names:
            file = Path(directory) / name
            if not file.is_symlink():
                total += file.stat().st_size
    return total


def _on_disk(path):
    """Bytes under PATH, counting hard-linked files once."""
    seen, total = set(), 0
    for directory, _, names in os.walk(path):
        for name in names:
            info = (Path(directory) / name).lstat()
            if (info.st_dev, info.st_ino) not in seen:
                seen.add((info.st_dev, info.st_ino))
                total += info.st_size
    return total


def _human(size):
    for unit in ('B', 'K', 'M', 'G'):
        if size < 1024 or unit == 'G':
            return f'{size:.0f}{unit}' if unit == 'B' else f'{size:.1f}{unit}'
        size /= 1024


def _keyed(directory):
    """(name, key, path) for DIRECTORY/NAME/KEY, oldest use first within each name."""
    if not directory.is_dir():
        return []
    return [(parent.name, entry.name, entry)
            for parent in sorted(directory.iterdir()) if parent.is_dir() and not parent.name.startswith('.')
            for entry in sorted(parent.iterdir(), key=lambda p: p.stat().st_mtime) if entry.is_dir()]


def entries(cache=None):
    """(component, key, path) for published entries and leftover build directories."""
    return _keyed(Path(cache or cache_root()) / 'components-v2')


def work_entries(cache=None):
    """(recipe, key, path) for the recipes' work directories (m1n1, Pongo, recovery tools)."""
    return _keyed(Path(cache or cache_root()) / 'work')


def _summary(path):
    record = path / INPUTS_RECORD
    if not record.is_file():
        return 'inputs not recorded'
    inputs = json.loads(record.read_text())
    if 'tree' in inputs:
        return f"tree {inputs['tree'][:12]}"
    if 'base' in inputs:
        return f"base {inputs['base'][:12]} dtb {inputs.get('dtb')}"
    return ''


def show(cache=None):
    root = Path(cache or cache_root())
    rows = entries(root)
    total = 0
    for component, key, path in rows:
        size = _size(path)
        total += size
        when = datetime.fromtimestamp(path.stat().st_mtime).strftime('%Y-%m-%d %H:%M')  # last use
        state = 'unfinished build' if key.startswith('build-') else _summary(path)
        print(f'{component:22} {key[:12]:12} {_human(size):>7}  {when}  {state}')
    components = root / 'components-v2'
    if rows:
        print(f'{len(rows)} entries, {_human(total)} ({_human(_on_disk(components))} on disk) in {components}')
    else:
        print('Component cache is empty')
    compiler = root / 'ccache'
    if compiler.is_dir():
        # ccache keeps itself within its own size limit (2 GB, set in kernel/build).
        print(f'Compiler cache {_human(_on_disk(compiler))} in {compiler}')
    work = work_entries(root)
    if work:
        recipes = sorted({recipe for recipe, _, _ in work})
        print(f'{len(work)} work director{"y" if len(work) == 1 else "ies"} ({", ".join(recipes)}), '
              f'{_human(_on_disk(root / "work"))} in {root / "work"}')
    for tree, used, clean in kernel_trees(root):
        when = datetime.fromtimestamp(used).strftime('%Y-%m-%d %H:%M')
        print(f'Kernel tree {tree.name:28} {_human(_on_disk(tree)):>7}  {when}  '
              f'{"no local edits" if clean else "local edits, kept"}')
    store = _store() if cache is None else None
    downloaded = assets(store) if store else []
    if downloaded:
        print(f'{len(downloaded)} downloaded release assets, '
              f'{_human(sum(p.stat().st_size for p in downloaded))} in {downloaded[0].parent}')
    for legacy in legacy_firmware(store) if store else []:
        print(f'Unused {legacy} ({_human(_on_disk(legacy))}), left by an older Orchard; --prune removes it')
    print(f'Orchard uses {_human(usage(root, store))} of its {_human(budget())} limit; '
          'builds prune the least recently used entries above it (ORCHARD_CACHE_BUDGET_GB)')


def remove(selectors, cache=None):
    """Delete entries named COMPONENT/KEY-PREFIX or COMPONENT (all of its entries)."""
    root = Path(cache or cache_root())
    rows = entries(root)
    chosen = []
    for selector in selectors:
        component, _, prefix = selector.partition('/')
        matches = [r for r in rows if r[0] == component and r[1].startswith(prefix)]
        if not matches:
            raise ValueError(f'No cache entry matches {selector}')
        if prefix and len(matches) > 1:
            raise ValueError(f'{selector} is ambiguous; give more of the key')
        chosen += [m for m in matches if m not in chosen]
    # The build lock keeps removal away from a running build and its sources.
    with component_lock(root, exclusive=True):
        for component, key, path in chosen:
            shutil.rmtree(path)
            print(f'Removed {component}/{key}')
        remove_unshared(root / 'components-v2')


def prune(keep, cache=None):
    """Keep the KEEP most recently used entries of each component; delete the rest.

    Also removes builds that never finished, shares identical files of
    entries published before sharing existed, and keeps the KEEP most
    recently used work directories of each recipe. Kernel trees are kept:
    they hold editable source.
    """
    if keep < 1:
        raise ValueError('--keep must be at least 1')
    root = Path(cache or cache_root())
    components = root / 'components-v2'
    components.mkdir(parents=True, exist_ok=True)
    with component_lock(root, exclusive=True):
        before = _on_disk(components)
        kept = {}
        for component, key, path in reversed(entries(root)):  # newest use first
            if key.startswith('build-') or kept.setdefault(component, 0) >= keep:
                shutil.rmtree(path)
                print(f'Removed {component}/{key}')
                continue
            kept[component] += 1
            share_files(components, path, json.loads((path / 'component.json').read_text()))
        remove_unshared(components)
        # Entry locks of finished builds; none is held while this holds the cache exclusively.
        for lock in (components / '.locks').glob('*'):
            lock.unlink()
        work = root / 'work'
        before += _on_disk(work)
        used = {}
        for recipe, key, path in reversed(work_entries(root)):
            used[recipe] = used.get(recipe, 0) + 1
            if used[recipe] > keep:
                shutil.rmtree(path)
                print(f'Removed work {recipe}/{key}')
        after = _on_disk(components) + _on_disk(work)
    print(f'Freed {_human(before - after)}; {_human(after)} on disk in {root}')
    fit(cache=root)
    trim_vm()


def trim_vm():
    """On macOS, give the Podman VM's freed space back to macOS; deletes nothing.

    What is deleted inside the VM (images, layers, build volumes) stays
    allocated in its disk file until the VM trims it. The VM also trims
    daily (fstrim.timer); this does it now, after a cleanup.
    """
    if platform.system() != 'Darwin' or not shutil.which('podman'):
        return
    try:
        state = subprocess.run(['podman', 'machine', 'inspect', '--format', '{{.State}}'],
                               capture_output=True, text=True, timeout=30)
        if state.returncode or state.stdout.strip() != 'running':
            print('The Podman VM is not running; nothing to trim')
            return
        before = shutil.disk_usage(Path.home()).free
        result = subprocess.run(['podman', 'machine', 'ssh', 'sudo', 'fstrim', '-a'],
                                capture_output=True, text=True, timeout=600)
    except (OSError, subprocess.TimeoutExpired) as error:
        print(f'Could not trim the Podman VM: {error}')
        return
    if result.returncode:
        print(f'Could not trim the Podman VM: {(result.stderr or result.stdout).strip()[-300:]}')
        return
    given = max(0, shutil.disk_usage(Path.home()).free - before)
    print(f'Trimmed the Podman VM: {_human(given)} given back to macOS')


# The automatic size limit. Each build first deletes the least recently used
# cache entries until Orchard's two directories (the shared cache and the
# store) fit in it. Only what Orchard can make again goes: older component
# entries and work directories, downloaded release assets, and kernel trees
# without local edits. The newest entry of each component, work recipe and
# the newest kernel tree always stay.
BUDGET_GB = 20


def budget():
    """Bytes Orchard's directories may use; ORCHARD_CACHE_BUDGET_GB (GiB, as listed) changes it, 0 turns it off."""
    return float(os.environ.get('ORCHARD_CACHE_BUDGET_GB', BUDGET_GB)) * 1024 ** 3


def _store():
    from .cache import cache_root as store_root
    return store_root()


@contextmanager
def _try_lock(path):
    """Hold PATH's flock exclusively, or yield False if another process holds it."""
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open('a') as lock:
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            yield False
            return
        yield True


def legacy_firmware(store=None):
    """Directories an older Orchard left in the firmware store; this one only reads OUTPUTS.

    Firmware used to come from a full IPSW download (archive/) unpacked on disk
    (extracted/, extra/); it is now read from Apple's server by byte range.
    """
    root = Path(store or _store()) / 'firmware'
    if not root.is_dir():
        return []
    return [entry for profile in sorted(root.iterdir()) if profile.is_dir()
            for entry in sorted(profile.iterdir()) if entry.name in ('archive', 'extracted', 'extra')]


def kernel_trees(cache=None):
    """(path, last use, clean) for each shared kernel tree, oldest use first.

    Clean means the source is exactly what Orchard prepared (prepared-tree):
    no local edits, so the tree can be made again.
    """
    from .sources import working_tree
    root = Path(cache or cache_root()) / 'kernel-work'
    if not root.is_dir():
        return []
    trees = []
    for tree in root.iterdir():
        if not tree.is_dir():
            continue
        marks = [tree / name for name in ('prepared-tree', 'applied-patches') if (tree / name).exists()]
        used = max((mark.stat().st_mtime for mark in marks), default=tree.stat().st_mtime)
        clean = False
        baseline = tree / 'prepared-tree'
        if baseline.is_file() and (tree / 'source/.git').exists():
            try:
                clean = working_tree(tree / 'source') == baseline.read_text().strip()
            except (OSError, subprocess.CalledProcessError):
                clean = False
        trees.append((tree, used, clean))
    return sorted(trees, key=lambda row: row[1])


def assets(store=None):
    """Downloaded release assets, by content hash; each is downloaded again when needed."""
    root = Path(store or _store()) / 'artifacts/sha256'
    if not root.is_dir():
        return []
    return sorted((p for p in root.iterdir() if p.is_file() and '.' not in p.name), key=lambda p: p.stat().st_mtime)


def _remove_asset(path):
    with _try_lock(path.with_suffix('.asset.lock')) as free:
        if not free:
            return False
        path.unlink()
        for leftover in (path.with_suffix('.download'), path.with_suffix('.download.parts')):
            if leftover.is_dir():
                shutil.rmtree(leftover)
            elif leftover.exists():
                leftover.unlink()
    for lock in (path.with_suffix('.asset.lock'), path.with_suffix('.download.lock')):
        lock.unlink(missing_ok=True)
    return True


def _remove_tree(path):
    with _try_lock(path / '.lock') as free:
        if not free:
            return False
        shutil.rmtree(path)
    return True


def usage(cache=None, store=None):
    """Bytes on disk in Orchard's cache and, if given, its store."""
    return sum(_on_disk(Path(root)) for root in (cache or cache_root(), store) if root and Path(root).is_dir())


def fit(limit=None, cache=None, store=None, report=print):
    """Delete least recently used entries until Orchard's directories fit in LIMIT bytes.

    Never waits for a running build: what it uses is skipped. Returns the bytes
    still in use. A CACHE given without a STORE is handled alone: the store
    belongs to the default cache.
    """
    store = Path(store) if store else (_store() if cache is None else None)
    cache = Path(cache or cache_root())
    limit = budget() if limit is None else limit
    for legacy in legacy_firmware(store) if store else []:
        shutil.rmtree(legacy)
        report(f'Removed unused {legacy.relative_to(store)} (left by an older Orchard)')
    if limit <= 0:
        return usage(cache, store)
    used = usage(cache, store)
    if used <= limit:
        return used
    start = used
    candidates = []  # (last use, size, label, remove)
    with _try_lock(cache / '.lock') as idle:
        if idle:
            # Component entries and work directories, all but the newest of each.
            for rows, kind in ((entries(cache), ''), (work_entries(cache), 'work ')):
                newest = {}
                for name, key, path in rows:  # oldest use first
                    newest[name] = (key, path)
                for name, key, path in rows:
                    if newest[name][1] != path:
                        candidates.append((path.stat().st_mtime, path, f'{kind}{name}/{key[:12]}',
                                           lambda p=path: (shutil.rmtree(p), True)[1]))
        trees = kernel_trees(cache)
        for tree, last, clean in trees[:-1]:
            if clean:
                candidates.append((last, tree, f'kernel tree {tree.name}', lambda p=tree: _remove_tree(p)))
        for asset in assets(store) if store else []:
            candidates.append((asset.stat().st_mtime, asset, f'asset {asset.name[:12]}',
                               lambda p=asset: _remove_asset(p)))
        for _, path, label, remove in sorted(candidates, key=lambda row: row[0]):
            if used <= limit:
                break
            size = _on_disk(path) if path.is_dir() else path.lstat().st_size
            if remove():
                used -= size
                report(f'Removed {label} ({_human(size)}, least recently used)')
        if idle:
            remove_unshared(cache / 'components-v2')
    if used < start:
        report(f'Freed {_human(start - used)}; Orchard uses {_human(used)} of its {_human(limit)} limit')
    if used > limit:
        edited = [tree.name for tree, _, clean in kernel_trees(cache) if not clean]
        note = f'; kernel trees with local edits are kept: {", ".join(edited)}' if edited else ''
        report(f'Orchard uses {_human(used)}, over its {_human(limit)} limit{note}. '
               'See orchard cache; ORCHARD_CACHE_BUDGET_GB changes the limit')
    return used
