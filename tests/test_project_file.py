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
from orchard import build, distro, project  # noqa: E402
from test_distro import ENTRY  # noqa: E402


class ProjectFile(unittest.TestCase):
    def setUp(self):
        self.root = Path(tempfile.mkdtemp()).resolve()
        self.addCleanup(shutil.rmtree, self.root)
        (self.root / '.git').mkdir()
        (self.root / 'tools').mkdir()
        (self.root / 'tools/build.py').write_text(ENTRY + "\nimport os\n"
                                                  "(args.output / 'tree').write_text(os.environ.get('ORCHARD_KERNEL_TREE', ''))\n"
                                                  "(args.output / 'packages').write_text(os.environ.get('ORCHARD_PACKAGES', ''))\n")

    def release(self, extra=''):
        (self.root / 'orchard.toml').write_text(f'name = "fixture-linux"\n[release]\nbuild = "tools/build.py"\n{extra}')

    def test_a_release_project_is_a_distribution(self):
        self.release('selects-platform = true\n')
        (self.root / 'desktop').mkdir()
        self.assertEqual(distro.find(self.root / 'desktop'), self.root)
        settings = distro.settings(self.root)
        self.assertEqual((settings['name'], settings['build'], settings['selects_platform']),
                         ('fixture-linux', self.root / 'tools/build.py', True))

    def test_a_payload_project_is_no_distribution_and_has_its_settings(self):
        (self.root / 'orchard.toml').write_text('name = "check"\n[payload]\nmodules = true\n')
        self.assertIsNone(distro.find(self.root))
        self.assertEqual(build.payload_settings(self.root), {'transport': True, 'modules': True, 'firmware': False})

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

    def test_a_build_records_its_plain_command_and_passes_a_kernel_tree(self):
        self.release()
        tree = self.root / 'linux'
        with contextlib.redirect_stdout(io.StringIO()):
            index = distro.build(self.root, version='dev-1', kernel_tree=tree)
        self.assertEqual((index.parent / 'tree').read_text(), str(tree))
        commands = (self.root / 'out/releases/dev-1.commands.sh').read_text()
        self.assertIn(f'ORCHARD_KERNEL_TREE={tree}', commands)
        self.assertIn('ORCHARD_CHECKOUT=', commands)
        self.assertIn('tools/build.py --version dev-1 --output', commands)

    def test_a_build_passes_a_packages_checkout(self):
        self.release()
        packages = self.root / 'packages-checkout'
        with contextlib.redirect_stdout(io.StringIO()):
            index = distro.build(self.root, version='dev-1', packages=packages)
        self.assertEqual((index.parent / 'packages').read_text(), str(packages))
        self.assertIn(f'ORCHARD_PACKAGES={packages}', (self.root / 'out/releases/dev-1.commands.sh').read_text())
        with contextlib.redirect_stdout(io.StringIO()):
            index = distro.build(self.root, version='dev-2')
        self.assertEqual((index.parent / 'packages').read_text(), '')

    def test_the_packages_argument_wins_over_the_local_file_and_must_be_a_packages_checkout(self):
        checkout = self.root.parent / f'{self.root.name}-packages'
        self.addCleanup(shutil.rmtree, checkout, True)
        self.assertIsNone(build.resolve_packages(self.root))
        (self.root / 'orchard.local.toml').write_text(f'packages = "../{checkout.name}"\n')
        with self.assertRaisesRegex(ValueError, 'not a checkout of the Orchard package repository'):
            build.resolve_packages(self.root)
        (checkout / 'tools').mkdir(parents=True)
        (checkout / 'packages').mkdir()
        (checkout / 'tools/pkgrepo').write_text('')
        self.assertEqual(build.resolve_packages(self.root), checkout)
        with self.assertRaisesRegex(ValueError, '--packages'):
            build.resolve_packages(self.root, self.root / 'elsewhere')

    def test_a_packages_checkout_beside_the_distribution_is_used_unless_turned_off(self):
        sibling = self.root.parent / 'packages'
        if sibling.exists():
            self.skipTest('a packages directory already sits beside the test checkout')
        self.addCleanup(shutil.rmtree, sibling, True)
        (sibling / 'tools').mkdir(parents=True)
        (sibling / 'packages').mkdir()
        (sibling / 'tools/pkgrepo').write_text('')
        self.assertEqual(build.resolve_packages(self.root), sibling.resolve())
        (self.root / 'orchard.local.toml').write_text('packages = false\n')
        self.assertIsNone(build.resolve_packages(self.root))


if __name__ == '__main__':
    unittest.main()
