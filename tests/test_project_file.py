"""orchard.toml: one project file for RAM payloads and distributions; the older files still read."""
import contextlib
import io
from pathlib import Path
import shutil
import sys
import tempfile
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'src'))
sys.path.insert(0, str(Path(__file__).resolve().parent))
from orchard import distro, project  # noqa: E402


class ProjectFile(unittest.TestCase):
    def setUp(self):
        self.root = Path(tempfile.mkdtemp()).resolve()
        self.addCleanup(shutil.rmtree, self.root)
        (self.root / '.git').mkdir()
        (self.root / 'tools').mkdir()
        (self.root / 'tools/build.py').write_text('')

    def release(self, extra=''):
        (self.root / 'orchard.toml').write_text(f'name = "fixture-linux"\n[release]\nbuild = "tools/build.py"\n{extra}')

    def test_a_release_project_is_a_distribution(self):
        self.release('selects-platform = true\n')
        (self.root / 'desktop').mkdir()
        self.assertEqual(distro.find(self.root / 'desktop'), self.root)
        settings = distro.settings(self.root)
        self.assertEqual((settings['name'], settings['build'], settings['selects_platform']),
                         ('fixture-linux', self.root / 'tools/build.py', True))

    def test_selects_platform_is_a_boolean_setting(self):
        self.release()
        self.assertFalse(distro.settings(self.root)['selects_platform'])
        self.release('selects-platform = "yes"\n')
        with self.assertRaisesRegex(ValueError, 'true or false'):
            distro.settings(self.root)

    def test_a_payload_project_is_no_distribution(self):
        (self.root / 'orchard.toml').write_text('name = "check"\n[payload]\nmodules = true\n')
        self.assertIsNone(distro.find(self.root))

    def test_payload_and_release_together_or_unknown_settings_are_refused(self):
        for text in ('name = "x"\n[payload]\n[release]\nbuild = "tools/build.py"\n', 'name = "x"\nboard = "j71b"\n',
                     '[payload]\nmodules = true\n'):
            with self.subTest(text=text):
                (self.root / 'orchard.toml').write_text(text)
                with self.assertRaises(ValueError):
                    project.read(self.root)

    def test_the_older_files_are_read_with_a_notice(self):
        (self.root / 'distro.toml').write_text('name = "fixture-linux"\nbuild = "tools/build.py"\n')
        notice = io.StringIO()
        with contextlib.redirect_stderr(notice):
            self.assertEqual(distro.settings(self.root)['name'], 'fixture-linux')
        self.assertIn('rename it to orchard.toml', notice.getvalue())


if __name__ == '__main__':
    unittest.main()
