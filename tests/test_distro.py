"""A distribution checkout as a project: its orchard.toml or the older distro.toml."""
from pathlib import Path
import shutil
import tempfile
import unittest

from orchard import distro


class DistributionProject(unittest.TestCase):
    def setUp(self):
        self.root = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.root)
        (self.root / '.git').mkdir()
        (self.root / 'tools').mkdir()
        (self.root / 'tools/build.py').write_text('')
        (self.root / 'distro.toml').write_text('name = "fixture-linux"\nbuild = "tools/build.py"\n')

    def test_found_from_a_subdirectory_but_not_beyond_the_repository(self):
        (self.root / 'desktop').mkdir()
        self.assertEqual(distro.find(self.root / 'desktop'), self.root.resolve())
        inner = self.root / 'nested'
        (inner / '.git').mkdir(parents=True)
        self.assertIsNone(distro.find(inner))

    def test_settings_refuse_paths_outside_and_unknown_keys(self):
        for text in ('name = "fixture-linux"\nbuild = "../build"\n',
                     'name = "fixture-linux"\nbuild = "/bin/sh"\n',
                     'name = "Fixture Linux"\nbuild = "tools/build.py"\n',
                     'name = "fixture-linux"\nbuild = "tools/build.py"\ndevice = "j71b"\n',
                     'name = "fixture-linux"\nbuild = "tools/missing"\n'):
            with self.subTest(text=text):
                (self.root / 'distro.toml').write_text(text)
                with self.assertRaises(ValueError):
                    distro.settings(self.root)

    def test_a_shell_entry_must_be_executable(self):
        (self.root / 'tools/build').write_text('#!/bin/sh\n')
        (self.root / 'distro.toml').write_text('name = "fixture-linux"\nbuild = "tools/build"\n')
        with self.assertRaisesRegex(ValueError, 'not executable'):
            distro.settings(self.root)
        (self.root / 'tools/build').chmod(0o755)
        self.assertEqual(distro.settings(self.root)['build'], self.root / 'tools/build')


if __name__ == '__main__':
    unittest.main()
