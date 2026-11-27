"""Shared upstream Git objects, with independent editable source checkouts.

Only acquisition and Git objects are shared. Never resets an existing checkout.
Cached revisions are retained by refs; do not prune this cache while checkouts
borrow its objects. Managed by Orchard CLI, independently of distro or hardware repositories.
"""
from contextlib import contextmanager
import fcntl
import hashlib
import os
from pathlib import Path
import shutil
import subprocess
import tempfile


def cache_root():
    from .cache import shared_cache_root
    return shared_cache_root()


def git(repository, *arguments, env=None):
    return subprocess.check_output(['git', '-C', str(repository), *map(str, arguments)],
                                   env=env, stderr=subprocess.PIPE).strip()


@contextmanager
def repository(url, commit, *, cache=None, donor=None):
    """Yield a bare object store containing commit, fetching only when absent."""
    root = Path(cache) if cache is not None else cache_root()
    root = root / 'git'
    root.mkdir(parents=True, exist_ok=True)
    name = hashlib.sha256(url.encode()).hexdigest()
    bare = root / (name + '.git')
    with (root / (name + '.lock')).open('a') as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        if not bare.exists():
            subprocess.run(['git', 'init', '--bare', '-q', str(bare)], check=True)
        try:
            git(bare, 'cat-file', '-e', commit + '^{commit}')
        except subprocess.CalledProcessError:
            origin = url
            if donor is not None and (Path(donor) / '.git').exists():
                try:
                    git(donor, 'cat-file', '-e', commit + '^{commit}')
                    origin = Path(donor).resolve().as_uri()
                except subprocess.CalledProcessError:
                    pass
            print(f'Caching upstream Git revision {commit} from {origin}', flush=True)
            subprocess.run(['git', '-C', str(bare), 'fetch', '--progress', '--no-tags',
                            '--depth', '1', origin, commit], check=True)
        git(bare, 'update-ref', 'refs/heads/orchard/' + commit, commit)
        yield bare


def checkout(url, commit, source, *, cache=None):
    source = Path(source).resolve()
    if source.exists():
        raise ValueError(f'Source directory exists; will not reset it: {source}')
    source.parent.mkdir(parents=True, exist_ok=True)
    with repository(url, commit, cache=cache) as bare:
        with tempfile.TemporaryDirectory(prefix='source-', dir=source.parent) as temporary:
            stage = Path(temporary) / 'source'
            subprocess.run(['git', 'init', '-q', str(stage)], check=True)
            # clone --shared does not reliably borrow objects from shallow repos.
            # Explicit alternates retain one shared upstream object store.
            info = stage / '.git/objects/info'
            info.mkdir(parents=True, exist_ok=True)
            (info / 'alternates').write_text(str(bare / 'objects') + '\n')
            if (bare / 'shallow').exists():
                shutil.copyfile(bare / 'shallow', stage / '.git/shallow')
            git(stage, 'remote', 'add', 'origin', url)
            git(stage, 'checkout', '-q', '--detach', commit)
            stage.rename(source)


def declared_tree(url, commit, *, cache=None, donor=None):
    """The tree of COMMIT, without checking out Linux."""
    with repository(url, commit, cache=cache, donor=donor) as bare:
        return git(bare, 'rev-parse', commit + '^{tree}').decode()


def working_tree(source, *, force=True):
    """Snapshot tracked/untracked source edits without changing the user's index.

    force also takes files the tree's .gitignore matches: orchard's own kernel
    trees build elsewhere, so nothing in them is rightly ignored. A developer's
    kernel tree (force=False) keeps its ignore rules, for in-tree build output.
    """
    source = Path(source).resolve()
    with tempfile.TemporaryDirectory(prefix='orchard-index-') as temporary:
        index = Path(temporary) / 'index'
        original = Path(os.fsdecode(git(source, 'rev-parse', '--git-path', 'index')))
        if not original.is_absolute():
            original = source / original
        env = {**os.environ, 'GIT_INDEX_FILE': str(index)}
        if original.exists():
            # copy2 keeps the index's mtime: git re-hashes a file whose mtime is
            # not older than the index ("racy git"), so an edit in the same second
            # as the last index write, with the same size, is still seen.
            shutil.copy2(original, index)
        else:
            git(source, 'read-tree', 'HEAD', env=env)
        # --force: on a case-insensitive filesystem git ignores case too, so an
        # ignore pattern like the kernel's *.s would drop a new headsmp.S from
        # the snapshot. The kernel builds into a separate directory, so the
        # source holds no files that are rightly ignored.
        git(source, 'add', '--all', *(['--force'] if force else []), '--', '.', env=env)
        return git(source, 'write-tree', env=env).decode()


