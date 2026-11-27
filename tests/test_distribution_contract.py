"""The distribution contract (docs/distribution-contract.md): commands and arguments orchard keeps stable."""
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'src'))
from orchard import inputs  # noqa: E402

# Every command of the contract and the options a distribution may rely on.
COMMANDS = {
    'orchard.inputs': ['--output', '--platform', '--kernel-dir', '--m1n1', '--pongo', '--tools', '--loader'],
    'orchard.image.platform': ['--output', '--kernel-dir', '--busybox', '--receipt'],
    'orchard.image.platforms_index': ['--release', '--inputs', '--kernels', '--tools', '--commit', '--platform',
                                      '--architecture', '--loader'],
    'orchard.image.root_image': ['--output'],
    'orchard.image.recovery': ['--dtb', '--loader', '--logo', '--kernel', '--modules-dir', '--busybox', '--dropbear',
                               '--dropbearkey', '--wpa-supplicant', '--wpa-cli', '--iw', '--base', '--base-receipt',
                               '--root-receipt', '--output', '--verbose'],
    'orchard.manifests': ['--verify-local'],
    'orchard.image.publish': ['--distro', '--tag', '--release-dir', '--output'],
}


class DistributionContract(unittest.TestCase):
    def help(self, *argv):
        result = subprocess.run([sys.executable, *argv, '--help'], capture_output=True, text=True,
                                env={**os.environ, 'PYTHONPATH': str(ROOT / 'src')})
        self.assertEqual(result.returncode, 0, result.stderr)
        return result.stdout

    def test_every_command_keeps_its_options(self):
        for module, options in COMMANDS.items():
            with self.subTest(module=module):
                text = self.help('-m', module)
                for option in options:
                    self.assertIn(option, text)

    def test_the_by_path_scripts_need_no_orchard_package(self):
        # The producers, and the paths they had inside the package, which stay as shims.
        for script in ('producers/assemble.py', 'producers/cascadia.py', 'producers/initramfs.py', 'producers/buildenv.py',
                       'producers/platform_layer.py', 'producers/platforms_index.py', 'ci/keys.py', 'ci/reuse_kernel.py',
                       'boot/cascadia/build', 'src/orchard/image/assemble.py', 'src/orchard/image/cascadia.py',
                       'src/orchard/image/initramfs.py', 'src/orchard/buildenv.py'):
            with self.subTest(script=script):
                result = subprocess.run([sys.executable, '-I', str(ROOT / script), '--help'], capture_output=True,
                                        text=True, cwd=tempfile.gettempdir())
                self.assertEqual(result.returncode, 0, result.stderr)

    def test_no_script_run_by_path_hides_a_standard_module(self):
        # A script's directory comes first on its import path: producers/platform.py
        # once stood in for the standard `platform` that buildenv.py imports.
        for directory in ('producers', 'ci', 'kernel'):
            for script in (ROOT / directory).glob('*.py'):
                self.assertNotIn(script.stem, sys.stdlib_module_names, script)

    def components(self, root):
        (root / 'tools').mkdir()
        for path in (root / 'pongo.bin', root / 'm1n1.bin', *(root / 'tools' / name for name in inputs.TOOLS)):
            path.write_bytes(b'x')
        return {'m1n1': root / 'm1n1.bin', 'pongo': root / 'pongo.bin', 'tools': root / 'tools'}

    def test_inputs_names_every_path_a_distribution_takes(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            document = inputs.resolve(['apple-16k'], {'apple-16k': root}, shared=self.components(root))
        self.assertEqual(document['schema'], 1)
        self.assertEqual(set(document['platforms']), {'apple-16k'})
        self.assertIn('j71b', document['platforms']['apple-16k']['boards'])
        self.assertEqual(document['platforms']['apple-16k']['kernel'], str(root.resolve()))
        self.assertEqual(document['shared']['m1n1'], str((root / 'm1n1.bin').resolve()))
        self.assertEqual(set(document['shared']['tools']),
                         {'busybox', 'dropbear', 'dropbearkey', 'wpa_supplicant', 'wpa_cli', 'iw',
                          'orchard-write', 'orchard-install-usb', 'resize2fs', 'tune2fs'})
        self.assertIn('commit', document['orchard'])
        json.dumps(document)

    def test_a_component_that_is_not_given_names_the_orchard_command(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            shared = self.components(root)
            with self.assertRaisesRegex(ValueError, r'--kernel-dir apple-16k=DIRECTORY is not given.*orchard build'):
                inputs.resolve(['apple-16k'], {}, shared=shared)
            with self.assertRaisesRegex(ValueError, '--pongo is not given'):
                inputs.resolve(['apple-16k'], {'apple-16k': root}, shared={**shared, 'pongo': None})

    def test_the_command_writes_the_document(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            shared = self.components(root)
            output = root / 'out/inputs.json'
            argv = ['orchard.inputs', '--output', str(output), '--platform', 'apple-16k', '--kernel-dir', f'apple-16k={root}',
                    '--m1n1', str(shared['m1n1']), '--pongo', str(shared['pongo']), '--tools', str(shared['tools'])]
            with mock.patch.object(sys, 'argv', argv), mock.patch('builtins.print'):
                inputs.main()
            document = json.loads(output.read_text())
        self.assertEqual(document['produced'], [])
        self.assertEqual(set(document['platforms']), {'apple-16k'})

    def test_unknown_platforms_are_refused(self):
        with self.assertRaisesRegex(ValueError, 'Unknown or unselected platform apple-64k'):
            inputs.resolve(['apple-64k'], {})

    def test_the_distributions_tests_find_what_they_import(self):
        # omarchy-a and minimal-linux test against orchard main with these (their tests/).
        from orchard import distro, hardware
        from orchard.image import initramfs, platform, publish
        from orchard.manifests import validate
        for found in (distro.settings, hardware.ROOT, hardware.firmware_path, initramfs.record, platform.platforms,
                      platform.write, publish.publish, validate):
            self.assertIsNotNone(found)


if __name__ == '__main__':
    unittest.main()
