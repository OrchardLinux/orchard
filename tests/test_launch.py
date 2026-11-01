"""Launch reads boot entries from the device through m1n1 and boots one; it never writes."""
import io
import os
from pathlib import Path
import tempfile
import unittest
import uuid
from unittest.mock import ANY, Mock, patch

from boot_image import ROOT_ARM64, XBOOTLDR, fat32, gpt_disk
from orchard import launch
from boot_reader import Disk, read_boot
from orchard.transports.m1n1 import NO_BOOT_MANAGER, M1n1Error
from orchard.progress import Progress

ROOT = uuid.UUID('4d6c1a2e-0000-4000-8000-000000000001')
KERNEL = b'ARM64 Image ' * 4000


def entry(title, version, root=ROOT, devicetree='t8010-j71b.dtb'):
    return (f'title {title}\nversion {version}\nsort-key {title.lower()}\n'
            f'linux /t/{version}/linux\ninitrd /t/{version}/initrd\ninitrd /t/{version}/extra\n'
            f'devicetree /t/{version}/{devicetree}\noptions root=PARTUUID={root} rw\n')


def image(entries, kernel=KERNEL):
    files = {}
    for name, text in entries.items():
        files['/loader/entries/' + name] = text.encode()
        version = text.split('version ')[1].split('\n')[0]
        files[f'/t/{version}/linux'] = kernel
        files[f'/t/{version}/initrd'] = b'initrd-one' * 900
        files[f'/t/{version}/extra'] = b'initrd-two' * 7
        files[f'/t/{version}/t8010-j71b.dtb'] = b'\xd0\x0d\xfe\xed' + b'D' * 500
    return gpt_disk([(XBOOTLDR, uuid.uuid4(), 'boot', fat32(files)),
                     (ROOT_ARM64, ROOT, 'root', b'\0' * 4096)])


class FakeM1n1:
    """An m1n1 with the boot manager ops (orchard 0007-0009) over a disk image.

    The listing is modelled with the test-side reader (tests/boot_reader.py),
    in m1n1's reply shape: the BLS keys plus `root`. The host never reads storage.
    """

    def __init__(self, disk, fail=None, boot_manager=True):
        self.disk, self.fail, self.boot_manager = disk, fail, boot_manager
        self.calls, self.started = [], None

    def __enter__(self):
        return self

    def __exit__(self, *_):
        return False

    def storage_init(self):
        self.calls.append('init')

    def storage_shutdown(self):
        self.calls.append('shutdown')

    def boot_entries(self, devicetree=None):
        self.calls.append('entries')
        if not self.boot_manager:
            raise M1n1Error(NO_BOOT_MANAGER)
        _, _, entries = read_boot(Disk(lambda lba: self.disk[lba * 4096:(lba + 1) * 4096]), devicetree)
        return [(e.id, {**e.keys, **({'root': [e.root]} if e.root else {})}, e.problems) for e in entries]

    def boot_start(self, entry_id, devicetree=None):
        self.calls.append('start')
        if self.fail:
            raise M1n1Error(self.fail)
        self.started = (entry_id, devicetree)


LOADER = {'pongo': Path('/loaders/pongo.bin'), 'm1n1': Path('/loaders/j71b/m1n1.bin')}


class LaunchFlow(unittest.TestCase):
    def run_launch(self, disk, answers=('',), entry_id=None, mode='dfu', proxy=None):
        adapter = Mock(PROFILE='j71b', NAME='iPad', DEVICETREE='t8010-j71b.dtb')
        adapter.start_loader.return_value = '/dev/cu.usbmodemTEST1'
        proxy = proxy or FakeM1n1(disk)
        replies = iter(answers)
        with tempfile.TemporaryDirectory() as home, patch.dict(os.environ, ORCHARD_HOME=home), \
                patch.object(launch, 'choose', return_value=(adapter, {'mode': mode, 'ecid': 1})), \
                patch.object(launch, 'loader_files', return_value=LOADER) as loader, \
                patch.object(launch, 'M1n1Proxy', return_value=proxy):
            report = launch.launch(entry=entry_id, input_fn=lambda _: next(replies),
                                   progress=Progress(io.StringIO()))
        loader.assert_called_once_with('j71b', ANY)
        adapter.start_loader.assert_called_once_with({'mode': mode, 'ecid': 1}, LOADER, ANY, quiet=True)
        return report, proxy

    def test_m1n1_lists_and_starts_the_chosen_entry(self):
        disk = image({'omarchy-2.conf': entry('Omarchy', '6.18.2'), 'omarchy-1.conf': entry('Omarchy', '6.17.9')})
        report, proxy = self.run_launch(disk, answers=('2',))
        self.assertTrue(report['started'])
        self.assertEqual(report['entry'], {'id': 'omarchy-1', 'title': 'Omarchy', 'version': '6.17.9',
                                           'root': str(ROOT)})
        self.assertEqual(proxy.started, ('omarchy-1', 't8010-j71b.dtb'))
        # m1n1 shuts storage down itself when it starts the entry.
        self.assertEqual(proxy.calls, ['init', 'entries', 'start'])

    def test_default_is_the_first_bootable_entry_in_bls_order(self):
        disk = image({'a.conf': entry('Omarchy', '6.17.9'), 'b.conf': entry('Omarchy', '6.18.2')})
        report, _ = self.run_launch(disk)
        self.assertEqual(report['entry']['version'], '6.18.2')

    def test_the_only_system_starts_without_a_question(self):
        disk = image({'a.conf': entry('Omarchy', '6.18.2')})
        report, _ = self.run_launch(disk, answers=())
        self.assertEqual(report['entry']['title'], 'Omarchy')

    def test_entry_option_skips_the_question(self):
        disk = image({'omarchy.conf': entry('Omarchy', '6.18.2'), 'minimal.conf': entry('Minimal', '6.18.2')})
        report, _ = self.run_launch(disk, answers=(), entry_id='minimal')
        self.assertEqual(report['entry']['title'], 'Minimal')

    def test_unbootable_entries_are_listed_and_refused(self):
        disk = image({'other.conf': entry('Other', '6.18.2', devicetree='t8010-j171.dtb')})
        with self.assertRaisesRegex(ValueError, 'No bootable entry'):
            self.run_launch(disk)

    def test_storage_is_released_when_nothing_boots(self):
        disk = image({})
        proxy = FakeM1n1(disk)
        with self.assertRaisesRegex(ValueError, 'No boot entries'):
            self.run_launch(disk, proxy=proxy)
        self.assertEqual(proxy.calls, ['init', 'entries', 'shutdown'])

    def test_m1n1_start_failure_is_reported(self):
        disk = image({'a.conf': entry('Omarchy', '6.18.2')})
        proxy = FakeM1n1(disk, fail='m1n1 could not start the entry: out of memory')
        with self.assertRaisesRegex(M1n1Error, 'out of memory'):
            self.run_launch(disk, answers=(), proxy=proxy)
        self.assertEqual(proxy.calls, ['init', 'entries', 'start'])

    def test_an_m1n1_without_the_boot_manager_is_refused(self):
        # No host-side fallback: the host never reads the disk itself.
        disk = image({'a.conf': entry('Omarchy', '6.18.2')})
        proxy = FakeM1n1(disk, boot_manager=False)
        with self.assertRaisesRegex(M1n1Error, 'no boot manager'):
            self.run_launch(disk, proxy=proxy)
        self.assertEqual(proxy.calls, ['init', 'entries', 'shutdown'])

    def test_running_systems_are_not_rebooted(self):
        with self.assertRaisesRegex(ValueError, 'DFU mode'):
            self.run_launch(image({}), mode='linux')

    def test_command_accepts_launch_without_arguments(self):
        from orchard import command
        with patch('sys.argv', ['orchard', 'launch']), patch.object(launch, 'launch', return_value={}) as run:
            command.main()
        run.assert_called_once_with(entry=None, progress=ANY)

    def test_loader_ships_with_orchard_or_is_built(self):
        # Launching needs nothing from a distro or an install: only Pongo and m1n1.
        from orchard import hardware as hardware_data
        from orchard import build
        with patch.object(hardware_data, 'loader', return_value=LOADER), \
                patch.object(build, 'prepare_loader') as built:
            self.assertEqual(launch.loader_files('j71b', Progress(io.StringIO())), LOADER)
        built.assert_not_called()
        with patch.object(hardware_data, 'loader', return_value=None), \
                patch.object(build, 'prepare_loader', return_value=LOADER) as built:
            self.assertEqual(launch.loader_files('j71b', Progress(io.StringIO())), LOADER)
        built.assert_called_once_with(device='j71b')


if __name__ == '__main__':
    unittest.main()
