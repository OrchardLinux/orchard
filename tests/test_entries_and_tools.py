"""Entries reach a boot chain through a tool; tools are fetched on demand and pinned."""
import hashlib
import os
from pathlib import Path
import tempfile
import tomllib
import unittest
from unittest import mock

from orchard import tools
from orchard.apple import boot, entries

ROOT = Path(__file__).resolve().parents[1]


class Selection(unittest.TestCase):
    def setUp(self):
        patcher = mock.patch.dict(os.environ)
        patcher.start()
        self.addCleanup(patcher.stop)
        os.environ.pop('ORCHARD_VIA', None)

    def test_the_default_is_palera1n(self):
        self.assertEqual(entries.select({}), 'palera1n')
        self.assertEqual(entries.select(None), 'palera1n')

    def test_a_board_can_prefer_one(self):
        self.assertEqual(entries.select({'boot': {'entries': ['checkra1n']}}), 'checkra1n')

    def test_an_entry_this_code_lacks_is_skipped(self):
        self.assertEqual(entries.select({'boot': {'entries': ['from-the-future', 'checkra1n']}}), 'checkra1n')
        self.assertEqual(entries.select({'boot': {'entries': ['from-the-future']}}), 'palera1n')

    def test_via_wins_on_any_board(self):
        os.environ['ORCHARD_VIA'] = 'checkra1n'
        self.assertEqual(entries.select({'boot': {'entries': ['palera1n']}}), 'checkra1n')

    def test_an_unknown_via_is_refused(self):
        os.environ['ORCHARD_VIA'] = 'nonsense'
        with self.assertRaisesRegex(ValueError, 'Unknown entry'):
            entries.select({})


class Data(unittest.TestCase):
    METHODS = tomllib.loads((ROOT / 'methods.toml').read_text())

    def test_every_entry_reaches_a_method_that_lists_it(self):
        for name, entry in entries.ENTRIES.items():
            with self.subTest(entry=name):
                self.assertIn(name, self.METHODS[entry.method]['entries'])

    def test_every_entry_has_a_tool(self):
        for entry in entries.ENTRIES.values():
            self.assertIn(entry.tool, tools.TOOLS)

    def test_a_board_prefers_only_entries_of_its_methods(self):
        for path in sorted((ROOT / 'devices').glob('*/device.toml')):
            described = tomllib.loads(path.read_text()).get('boot', {})
            allowed = {name for method in described.get('methods', []) for name in self.METHODS[method]['entries']}
            with self.subTest(board=path.parent.name):
                self.assertEqual(set(described.get('entries', [])) - allowed, set())


class StartPongo(unittest.TestCase):
    DEVICE = {'location': 'selected', 'ecid': 1}

    def run_entry(self, entry):
        with tempfile.NamedTemporaryFile() as log, \
                mock.patch.object(boot, 'ensure_tool', return_value='/tool') as ensure, \
                mock.patch.object(boot.usb, 'selected'), \
                mock.patch.object(boot.replug, 'port_control', return_value=None) as control, \
                mock.patch.object(boot, 'wait_mode'), \
                mock.patch.object(boot, 'run_process_group') as execute:
            boot.start_pongo(self.DEVICE, 'pongo.bin', log, lambda text: None, chip=0x8010, board=0x18,
                             product='', entry=entry)
        return ensure.call_args.args[0], execute.call_args.args[0], control.called

    def test_palera1n_is_unchanged(self):
        self.assertEqual(self.run_entry('palera1n'), ('palera1n', ['/tool', '-p', '-k', 'pongo.bin'], True))

    def test_checkra1n_runs_on_a_chip_it_has_not_run_on(self):
        self.assertEqual(self.run_entry('checkra1n'),
                         ('checkra1n', ['/tool', '-E', '-p', '-v', '-n', '-k', 'pongo.bin'], False))


class EnsureTool(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name).resolve()
        self.data = b'tool bytes'
        self.sha256 = hashlib.sha256(self.data).hexdigest()
        host = {('Linux', 'arm64'): ('https://example.invalid/tool', len(self.data), self.sha256)}
        for target in (mock.patch.dict(os.environ, {'ORCHARD_CACHE_DIR': str(self.root / 'cache'),
                                                    'PATH': str(self.root / 'bin')}),
                       mock.patch.dict(tools.TOOLS, {'palera1n': {'version': 'v1', 'hosts': host}}),
                       mock.patch.object(tools.platform, 'system', return_value='Linux'),
                       mock.patch.object(tools.platform, 'machine', return_value='aarch64'),
                       mock.patch.object(tools.Path, 'home', return_value=self.root)):
            target.start()
            self.addCleanup(target.stop)
        os.environ.pop('ORCHARD_PALERA1N', None)
        self.source = self.root / 'download'
        self.source.write_bytes(self.data)

    def executable(self, path, data):
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(data)
        path.chmod(0o755)
        return path

    def test_it_downloads_once_into_the_cache(self):
        with mock.patch('orchard.releases.materialize', return_value=self.source) as fetch:
            first = tools.ensure_tool('palera1n')
            second = tools.ensure_tool('palera1n')
        fetch.assert_called_once()
        self.assertEqual(first, second)
        self.assertEqual(first, str(self.root / 'cache/tools/palera1n/v1/palera1n'))
        self.assertTrue(os.access(first, os.X_OK))
        self.assertEqual(fetch.call_args.args[0]['sha256'], self.sha256)

    def test_a_matching_binary_on_path_is_reused(self):
        path = self.executable(self.root / 'bin/palera1n', self.data)
        self.assertEqual(tools.find('palera1n'), str(path))

    def test_a_matching_binary_in_local_bin_counts_off_path(self):
        path = self.executable(self.root / '.local/bin/palera1n', self.data)
        self.assertEqual(tools.find('palera1n'), str(path))

    def test_another_build_on_path_is_ignored(self):
        self.executable(self.root / 'bin/palera1n', b'another version')
        self.assertIsNone(tools.find('palera1n'))

    def test_an_override_is_used_unchecked(self):
        with mock.patch.dict(os.environ, {'ORCHARD_PALERA1N': '/my/palera1n'}):
            self.assertEqual(tools.ensure_tool('palera1n'), '/my/palera1n')

    def test_a_download_that_does_not_match_is_refused(self):
        self.source.write_bytes(b'something else')
        with mock.patch('orchard.releases.materialize', return_value=self.source):
            with self.assertRaisesRegex(ValueError, 'pinned SHA-256'):
                tools.ensure_tool('palera1n')
        self.assertIsNone(tools.find('palera1n'))

    def test_a_host_without_a_build_says_so(self):
        with mock.patch.object(tools.platform, 'machine', return_value='riscv64'):
            with self.assertRaisesRegex(ValueError, 'no build'):
                tools.ensure_tool('palera1n')



class ChainInputs(unittest.TestCase):
    def test_each_entry_declares_its_tool(self):
        for name in entries.ENTRIES:
            self.assertEqual(entries.inputs(name), [entries.ToolInput(entries.ENTRIES[name].tool)])

    def test_a_tool_input_asks_the_tool_cache(self):
        item = entries.ToolInput('palera1n')
        with mock.patch('orchard.tools.find', return_value=None):
            self.assertFalse(item.cached())
        with mock.patch('orchard.tools.find', return_value='/somewhere/palera1n'):
            self.assertTrue(item.cached())
        with mock.patch('orchard.tools.ensure_tool', return_value='/x') as ensure:
            self.assertEqual(item.fetch('progress', 'clock'), '/x')
        ensure.assert_called_once_with('palera1n', progress='progress', clock='clock')


if __name__ == '__main__':
    unittest.main()
