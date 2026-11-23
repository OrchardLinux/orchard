"""Builds keep Orchard's directories within a size limit, deleting only what Orchard can make again."""
import io
import os
from pathlib import Path
import subprocess
import tempfile
import unittest
from unittest.mock import patch

from orchard import components


class Budget(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.cache = Path(temporary.name) / 'cache'
        self.store = Path(temporary.name) / 'store'
        self.clock = 1_000_000

    def file(self, path, size):
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(b'x' * size)
        self.clock += 100
        os.utime(path, (self.clock, self.clock))
        for parent in path.parents:
            if parent in (self.cache, self.store, self.cache.parent):
                break
            os.utime(parent, (self.clock, self.clock))
        return path

    def fit(self, limit):
        out = io.StringIO()
        with patch('sys.stdout', new=out):
            used = components.fit(limit, self.cache, self.store)
        return used, out.getvalue()

    def kernel_tree(self, name, size, edited=False):
        tree = self.cache / 'kernel-work' / name
        source = tree / 'source'
        source.mkdir(parents=True)
        subprocess.run(['git', 'init', '-q', str(source)], check=True)
        self.file(source / 'Makefile', size)
        subprocess.run(['git', '-C', str(source), 'add', '.'], check=True)
        subprocess.run(['git', '-C', str(source), '-c', 'user.name=t', '-c', 'user.email=t@t', 'commit', '-qm', 'base'],
                       check=True)
        from orchard.sources import working_tree
        (tree / 'prepared-tree').write_text(working_tree(source) + '\n')
        if edited:
            (source / 'Makefile').write_text('edited')
        self.clock += 100
        os.utime(tree / 'prepared-tree', (self.clock, self.clock))
        return tree

    def test_under_the_limit_nothing_is_removed(self):
        asset = self.file(self.store / 'artifacts/sha256/aa', 1000)
        used, out = self.fit(10_000)
        self.assertTrue(asset.exists())
        self.assertEqual(out, '')
        self.assertGreaterEqual(used, 1000)

    def test_least_recently_used_goes_first_and_newest_of_each_kind_stays(self):
        old = self.file(self.cache / 'components-v2/kernel-j71b/old/Image', 4000)
        new = self.file(self.cache / 'components-v2/kernel-j71b/new/Image', 4000)
        asset = self.file(self.store / 'artifacts/sha256/bb', 4000)
        only = self.file(self.cache / 'work/m1n1/one/out', 4000)
        _, out = self.fit(13000)
        self.assertFalse(old.exists())
        self.assertTrue(new.exists() and only.exists() and asset.exists())
        self.assertIn('kernel-j71b/old', out)

    def test_kernel_trees_with_local_edits_are_kept(self):
        edited = self.kernel_tree('apple-4k-a', 3000, edited=True)
        clean = self.kernel_tree('apple-16k-b', 3000)
        newest = self.kernel_tree('apple-armv7-c', 3000)
        _, out = self.fit(1000)
        self.assertTrue(edited.exists() and newest.exists())
        self.assertFalse(clean.exists())
        self.assertIn('local edits are kept: apple-4k-a', out)

    def test_a_tree_in_use_by_a_build_is_skipped(self):
        busy = self.kernel_tree('apple-4k-a', 3000)
        self.kernel_tree('apple-16k-b', 3000)
        with components._try_lock(busy / '.lock') as held:
            self.assertTrue(held)
            self.fit(1000)
        self.assertTrue(busy.exists())

    def test_assets_take_their_download_leftovers_along(self):
        asset = self.file(self.store / 'artifacts/sha256/cc', 4000)
        (self.store / 'artifacts/sha256/cc.asset.lock').touch()
        (self.store / 'artifacts/sha256/cc.download.parts').mkdir()
        self.file(self.store / 'artifacts/sha256/dd', 10)
        self.fit(1000)
        self.assertEqual(sorted(p.name for p in asset.parent.iterdir()), ['dd'])

    def test_older_firmware_downloads_go_even_under_the_limit_and_outputs_stay(self):
        archive = self.file(self.store / 'firmware/j71b/archive/full.ipsw', 10)
        output = self.file(self.store / 'firmware/j71b/outputs/abc', 10)
        _, out = self.fit(10**9)
        self.assertFalse(archive.parent.exists())
        self.assertTrue(output.exists())
        self.assertIn('older Orchard', out)

    def test_nothing_outside_the_two_directories_is_touched(self):
        outside = self.file(self.cache.parent / 'out/rootfs.img', 50_000)
        self.file(self.store / 'artifacts/sha256/ee', 10)
        self.fit(1)
        self.assertTrue(outside.exists())

    def test_a_cache_given_alone_leaves_the_default_store_alone(self):
        with patch.object(components, '_store', side_effect=AssertionError('store touched')):
            out = io.StringIO()
            with patch('sys.stdout', new=out):
                components.fit(1, self.cache)
                components.prune(5, self.cache)

    def test_zero_turns_the_limit_off(self):
        asset = self.file(self.store / 'artifacts/sha256/ff', 4000)
        with patch.dict(os.environ, {'ORCHARD_CACHE_BUDGET_GB': '0'}):
            self.fit(None)
        self.assertTrue(asset.exists())


if __name__ == '__main__':
    unittest.main()
