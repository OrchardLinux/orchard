"""Shared subsystem directories can carry patches for different kernel bases."""
import json
import os
from pathlib import Path
import runpy
import subprocess
import sys
import tempfile
import unittest

from orchard import published

ROOT = Path(__file__).resolve().parents[1]
EXPORT = ROOT / 'tools/kernel-tree/export.py'
CONVERT = ROOT / 'tools/kernel-tree/convert.py'
REGENERATE = ROOT / 'tools/kernel-tree/regenerate'


def write(root, path, text):
    target = root / path
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(text)
    return target


def git(root, *args, input=None):
    return subprocess.run(['git', '-C', str(root), '-c', 'user.name=Test Author',
                           '-c', 'user.email=test@example.invalid', *args], input=input,
                          check=True, capture_output=True).stdout.decode().strip()


def commit(root, message):
    git(root, 'add', '.')
    git(root, 'commit', '-qm', message, '--no-gpg-sign')
    return git(root, 'rev-parse', 'HEAD')


def layout(root):
    """Minimal hardware tree with separate ARMv7 and shared ARM64 branches."""
    root = root.resolve()
    write(root, 'kernel/series.py', (ROOT / 'kernel/series.py').read_text())
    write(root, 'kernel/upstream.lock', 'url=https://example.invalid/linux\ncommit=unused\n')
    write(root, 'kernel/sources.toml', '')
    write(root, 'kernel/Containerfile', 'FROM scratch\n')
    write(root, 'kernel/install-toolchain', '# toolchain\n')
    (root / 'kernel/patches/drivers/usb').mkdir(parents=True)
    for name, arch, branch in (('armv7', 'arm', 'armv7'),
                               ('arm64-4k', 'arm64', 'main'),
                               ('arm64-16k', 'arm64', 'main')):
        write(root, f'platforms/{name}.toml', f'''schema = 1
kind = "hardware.platform"
name = "{name}"
[kernel]
arch = "{arch}"
image = "{'zImage' if arch == 'arm' else 'Image.gz'}"
dtbs = "arch/{arch}/boot/dts/apple"
upstream = "kernel/upstream.lock"
series = "kernel/series/{name}"
config = "kernel/configs/{name}/config"
defconfig = "orchard_{name.replace('-', '')}_defconfig"
branch = "{branch}"
loader = "{'cascadia' if arch == 'arm' else 'm1n1'}"
''')
        write(root, f'kernel/configs/{name}/config', 'CONFIG_TEST=y\n')
        write(root, f'kernel/series/{name}',
              '' if arch == 'arm' else 'patches/drivers\n')
    return root


class ScopedPatches(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = layout(Path(self.temporary.name) / 'orchard')
        self.api = runpy.run_path(str(self.root / 'kernel/series.py'))

    def patch(self, path, platforms=None):
        patch = write(self.root, f'kernel/patches/{path}', '# synthetic patch\n')
        if platforms is not None:
            write(self.root, f'kernel/patches/{path}.toml',
                  'schema = 1\nplatforms = ' + json.dumps(platforms) + '\n')
        return patch

    def selected(self, name):
        return [str(path.relative_to(self.root / 'kernel/patches'))
                for path in self.api['patches'](name)]

    def test_normal_and_scoped_layers_preserve_layer_and_filename_order(self):
        self.patch('drivers/usb/0001-common.patch')
        self.patch('drivers/usb/0003-arm64.patch', ['arm64-4k', 'arm64-16k'])
        self.patch('drivers/usb/0002-armv7.patch', ['armv7'])
        self.patch('devices/a5/0001-board.patch', ['armv7'])
        write(self.root, 'kernel/series/armv7',
              'scoped patches/devices/a5 # board first\nscoped patches/drivers/usb\n')
        self.assertEqual(self.selected('armv7'),
                         ['devices/a5/0001-board.patch', 'drivers/usb/0002-armv7.patch'])
        self.assertEqual(self.selected('arm64-4k'),
                         ['drivers/usb/0001-common.patch', 'drivers/usb/0003-arm64.patch'])
        write(self.root, 'kernel/series/armv7', 'patches/drivers/usb\n')
        self.assertEqual(self.selected('armv7'),
                         ['drivers/usb/0001-common.patch', 'drivers/usb/0002-armv7.patch'])

    def test_armv7_patch_leaves_arm64_selection_and_published_keys_unchanged(self):
        self.patch('drivers/usb/0001-common.patch')
        selected = self.selected('arm64-4k')
        keys = {name: published.source_key(self.root, name)
                for name in ('arm64-4k', 'arm64-16k')}
        self.patch('drivers/usb/0002-armv7.patch', ['armv7'])
        write(self.root, 'kernel/series/armv7', 'scoped patches/drivers/usb\n')
        self.assertEqual(self.selected('arm64-4k'), selected)
        for name, key in keys.items():
            self.assertEqual(published.source_key(self.root, name), key)

    def test_selected_scope_is_in_published_inputs(self):
        patch = self.patch('drivers/usb/0001-armv7.patch', ['armv7'])
        write(self.root, 'kernel/series/armv7', 'scoped patches/drivers/usb\n')
        before = published.source_key(self.root, 'armv7')
        with Path(str(patch) + '.toml').open('a') as sidecar:
            sidecar.write('# provenance changed\n')
        self.assertNotEqual(published.source_key(self.root, 'armv7'), before)

    def test_invalid_scopes_fail_instead_of_silently_skipping(self):
        patch = self.patch('drivers/usb/0001.patch')
        invalid = ('schema = 2\nplatforms = ["armv7"]\n',
                   'schema = true\nplatforms = ["armv7"]\n',
                   'schema = 1\nplatforms = []\n',
                   'schema = 1\nplatforms = ["armv7", "armv7"]\n',
                   'schema = 1\nplatforms = ["unknown"]\n',
                   'schema = 1\nplatforms = ["../armv7"]\n',
                   'schema = 1\nplatforms = [3]\n',
                   'schema = 1\nplatforms = ["armv7"]\nextra = 1\n',
                   'schema = 1\nplatforms = "armv7"\n',
                   'invalid toml = [\n')
        for text in invalid:
            with self.subTest(text=text):
                Path(str(patch) + '.toml').write_text(text)
                with self.assertRaises(ValueError):
                    self.selected('arm64-4k')

    def test_orphan_scopes_and_symlinks_are_refused(self):
        sidecar = write(self.root, 'kernel/patches/drivers/usb/0001.patch.toml',
                        'schema = 1\nplatforms = ["armv7"]\n')
        with self.assertRaisesRegex(ValueError, 'Orphan'):
            self.selected('arm64-4k')
        sidecar.unlink()
        patch = self.patch('drivers/usb/0001.patch')
        outside = write(Path(self.temporary.name), 'outside.toml',
                        'schema = 1\nplatforms = ["armv7"]\n')
        sidecar.symlink_to(outside)
        with self.assertRaisesRegex(ValueError, 'leaves the kernel'):
            self.selected('arm64-4k')
        sidecar.unlink()
        patch.unlink()
        patch.symlink_to(outside)
        with self.assertRaisesRegex(ValueError, 'leaves the kernel'):
            self.selected('arm64-4k')

    def test_layer_containment_and_duplicate_selection(self):
        self.patch('drivers/usb/0001.patch')
        for text, message in (('patches/drivers\npatches/drivers\n', 'Duplicate patch layer'),
                              ('patches/drivers\nscoped patches/drivers\n', 'Duplicate patch layer'),
                              ('patches/drivers\npatches/drivers/usb\n', 'multiple layers'),
                              ('../outside\n', 'Invalid patch layer'),
                              ('/tmp\n', 'Invalid patch layer'),
                              ('scoped patches/drivers extra\n', 'Invalid patch layer')):
            with self.subTest(text=text):
                write(self.root, 'kernel/series/armv7', text)
                with self.assertRaisesRegex(ValueError, message):
                    self.selected('armv7')
        (self.root / 'kernel/patches/escape').symlink_to(Path(self.temporary.name))
        write(self.root, 'kernel/series/armv7', 'patches/escape\n')
        with self.assertRaisesRegex(ValueError, 'leaves the kernel'):
            self.selected('armv7')

    def test_producer_stops_on_invalid_selection_before_starting_build_tools(self):
        self.patch('drivers/usb/0001.patch', ['unknown'])
        write(self.root, 'kernel/series/armv7', 'scoped patches/drivers/usb\n')
        build = write(self.root, 'kernel/build', (ROOT / 'kernel/build').read_text())
        marker = self.root.parent / 'podman-called'
        fake = write(self.root.parent, 'bin/podman', f'#!/bin/sh\ntouch "{marker}"\nexit 1\n')
        fake.chmod(0o755)
        output, work = self.root.parent / 'output', self.root.parent / 'work'
        result = subprocess.run(['bash', str(build), 'armv7', '--output', str(output),
                                 '--work-dir', str(work)], capture_output=True, text=True,
                                env={**os.environ, 'ORCHARD_PYTHON': sys.executable,
                                     'PATH': str(fake.parent) + os.pathsep + os.environ['PATH']})
        self.assertNotEqual(result.returncode, 0)
        self.assertIn('Unknown platform', result.stderr)
        self.assertFalse(marker.exists())
        self.assertFalse(output.exists())
        self.assertFalse(work.exists())


class ScopedExport(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = layout(Path(self.temporary.name) / 'orchard')
        self.linux = Path(self.temporary.name) / 'linux'
        git(Path(self.temporary.name), 'init', '-q', '-b', 'main', str(self.linux))
        write(self.linux, 'drivers/usb/test.c', 'int old;\n')
        self.base = commit(self.linux, 'base')
        git(self.linux, 'switch', '-qc', 'work')
        write(self.linux, 'drivers/usb/test.c', 'int new;\n')
        self.change = commit(self.linux, 'usb: test: Update value')

    def export(self, *args):
        return subprocess.run([sys.executable, str(EXPORT), '--orchard', str(self.root),
                               '--linux', str(self.linux), *args, 'work'],
                              capture_output=True, text=True)

    def test_export_writes_scope_and_missing_layer_without_renumbering(self):
        old = write(self.root, 'kernel/patches/drivers/usb/0042-existing.patch', '# preserved\n')
        result = self.export('--platform', 'armv7')
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(old.read_text(), '# preserved\n')
        new = next((self.root / 'kernel/patches/drivers/usb').glob('0043-*.patch'))
        self.assertEqual(Path(str(new) + '.toml').read_text(),
                         'schema = 1\nplatforms = ["armv7"]\n')
        self.assertEqual((self.root / 'kernel/series/armv7').read_text(),
                         'scoped patches/drivers/usb\n')
        api = runpy.run_path(str(self.root / 'kernel/series.py'))
        self.assertEqual(api['patches']('armv7'), [new])
        self.assertEqual(api['patches']('arm64-4k'), [old])

    def test_existing_layer_is_reused_and_shared_platform_order_is_deterministic(self):
        result = self.export('--platform', 'arm64-4k', '--platform', 'arm64-16k',
                             '--platform', 'arm64-4k')
        self.assertEqual(result.returncode, 0, result.stderr)
        new = next((self.root / 'kernel/patches/drivers/usb').glob('*.patch'))
        self.assertEqual(Path(str(new) + '.toml').read_text(),
                         'schema = 1\nplatforms = ["arm64-16k", "arm64-4k"]\n')
        self.assertEqual((self.root / 'kernel/series/arm64-4k').read_text(), 'patches/drivers\n')
        self.assertEqual((self.root / 'kernel/series/arm64-16k').read_text(), 'patches/drivers\n')

    def test_shared_series_receives_each_new_layer_once(self):
        descriptor = self.root / 'platforms/arm64-4k.toml'
        descriptor.write_text(descriptor.read_text().replace('series/arm64-4k', 'series/arm64-16k'))
        write(self.root, 'kernel/series/arm64-16k', '')
        result = self.export('--platform', 'arm64-4k', '--platform', 'arm64-16k')
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual((self.root / 'kernel/series/arm64-16k').read_text(),
                         'scoped patches/drivers/usb\n')

    def test_planned_layer_overlaps_fail_before_writes(self):
        write(self.root, 'kernel/series/armv7', 'scoped patches/drivers/usb\n')
        existing = write(self.root, 'kernel/patches/drivers/usb/0001-existing.patch', '# preserved\n')
        sidecar = write(self.root, 'kernel/patches/drivers/usb/0001-existing.patch.toml',
                        'schema = 1\nplatforms = ["armv7"]\n')
        result = self.export('--platform', 'armv7', '--topic', f'{self.change}=drivers')
        self.assertNotEqual(result.returncode, 0)
        self.assertIn('Planned layers', result.stderr)
        self.assertEqual(list((self.root / 'kernel/patches').rglob('*.patch')), [existing])
        self.assertEqual((self.root / 'kernel/series/armv7').read_text(),
                         'scoped patches/drivers/usb\n')
        existing.unlink()
        sidecar.unlink()
        write(self.root, 'kernel/series/armv7', '')
        write(self.linux, 'drivers/usb/test.c', 'int newer;\n')
        second = commit(self.linux, 'usb: test: Update again')
        result = self.export('--platform', 'armv7', '--topic', f'{self.change}=drivers',
                             '--topic', f'{second}=drivers/usb')
        self.assertNotEqual(result.returncode, 0)
        self.assertIn('Planned layers', result.stderr)
        self.assertEqual(list((self.root / 'kernel/patches').rglob('*.patch')), [])

    def test_export_checks_orphans_in_a_new_layer_before_writing(self):
        write(self.root, 'kernel/patches/drivers/mtd/orphan.patch.toml', 'invalid = true\n')
        result = self.export('--platform', 'armv7', '--topic', f'{self.change}=drivers/mtd')
        self.assertNotEqual(result.returncode, 0)
        self.assertIn('Orphan patch scope', result.stderr)
        self.assertEqual(list((self.root / 'kernel/patches').rglob('*.patch')), [])
        self.assertEqual((self.root / 'kernel/series/armv7').read_text(), '')

    def test_dry_run_reserves_nothing_and_export_is_reproducible(self):
        before = {str(p.relative_to(self.root)): p.read_bytes()
                  for p in self.root.rglob('*') if p.is_file()}
        for _ in range(2):
            result = self.export('--platform', 'armv7', '--dry-run')
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertIn('drivers/usb/0001', result.stdout)
        self.assertEqual({str(p.relative_to(self.root)): p.read_bytes()
                          for p in self.root.rglob('*') if p.is_file()}, before)
        result = self.export('--platform', 'armv7')
        self.assertEqual(result.returncode, 0, result.stderr)
        first = next((self.root / 'kernel/patches/drivers/usb').glob('*.patch'))
        expected = first.read_bytes()
        first.unlink()
        Path(str(first) + '.toml').unlink()
        result = self.export('--platform', 'armv7')
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(first.read_bytes(), expected)
        self.assertEqual((self.root / 'kernel/series/armv7').read_text(),
                         'scoped patches/drivers/usb\n')

    def test_unknown_incomplete_and_escaping_scopes_fail_before_writes(self):
        for args, error in ((('--platform', 'unknown'), 'Unknown platform'),
                            (('--platform', 'arm64-4k'), 'shares kernel branch'),
                            (('--platform', 'armv7', '--topic', f'{self.change}=../escape'),
                             'Invalid patch topic')):
            with self.subTest(args=args):
                result = self.export(*args)
                self.assertNotEqual(result.returncode, 0)
                self.assertIn(error, result.stderr)
                self.assertEqual(list((self.root / 'kernel/patches').rglob('*.patch')), [])

    def test_a5_dts_topic_is_inferred(self):
        git(self.linux, 'reset', '--hard', self.base)
        write(self.linux, 'arch/arm/boot/dts/apple/s5l8942x-p105.dts', '/dts-v1/;\n')
        commit(self.linux, 'ARM: dts: apple: Add P105')
        result = self.export('--platform', 'armv7', '--dry-run')
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn('devices/s5l8942x/0001', result.stdout)

    def test_converter_uses_the_committed_selector_and_is_deterministic(self):
        # A foreign patch follows the exported ARMv7 patch in the same subsystem.
        result = self.export('--platform', 'armv7')
        self.assertEqual(result.returncode, 0, result.stderr)
        armv7 = next((self.root / 'kernel/patches/drivers/usb').glob('*.patch'))
        git(self.linux, 'switch', '-qc', 'foreign', self.base)
        write(self.linux, 'drivers/usb/test.c', 'int arm64;\n')
        commit(self.linux, 'usb: test: Update ARM64 value')
        foreign = write(self.root, 'kernel/patches/drivers/usb/0002-arm64.patch',
                        git(self.linux, 'format-patch', '--stdout', '--no-signature', '-1') + '\n')
        write(self.root, str(foreign.relative_to(self.root)) + '.toml',
              'schema = 1\nplatforms = ["arm64-4k", "arm64-16k"]\n')
        git(self.root.parent, 'init', '-q', '-b', 'main', str(self.root))
        revision = commit(self.root, 'Add scoped patches')
        # Dirty files must have no effect on regeneration from a commit.
        write(self.root, 'kernel/series/armv7', 'patches/drivers\n')
        for platform, patch, expected in (('armv7', armv7, 'int new;\n'),
                                           ('arm64-4k', foreign, 'int arm64;\n')):
            converted = []
            for _ in range(2):
                mapping = self.root.parent / f'{platform}.map'
                result = subprocess.run([sys.executable, str(CONVERT), '--orchard', str(self.root),
                                         '--commit', revision, '--layers', 'kernel',
                                         '--series', f'series/{platform}', '--platform', platform,
                                         '--target', str(self.linux), '--base', self.base,
                                         '--map', str(mapping)], capture_output=True, text=True)
                self.assertEqual(result.returncode, 0, result.stderr)
                tip = git(self.linux, 'rev-parse', 'refs/orchard/converted')
                converted.append(tip)
                self.assertEqual(git(self.linux, 'show', f'{tip}:drivers/usb/test.c') + '\n', expected)
                self.assertEqual(mapping.read_text().split('\t')[0], str(patch.relative_to(self.root)))
            self.assertEqual(converted[0], converted[1])

    def test_scope_removal_retains_its_conversion_provenance(self):
        result = self.export('--platform', 'armv7')
        self.assertEqual(result.returncode, 0, result.stderr)
        write(self.root, 'kernel/series/armv7', 'patches/drivers/usb\n')
        git(self.root.parent, 'init', '-q', '-b', 'main', str(self.root))
        commit(self.root, 'Add scoped patch')
        patch = next((self.root / 'kernel/patches/drivers/usb').glob('*.patch'))
        Path(str(patch) + '.toml').unlink()
        git(self.root, '-c', 'user.name=Scope Remover', 'commit', '-qam', 'Remove scope', '--no-gpg-sign')
        mapping = self.root.parent / 'mapping'
        result = subprocess.run([sys.executable, str(CONVERT), '--orchard', str(self.root),
                                 '--commit', 'HEAD', '--layers', 'kernel', '--series', 'series/armv7',
                                 '--target', str(self.linux), '--base', self.base, '--map', str(mapping)],
                                capture_output=True, text=True)
        self.assertEqual(result.returncode, 0, result.stderr)
        converted = mapping.read_text().split('\t')[1].strip()
        self.assertEqual(git(self.linux, 'log', '-1', '--format=%cn', converted), 'Scope Remover')

    def test_regeneration_is_repeatable_and_rejects_divergent_shared_branches(self):
        result = self.export('--platform', 'armv7')
        self.assertEqual(result.returncode, 0, result.stderr)
        write(self.root, 'kernel/upstream.lock', f'url=unused\ncommit={self.base}\n')
        write(self.root, 'devices/.keep', '')
        for script in ('convert.py', 'regenerate'):
            write(self.root, f'tools/kernel-tree/{script}', (ROOT / f'tools/kernel-tree/{script}').read_text())
        # The toolchain leaf is replaced; regeneration still archives sources,
        # converts patches, writes configs and creates the generated branches.
        fake = write(self.root.parent, 'bin/podman', f'''#!{sys.executable}
import pathlib
import sys
if sys.argv[1:3] == ['image', 'exists']:
    sys.exit(0)
sys.stdin.buffer.read()
out = pathlib.Path(sys.argv[sys.argv.index('-v') + 1].split(':')[0])
for line in (out / 'platforms').read_text().splitlines():
    name, arch, defconfig = line.split()
    (out / name / 'defconfig').write_bytes((out / name / 'config').read_bytes())
''')
        fake.chmod(0o755)
        m1n1 = self.root.parent / 'm1n1'
        git(self.root.parent, 'init', '-q', '-b', 'main', str(m1n1))
        write(m1n1, 'README', 'base\n')
        m1n1_base = commit(m1n1, 'm1n1 base')
        write(m1n1, 'README', 'patched\n')
        commit(m1n1, 'm1n1: Update README')
        write(self.root, 'boot/m1n1/patches/orchard/0001-readme.patch',
              git(m1n1, 'format-patch', '--stdout', '--no-signature', '-1') + '\n')
        git(m1n1, 'reset', '--hard', m1n1_base)
        write(self.root, 'boot/m1n1/upstream.lock', f'url=unused\ncommit={m1n1_base}\n')
        write(self.root, 'boot/m1n1/series', 'patches/orchard\n')
        write(self.root, 'boot/m1n1/sources.toml', '')
        git(self.root.parent, 'init', '-q', '-b', 'main', str(self.root))
        revision = commit(self.root, 'Add scoped regeneration fixture')
        env = {**os.environ, 'PATH': str(fake.parent) + os.pathsep + os.environ['PATH']}

        def regenerate(revision):
            return subprocess.run([sys.executable, str(self.root / 'tools/kernel-tree/regenerate'),
                                   '--linux', str(self.linux), '--m1n1', str(m1n1), revision],
                                  env=env, capture_output=True, text=True)

        tips = []
        for _ in range(2):
            result = regenerate(revision)
            self.assertEqual(result.returncode, 0, result.stderr)
            tips.append(tuple(git(self.linux, 'rev-parse', branch) for branch in ('armv7', 'main')) +
                        (git(m1n1, 'rev-parse', 'main'),))
            self.assertEqual(git(self.linux, 'show', 'armv7:drivers/usb/test.c'), 'int new;')
            self.assertEqual(git(self.linux, 'show', 'main:drivers/usb/test.c'), 'int old;')
            self.assertEqual(git(m1n1, 'show', 'main:README'), 'patched')
        self.assertEqual(tips[0], tips[1])
        new = next((self.root / 'kernel/patches/drivers/usb').glob('*.patch'))
        Path(str(new) + '.toml').write_text('schema = 1\nplatforms = ["armv7", "arm64-4k"]\n')
        bad = commit(self.root, 'Make shared branch scopes inconsistent')
        result = regenerate(bad)
        self.assertNotEqual(result.returncode, 0)
        self.assertIn('select different patches', result.stderr)
        self.assertEqual(tuple(git(self.linux, 'rev-parse', branch) for branch in ('armv7', 'main')) +
                         (git(m1n1, 'rev-parse', 'main'),), tips[0])


if __name__ == '__main__':
    unittest.main()
