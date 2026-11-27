"""The retired launcher (tools/launcher), and a checkout's bin/orchard, which runs the orchard command on the checkout."""
import os
from pathlib import Path
import subprocess
import tempfile
import unittest

ROOT = Path(__file__).resolve().parents[1]
RETIRED = 'the orchard command is a program of its own now'


class Launchers(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.bin = Path(temporary.name).resolve() / 'bin with spaces'
        self.bin.mkdir()

    def run_checkout(self, *arguments, path=None):
        return subprocess.run(['sh', str(ROOT / 'bin/orchard'), *arguments], capture_output=True, text=True,
                              env={'PATH': f'{path or self.bin}:/usr/bin:/bin', 'HOME': str(self.bin)})

    def program(self, text):
        (self.bin / 'orchard').write_text(text)
        (self.bin / 'orchard').chmod(0o755)

    def test_the_retired_launcher_names_the_install_line_and_fails(self):
        result = subprocess.run(['sh', str(ROOT / 'tools/launcher'), 'build'], capture_output=True, text=True)
        self.assertEqual(result.returncode, 1)
        self.assertIn(RETIRED, result.stderr)
        self.assertIn('OrchardLinux/tools/main/install.sh | sh', result.stderr)

    def test_a_checkout_runs_the_program_on_path_with_its_own_data(self):
        self.program('#!/bin/sh\nprintf "%s|%s\\n" "$ORCHARD_DATA" "$*"\n')
        result = self.run_checkout('build', 'a b')
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(result.stdout.strip(), f'{ROOT}|build a b')

    def test_a_retired_launcher_on_path_is_not_the_program(self):
        self.program((ROOT / 'tools/launcher').read_text())
        result = self.run_checkout('build')
        self.assertEqual(result.returncode, 1)
        self.assertIn('is not installed', result.stderr)
        self.assertIn('install.sh | sh', result.stderr)

    def test_the_programs_exit_status_is_the_checkouts(self):
        self.program('#!/bin/sh\nexit 7\n')
        self.assertEqual(self.run_checkout('devices').returncode, 7)


if __name__ == '__main__':
    unittest.main()
