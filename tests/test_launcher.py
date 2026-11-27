"""bin/orchard runs on its own checkout; fake uv and a stand-in for the orchard command."""
from pathlib import Path
import shutil
import subprocess
import tempfile
import unittest

LAUNCHER = Path(__file__).resolve().parents[1] / 'bin/orchard'


class LauncherTests(unittest.TestCase):
    """bin/orchard belongs to the checkout it is in, from wherever it is called."""
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name).resolve()
        self.bin = self.root / 'fake-bin'
        self.bin.mkdir()
        self.log = self.root / 'ran.log'
        (self.bin / 'uv').write_text('#!/bin/sh\nprintf "%s\\n" "$UV_PROJECT_ENVIRONMENT" "$@" > "$LAUNCH_LOG"\n')
        (self.bin / 'orchard').write_text('#!/bin/sh\nprintf "%s\\n" "$ORCHARD_DATA" "$@" > "$LAUNCH_LOG"\n')
        for name in ('uv', 'orchard'):
            (self.bin / name).chmod(0o755)
        self.checkout = self.root / 'orchard'
        (self.checkout / 'bin').mkdir(parents=True)
        shutil.copy2(LAUNCHER, self.checkout / 'bin/orchard')

    def launch(self, launcher, cwd, *arguments, **env):
        return subprocess.run(['sh', str(launcher), *arguments], cwd=cwd, capture_output=True, text=True,
                              env={'PATH': f'{self.bin}:/usr/bin:/bin', 'HOME': str(self.root),
                                   'LAUNCH_LOG': str(self.log), 'UV_PROJECT_ENVIRONMENT': '/elsewhere', **env})

    def test_runs_on_its_own_checkout_from_anywhere_and_through_a_link(self):
        elsewhere = self.root / 'omarchy-a'
        elsewhere.mkdir()
        link = self.root / 'link-to-orchard'
        link.symlink_to(self.checkout / 'bin/orchard')
        for launcher in (self.checkout / 'bin/orchard', link):
            with self.subTest(launcher=launcher.name):
                result = self.launch(launcher, elsewhere, 'build', 'with spaces')
                self.assertEqual(result.returncode, 0, result.stderr)
                self.assertEqual(self.log.read_text().splitlines(), [str(self.checkout), 'build', 'with spaces'])

    def test_named_data_is_kept(self):
        self.launch(self.checkout / 'bin/orchard', self.root, 'devices', ORCHARD_DATA='/other')
        self.assertEqual(self.log.read_text().splitlines(), ['/other', 'devices'])

    def test_python_runs_in_the_checkout_environment(self):
        result = self.launch(self.checkout / 'bin/orchard', self.root, 'python', '-m', 'unittest')
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(self.log.read_text().splitlines(),
                         [str(self.checkout / '.venv'), 'run', '--quiet', '--project', str(self.checkout),
                          'python', '-m', 'unittest'])


if __name__ == '__main__':
    unittest.main()
