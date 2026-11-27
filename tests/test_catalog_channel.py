"""The device data as it is published, and where a checkout reads its own."""
import importlib.machinery
import importlib.util
import os
from pathlib import Path
import tempfile
import unittest
from unittest import mock

from orchard import hardware

ROOT = Path(__file__).resolve().parents[1]


def tool():
    loader = importlib.machinery.SourceFileLoader('catalog_generate', str(ROOT / 'tools/catalog/generate'))
    spec = importlib.util.spec_from_loader(loader.name, loader)
    module = importlib.util.module_from_spec(spec)
    loader.exec_module(module)
    return module


class Channel(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.home = Path(temporary.name)
        self.server = self.home / 'server'
        tool().publish(self.server, 'abc123')


    def test_publishing_the_same_data_gives_the_same_bytes(self):
        second = self.home / 'second'
        tool().publish(second, 'abc123')
        self.assertEqual((second / 'catalog.json').read_text(), (self.server / 'catalog.json').read_text())


class DataRoot(unittest.TestCase):
    def setUp(self):
        hardware.data_root.cache_clear()
        self.addCleanup(hardware.data_root.cache_clear)

    def test_a_checkout_uses_its_own_files(self):
        with mock.patch.dict(os.environ):
            os.environ.pop('ORCHARD_DATA', None)
            self.assertEqual(hardware.data_root(), hardware.ROOT)

    def test_orchard_data_wins(self):
        with tempfile.TemporaryDirectory() as directory, mock.patch.dict(os.environ, {'ORCHARD_DATA': directory}):
            (Path(directory) / 'devices').mkdir()
            self.assertEqual(hardware.data_root(), Path(directory))

    def test_orchard_data_must_hold_devices(self):
        with tempfile.TemporaryDirectory() as directory, mock.patch.dict(os.environ, {'ORCHARD_DATA': directory}):
            with self.assertRaisesRegex(ValueError, 'no devices/'):
                hardware.data_root()


if __name__ == '__main__':
    unittest.main()
