"""Installing on a 32-bit board: the iOS disk's LwVM, a root in an existing Linux partition, no boot partition.

Synthetic disks and invented identities only; no device is touched. The 64-bit
path is checked next to it, to show that it did not change.
"""
import io
import json
from pathlib import Path
import re
import struct
import subprocess
import sys
import tempfile
import unittest
from unittest import mock
import uuid
import zlib

from orchard import install_device as D
from orchard import install_direct as direct
from orchard import install_layout as L
from orchard import launcher, lwvm, service
from orchard.apple import linux_usb
from orchard.hardware import ROOT

sys.path.insert(0, str(Path(__file__).resolve().parent))

MEDIA = 64_000_000_000            # the 64 GB model's disk
CHUNK = lwvm.chunk_size(MEDIA)    # 64 MiB
HFS = uuid.UUID(lwvm.HFS_TYPE).bytes
LINUX = uuid.UUID(lwvm.LINUX_TYPE).bytes
SYSTEM_ID, DATA_ID, LINUX_ID = ('11111111-2222-4333-8444-555555555555', '22222222-3333-4444-8555-666666666666',
                                '33333333-4444-4555-8666-777777777777')
# (name, type, GUID, attributes, physical chunks in logical order)
PARTS = (('System', HFS, SYSTEM_ID, 0, list(range(1, 21))),
         ('Data', HFS, DATA_ID, 0x3000000000000, list(range(21, 801))),
         ('Linux', LINUX, LINUX_ID, 0, list(range(801, 953))))


def header(parts=PARTS):
    """An LwVM header for PARTS, chunks given per partition in logical order."""
    h = bytearray(lwvm.HEADER_BYTES)
    h[:16] = lwvm.MAGIC
    h[16:32] = uuid.UUID('aaaaaaaa-bbbb-4ccc-8ddd-eeeeeeeeeeee').bytes
    struct.pack_into('<QI', h, 0x20, MEDIA, len(parts))
    begin = lwvm.HEADER_BYTES
    for index in range(1024):
        struct.pack_into('<H', h, 0x800 + index * 2, 0xf000 if index == 0 else 0xf111 if index < MEDIA // CHUNK else 0xf3ff)
    for index, (name, kind, guid, flags, chunks) in enumerate(parts):
        at = 0x200 + index * 0x80
        size = len(chunks) * CHUNK
        h[at:at + 16] = kind
        h[at + 16:at + 32] = uuid.UUID(guid).bytes
        struct.pack_into('<QQQ', h, at + 32, begin, begin + size, flags)
        h[at + 56:at + 56 + 2 * len(name)] = name.encode('utf-16-le')
        for logical, physical in enumerate(chunks):
            struct.pack_into('<H', h, 0x800 + physical * 2, index << 12 | logical)
        begin += size
    struct.pack_into('<I', h, 0x2c, 0)
    struct.pack_into('<I', h, 0x2c, zlib.crc32(h))
    return bytes(h)


def kernel_view(parts=PARTS):
    """What the kernel's LwVM reader adds: contiguous partitions only, numbered as their entries."""
    return {index + 1: (chunks[0] * CHUNK // 512, len(chunks) * CHUNK // 512)
            for index, (_, _, _, _, chunks) in enumerate(parts) if chunks == list(range(chunks[0], chunks[0] + len(chunks)))}


class FakeDevice:
    """The add-on's shell as install_direct uses it: sysfs and dd reads of a synthetic disk."""
    def __init__(self, disks=('applesftl0',), primary=None, backup=None, kernel=None, ext4=()):
        self.disks, self.kernel, self.ext4 = list(disks), kernel_view() if kernel is None else kernel, set(ext4)
        self.primary = header() if primary is None else primary
        self.backup = self.primary if backup is None else backup
        self.commands = []

    def shell(self, connection, command, *, input=b'', timeout=60):
        self.commands.append(command)
        if command.startswith('for d in /sys/class/block/*'):
            return ''.join(name + '\n' for name in self.disks).encode()
        if command.endswith('/queue/logical_block_size'):
            return b'512\n'
        if command == 'cat /sys/class/block/applesftl0/size':
            return f'{MEDIA // 512}\n'.encode()
        if command.startswith('for p in /sys/class/block/applesftl0/applesftl0*'):
            return ''.join(f'{n} {start} {size}\n' for n, (start, size) in sorted(self.kernel.items())).encode()
        match = re.fullmatch(r'dd if=/dev/applesftl0p([0-9]+) bs=4096 count=1 2>/dev/null \|\| true', command)
        if match:
            data = bytearray(4096)
            if int(match.group(1)) in self.ext4:
                data[1080:1082] = b'\x53\xef'
                data[1144:1150] = b'linux'
            return bytes(data)
        raise AssertionError('unexpected command: ' + command)

    def read_disk(self, connection, disk, offset, size, block):
        self.commands.append(f'read {disk} {offset}')
        if offset == 0:
            return self.primary
        if offset == CHUNK - lwvm.HEADER_BYTES:
            return self.backup
        raise AssertionError(f'unexpected read at {offset}')

    def __enter__(self):
        self.patches = [mock.patch.object(direct, 'shell', self.shell), mock.patch.object(direct, 'read_disk', self.read_disk)]
        for patch in self.patches:
            patch.start()
        return self

    def __exit__(self, *_):
        for patch in self.patches:
            patch.stop()


class DiskDiscovery(unittest.TestCase):
    def test_the_one_internal_disk_is_found_nvme_or_the_ios_flash(self):
        for names, expected in ((['loop0', 'nvme0n1', 'zram0'], '/dev/nvme0n1'), (['applesftl0', 'mtdblock0'], '/dev/applesftl0')):
            with self.subTest(names=names), FakeDevice(disks=names):
                self.assertEqual(direct.find_disk(None), expected)

    def test_two_internal_disks_stop_the_install(self):
        with FakeDevice(disks=['nvme0n1', 'applesftl0']), self.assertRaisesRegex(D.InstallError, 'More than one'):
            direct.find_disk(None)

    def test_it_waits_for_storage_then_gives_up(self):
        with FakeDevice(disks=[]), mock.patch.object(direct.time, 'sleep'), \
                mock.patch.object(direct.time, 'monotonic', side_effect=[0, 1, 31]), \
                self.assertRaisesRegex(ValueError, 'did not come up within 30 s'):
            direct.find_disk(None)


class LwvmLayout(unittest.TestCase):
    def test_the_layout_is_the_kernels_partitions_with_their_lwvm_identity(self):
        with FakeDevice(ext4={3}) as device:
            layout = direct.inspect(None, '/dev/applesftl0')
        self.assertEqual(layout['disk']['table'], 'lwvm')
        self.assertEqual(layout['disk']['sector'], 512)
        self.assertEqual(layout['free'], [])
        rows = {p['number']: p for p in layout['partitions']}
        self.assertEqual(sorted(rows), [1, 2, 3])
        self.assertEqual((rows[3]['partuuid'], rows[3]['type'], rows[3]['name'], rows[3]['fs']),
                         (LINUX_ID, lwvm.LINUX_TYPE, 'Linux', 'ext4'))
        self.assertEqual((rows[3]['start'], rows[3]['blocks']), (801 * CHUNK // 512, 152 * CHUNK // 512))
        self.assertTrue(rows[2]['attributes'])
        # Only reads: dd from the disk and partitions, cat of sysfs.
        self.assertFalse([c for c in device.commands if 'of=' in c or 'blockdev' in c or '>' in c.replace('2>/dev/null', '')])

    def test_only_an_unencrypted_linux_partition_is_offered_and_kept_in_place(self):
        with FakeDevice() as _:
            layout = direct.inspect(None, '/dev/applesftl0')
        (option,) = L.options(layout)
        self.assertEqual((option.kind, option.partition['number'], option.creates_boot), ('replace', 3, False))
        self.assertIn('replace Linux partition 3', option.description)
        plan = L.plan(layout, option, {'bytes': 1, 'sha256': 'x', 'expanded_bytes': 1 << 30},
                      {'id': 'omarchy', 'title': 'Omarchy', 'options': 'rootwait', 'devicetree': 's5l8942x-p105.dtb'},
                      {'path': 'p', 'bytes': 1, 'sha256': 'y', 'expanded_bytes': 1 << 20})
        self.assertIsNone(plan['boot'])
        self.assertIsNone(plan['remove'])
        self.assertEqual(plan['disk']['table'], 'lwvm')
        self.assertEqual((plan['root']['number'], plan['root']['partuuid'], plan['root']['start'], plan['root']['blocks']),
                         (3, LINUX_ID, 801 * CHUNK // 512, 152 * CHUNK // 512))
        encrypted = json.loads(json.dumps(layout))
        encrypted['partitions'][2]['attributes'] = 1
        self.assertEqual(L.options(encrypted), [])
        with self.assertRaisesRegex(ValueError, 'never resizes iOS'):
            service.choose_target(encrypted, service.Clock(), input_fn=lambda _: '1', output=lambda *_: None)

    def test_replacing_it_must_be_typed_out(self):
        with FakeDevice():
            layout = direct.inspect(None, '/dev/applesftl0')
        said = []
        answers = iter(['1', 'erase 3'])
        option = service.choose_target(layout, service.Clock(), input_fn=lambda _: next(answers), output=said.append)
        self.assertEqual(option.partition['number'], 3)
        self.assertFalse(any('previous Orchard installer' in line for line in said))
        answers = iter(['1', 'yes'])
        with self.assertRaisesRegex(ValueError, 'Not confirmed'):
            service.choose_target(layout, service.Clock(), input_fn=lambda _: next(answers), output=said.append)

    def test_a_scattered_partition_the_kernel_skipped_is_not_offered(self):
        parts = (*PARTS[:2], ('Linux', LINUX, LINUX_ID, 0, [*range(850, 953), *range(801, 850)]))
        with FakeDevice(primary=header(parts), kernel=kernel_view(parts)):
            layout = direct.inspect(None, '/dev/applesftl0')
        self.assertEqual([p['number'] for p in layout['partitions']], [1, 2])

    def test_headers_that_disagree_or_a_kernel_that_disagrees_stop_the_install(self):
        other = bytearray(header())
        other[0x200 + 2 * 0x80 + 56:0x200 + 2 * 0x80 + 66] = 'Other'.encode('utf-16-le')
        with FakeDevice(backup=bytes(other)), self.assertRaisesRegex(D.InstallError, 'disagrees'):
            direct.inspect(None, '/dev/applesftl0')
        moved = {**kernel_view(), 3: (800 * CHUNK // 512, 152 * CHUNK // 512)}
        with FakeDevice(kernel=moved), self.assertRaisesRegex(D.InstallError, 'lies elsewhere'):
            direct.inspect(None, '/dev/applesftl0')

    def test_the_live_disk_is_checked_against_the_plan_before_writing(self):
        with FakeDevice():
            layout = direct.inspect(None, '/dev/applesftl0')
            plan = L.plan(layout, L.options(layout)[0], {'bytes': 1, 'sha256': 'x', 'expanded_bytes': 1},
                          {'id': 'omarchy', 'title': 'O', 'devicetree': 'd'}, {'bytes': 1, 'sha256': 'y', 'expanded_bytes': 1})
            node, partitions = direct.lwvm_target(None, plan, '/dev/applesftl0')
        self.assertEqual(node, '/dev/applesftl0p3')
        self.assertEqual(len(partitions), 3)
        changed = dict(plan, disk=dict(plan['disk'], table_sha256='0' * 64))
        with FakeDevice(), self.assertRaisesRegex(D.InstallError, 'changed since it was inspected'):
            direct.lwvm_target(None, changed, '/dev/applesftl0')
        elsewhere = dict(plan, root=dict(plan['root'], partuuid=DATA_ID))
        with FakeDevice(), self.assertRaisesRegex(D.InstallError, 'not the inspected Linux partition'):
            direct.lwvm_target(None, elsewhere, '/dev/applesftl0')

    def test_a_gpt_disk_is_inspected_as_before(self):
        with mock.patch.object(direct, 'read_disk', return_value=bytes(4096)), \
                mock.patch.object(direct, 'read_table', side_effect=RuntimeError('GPT path')) as table:
            with self.assertRaisesRegex(RuntimeError, 'GPT path'):
                direct.inspect(None, '/dev/nvme0n1')
        table.assert_called_once_with(None, '/dev/nvme0n1')


class RootLookup(unittest.TestCase):
    """orchard's initrd finds a root on the partitions the kernel's LwVM reader added."""
    INIT = ROOT / 'boot/initramfs/root/init'

    def function(self, name):
        return re.search(rf'^{name}\(\) \{{.*?^\}}', self.INIT.read_text(), re.M | re.S).group(0)

    def lookup(self, image, number, sector=512):
        """The init's own lookup line, GPT first, then LwVM."""
        script = (self.function('gpt_partuuid') + '\n' + self.function('lwvm_partuuid') + '\n' +
                  'id=$(gpt_partuuid "$0" "$1" "$2") || id=$(lwvm_partuuid "$0" "$2"); echo "$id"')
        result = subprocess.run(['sh', '-c', script, str(image), str(sector), str(number)], capture_output=True, text=True)
        return result.stdout.strip() or None

    def test_every_lwvm_entry_reads_as_the_partuuid_the_kernel_gives_it(self):
        with tempfile.TemporaryDirectory() as directory:
            image = Path(directory) / 'lwvm'
            image.write_bytes(header() + bytes(8192))
            self.assertEqual([self.lookup(image, n) for n in (1, 2, 3, 4)], [SYSTEM_ID, DATA_ID, LINUX_ID, None])

    def test_gpt_disks_read_exactly_as_before(self):
        from test_root_initrd import gpt
        guids = [str(uuid.uuid4()) for _ in range(2)]
        with tempfile.TemporaryDirectory() as directory:
            for sector in (512, 4096):
                image = Path(directory) / f'gpt-{sector}'
                gpt(image, sector, [guids[0], None, guids[1]])
                with self.subTest(sector=sector):
                    self.assertEqual([self.lookup(image, n, sector) for n in (1, 2, 3)], [guids[0], None, guids[1]])
            blank = Path(directory) / 'blank'
            blank.write_bytes(bytes(16384))
            self.assertIsNone(self.lookup(blank, 1, 4096))

    def test_the_init_tries_gpt_first(self):
        text = self.INIT.read_text()
        self.assertIn('id=$(gpt_partuuid "/dev/$disk" "$sector" "$(cat "$number")") ||\n'
                      '            id=$(lwvm_partuuid "/dev/$disk" "$(cat "$number")")', text)


class Finish(unittest.TestCase):
    """Inside the new root without a boot partition: the entry stays in the root's /boot."""
    def run_finish(self, kernel, boot):
        work = tempfile.TemporaryDirectory()
        self.addCleanup(work.cleanup)
        root = Path(work.name) / 'root'
        source = root / 'usr/lib/modules/6.12'
        (source / 'dtb').mkdir(parents=True)
        (source / 'vmlinuz').write_bytes(kernel)
        (source / 'initrd').write_bytes(b'initrd')
        (source / 'dtb/s5l8942x-p105.dtb').write_bytes(b'dtb')
        (root / 'boot').mkdir()
        (root / 'etc').mkdir()
        layer = Path(work.name) / 'layer.tar.gz'
        layer.write_bytes(b'')
        plan = {'layer': {'path': str(layer), 'bytes': 0, 'sha256': '', 'expanded_bytes': 0}, 'firmware': None,
                'account': None, 'boot': boot,
                'root': {'partuuid': LINUX_ID, 'fs_uuid': '5f0c2a1e-0000-4000-8000-000000000001'},
                'entry': {'id': 'omarchy', 'title': 'Omarchy', 'options': 'rootwait', 'devicetree': 's5l8942x-p105.dtb'}}
        with mock.patch.object(D, 'install_layer', return_value=0), mock.patch.object(D, 'emit'):
            D.finish(plan, root, {SYSTEM_ID}, None)
        return root

    def test_the_root_names_itself_by_filesystem_and_keeps_the_entry_in_boot(self):
        from test_cascadia_loader import kernel
        root = self.run_finish(kernel(), None)
        self.assertEqual((root / 'etc/fstab').read_text(), 'UUID=5f0c2a1e-0000-4000-8000-000000000001 / ext4 rw,relatime 0 1\n')
        conf = (root / 'boot/loader/entries/omarchy-33333333-6.12.conf').read_text()
        self.assertIn(f'options root=PARTUUID={LINUX_ID} rw rootwait\n', conf)
        self.assertIn('architecture arm\n', conf)
        self.assertEqual((root / 'boot/omarchy-33333333/6.12/s5l8942x-p105.dtb').read_bytes(), b'dtb')

    def test_64_bit_entries_and_fstab_are_as_before(self):
        root = self.run_finish(b'KERNEL', {'partuuid': '44444444-0000-4000-8000-000000000001'})
        self.assertEqual((root / 'etc/fstab').read_text(),
                         f'PARTUUID={LINUX_ID} / ext4 rw,relatime 0 1\n'
                         'PARTUUID=44444444-0000-4000-8000-000000000001 /boot vfat rw,relatime,fmask=0077,dmask=0077 0 2\n')
        self.assertIn('architecture aa64\n', (root / 'boot/loader/entries/omarchy-33333333-6.12.conf').read_text())


class InstalledBundle(unittest.TestCase):
    def test_the_installed_system_starts_from_its_root_with_the_boards_own_arguments(self):
        board = 'earlycon=s3c6400,0x32500000 lpj=5000000 root=/dev/ram0 rw rootwait panic=10'
        self.assertEqual(direct.installed_bootargs(board, LINUX_ID, 'rootwait fbcon=map:9'),
                         f'earlycon=s3c6400,0x32500000 lpj=5000000 rootwait panic=10 root=PARTUUID={LINUX_ID} rw rootwait fbcon=map:9')

    def test_the_bundle_carries_bootargs_and_serial_and_no_add_on(self):
        from test_armv7_direct import P105, armv7_layer
        from test_cascadia_loader import loader, tree
        from orchard.image import cascadia
        with tempfile.TemporaryDirectory() as directory:
            work = Path(directory)
            armv7_layer(work / 'layer', cascadia.write_fdt(tree()))
            (work / 'loader').write_bytes(loader(P105))
            adapter = mock.Mock(PROFILE='p105', DEVICETREE='s5l8942x-p105.dtb')
            plan = {'root': {'partuuid': LINUX_ID}, 'entry': {'options': 'rootwait'}}
            image = direct.installed_bundle(adapter, {'cascadia': work / 'loader', 'layer': work / 'layer'},
                                            {'serial_number': 'F4KESER1AL00'}, plan)
        config = cascadia.configuration(image)
        root = cascadia.read_fdt(image[config['dtb_offset']:config['dtb_offset'] + config['dtb_size']])[0]
        self.assertEqual(root['children'][0]['properties']['bootargs'], f'old root=PARTUUID={LINUX_ID} rw rootwait\0'.encode())
        self.assertEqual(root['properties']['serial-number'], b'F4KESER1AL00\0')
        self.assertEqual(image[config['initrd_offset']:], b'INITRD\0\0')   # the initrd alone, padded

    def test_a_ram_payload_gets_the_serial_number_just_before_the_boot(self):
        from test_armv7_direct import P105
        from test_cascadia_loader import kernel, loader, tree
        from orchard.image import cascadia
        image, _ = cascadia.assemble(loader(P105), kernel(), cascadia.write_fdt(tree()), b'RAMDISK', P105)
        patched = cascadia.with_serial(image, 'F4KESER1AL00')
        config = cascadia.configuration(patched)
        root = cascadia.read_fdt(patched[config['dtb_offset']:config['dtb_offset'] + config['dtb_size']])[0]
        self.assertEqual(root['properties']['serial-number'], b'F4KESER1AL00\0')
        self.assertEqual(patched[config['initrd_offset']:], b'RAMDISK')
        self.assertEqual(patched[:0x8000][:4], image[:4])
        with self.assertRaises(ValueError):
            cascadia.with_serial(image, 'not a serial;')


class Launcher(unittest.TestCase):
    def setUp(self):
        work = tempfile.TemporaryDirectory()
        self.addCleanup(work.cleanup)
        self.work = Path(work.name)

    def fake(self, lines, code=0):
        path = self.work / 'orchard-launcher'
        path.write_text('#!/bin/sh\necho "$@" > "$(dirname "$0")/argv"\n' +
                        ''.join(f"echo '{line}'\n" for line in lines) + f'exit {code}\n')
        path.chmod(0o755)
        return path

    def load(self, path):
        progress = mock.Mock()
        with mock.patch.dict('os.environ', {'ORCHARD_LAUNCHER': str(path)}):
            result = launcher.load(self.work / 'bundle.bin', chain='recovery-autogo', catalog=self.work / 'catalog',
                                   progress=progress)
        return result, progress

    def test_steps_and_requests_are_shown_and_started_returns(self):
        path = self.fake(['{"event":"step","step":"ibss","message":"Sending iBSS"}',
                          '{"event":"needs_user","message":"Put the iPad in DFU mode"}', 'plain text', '{"result":"started"}'])
        result, progress = self.load(path)
        self.assertEqual(result, {'result': 'started'})
        progress.step.assert_called_once_with('Sending iBSS')
        progress.say.assert_called_once_with('Put the iPad in DFU mode')
        self.assertEqual((self.work / 'argv').read_text().split(),
                         ['load', str(self.work / 'bundle.bin'), '--chain', 'recovery-autogo', '--catalog', str(self.work / 'catalog')])

    def test_an_error_result_or_no_result_raises_with_the_reason(self):
        with self.assertRaisesRegex(launcher.LauncherError, 'iBEC was refused') as caught:
            self.load(self.fake(['{"result":"error","reason":"ibec","message":"iBEC was refused"}'], code=1))
        self.assertEqual(caught.exception.reason, 'ibec')
        with self.assertRaisesRegex(launcher.LauncherError, 'without a result'):
            self.load(self.fake(['{"event":"step","step":"dfu"}'], code=3))

    def test_a_missing_launcher_says_so(self):
        with self.assertRaisesRegex(launcher.LauncherError, 'not installed'):
            self.load(self.work / 'absent')


class BoardData(unittest.TestCase):
    def test_p105_starts_through_the_launcher_and_warns_about_its_storage(self):
        from orchard import devices, hardware
        adapter = devices.load('p105')
        self.assertEqual(adapter.LAUNCHER_CHAIN, 'recovery-autogo')
        self.assertEqual(adapter.boot_inputs(), [])
        caveats = '\n'.join(service.caveats(adapter))
        self.assertIn('freed space is not reused yet', caveats)
        with mock.patch.dict('os.environ', {'ORCHARD_LAUNCHER': str(Path(tempfile.gettempdir()) / 'absent-launcher')}):
            self.assertIn('it starts through orchard-launcher, which is not installed', hardware.install_blockers('p105'))
        with tempfile.NamedTemporaryFile() as binary:
            Path(binary.name).chmod(0o755)
            with mock.patch.dict('os.environ', {'ORCHARD_LAUNCHER': binary.name}):
                self.assertEqual(hardware.install_blockers('p105'), [])

    def test_64_bit_boards_are_unchanged(self):
        from orchard import devices
        adapter = devices.load('j71b')
        self.assertIsNone(adapter.LAUNCHER_CHAIN)
        self.assertEqual([item.name for item in adapter.boot_inputs()], ['palera1n'])
        self.assertFalse(any('freed space' in line for line in service.caveats(adapter)))


class RamBoot(unittest.TestCase):
    def test_orchard_run_hands_a_cascadia_payload_with_the_serial_to_the_launcher(self):
        from orchard import boot
        with tempfile.TemporaryDirectory() as directory:
            output = Path(directory)
            (output / 'linux.bin').write_bytes(b'PAYLOAD')
            device = {'mode': 'dfu', 'chip': 0x8942, 'board': 0x0a, 'ecid': 1, 'location': 1, 'serial_number': 'F4KESER1AL00'}
            sent = []
            with mock.patch.object(boot, 'verify_ram', return_value={'boards': ['p105'], 'loader_format': 'cascadia',
                                                                      'boot_method': 'recovery-autogo'}), \
                    mock.patch('orchard.apple.usb.sole_target', return_value=device), \
                    mock.patch('orchard.image.cascadia.with_serial', side_effect=lambda image, serial: image + serial.encode()), \
                    mock.patch('orchard.launcher.load', side_effect=lambda image, **kw: sent.append((image.read_bytes(), kw['chain']))), \
                    mock.patch('orchard.apple.boot.start_pongo') as pongo, mock.patch('builtins.print'):
                boot.boot_ram(output, yes=True)
        self.assertEqual(sent, [(b'PAYLOADF4KESER1AL00', 'recovery-autogo')])
        pongo.assert_not_called()


class UsbListing(unittest.TestCase):
    def test_dfu_tags_give_the_serial_number_and_the_exploit(self):
        serial = 'CPID:8942 CPRV:15 CPFM:03 SCEP:10 BDID:0A ECID:000012345678ABCD IBFL:00 SRNM:[F4KESER1AL00] PWND:[checkm8]'
        self.assertEqual(linux_usb.serial_tags(serial), {'serial_number': 'F4KESER1AL00', 'pwned': 'checkm8'})
        self.assertEqual(linux_usb.serial_tags('CPID:8010 ECID:1'), {'serial_number': None, 'pwned': None})
        self.assertEqual(linux_usb.serial_tags('CPID:8010 YOLO:checkra1n'), {'serial_number': None, 'pwned': 'checkra1n'})


class Flow(unittest.TestCase):
    """service.install_direct end to end with the device mocked: LwVM writes the root only, GPT as before."""
    def run_flow(self, layout, plan_boot):
        from orchard.progress import Clock, Progress
        work = tempfile.TemporaryDirectory()
        self.addCleanup(work.cleanup)
        work = Path(work.name)
        for name in ('root', 'layer', 'map'):
            (work / name).write_bytes(b'{"ranges": [[0, 1]]}' if name == 'map' else b'x')
        record = {'release': '1', 'name': 'Omarchy', 'assets': {'root': {'bytes': 1, 'sha256': 'r', 'expanded_bytes': 1},
                                                                  'layer': {'bytes': 1, 'sha256': 'l', 'expanded_bytes': 1}}}
        assets = {'root': work / 'root', 'layer': work / 'layer', 'map': work / 'map'}
        boot_assets = {'install_tools': work / 'tools', 'layer': work / 'layer',
                       **({'cascadia': work / 'loader'} if plan_boot is None else {'m1n1': work / 'm1n1', 'pongo': work / 'pongo'})}
        done = lambda value: mock.Mock(done=lambda: True, result=lambda: value)
        adapter = mock.Mock(PROFILE='p105' if plan_boot is None else 'j71b', DEVICETREE='tree.dtb', NAME='Tablet')
        adapter.prepare_mixin.return_value = (work / 'layer', {'bytes': 1, 'sha256': 'f'})
        connection = mock.Mock(argv=['ssh'], scope=mock.Mock(boot_id='B', root=work / 'state',
                                                            device=mock.Mock(profile='p', ecid=1)))
        session = mock.MagicMock()
        session.return_value.__enter__.return_value = connection
        calls = dict(make_keys=mock.Mock(return_value=('c', 'h', 'a', b'k')),
                     add_on=mock.Mock(return_value=b''), boot_image=mock.Mock(return_value=b'IMG'),
                     start=mock.Mock(return_value={'boot_id': 'B'}), session=session,
                     find_disk=mock.Mock(return_value=layout['disk']['path']),
                     inspect=mock.Mock(return_value=layout),
                     lwvm_target=mock.Mock(return_value=('/dev/applesftl0p3', layout['partitions'])),
                     write_partitions=mock.Mock(side_effect=lambda c, plan, d: ({plan['root']['partuuid']: '/dev/nvme0n1p5',
                                                                                  plan['boot']['partuuid']: '/dev/nvme0n1p4'}, [])),
                     write_at=mock.Mock(), stream_image=mock.Mock(return_value={}), grow=mock.Mock(),
                     mount_new_root=mock.Mock(), unmount_new_root=mock.Mock(),
                     shell=mock.Mock(return_value=b''), installed_bundle=mock.Mock(return_value=b'BUNDLE'),
                     link_local_interfaces=mock.Mock(return_value=set()))
        with mock.patch.multiple(direct, **calls), \
                mock.patch.object(service.remote, 'put'), mock.patch.object(service.remote, 'run', return_value=[]), \
                mock.patch.object(service, 'private_directory', side_effect=lambda p: Path(p).mkdir(parents=True, exist_ok=True) or Path(p)), \
                mock.patch.dict('os.environ', {}, clear=False):
            answers = iter(['1', 'erase 3' if plan_boot is None else ''])
            report = service.install_direct(adapter, {'resumed': False, 'ecid': 1, 'serial_number': 'F4KESER1AL00'}, 'Omarchy',
                                            done((record, assets)), done({}), done(({'account': None}, boot_assets)), [],
                                            None, {'ssh_keys': []}, clock=Clock(), input_fn=lambda _: next(answers),
                                            secrets_fn=None, progress=Progress(io.StringIO()))
        return report, calls

    def test_lwvm_writes_the_root_only_and_keeps_a_boot_bundle_on_the_host(self):
        with FakeDevice():
            layout = direct.inspect(None, '/dev/applesftl0')
        report, calls = self.run_flow(layout, None)
        calls['write_partitions'].assert_not_called()
        calls['write_at'].assert_not_called()
        calls['lwvm_target'].assert_called_once()
        self.assertEqual(calls['stream_image'].call_args.args[1], '/dev/applesftl0p3')
        self.assertEqual(calls['mount_new_root'].call_args.args[1:], ('/dev/applesftl0p3', None))
        self.assertEqual(calls['unmount_new_root'].call_args.kwargs, {'boot': False})
        self.assertEqual(calls['unmount_new_root'].call_args.args[1], LINUX_ID)
        self.assertEqual((report['root'], report['boot']), (LINUX_ID, None))
        self.assertEqual(Path(report['boot_bundle']).read_bytes(), b'BUNDLE')
        # The device's serial number reaches the install payload's tree.
        self.assertEqual(calls['boot_image'].call_args.args[3]['serial_number'], 'F4KESER1AL00')

    def test_gpt_installs_as_before(self):
        layout = {'disk': {'path': '/dev/nvme0n1', 'sector': 4096, 'first': 6, 'last': 1 << 22, 'table_sha256': 'h'},
                  'partitions': [], 'free': [{'start': 256, 'blocks': 1 << 22}]}
        report, calls = self.run_flow(layout, True)
        calls['lwvm_target'].assert_not_called()
        calls['write_partitions'].assert_called_once()
        self.assertTrue(calls['write_at'].called)       # the new FAT32 boot partition
        self.assertEqual(calls['mount_new_root'].call_args.args[1:], ('/dev/nvme0n1p5', '/dev/nvme0n1p4'))
        self.assertEqual(calls['unmount_new_root'].call_args.kwargs, {'boot': True})
        self.assertNotIn('boot_bundle', report)
        self.assertIsNotNone(report['boot'])


if __name__ == '__main__':
    unittest.main()
