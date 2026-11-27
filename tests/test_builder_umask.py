"""A builder's umask must not reach image modes or recipe keys (orchard#50)."""
import importlib.util
import os
from pathlib import Path
import stat
import tempfile
import unittest

from orchard.image import initramfs

_spec = importlib.util.spec_from_file_location('ci_keys', Path(__file__).resolve().parents[1] / 'ci/keys.py')
keys_module = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(keys_module)


def tree(base, umask):
    previous = os.umask(umask)
    try:
        for name in ('root', 'tmp', 'etc'):
            (base / name).mkdir()
        (base / 'etc/motd').write_text('hello\n')
        (base / 'init').write_text('#!/bin/sh\n')
        (base / 'init').chmod(0o777 & ~umask)
    finally:
        os.umask(previous)


class BuilderUmaskTests(unittest.TestCase):
    def test_initramfs_modes_do_not_follow_the_umask(self):
        results = []
        for umask in (0o022, 0o002):
            with tempfile.TemporaryDirectory() as directory:
                base = Path(directory)
                tree(base, umask)
                results.append({name: mode for name, (mode, _) in initramfs.entries([base]).items()})
        self.assertEqual(results[0], results[1])
        self.assertEqual(stat.S_IMODE(results[1]['root']), 0o755)
        self.assertEqual(stat.S_IMODE(results[1]['etc/motd']), 0o644)
        self.assertEqual(stat.S_IMODE(results[1]['init']), 0o755)

    def test_recipe_keys_do_not_follow_the_umask(self):
        keys = []
        for umask in (0o022, 0o002):
            with tempfile.TemporaryDirectory() as directory:
                base = Path(directory)
                tree(base, umask)
                keys.append(keys_module.recipe_entries(base, ['etc', 'init']))
        self.assertEqual(keys[0], keys[1])
        self.assertEqual(keys[0]['etc/motd'].split(':')[0], str(0o644))
        self.assertEqual(keys[0]['init'].split(':')[0], str(0o755))


if __name__ == '__main__':
    unittest.main()
