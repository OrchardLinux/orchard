"""The orchard on PATH (tools/launcher) runs the newest nightly per command; pinned or offline as asked."""
import os
from pathlib import Path
import shutil
import subprocess
import sys
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

    def fake_python(self, lock='yield 3'):
        # The version's Python, with a lock of its own.
        package = self.root / 'fake-package/orchard'
        package.mkdir(parents=True, exist_ok=True)
        (package / '__init__.py').touch()
        (package / 'state.py').write_text(
            'import contextlib, os\n@contextlib.contextmanager\ndef operation_lock():\n'
            f"    os.dup2(os.open({str(self.root / 'lock')!r}, os.O_CREAT | os.O_RDWR), 3)\n    {lock}\n")
        self.ran('doctor', ORCHARD2='0')  # clones the version
        python = self.clone / '.venv/bin/python'
        python.parent.mkdir(parents=True, exist_ok=True)
        python.write_text(f'#!/bin/sh\nPYTHONPATH={package.parent} exec {sys.executable} "$@"\n')
        python.chmod(0o755)

    def test_install_and_launch_run_with_the_lock_and_their_entry(self):
        self.orchard2('printf "orchard2 %s entry=%s data=%s\\n" "$*" "${ORCHARD_BOOT:-}" "$ORCHARD_DATA" >> "$LOG"; [ -e /dev/fd/3 ] && echo locked >> "$LOG"; exit 4')
        shutil.rmtree(self.clone, ignore_errors=True)
        self.fake_python()
        data = f'data={self.clone}'
        for arguments, expected in (
                (['install', 'omarchy-a', '--yes'], f'orchard2 install omarchy-a --yes entry= {data}'),
                (['install', '--via', 'checkra1n', 'a b'], f'orchard2 install a b entry=checkra1n {data}'),
                (['install', '--manifest', 'r.toml'], f'orchard2 install --index r.toml entry= {data}'),
                (['install', '--release=r.toml'], f'orchard2 install --index=r.toml entry= {data}'),
                (['launch', '--via=palera1n', '--list'], f'orchard2 launch --list entry=palera1n {data}')):
            self.assertEqual(self.ran(*arguments, ORCHARD2='1'), [expected, 'locked'], self.result.stderr)
            self.assertEqual(self.result.returncode, 4)
        self.assertEqual(self.ran('launch', ORCHARD2='1', ORCHARD_VIA='checkra1n')[0], f'orchard2 launch entry=checkra1n {data}')
        # orchard2 knows the routes: any name is passed on for it to refuse.
        self.assertEqual(self.ran('launch', '--via', 'ibss', ORCHARD2='1')[0], f'orchard2 launch entry=ibss {data}')
        self.assertEqual(self.ran('launch', '--via', ORCHARD2='1'), [])
        self.assertEqual(self.result.returncode, 2)
        self.assertEqual(self.ran('launch', '--list'), ['python launch --list'])

    def test_a_busy_device_or_an_older_version_stops_before_orchard2(self):
        shutil.rmtree(self.clone, ignore_errors=True)
        for lock, message in (("raise ValueError('Another Orchard device operation is active')", 'operation is active'),
                              ('yield', 'older than the switch')):
            self.fake_python(lock)
            self.assertEqual(self.ran('install', 'x', ORCHARD2='1'), [])
            self.assertEqual(self.result.returncode, 1)
            self.assertIn(message, self.result.stderr)

    def test_distros_are_refused_not_ignored(self):
        self.assertEqual(self.ran('distros', 'a', 'https://example.org', ORCHARD2='1'), [])
        self.assertEqual(self.result.returncode, 2)
        self.assertEqual(self.ran('distros'), ['python distros'])

    def test_cache_is_orchard2s_with_its_own_spelling(self):
        self.orchard2('printf "orchard2 %s\\n" "$*" >> "$LOG"; exit 8')
        for arguments in ([], ['ls'], ['rm', 'kernel/0b'], ['prune', '--keep', '2'], ['--json']):
            self.assertEqual(self.ran('cache', *arguments, ORCHARD2='1'), [' '.join(['orchard2 cache', *arguments])])
            self.assertEqual(self.result.returncode, 8)
        self.assertNotIn("Python's own cache", self.result.stderr)  # there is none yet
        (self.root / 'cache').mkdir()
        self.ran('cache', ORCHARD2='1')
        self.assertIn("Python's own cache uses", self.result.stderr)
        self.assertEqual(self.result.stdout, '')
        for arguments, hint in ((['--prune'], 'cache prune'), (['--keep', '2'], 'cache prune'), (['m1n1'], 'cache rm m1n1')):
            self.assertEqual(self.ran('cache', *arguments, ORCHARD2='1'), [])
            self.assertEqual(self.result.returncode, 2)
            self.assertIn(hint, self.result.stderr)
        self.assertEqual(self.ran('cache', '--prune'), ['python cache --prune'])

    def test_run_is_orchard2s(self):
        self.orchard2('printf "orchard2 %s boot=%s\\n" "$*" "${ORCHARD_BOOT:-}" >> "$LOG"; [ -e /dev/fd/3 ] && echo locked >> "$LOG"')
        shutil.rmtree(self.clone, ignore_errors=True)
        self.fake_python()
        # It builds by itself: one command, with the route and without -y.
        self.assertEqual(self.ran('run', '-y', 'a', '--via', 'checkra1n', '--device', 'j71b', ORCHARD2='1'),
                         ['orchard2 run a --device j71b boot=checkra1n', 'locked'])
        self.assertEqual(self.ran('run', ORCHARD2='1'), ['orchard2 run boot=', 'locked'])
        self.assertEqual(self.ran('run', '--no-build', 'a', '--cache', 'c', ORCHARD2='1', ORCHARD_VIA='ibss'),
                         ['orchard2 run --no-build a --cache c boot=ibss', 'locked'])

    def test_what_orchard2_does_not_boot_is_booted_here(self):
        self.orchard2('printf "orchard2 %s\\n" "$*" >> "$LOG"; [ "$1" = build ] || exit 10')
        shutil.rmtree(self.clone, ignore_errors=True)
        self.fake_python()
        # No boot route for the device (exit status 10): Python boots what orchard2 built.
        self.assertEqual(self.ran('run', '-y', 'a', ORCHARD2='1'), ['orchard2 run a', 'python run --no-build -y a'])
        self.assertIn('booting what it built here', self.result.stderr)
        # Another failure is the run's result.
        self.orchard2('printf "orchard2 %s\\n" "$*" >> "$LOG"; exit 4')
        self.assertEqual(self.ran('run', 'a', ORCHARD2='1'), ['orchard2 run a'])
        self.assertEqual(self.result.returncode, 4)
        # A distribution checkout's release is built by orchard2 and booted here.
        self.orchard2('printf "orchard2 %s\\n" "$*" >> "$LOG"')
        (self.root / 'work/d').mkdir()
        (self.root / 'work/d/orchard.toml').write_text('[release]\n')
        self.assertEqual(self.ran('run', 'd', '--via', 'checkra1n', ORCHARD2='1'),
                         ['orchard2 build d', 'python run --no-build d --via checkra1n'])

    def test_a_failed_build_is_not_booted_and_keeps_its_status(self):
        shutil.rmtree(self.clone, ignore_errors=True)
        self.fake_python()
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
