"""Files of the kernel source orchard builds, for tests that compile or read them.

The kernel is OrchardLinux/linux at the commit a platform's lock names
(kernel/upstream.lock). read() returns one file of that commit:

- from ORCHARD_KERNEL_SOURCE, a kernel tree, when it is set (a kernel pull
  request's checks point it at the tree under review, and name the platforms
  that tree is the source of in ORCHARD_KERNEL_PLATFORMS);
- else from a partial clone kept in the temporary directory: the commit's
  directory listing is fetched once, each file when a test first asks for it.

Without the variable and without a network the tests that need a file are
skipped, not failed: nothing in orchard itself changed.
"""
import os
from pathlib import Path
import subprocess
import tempfile
import unittest

ROOT = Path(__file__).resolve().parents[1]
LOCKS = {'apple-16k': 'kernel/upstream.lock', 'apple-4k': 'kernel/upstream.lock',
         'apple-armv7': 'kernel/upstream-armv7.lock'}
_read = {}


def lock(platform='apple-16k'):
    """{'url', 'commit'} of a platform's kernel."""
    text = (ROOT / LOCKS[platform]).read_text()
    return dict(line.split('=', 1) for line in text.splitlines() if '=' in line)


def _git(repository, *arguments, timeout=300):
    try:
        return subprocess.run(['git', '-C', str(repository), *arguments], capture_output=True, text=True,
                              timeout=timeout)
    except subprocess.TimeoutExpired as e:
        return subprocess.CompletedProcess(e.cmd, 124, '', f'timed out after {timeout} s')


def _show(repository, name):
    """`git cat-file blob NAME`, which fetches the file: a stalled fetch is tried again."""
    for attempt in range(3):
        shown = _git(repository, 'cat-file', 'blob', name, timeout=60)
        if shown.returncode != 124:
            break
    return shown


def _clone(url, commit):
    """A bare partial clone holding COMMIT without its files, or None when it cannot be fetched."""
    repository = Path(tempfile.gettempdir()) / f'orchard-kernel-source-{os.getuid()}' / commit[:16]
    if _git(repository, 'cat-file', '-e', commit + '^{commit}').returncode == 0:
        return repository
    repository.mkdir(parents=True, exist_ok=True)
    for arguments in (('init', '-q', '--bare'), ('remote', 'add', 'origin', url),
                      ('config', 'remote.origin.promisor', 'true'),
                      ('config', 'remote.origin.partialclonefilter', 'blob:none')):
        _git(repository, *arguments)
    fetched = _git(repository, 'fetch', '-q', '--no-tags', '--depth', '1', '--filter=blob:none', 'origin', commit)
    return repository if fetched.returncode == 0 else None


def read(path, platform='apple-16k'):
    """The text of PATH in PLATFORM's kernel source; skips the calling test when it cannot be had."""
    key = (platform, path)
    if key in _read:
        return _read[key]
    tree = os.environ.get('ORCHARD_KERNEL_SOURCE')
    if tree:
        # A tree is one branch: the platforms it is the source of, when named.
        named = os.environ.get('ORCHARD_KERNEL_PLATFORMS', '').split()
        if named and platform not in named:
            raise unittest.SkipTest(f'{tree} is not the source of {platform}')
        file = Path(tree) / path
        if not file.is_file():
            raise AssertionError(f'{path} is not in the kernel source {tree}')
        text = file.read_text()
    else:
        selected = lock(platform)
        repository = _clone(selected['url'], selected['commit'])
        if repository is None:
            raise unittest.SkipTest(f'The kernel source ({selected["url"]} {selected["commit"][:12]}) cannot be '
                                    'fetched; set ORCHARD_KERNEL_SOURCE to a checkout of it')
        shown = _show(repository, f'{selected["commit"]}:{path}')
        if shown.returncode:
            if 'does not exist' in shown.stderr or 'Not a valid object name' in shown.stderr:
                raise AssertionError(f'{path} is not in the kernel source at {selected["commit"][:12]}')
            raise unittest.SkipTest(f'{path} of the kernel source cannot be fetched: {shown.stderr.strip()}')
        text = shown.stdout
    _read[key] = text
    return text


def function(source, label):
    """The C function (or initialised object) of SOURCE that starts at LABEL, braces balanced."""
    start = source.index(label)
    opening = source.index('{', start)
    depth = 0
    for position in range(opening, len(source)):
        depth += source[position] == '{'
        depth -= source[position] == '}'
        if not depth:
            return source[start:position + 1]
    raise ValueError(f'Incomplete C function: {label}')
