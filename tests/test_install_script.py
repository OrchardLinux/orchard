"""A release's install.sh installs the orchard command and runs `orchard install` for that release. No network or device."""
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest

PRODUCER = Path(__file__).resolve().parents[1] / 'producers/install_script.py'
INDEX = 'https://github.com/OrchardLinux/example/releases/download/v9.9.9/index.toml'


def write(output, index=INDEX, url='https://example.org/install.sh', name='Example Linux'):
    return subprocess.run([sys.executable, str(PRODUCER), '--name', name, '--index', index, '--url', url,
                           '--output', str(output)], capture_output=True, text=True)


class InstallScript(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name).resolve()
        self.bin = self.root / 'bin'
        self.bin.mkdir()
        self.log = self.root / 'log'
        # curl prints a script that "installs" the command; the command records how it was called.
        (self.root / 'command').write_text('#!/bin/sh\nprintf "orchard %s\\n" "$*" >> "$LOG"\n')
        (self.root / 'tools-install.sh').write_text('cp "$HOME/command" "$ORCHARD_BIN/orchard"; chmod 755 "$ORCHARD_BIN/orchard"\n')
        (self.bin / 'curl').write_text('#!/bin/sh\nprintf "curl %s\\n" "$*" >> "$LOG"\ncat "$HOME/tools-install.sh"\n')
        (self.bin / 'curl').chmod(0o755)
        self.script = self.root / 'install.sh'
        self.assertEqual(write(self.script).returncode, 0)

    def run_script(self, *arguments, script=None):
        return subprocess.run(['sh', str(script or self.script), *arguments], capture_output=True, text=True,
                              stdin=subprocess.DEVNULL, start_new_session=True,
                              env={'PATH': f'{self.bin}:/usr/bin:/bin', 'HOME': str(self.root), 'LOG': str(self.log),
                                   'ORCHARD_BIN': str(self.bin)})

    def test_installs_the_command_then_installs_this_release_with_the_arguments(self):
        result = self.run_script('--ibss')
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(self.log.read_text().splitlines(),
                         ['curl -fsSL https://raw.githubusercontent.com/OrchardLinux/tools/main/install.sh',
                          f'orchard install {INDEX} --ibss'])

    def test_it_names_the_distribution_and_its_own_address(self):
        text = self.script.read_text()
        self.assertIn('# Install Example Linux on an iPad or iPhone', text)
        self.assertIn('curl -fsSL https://example.org/install.sh | sh', text)
        self.assertNotIn('@', text.replace('"$@"', ''))

    def test_next_to_a_local_build_it_installs_that_build(self):
        local = self.root / 'release'
        local.mkdir()
        (local / 'index.toml').write_text('')
        self.assertEqual(write(local / 'install.sh', 'index.toml', 'install.sh').returncode, 0)
        self.assertEqual(self.run_script(script=local / 'install.sh').returncode, 0)
        self.assertEqual(self.log.read_text().splitlines()[-1], f'orchard install {local}/index.toml')

    def test_only_plain_values_are_written_into_the_script(self):
        self.assertNotEqual(write(self.root / 'x.sh', index="x'; rm -rf ~; '").returncode, 0)
        self.assertNotEqual(write(self.root / 'x.sh', name='$(reboot)').returncode, 0)
        self.assertFalse((self.root / 'x.sh').exists())


if __name__ == '__main__':
    unittest.main()
