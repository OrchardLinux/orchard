"""File-backed Git acquisition tests. No network or device access."""
import os
from pathlib import Path
import subprocess
import shutil
import tempfile
import unittest
from unittest.mock import patch

from orchard import sources

import hardware_layout


class SourceCacheTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name).resolve()
        self.upstream = self.root / 'upstream'
        self.upstream.mkdir()
        self.cache = self.root / 'cache'
        self.git('init', '-q')
        (self.upstream / 'driver.c').write_text('original\n')
        (self.upstream / 'untouched.c').write_text('unchanged\n')
        self.git('add', '.')
        self.git('-c', 'user.name=Test', '-c', 'user.email=test@example.invalid',
                 '-c', 'commit.gpgsign=false', 'commit', '-qm', 'fixture')
        self.commit = self.git('rev-parse', 'HEAD').decode()
        self.url = self.upstream.as_uri()

    def git(self, *args):
        return sources.git(self.upstream, *args)

    def test_two_checkouts_share_objects_without_contacting_upstream_again(self):
        first, second = self.root / 'first', self.root / 'second'
        sources.checkout(self.url, self.commit, first, cache=self.cache)
        # Make the original URL unavailable: the second checkout must be offline.
        self.upstream.rename(self.root / 'upstream-offline')
        sources.checkout(self.url, self.commit, second, cache=self.cache)
        self.assertEqual((first / 'driver.c').read_text(), 'original\n')
        self.assertEqual((second / 'driver.c').read_text(), 'original\n')
        self.assertEqual((first / '.git/objects/info/alternates').read_text(),
                         (second / '.git/objects/info/alternates').read_text())
        (first / 'driver.c').write_text('local edit\n')
        self.assertEqual((second / 'driver.c').read_text(), 'original\n')
        with self.assertRaises(ValueError):
            sources.checkout(self.url, self.commit, first, cache=self.cache)
        self.assertEqual((first / 'driver.c').read_text(), 'local edit\n')

    def test_declared_tree_is_the_commits_and_the_user_index_is_left_alone(self):
        tree = sources.declared_tree(self.url, self.commit, cache=self.cache)
        local = self.root / 'local'
        sources.checkout(self.url, self.commit, local, cache=self.cache)
        index_before = (local / '.git/index').read_bytes()
        self.assertEqual(tree, sources.working_tree(local))
        self.assertEqual(index_before, (local / '.git/index').read_bytes())
        (local / 'new.c').write_text('untracked\n')
        self.assertNotEqual(tree, sources.working_tree(local))

    def test_local_checkout_can_seed_cache_without_remote_access(self):
        missing_url = (self.root / 'missing-origin').as_uri()
        with sources.repository(missing_url, self.commit, cache=self.cache, donor=self.upstream) as bare:
            self.assertEqual(sources.git(bare, 'rev-parse', self.commit).decode(), self.commit)

    def test_the_source_is_the_locks_commit_and_edits_are_never_reset(self):
        from orchard import kernel
        hardware = self.root / 'hardware'
        hardware_layout.write(hardware)
        (hardware / 'kernel/upstream.lock').write_text(f'url={self.url}\ncommit={self.commit}\n')
        source, output = kernel.prepare_source(hardware, self.cache, 'j71b')
        self.assertEqual((source / 'driver.c').read_text(), 'original\n')
        self.assertTrue(output.is_dir())
        # The marks: the commit, as kernel/build writes it, and its tree.
        self.assertEqual((source.parent / 'applied-patches').read_text(), self.commit + '\n')
        self.assertEqual((source.parent / 'prepared-tree').read_text().strip(), sources.working_tree(source))
        (source / 'driver.c').write_text('local experiment\n')
        kernel.prepare_source(hardware, self.cache, 'j71b')
        self.assertEqual((source / 'driver.c').read_text(), 'local experiment\n')
        self.assertEqual(kernel.source_tree(hardware, self.cache, 'j71b'), sources.working_tree(source))

    def test_a_tree_marked_for_another_state_is_taken_clean_and_refused_with_edits(self):
        from orchard import build, kernel
        hardware = self.root / 'hardware'
        hardware_layout.write(hardware)
        (hardware / 'kernel/upstream.lock').write_text(f'url={self.url}\ncommit={self.commit}\n')
        source, _ = kernel.prepare_source(hardware, self.cache, 'j71b')
        marker = source.parent / 'applied-patches'
        # An older tree's empty mark counts as this commit's.
        marker.write_bytes(b'')
        (source / 'driver.c').write_text('user edit\n')
        kernel.prepare_source(hardware, self.cache, 'j71b')
        self.assertEqual((source / 'driver.c').read_text(), 'user edit\n')
        # Any other mark: clean is taken over, edits are never discarded.
        marker.write_text('something else\n')
        with self.assertRaisesRegex(ValueError, 'refusing to discard edits'):
            build.kernel_base_key(hardware, 'j71b', self.cache)
        with self.assertRaises(ValueError):
            kernel.prepare_source(hardware, self.cache, 'j71b')
        self.assertEqual((source / 'driver.c').read_text(), 'user edit\n')
        sources.git(source, 'checkout', '--', 'driver.c')
        kernel.prepare_source(hardware, self.cache, 'j71b')
        self.assertEqual(marker.read_text(), self.commit + '\n')

    def test_cache_location_override(self):
        with patch.dict(os.environ, {'ORCHARD_CACHE_DIR': str(self.cache)}):
            self.assertEqual(sources.cache_root(), self.cache)


if __name__ == '__main__':
    unittest.main()
