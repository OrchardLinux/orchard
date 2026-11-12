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
    'orchard.inputs': ['--output', '--platform', '--kernel-dir', '--m1n1', '--pongo', '--tools', '--kernel-tree',
                       '--loader'],
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
        release = self.help('-m', 'orchard.installer', 'release')
        for option in ('--installer-url', '--pongo', '--m1n1'):
            self.assertIn(option, release)

    def test_the_by_path_scripts_need_no_orchard_package(self):
        for script in ('src/orchard/image/assemble.py', 'src/orchard/image/cascadia.py', 'src/orchard/buildenv.py'):
            with self.subTest(script=script):
                result = subprocess.run([sys.executable, '-I', str(ROOT / script), '--help'], capture_output=True,
                                        text=True, cwd=tempfile.gettempdir())
                self.assertEqual(result.returncode, 0, result.stderr)

    def test_inputs_names_every_path_a_distribution_takes(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            tools = root / 'tools'
            shared = {'pongo': root / 'pongo.bin', 'loader': root / 'm1n1.bin', 'tools': tools}
            tools.mkdir()
            for path in (root / 'pongo.bin', root / 'm1n1.bin', *(tools / name for name in inputs.TOOLS)):
                path.write_bytes(b'x')
            with mock.patch('orchard.build.prepare_shared', return_value=shared), \
                    mock.patch('orchard.image.platform.kernel', side_effect=lambda name, **kw: root / name):
                document = inputs.resolve(['apple-16k'], {})
        self.assertEqual(document['schema'], 1)
        self.assertEqual(set(document['platforms']), {'apple-16k'})
        self.assertIn('j71b', document['platforms']['apple-16k']['boards'])
        self.assertEqual(document['shared']['m1n1'], str((root / 'm1n1.bin').resolve()))
        self.assertEqual(set(document['shared']['tools']),
                         {'busybox', 'dropbear', 'dropbearkey', 'wpa_supplicant', 'wpa_cli', 'iw',
                          'orchard-write', 'resize2fs', 'tune2fs'})
        self.assertIn('commit', document['orchard'])
        json.dumps(document)

    def test_given_components_are_used_without_building_anything(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            (root / 'tools').mkdir()
            for path in (root / 'pongo.bin', root / 'm1n1.bin', *(root / 'tools' / name for name in inputs.TOOLS)):
                path.write_bytes(b'x')
            with mock.patch('orchard.build.prepare_shared') as shared, mock.patch('orchard.image.platform.kernel') as kernel:
                document = inputs.resolve(['apple-16k'], {'apple-16k': root}, shared={
                    'm1n1': root / 'm1n1.bin', 'pongo': root / 'pongo.bin', 'tools': root / 'tools'})
            shared.assert_not_called()
            kernel.assert_not_called()
        self.assertEqual(document['platforms']['apple-16k']['kernel'], str(root.resolve()))

    def test_inputs_records_how_it_got_each_component(self):
        # A distribution build's own record only names its build script: the inputs document says the rest.
        from orchard import explain
        def resolve(*_, **__):
            explain.note('m1n1: downloaded from orchard\'s nightly-1 (m1n1.bin, sha256 0)')
            explain.command(['make', 'm1n1'])
            return {'schema': 1, 'platforms': {}}
        with tempfile.TemporaryDirectory() as temporary:
            output = Path(temporary) / 'inputs.json'
            with mock.patch.object(inputs, 'resolve', resolve), \
                    mock.patch.object(sys, 'argv', ['orchard.inputs', '--output', str(output)]), \
                    mock.patch('builtins.print'):
                inputs.main()
            document = json.loads(output.read_text())
        self.assertEqual(document['produced'], [
            {'note': 'm1n1: downloaded from orchard\'s nightly-1 (m1n1.bin, sha256 0)'}, {'run': ['make', 'm1n1']}])

    def test_unknown_platforms_are_refused(self):
        with self.assertRaisesRegex(ValueError, 'Unknown or unselected platform apple-64k'):
            inputs.resolve(['apple-64k'], {})


if __name__ == '__main__':
    unittest.main()
