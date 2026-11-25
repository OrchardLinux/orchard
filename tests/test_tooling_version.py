"""The orchard on PATH (tools/launcher) runs the newest nightly per command; pinned or offline as asked."""
import os
from pathlib import Path
import shutil
import subprocess
import tempfile
import unittest

ORCHARD = Path(__file__).resolve().parents[1] / 'tools/launcher'
NAME = 'nightly-20261103-0123456789ab'


@unittest.skipUnless(shutil.which('git') and shutil.which('curl'), 'needs git and curl')
class Fixture(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name).resolve()
        repo = self.root / 'repo'
        (repo / 'tools').mkdir(parents=True)
        (repo / 'pyproject.toml').write_text('[project]\nname = "orchard"\n')
        (repo / 'tools/launcher').write_text('the nightly launcher\n')
        git = ['git', '-C', str(repo), '-c', 'user.email=t@example.invalid', '-c', 'user.name=t']
        subprocess.run(['git', 'init', '-q', str(repo)], check=True)
        subprocess.run([*git, 'add', '.'], check=True)
        subprocess.run([*git, 'commit', '-qm', 'x', '--no-gpg-sign'], check=True)
        subprocess.run([*git, 'tag', NAME], check=True)
        (self.root / 'releases/nightly').mkdir(parents=True)
        (self.root / 'releases/nightly/latest.json').write_text(f'{{"schema": 1, "name": "{NAME}"}}\n')
        (self.root / 'work').mkdir()
        # uv prints the project it was asked to run instead of running it.
        self.bin = self.root / 'fake-bin'
        self.bin.mkdir()
        (self.bin / 'uv').write_text('#!/bin/sh\nprintf "%s\\n" "$4"\n')
        (self.bin / 'uv').chmod(0o755)
        self.launcher = self.root / 'home/.local/bin/orchard'
        self.launcher.parent.mkdir(parents=True)
        shutil.copy2(ORCHARD, self.launcher)

    def run_orchard(self, *arguments, releases='releases', **env):
        return subprocess.run([str(self.launcher), *arguments], cwd=self.root / 'work', capture_output=True, text=True,
                              env={**os.environ, 'PATH': f'{self.bin}:{os.environ["PATH"]}',
                                   'ORCHARD_CACHE_DIR': str(self.root / 'cache'), 'HOME': str(self.root / 'home'),
                                   'ORCHARD2': '', 'XDG_CONFIG_HOME': '',
                                   'ORCHARD_MAINLINE_URL': f'file://{self.root}/repo',
                                   'ORCHARD_RELEASES_URL': f'file://{self.root}/{releases}', **env})


@unittest.skipUnless(shutil.which('git') and shutil.which('curl'), 'needs git and curl')
class ToolingVersion(Fixture):
    def test_the_newest_nightly_then_offline_the_cached_one(self):
        result = self.run_orchard('build')
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(result.stdout.strip(), str(self.root / 'cache/tooling' / NAME))
        offline = self.run_orchard('build', releases='nowhere')
        self.assertEqual(offline.stdout.strip(), str(self.root / 'cache/tooling' / NAME))
        self.assertIn('no network', offline.stderr)

    def test_without_a_nightly_reachable_or_cached_it_says_so(self):
        result = self.run_orchard('build', releases='nowhere')
        self.assertNotEqual(result.returncode, 0)
        self.assertEqual(result.stdout, '')
        self.assertIn('no nightly reachable', result.stderr)
        self.assertFalse((self.root / 'cache/orchard-main').exists())

    def test_a_pinned_version_and_an_unknown_one(self):
        result = self.run_orchard('build', releases='nowhere', ORCHARD_VERSION=NAME)
        self.assertEqual(result.stdout.strip(), str(self.root / 'cache/tooling' / NAME))
        result = self.run_orchard('build', ORCHARD_VERSION='v9.9.9')
        self.assertNotEqual(result.returncode, 0)
        self.assertIn('no orchard version v9.9.9', result.stderr)

    def test_an_orchard_line_in_the_local_file_is_refused_not_ignored(self):
        (self.root / 'work/orchard.local.toml').write_text('orchard = "../orchard"\n')
        result = self.run_orchard('build')
        self.assertNotEqual(result.returncode, 0)
        self.assertIn('no longer read', result.stderr)

    def test_an_older_launcher_says_so_and_update_installs_the_nightlys(self):
        self.assertIn(f'a newer launcher is in {NAME}', self.run_orchard('build').stderr)
        update = self.run_orchard('update')
        self.assertEqual(update.returncode, 0, update.stderr)
        self.assertEqual(self.launcher.read_text(), 'the nightly launcher\n')

    def test_update_replaces_the_link_an_older_setup_made(self):
        self.launcher.unlink()
        self.launcher.symlink_to(ORCHARD)
        self.assertEqual(self.run_orchard('update').returncode, 0)
        self.assertFalse(self.launcher.is_symlink())
        self.assertEqual(self.launcher.read_text(), 'the nightly launcher\n')


@unittest.skipUnless(shutil.which('git') and shutil.which('curl'), 'needs git and curl')
class Orchard2Switch(Fixture):
    """build, inputs and run's build step go to orchard2 where it is switched on, and only there."""
    def setUp(self):
        super().setUp()
        self.log = self.root / 'log'
        self.log.touch()
        (self.bin / 'uv').write_text('#!/bin/sh\n[ "$1" = sync ] || { shift 5; printf "python %s\\n" "$*" >> "$LOG"; }\n')
        self.orchard2('printf "orchard2 %s data=%s python=%s\\n" "$*" "$ORCHARD_DATA" "$ORCHARD_PYTHON" >> "$LOG"')
        self.clone = self.root / 'cache/tooling' / NAME
        self.env = {'data': f'data={self.clone} python={self.clone}/.venv/bin/python'}

    def orchard2(self, script):
        (self.bin / 'orchard2').write_text('#!/bin/sh\n' + script + '\n')
        (self.bin / 'orchard2').chmod(0o755)

    def ran(self, *arguments, **env):
        self.log.write_text('')
        self.result = self.run_orchard(*arguments, LOG=str(self.log), **env)
        return self.log.read_text().splitlines()

    def config(self, text):
        path = self.root / 'home/.config/orchard/config.toml'
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text)

    def test_off_by_default_and_by_the_variable(self):
        self.assertEqual(self.ran('build', '.'), ['python build .'])
        self.config('orchard2 = true\n')
        self.assertEqual(self.ran('build', '.', ORCHARD2='0'), ['python build .'])
        self.config('orchard2 = false\n')
        self.assertEqual(self.ran('build', '.'), ['python build .'])
        self.assertEqual(self.ran('build', '.', ORCHARD2='yes'), [])
        self.assertIn('ORCHARD2 is 1', self.result.stderr)

    def test_the_variable_or_the_config_file_turns_it_on(self):
        expected = [f'orchard2 build . {self.env["data"]}']
        self.assertEqual(self.ran('build', '.', ORCHARD2='1'), expected)
        for text in ('orchard2 = true\n', '# x\n  orchard2=true # testing\n'):
            self.config(text)
            self.assertEqual(self.ran('build', '.'), expected)
        self.assertEqual(self.ran('devices'), [f'orchard2 devices {self.env["data"]}'])
        self.assertEqual(self.ran('devices', ORCHARD2='0'), ['python devices'])
        self.assertEqual(self.ran('doctor'), ['python doctor'])

    def test_arguments_are_passed_on_as_they_are(self):
        self.assertEqual(self.ran('build', 'a b', '--explain', ORCHARD2='1'),
                         [f'orchard2 build a b --explain {self.env["data"]}'])

    def test_run_builds_with_it_and_boots_here(self):
        self.assertEqual(self.ran('run', '-y', 'a', '--via', 'checkra1n', '--device', 'j71b', ORCHARD2='1'),
                         [f'orchard2 build a --device j71b {self.env["data"]}',
                          'python run --no-build -y a --via checkra1n --device j71b'])
        self.assertEqual(self.ran('run', '--no-build', 'a', ORCHARD2='1'), ['python run --no-build a'])

    def test_a_failed_build_is_not_booted_and_keeps_its_status(self):
        self.orchard2('exit 13')
        self.assertEqual(self.ran('run', 'a', ORCHARD2='1'), [])
        self.assertEqual(self.result.returncode, 13)
        self.ran('build', ORCHARD2='1')
        self.assertEqual(self.result.returncode, 13)

    def test_on_without_the_tool_fails(self):
        (self.bin / 'orchard2').unlink()
        path = os.pathsep.join(d for d in os.environ['PATH'].split(os.pathsep) if not os.access(f'{d}/orchard2', os.X_OK))
        self.assertEqual(self.ran('build', ORCHARD2='1', PATH=f'{self.bin}:{path}'), [])
        self.assertIn('not on PATH', self.result.stderr)

    def test_a_checkouts_own_orchard_follows_it(self):
        checkout = self.root / 'checkout'
        for name in ('bin/orchard', 'tools/launcher'):
            (checkout / name).parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(ORCHARD.parents[1] / name, checkout / name)
        self.launcher = checkout / 'bin/orchard'
        # Its own code and data, whatever orchard.local.toml names.
        (self.root / 'work/orchard.local.toml').write_text('orchard = "../orchard"\n')
        self.assertEqual(self.ran('build', '.', ORCHARD2='1'),
                         [f'orchard2 build . data={checkout} python={checkout}/.venv/bin/python'])
        self.assertEqual(self.ran('build', '.'), ['python build .'])
        self.assertFalse((self.root / 'cache').exists())



if __name__ == '__main__':
    unittest.main()
