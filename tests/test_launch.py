"""Launch puts m1n1 on the device: it boots the default by itself, or waits for one command; nothing is written."""
import io
import os
from pathlib import Path
import tempfile
import unittest
import uuid
from unittest.mock import ANY, Mock, patch

from boot_image import ROOT_ARM64, XBOOTLDR, fat32, gpt_disk
from orchard import launch
from boot_reader import Disk, default_entry, loader_default, read_boot
from orchard.transports.m1n1 import NO_BOOT_MANAGER, M1n1Error
from orchard.progress import Progress

ROOT = uuid.UUID('4d6c1a2e-0000-4000-8000-000000000001')
KERNEL = b'ARM64 Image ' * 4000
M1N1 = b'm1n1 image'


def entry(title, version, root=ROOT, devicetree='t8010-j71b.dtb'):
    return (f'title {title}\nversion {version}\nsort-key {title.lower()}\n'
            f'linux /t/{version}/linux\ninitrd /t/{version}/initrd\ninitrd /t/{version}/extra\n'
            f'devicetree /t/{version}/{devicetree}\noptions root=PARTUUID={root} rw\n')


def image(entries, kernel=KERNEL, loader_conf=None):
    files = {'/loader/loader.conf': loader_conf.encode()} if loader_conf is not None else {}
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
    """An m1n1 waiting in its proxy, with the boot manager ops (orchard 0007-0009, 0016) over a disk image.

    The listing is modelled with the test-side reader (tests/boot_reader.py),
    in m1n1's reply shape: the BLS keys plus `default` and `root`. Without a DTB
    name m1n1 checks against its own board, here the J71b's tree.
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
        _, fat, entries = read_boot(Disk(lambda lba: self.disk[lba * 4096:(lba + 1) * 4096]),
                                    devicetree or 't8010-j71b.dtb')
        default = default_entry(entries, loader_default(fat))
        return [(e.id, {**({'default': ['yes']} if e is default else {}), **e.keys,
                        **({'root': [e.root]} if e.root else {})}, e.problems) for e in entries]

    def boot_start(self, entry_id, devicetree=None):
        self.calls.append('start')
        if self.fail:
            raise M1n1Error(self.fail)
        self.started = (entry_id, devicetree)


class Launch(unittest.TestCase):
    def setUp(self):
        home = tempfile.TemporaryDirectory()
        self.addCleanup(home.cleanup)
        self.home = Path(home.name)
        (self.home / 'm1n1.bin').write_bytes(M1N1)
        self.loader = {'pongo': self.home / 'pongo.bin', 'm1n1': self.home / 'm1n1.bin'}
        self.adapter = Mock(PROFILE='j71b', NAME='iPad', DEVICETREE='t8010-j71b.dtb')
        self.adapter.boot_inputs.return_value = []
        self.sent = []
        self.adapter.start_loader.side_effect = self.start_loader
        self.output = io.StringIO()
        for patcher in (patch.dict(os.environ, ORCHARD_HOME=str(self.home / 'state')),
                        patch.object(launch, 'loader_files', return_value=self.loader)):
            patcher.start()
            self.addCleanup(patcher.stop)

    def start_loader(self, device, loader, progress, quiet=False, proxy=True):
        self.sent.append((Path(loader['m1n1']).read_bytes(), proxy))
        return '/dev/cu.usbmodemTEST1' if proxy else None

    def run_launch(self, *, mode='dfu', proxy=None, waiting=None, chooser=None, **options):
        with patch.object(launch, 'choose', return_value=(self.adapter, {'mode': mode, 'ecid': 1})) as chose, \
                patch.object(launch, 'waiting_port', return_value=waiting), \
                patch.object(launch, 'M1n1Proxy', return_value=proxy) as connect:
            from orchard.apple import usb
            self.chose, self.connect = chose, connect
            with patch.object(usb, 'm1n1_ports', return_value=[]):
                return launch.launch(progress=Progress(self.output), chooser=chooser, **options)


class Autoboot(Launch):
    def test_m1n1_goes_on_with_autoboot_and_the_host_says_nothing_more(self):
        report = self.run_launch()
        # The m1n1 image with m1n1's appended variable, started without waiting for a proxy.
        self.assertEqual(self.sent, [(M1N1 + b'autoboot=1\n' + bytes(4), False)])
        self.connect.assert_not_called()
        self.assertTrue(report['started'])
        self.assertEqual(report['entry'], 'default')
        self.assertIn('Started.', self.output.getvalue())

    def test_a_new_computer_downloads_the_tool_visibly_before_starting_the_device(self):
        order = []
        tool = Mock(cached=Mock(return_value=False), fetch=Mock(side_effect=lambda *_: order.append('tool')))
        self.adapter.boot_inputs.return_value = [tool]
        self.adapter.start_loader.side_effect = lambda *_, **__: order.append('loader')
        self.run_launch()
        self.assertEqual(order, ['tool', 'loader'])
        self.assertIn('Starting download in parallel...', self.output.getvalue())

    def test_running_systems_are_not_rebooted(self):
        with self.assertRaisesRegex(ValueError, 'DFU mode'):
            self.run_launch(mode='linux')
        self.assertEqual(self.sent, [])


class List(Launch):
    def two_systems(self):
        return image({'omarchy-4d6c1a2e-6.18.2.conf': entry('Omarchy', '6.18.2'),
                      'arch-77aa0011-6.18.1.conf': entry('Arch', '6.18.1'),
                      'other.conf': entry('Other', '6.18.2', devicetree='t8010-j171.dtb')},
                     loader_conf='default arch-77aa0011-*\n')

    def test_the_chosen_entry_starts_with_one_command(self):
        proxy = FakeM1n1(self.two_systems())
        shown = []
        report = self.run_launch(list_entries=True, proxy=proxy,
                                 chooser=lambda entries: shown.append(entries) or entries[1])
        # The listing as m1n1 sent it: the default marked by m1n1, not chosen by the host.
        self.assertEqual([(e.id, e.default) for e in shown[0]],
                         [('arch-77aa0011-6.18.1', True), ('omarchy-4d6c1a2e-6.18.2', False), ('other', False)])
        self.assertEqual(proxy.started, ('omarchy-4d6c1a2e-6.18.2', None))
        self.assertEqual(proxy.calls, ['init', 'entries', 'start'])
        self.assertTrue(report['started'])
        # m1n1 waits without autoboot.
        self.assertEqual(self.sent, [(M1N1, True)])

    def test_an_m1n1_that_already_waits_is_used_as_it_is(self):
        proxy = FakeM1n1(self.two_systems())
        self.run_launch(list_entries=True, proxy=proxy, waiting='/dev/cu.waiting', chooser=lambda entries: entries[0])
        self.chose.assert_not_called()
        self.connect.assert_called_once_with('/dev/cu.waiting')
        self.assertEqual(self.sent, [])

    def test_leaving_the_picker_leaves_m1n1_waiting(self):
        proxy = FakeM1n1(self.two_systems())
        report = self.run_launch(list_entries=True, proxy=proxy, chooser=lambda entries: None)
        self.assertFalse(report['started'])
        # Storage stays up: the next command needs no second init.
        self.assertEqual(proxy.calls, ['init', 'entries'])
        self.assertIn('m1n1 waits; start an entry with: orchard launch --entry ID', self.output.getvalue())

    def test_without_a_terminal_the_list_is_printed_and_m1n1_waits(self):
        proxy = FakeM1n1(self.two_systems())
        with patch.object(launch, 'is_terminal', return_value=False):
            report = self.run_launch(list_entries=True, proxy=proxy)
        self.assertFalse(report['started'])
        self.assertIsNone(proxy.started)
        lines = self.output.getvalue().splitlines()
        self.assertIn('* Arch (6.18.1) (default)  arch-77aa0011-6.18.1', lines)
        self.assertIn('  Omarchy (6.18.2)  omarchy-4d6c1a2e-6.18.2', lines)
        self.assertTrue(any(line.startswith('  Other (6.18.2)  other - not bootable: ') for line in lines))

    def test_with_nothing_bootable_the_list_says_so(self):
        proxy = FakeM1n1(image({'other.conf': entry('Other', '6.18.2', devicetree='t8010-j171.dtb')}))
        chooser = Mock()
        self.run_launch(list_entries=True, proxy=proxy, chooser=chooser)
        chooser.assert_not_called()
        self.assertIn('No entry can start.', self.output.getvalue())

    def test_no_entries(self):
        proxy = FakeM1n1(image({}))
        with self.assertRaisesRegex(ValueError, 'No boot entries'):
            self.run_launch(list_entries=True, proxy=proxy)
        self.assertEqual(proxy.calls, ['init', 'entries'])

    def test_an_m1n1_without_the_boot_manager_is_refused(self):
        # No host-side fallback: the host never reads the disk itself.
        proxy = FakeM1n1(image({}), boot_manager=False)
        with self.assertRaisesRegex(M1n1Error, 'no boot manager'):
            self.run_launch(list_entries=True, proxy=proxy)


class Picker(unittest.TestCase):
    def entries(self):
        return [launch.Entry('a', {'title': ['A']}), launch.Entry('broken', {}, ['missing']),
                launch.Entry('b', {'title': ['B'], 'default': ['yes']}), launch.Entry('c', {'title': ['C']})]

    def pick(self, keys):
        drawn = []
        chosen = launch.pick(self.entries(), iter(keys), lambda entries, cursor: drawn.append(cursor))
        return (chosen.id if chosen else None), drawn

    def test_the_default_is_preselected_and_unbootable_entries_are_skipped(self):
        self.assertEqual(self.pick(['enter']), ('b', [2]))
        self.assertEqual(self.pick(['up', 'enter']), ('a', [2, 0]))
        self.assertEqual(self.pick(['up', 'up', 'down', 'down', 'down', 'enter']), ('c', [2, 0, 0, 2, 3, 3]))

    def test_esc_or_q_chooses_nothing(self):
        self.assertEqual(self.pick(['down', 'quit'])[0], None)
        self.assertEqual(self.pick([])[0], None)

    def test_an_unbootable_default_preselects_the_first_bootable_entry(self):
        entries = [launch.Entry('x', {'default': ['yes']}, ['missing']), launch.Entry('y', {})]
        self.assertEqual(launch.pick(entries, iter(['enter']), lambda *_: None).id, 'y')

    def test_terminal_keys(self):
        read, write = os.pipe()
        self.addCleanup(os.close, read)
        os.write(write, b'\x1b[B\x1b[Ajk\rq')  # then the end of input, which quits too
        os.close(write)
        keys = launch.terminal_keys(read)
        self.assertEqual([next(keys) for _ in range(7)], ['down', 'up', 'down', 'up', 'enter', 'quit', 'quit'])

    def test_lines_name_the_entry_like_bootctl(self):
        self.assertEqual(launch.describe(launch.Entry('b', {'title': ['B'], 'version': ['6.18'], 'default': ['yes']})),
                         'B (6.18) (default)  b')
        self.assertEqual(launch.describe(launch.Entry('x', {}, ['missing', 'no root'])),
                         'x  x - not bootable: missing; no root')


class EntryOption(Launch):
    def test_exactly_that_entry_starts_with_one_command(self):
        proxy = FakeM1n1(image({'a.conf': entry('Omarchy', '6.18.2')}))
        report = self.run_launch(entry='minimal', proxy=proxy, waiting='/dev/cu.waiting')
        # No listing, no check on the host: m1n1 starts exactly that entry or says why not.
        self.assertEqual(proxy.calls, ['init', 'start'])
        self.assertEqual(proxy.started, ('minimal', None))
        self.assertEqual(report['entry'], 'minimal')
        self.chose.assert_not_called()

    def test_without_a_waiting_m1n1_one_is_started_to_wait_first(self):
        proxy = FakeM1n1(image({'a.conf': entry('Omarchy', '6.18.2')}))
        self.run_launch(entry='a', proxy=proxy)
        self.assertEqual(self.sent, [(M1N1, True)])
        self.assertEqual(proxy.started, ('a', None))

    def test_m1n1_start_failure_is_reported_and_m1n1_keeps_waiting(self):
        proxy = FakeM1n1(image({}), fail='m1n1 could not start the entry: No boot entry gone')
        with self.assertRaisesRegex(M1n1Error, 'No boot entry gone'):
            self.run_launch(entry='gone', proxy=proxy, waiting='/dev/cu.waiting')
        self.assertEqual(proxy.calls, ['init', 'start'])


class Command(unittest.TestCase):
    def test_launch_takes_list_or_entry(self):
        from orchard import command
        for argv, expected in ((['orchard', 'launch'], {'entry': None, 'list_entries': False}),
                               (['orchard', 'launch', '--list'], {'entry': None, 'list_entries': True}),
                               (['orchard', 'launch', '--entry', 'x'], {'entry': 'x', 'list_entries': False})):
            with patch('sys.argv', argv), patch.object(launch, 'launch', return_value={}) as run:
                command.main()
            run.assert_called_once_with(progress=ANY, **expected)

    def test_loader_ships_with_orchard_or_is_built(self):
        # Launching needs nothing from a distro or an install: only Pongo and m1n1.
        from orchard import hardware as hardware_data
        from orchard import build
        loader = {'pongo': Path('/loaders/pongo.bin'), 'm1n1': Path('/loaders/j71b/m1n1.bin')}
        with patch.object(hardware_data, 'loader', return_value=loader), \
                patch.object(build, 'prepare_loader') as built:
            self.assertEqual(launch.loader_files('j71b', Progress(io.StringIO())), loader)
        built.assert_not_called()
        with patch.object(hardware_data, 'loader', return_value=None), \
                patch.object(build, 'prepare_loader', return_value=loader) as built:
            self.assertEqual(launch.loader_files('j71b', Progress(io.StringIO())), loader)
        built.assert_called_once_with(device='j71b')


if __name__ == '__main__':
    unittest.main()
