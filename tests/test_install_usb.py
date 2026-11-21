"""The raw-USB install receiver (boot/initramfs/recovery/orchard-install-usb.c) against docs/install-protocol.md.

The receiver is compiled with this computer's compiler and run with --test DIR: one Unix-socket
connection per USB session, a sparse disk image with a real GPT, and the programs it runs
(orchard-write, tune2fs, resize2fs, mount, umount, chroot) replaced by the small shell scripts below.
"""
import hashlib
import json
import os
from pathlib import Path
import shutil
import socket
import stat
import struct
import subprocess
import tempfile
import time
import unittest
import uuid
import zlib

from orchard import install_device as D
from orchard import install_direct as direct
from orchard import install_layout
from orchard.hardware import ROOT

SOURCE = ROOT / 'boot/initramfs/recovery/orchard-install-usb.c'
INFO, INSPECT, PUT, PLAN, STREAM, DATA = 1, 2, 3, 4, 5, 6
OK, ERROR, EVENT = 0x81, 0x82, 0x83
BLOCK = 4096
BLOCKS = 4 << 20        # a 16 GiB disk
APFS = '7c3457ef-0000-11aa-aa11-00306543ecac'
ECID = 0x1a2b3c4d5e6f

# What the receiver runs, as the test needs it: each logs its call to DIR/calls.
MOCKS = {
    'orchard-write': '''here=$(dirname "$0")/..
echo "orchard-write $*" >> "$here/calls"
cat > "$here/written"
[ "$(wc -c < "$here/written")" -eq "$3" ] || exit 1
echo "{\\"bytes\\": $3}"
''',
    'tune2fs': 'echo "tune2fs $*" >> "$(dirname "$0")/../calls"\n',
    'resize2fs': 'echo "resize2fs $*" >> "$(dirname "$0")/../calls"\n',
    'mount': '''echo "mount $*" >> "$(dirname "$0")/../calls"
if [ "$2" = ext4 ]; then mkdir -p "$4/etc"; printf 'NAME="Omarchy"\\nID=omarchy-a\\nVERSION_ID=0.4.0\\n' > "$4/etc/os-release"; fi
''',
    'umount': 'echo "umount $*" >> "$(dirname "$0")/../calls"\n',
    'chroot': '''here=$(dirname "$0")/..
echo "chroot $*" >> "$here/calls"
[ -e "$here/run/secrets.json" ] && echo "secrets staged" >> "$here/calls"
[ -e "$here/run/install_device.py" ] && echo "script staged" >> "$here/calls"
for line in '{"event": "step", "step": "firmware", "state": "done"}' \\
            '{"event": "step", "step": "remove-old-entries", "state": "done"}' \\
            '{"event": "step", "step": "account", "state": "done"}' \\
            '{"event": "step", "step": "platform-layer", "state": "done"}' \\
            '{"event": "step", "step": "boot-entry", "state": "done", "entry": "omarchy-a-1234-7.0.conf"}' \\
            '{"event": "step", "step": "boot-default", "state": "done"}' \\
            '{"event": "finished"}'; do echo "$line"; done
''',
}


def adt(ecid):
    """An Apple device tree with /chosen/unique-chip-id."""
    def prop(name, value):
        return name.encode().ljust(32, b'\0') + struct.pack('<I', len(value)) + value + bytes(-len(value) % 4)
    chosen = struct.pack('<II', 2, 0) + prop('name', b'chosen\0') + prop('unique-chip-id', struct.pack('<Q', ecid))
    return struct.pack('<II', 1, 1) + prop('name', b'device-tree\0') + chosen


def write_gpt(path):
    """A disk with an APFS container in its first GiB and free space after it, both GPT copies."""
    table = bytearray(128 * 128)
    table[:128] = direct.entry_bytes(APFS, str(uuid.uuid4()), 6, 262144, 'Container')
    first, last = 6, BLOCKS - 6

    def header(current, backup, entries):
        raw = bytearray(92)
        raw[:8] = b'EFI PART'
        struct.pack_into('<II', raw, 8, 0x10000, 92)
        struct.pack_into('<QQQQ', raw, 24, current, backup, first, last)
        raw[56:72] = uuid.UUID(int=7).bytes_le
        struct.pack_into('<QIII', raw, 72, entries, 128, 128, zlib.crc32(table))
        struct.pack_into('<I', raw, 16, zlib.crc32(raw))
        return bytes(raw).ljust(BLOCK, b'\0')
    with open(path, 'wb') as disk:
        disk.truncate(BLOCKS * BLOCK)
        disk.seek(BLOCK)
        disk.write(header(1, BLOCKS - 1, 2) + table)
        disk.seek(6 * BLOCK + 32)
        disk.write(b'NXSB')
        disk.seek((BLOCKS - 5) * BLOCK)
        disk.write(table + header(BLOCKS - 1, 1, BLOCKS - 5))


def read_gpt(path, lba=1):
    with open(path, 'rb') as disk:
        disk.seek(lba * BLOCK)
        header = disk.read(BLOCK)
        entries_lba, count, size = struct.unpack_from('<QII', header, 72)
        disk.seek(entries_lba * BLOCK)
        return header, disk.read(count * size)


class Link:
    """One USB session: framed messages over the receiver's test socket."""
    def __init__(self, path):
        self.socket = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        self.socket.settimeout(15)
        self.socket.connect(str(path))
        self.events = []

    def close(self):
        self.socket.close()

    def send(self, kind, payload=b'{}', flags=0, length=None):
        if isinstance(payload, dict):
            payload = json.dumps(payload).encode()
        self.socket.sendall(struct.pack('<BBHI', kind, flags, 0, len(payload) if length is None else length) + payload)

    def exactly(self, size):
        data = b''
        while len(data) < size:
            chunk = self.socket.recv(size - len(data))
            if not chunk:
                raise EOFError('the receiver closed the link')
            data += chunk
        return data

    def message(self):
        kind, flags, reserved, size = struct.unpack('<BBHI', self.exactly(8))
        assert flags == 0 and reserved == 0
        return kind, json.loads(self.exactly(size))

    def answer(self):
        while True:
            kind, body = self.message()
            if kind != EVENT:
                return kind, body
            self.events.append(body)

    def request(self, kind, payload=b'{}'):
        self.send(kind, payload)
        return self.answer()

    def transfer(self, kind, data, announced=None, sha256=None, name=None):
        body = {'bytes': len(data), 'sha256': sha256 or hashlib.sha256(data).hexdigest()}
        if name is not None:
            body['name'] = name
        answer = self.request(kind, announced or body)
        if answer[0] != OK:
            return answer
        for offset in range(0, len(data), 1 << 20):
            self.send(DATA, data[offset:offset + (1 << 20)])
        return self.answer()

    def until(self, event, seconds=20):
        deadline = time.monotonic() + seconds
        while time.monotonic() < deadline:
            kind, body = self.message()
            if kind == EVENT:
                self.events.append(body)
                if body['event'] == event:
                    return body
        raise AssertionError(f'no {event} event')


@unittest.skipUnless(shutil.which('cc'), 'no C compiler')
class Receiver(unittest.TestCase):
    ecid = ECID

    @classmethod
    def setUpClass(cls):
        cls.build = tempfile.TemporaryDirectory()
        cls.program = Path(cls.build.name) / 'orchard-install-usb'
        subprocess.run(['cc', '-O1', '-Wall', '-Wextra', '-Werror', '-pthread', '-o', str(cls.program), str(SOURCE)],
                       check=True, timeout=120)

    @classmethod
    def tearDownClass(cls):
        cls.build.cleanup()

    def setUp(self):
        work = tempfile.TemporaryDirectory()
        self.addCleanup(work.cleanup)
        self.dir = Path(work.name)
        for name in ('run', 'bin', 'newroot'):
            (self.dir / name).mkdir()
        self.disk = self.dir / 'disk'
        write_gpt(self.disk)
        for number in range(1, 9):     # the partition nodes the kernel would add
            (self.dir / f'disk{number}').touch()
        (self.dir / 'ecid').write_text(f'{ECID:x}\n')
        (self.dir / 'adt').write_bytes(adt(self.ecid))
        (self.dir / 'compatible').write_bytes(b'apple,j71b\0apple,t8010\0')
        (self.dir / 'boot_id').write_text('5f0c7a5e-0000-4000-8000-000000000001\n')
        for name, text in MOCKS.items():
            (self.dir / 'bin' / name).write_text('#!/bin/sh\n' + text)
            (self.dir / 'bin' / name).chmod(0o755)
        (self.dir / 'bin/install_device.py').write_text('# the finishing program\n')
        (self.dir / 'bin/boot-entries').write_text('# boot-entries\n')
        with open(self.dir / 'log', 'wb') as log:      # what the receiver says, for a failing test
            self.process = subprocess.Popen([str(self.program), '--test', str(self.dir)],
                                            stdout=subprocess.DEVNULL, stderr=log)
        self.addCleanup(self.stop)
        deadline = time.monotonic() + 10
        while not (self.dir / 'socket').exists():
            self.assertLess(time.monotonic(), deadline, 'the receiver did not start')
            time.sleep(0.02)
        self.link = self.connect()

    def stop(self):
        self.process.kill()
        self.process.wait(timeout=10)

    def connect(self):
        link = Link(self.dir / 'socket')
        self.addCleanup(link.close)
        return link

    def head(self):
        with open(self.disk, 'rb') as disk:
            return disk.read(64 * BLOCK)

    def ok(self, answer):
        self.assertEqual(answer[0], OK, answer[1])
        return answer[1]

    def refused(self, answer, code):
        self.assertEqual(answer[0], ERROR, answer[1])
        self.assertEqual(answer[1]['code'], code, answer[1])

    # --- the plan's files ---

    image = os.urandom(300_000)
    root_map = {'format': 1, 'block_size': 4096, 'blocks': 1000, 'restored_bytes': 15 * 4096,
                'ranges': [[0, 10], [20, 5]]}
    layer = b'LAYER' * 1000
    firmware = b'FIRMWARE' * 100

    def files(self):
        return {'platform.tar.gz': self.layer, 'firmware.tar.gz': self.firmware}

    def make_plan(self, layout, account=True):
        option = install_layout.options(layout)[0]

        def receipt(name, data, **extra):
            return {'path': '/run/orchard/' + name, 'bytes': len(data), 'sha256': hashlib.sha256(data).hexdigest(), **extra}
        image = {'bytes': len(self.image), 'sha256': hashlib.sha256(self.image).hexdigest(),
                 'expanded_bytes': 1 << 30, 'map': self.root_map}
        entry = {'id': 'omarchy-a', 'title': 'Omarchy', 'options': 'rootwait', 'devicetree': 't8010-j71b.dtb'}
        return install_layout.plan(layout, option, image, entry,
                                   layer=receipt('platform.tar.gz', self.layer, expanded_bytes=1 << 28),
                                   firmware=receipt('firmware.tar.gz', self.firmware),
                                   account={'user': 'user', 'password': True, 'pin': True, 'passwordless_sudo': False,
                                            'ssh_keys': []} if account else None)

    def put_all(self, link=None):
        for name, data in self.files().items():
            self.ok((link or self.link).transfer(PUT, data, name=name))


class Requests(Receiver):
    def test_info_names_this_device_and_its_phase(self):
        info = self.ok(self.link.request(INFO))
        self.assertEqual(info, {'protocol': 1, 'ecid': f'{ECID:x}', 'board': 'j71b',
                                'boot_id': '5f0c7a5e-0000-4000-8000-000000000001', 'phase': 'ready', 'staged': [],
                                'stream': None, 'record': None, 'error': None})

    def test_inspect_reports_install_devices_layout_and_the_disk_size(self):
        layout = self.ok(self.link.request(INSPECT))
        header, entries = read_gpt(self.disk)
        disk, partitions, free = D.parse_gpt(str(self.disk), BLOCK, header, entries)
        self.assertEqual(layout['disk'], {**disk, 'bytes': BLOCKS * BLOCK})
        self.assertEqual(layout['free'], free)
        self.assertEqual(layout['partitions'], [{**p, 'fs': 'apfs', 'fs_uuid': None, 'label': None} for p in partitions])

    def test_put_stages_plain_names_only_root_alone(self):
        for name in ('../escape', 'a/b', 'done', 'plan.json', '.hidden', 'x.part', '', 'a' * 101):
            with self.subTest(name=name):
                self.refused(self.link.request(PUT, {'name': name, 'bytes': 1, 'sha256': '0' * 64}), 'bad-name')
        self.assertEqual(self.ok(self.link.transfer(PUT, b'hello', name='layer.tar.zst')),
                         {'name': 'layer.tar.zst', 'bytes': 5})
        staged = self.dir / 'run/layer.tar.zst'
        self.assertEqual(staged.read_bytes(), b'hello')
        self.assertEqual(stat.S_IMODE(staged.stat().st_mode), 0o600)
        self.ok(self.link.transfer(PUT, b'', name='empty'))
        self.assertEqual(self.ok(self.link.request(INFO))['staged'], ['layer.tar.zst', 'empty'])

    def test_a_put_whose_bytes_differ_from_its_sha256_is_dropped(self):
        self.refused(self.link.transfer(PUT, b'hello', name='x', sha256=hashlib.sha256(b'other').hexdigest()),
                     'sha-mismatch')
        self.assertEqual(sorted(p.name for p in (self.dir / 'run').iterdir() if p.name != 'timeline'), [])
        self.assertEqual(self.ok(self.link.request(INFO))['staged'], [])

    def test_framing_errors_are_refused(self):
        self.refused(self.link.request(7), 'unknown-request')
        self.refused(self.link.request(DATA, b'xyz'), 'protocol')
        self.refused(self.link.request(INFO, b'not json'), 'protocol')
        self.refused(self.link.request(INFO, b'[]'), 'protocol')
        self.ok(self.link.request(INFO))            # still in step after each
        self.link.send(INFO, b'{}', flags=1)
        self.refused(self.link.answer(), 'protocol')
        # The framing is gone: nothing more on this session; a fresh one works.
        self.link.send(INFO)
        deadline = time.monotonic() + 1.5
        self.link.socket.settimeout(0.3)
        while time.monotonic() < deadline:
            try:
                self.assertEqual(self.link.message()[0], EVENT)     # heartbeats only, no answer
            except socket.timeout:
                pass
        self.link.close()     # a session ends with its link (on USB: SET_INTERFACE or a replug)
        fresh = self.connect()
        self.ok(fresh.request(INFO))
        fresh.send(INFO, b'', length=(1 << 20) + 1)
        self.refused(fresh.answer(), 'protocol')
        fresh.close()
        fresh = self.connect()
        fresh.send(0x90)
        self.refused(fresh.answer(), 'protocol')

    def test_messages_from_the_device_never_fill_whole_packets(self):
        self.link.send(INFO)
        while True:
            header = self.link.exactly(8)
            body = self.link.exactly(struct.unpack_from('<I', header, 4)[0])
            self.assertNotEqual((8 + len(body)) % 64, 0)
            if header[0] != EVENT:
                break

    def test_a_heartbeat_comes_every_second(self):
        self.assertEqual(self.link.until('heartbeat', 3), {'event': 'heartbeat', 'phase': 'ready'})

    def test_requests_out_of_phase_are_refused(self):
        self.refused(self.link.request(STREAM, {'bytes': 1, 'sha256': '0' * 64}), 'bad-state')


class Plans(Receiver):
    def test_a_valid_plan_writes_install_directs_tables_and_the_boot_filesystem(self):
        layout = self.ok(self.link.request(INSPECT))
        plan = self.make_plan(layout)
        header, entries = read_gpt(self.disk)
        writes, numbers = direct.new_tables(plan, BLOCK, header, entries)
        answer = self.ok(self.link.request(PLAN, plan))
        self.assertEqual(answer['root'], f'{self.disk}{numbers[plan["root"]["partuuid"]]}')
        self.assertEqual(answer['boot'], f'{self.disk}{numbers[plan["boot"]["partuuid"]]}')
        with open(self.disk, 'rb') as disk:
            for lba, data in writes:
                disk.seek(lba * BLOCK)
                self.assertEqual(disk.read(len(data)), data, lba)
            for offset, data in D.fat32_regions(plan['boot']['blocks'], BLOCK, int(plan['boot']['fs_uuid'], 16)):
                disk.seek(plan['boot']['start'] * BLOCK + offset)
                self.assertEqual(disk.read(len(data)), bytes(data), offset)
        for lba in (1, BLOCKS - 1):
            header, entries = read_gpt(self.disk, lba)
            raw = bytearray(header[:92])
            crc = struct.unpack_from('<I', raw, 16)[0]
            struct.pack_into('<I', raw, 16, 0)
            self.assertEqual(zlib.crc32(raw), crc)
            self.assertEqual(zlib.crc32(entries), struct.unpack_from('<I', header, 88)[0])
            _, partitions, _ = D.parse_gpt('disk', BLOCK, header, entries)
            found = {p['partuuid']: p for p in partitions}
            for spec in (plan['root'], plan['boot']):
                self.assertEqual((found[spec['partuuid']]['start'], found[spec['partuuid']]['blocks']),
                                 (spec['start'], spec['blocks']))
        saved = self.dir / 'run/plan.json'
        self.assertEqual(stat.S_IMODE(saved.stat().st_mode), 0o600)
        kept = json.loads(saved.read_text())
        self.assertEqual({key: value for key, value in kept.items() if key != 'present'}, plan)
        self.assertEqual(set(kept['present']), set(found))
        self.assertEqual(self.ok(self.link.request(INFO))['phase'], 'planned')
        self.refused(self.link.request(PLAN, plan), 'bad-state')

    def test_a_changed_table_or_a_plan_outside_free_space_changes_nothing(self):
        layout = self.ok(self.link.request(INSPECT))
        before = self.head()
        plan = self.make_plan(layout)
        changed = {**plan, 'disk': {**plan['disk'], 'table_sha256': '0' * 64}}
        self.refused(self.link.request(PLAN, changed), 'layout-changed')
        inside = {**plan, 'root': {**plan['root'], 'start': 100}}
        self.refused(self.link.request(PLAN, inside), 'plan-mismatch')
        outside = {**plan, 'layer': {**plan['layer'], 'path': '/tmp/platform.tar.gz'}}
        self.refused(self.link.request(PLAN, outside), 'plan-mismatch')
        self.refused(self.link.request(PLAN, {**plan, 'version': 3}), 'plan-version')
        self.assertEqual(self.head(), before)
        self.assertEqual(self.ok(self.link.request(INFO))['phase'], 'ready')


class Streams(Receiver):
    def planned(self, account=True):
        plan = self.make_plan(self.ok(self.link.request(INSPECT)), account=account)
        self.put_all()
        self.nodes = self.ok(self.link.request(PLAN, plan))
        return plan

    def test_stream_waits_for_every_staged_file_and_the_accounts_secrets(self):
        plan = self.planned()
        self.refused(self.link.transfer(STREAM, self.image), 'missing-file')
        (self.dir / 'run/firmware.tar.gz').unlink()
        self.ok(self.link.transfer(PUT, b'{"password": "x", "pin": "123456"}', name='secrets.json'))
        answer = self.link.transfer(STREAM, self.image)
        self.refused(answer, 'missing-file')
        self.assertIn('firmware.tar.gz', answer[1]['message'])
        self.refused(self.link.transfer(STREAM, self.image, announced={'bytes': 5, 'sha256': plan['image']['sha256']}),
                     'plan-mismatch')

    def test_a_cut_stream_is_incomplete_and_starts_again_then_the_device_finishes_by_itself(self):
        plan = self.planned()
        self.ok(self.link.transfer(PUT, b'{"password": "x", "pin": "123456"}', name='secrets.json'))
        self.ok(self.link.request(STREAM, {'bytes': len(self.image), 'sha256': plan['image']['sha256']}))
        self.link.send(DATA, self.image[:100_000])
        self.link.close()
        link = self.connect()
        deadline = time.monotonic() + 10
        while (info := self.ok(link.request(INFO)))['phase'] != 'stream-incomplete':
            self.assertLess(time.monotonic(), deadline)
            time.sleep(0.05)
        self.assertIsNone(info['stream'])
        self.assertEqual(self.ok(link.transfer(STREAM, self.image)),
                         {'bytes': len(self.image), 'sha256': plan['image']['sha256']})
        done = link.until('done')
        self.assertEqual([e['step'] for e in link.events if e['event'] == 'step'],
                         ['filesystem', 'firmware', 'account', 'kernel', 'entry', 'sync'])
        record = done['record']
        self.assertEqual(record, {'root_partuuid': plan['root']['partuuid'], 'boot_partuuid': plan['boot']['partuuid'],
                                  'entry': 'omarchy-a-1234-7.0',
                                  'os': {'id': 'omarchy-a', 'name': 'Omarchy', 'version': '0.4.0'},
                                  'image_sha256': plan['image']['sha256'], 'layer_sha256': plan['layer']['sha256']})
        deadline = time.monotonic() + 10
        while not (self.dir / 'run/done').exists():
            self.assertLess(time.monotonic(), deadline)
            time.sleep(0.05)
        self.assertEqual((self.dir / 'run/done').read_text(), plan['root']['partuuid'] + '\n')
        info = self.ok(link.request(INFO))
        self.assertEqual((info['phase'], info['record']), ('done', record))
        self.assertFalse((self.dir / 'run/secrets.json').exists())
        self.assertEqual((self.dir / 'written').read_bytes(), self.image)
        calls = (self.dir / 'calls').read_text().splitlines()
        root, boot = self.nodes['root'], self.nodes['boot']
        self.assertEqual(calls[0], f'orchard-write {root} {self.dir}/run/root.map {len(self.image)} {plan["image"]["sha256"]}')
        self.assertIn(f'tune2fs -U {plan["root"]["fs_uuid"]} -L omarchy-a {root}', calls)
        self.assertIn(f'resize2fs {root}', calls)
        self.assertIn(f'mount -t vfat {boot} {self.dir}/newroot/boot', calls)
        self.assertIn(f'chroot {self.dir}/newroot python3 /run/orchard/install_device.py finish /run/orchard/plan.json', calls)
        self.assertIn('secrets staged', calls)
        self.assertIn('script staged', calls)
        self.assertEqual(calls[-1], f'umount {self.dir}/newroot/')
        self.assertEqual((self.dir / 'run/root.map').read_text(), '0 10\n20 5\n')

    def test_a_stream_whose_bytes_differ_from_its_sha256_counts_for_nothing(self):
        plan = self.planned(account=False)
        bad = bytearray(self.image)
        bad[0] ^= 1
        self.refused(self.link.transfer(STREAM, bytes(bad), announced={'bytes': len(bad), 'sha256': plan['image']['sha256']}),
                     'sha-mismatch')
        self.assertEqual(self.ok(self.link.request(INFO))['phase'], 'stream-incomplete')


class KernelPackage(Receiver):
    def test_a_named_kernel_package_must_be_staged_before_the_stream(self):
        plan = self.make_plan(self.ok(self.link.request(INSPECT)), account=False)
        package = b'PACKAGE'
        plan['kernel_package'] = {'path': '/run/orchard/linux-orchard.pkg.tar.zst', 'base': 'pacman',
                                  'bytes': len(package), 'sha256': hashlib.sha256(package).hexdigest(), 'version': '7.0-1'}
        self.put_all()
        self.ok(self.link.request(PLAN, plan))
        answer = self.link.transfer(STREAM, self.image)
        self.refused(answer, 'missing-file')
        self.assertIn('linux-orchard.pkg.tar.zst', answer[1]['message'])
        self.ok(self.link.transfer(PUT, package, name='linux-orchard.pkg.tar.zst'))
        self.ok(self.link.transfer(STREAM, self.image))
        self.link.until('done')

    def test_a_map_beyond_the_root_partition_is_too_large(self):
        plan = self.make_plan(self.ok(self.link.request(INSPECT)), account=False)
        plan['image']['map'] = {**self.root_map, 'ranges': [[0, 10], [plan['root']['blocks'], 1]]}
        self.put_all()
        self.ok(self.link.request(PLAN, plan))
        self.refused(self.link.transfer(STREAM, self.image), 'too-large')


class WrongDevice(Receiver):
    ecid = 0x999

    def test_another_devices_add_on_is_refused(self):
        info = self.ok(self.link.request(INFO))
        self.assertEqual((info['phase'], info['ecid'], info['error']['code']), ('failed', '999', 'wrong-device'))
        self.refused(self.link.request(INSPECT), 'wrong-device')


del Receiver


class KernelConfigs(unittest.TestCase):
    def test_the_install_system_can_add_its_functionfs_instance(self):
        # The legacy g_ffs gadget, built in, puts f_fs into single-device mode: every
        # configfs ffs.* instance after it fails with EBUSY, and the receiver never starts.
        for config in sorted((ROOT / 'kernel/configs').glob('*/config')):
            settings = config.read_text().splitlines()
            if 'CONFIG_USB_CONFIGFS_F_FS=y' not in settings:
                continue
            with self.subTest(config.parent.name):
                self.assertNotIn('CONFIG_USB_FUNCTIONFS=y', settings)
                self.assertNotIn('CONFIG_USB_FUNCTIONFS=m', settings)


if __name__ == '__main__':
    unittest.main()
