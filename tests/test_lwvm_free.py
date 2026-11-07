"""A new Linux entry in free LwVM space: offered, computed on the host, written to both copies, checked.

Synthetic headers only (invented identities); no device. See docs/bring-up/p105-lwvm-write.md.
"""
from pathlib import Path
import struct
import sys
import unittest
from unittest import mock
import uuid

from orchard import install_device as D
from orchard import install_direct as direct
from orchard import install_layout as L
from orchard import lwvm, service

sys.path.insert(0, str(Path(__file__).resolve().parent))
from test_armv7_install import CHUNK, DATA_ID, HFS, MEDIA, PARTS, SYSTEM_ID, FakeDevice, header  # noqa: E402

# Data's entry shrunk to end at chunk 600, as the resize tool will leave it: chunks 601..952 are free.
SHRUNK = (PARTS[0], ('Data', HFS, DATA_ID, 0x3000000000000, list(range(21, 601))))
NEW_ID = '44444444-5555-4666-8777-888888888888'
IMAGE = {'bytes': 1, 'sha256': 'x', 'expanded_bytes': 1 << 30}
ENTRY = {'id': 'omarchy', 'title': 'Omarchy', 'options': 'rootwait', 'devicetree': 's5l8942x-p105.dtb'}
LAYER = {'path': 'p', 'bytes': 1, 'sha256': 'y', 'expanded_bytes': 1 << 20}


class WritableDevice(FakeDevice):
    """A FakeDevice whose header copies can be written, and whose kernel re-reads the table."""
    def __init__(self, primary, *, fail_readback=None, **kwargs):
        super().__init__(primary=primary, kernel={}, **kwargs)
        self.fail_readback, self.writes = fail_readback, []
        self.reread()

    def reread(self):
        snapshot = lwvm.parse(self.primary, self.primary, MEDIA)
        self.kernel = {p['number']: (p['extents'][0]['physical'] // 512, p['bytes'] // 512)
                       for p in snapshot['partitions'] if len(p['extents']) == 1}

    def shell(self, connection, command, *, input=b'', timeout=60):
        if 'blockdev --rereadpt' in command:
            self.commands.append(command)
            self.reread()
            return b''
        return super().shell(connection, command, input=input, timeout=timeout)

    def write_at(self, connection, disk, block, lba, data):
        self.writes.append(lba * block)
        if lba * block == 0:
            self.primary = data
        elif lba * block == CHUNK - lwvm.HEADER_BYTES:
            self.backup = data
        else:
            raise AssertionError(f'write outside the LwVM headers at {lba * block}')

    def read_disk(self, connection, disk, offset, size, block):
        data = super().read_disk(connection, disk, offset, size, block)
        return b'\0' * len(data) if offset == self.fail_readback and self.writes else data

    def __enter__(self):
        super().__enter__()
        self.patches.append(mock.patch.object(direct, 'write_at', self.write_at))
        self.patches[-1].start()
        return self


class Allocation(unittest.TestCase):
    def test_free_space_is_the_unallocated_run_cut_to_the_virtual_room(self):
        primary = header(SHRUNK)
        snapshot = lwvm.parse(primary, primary, MEDIA)
        self.assertEqual(lwvm.free_runs(primary, snapshot), [(601, 352)])
        self.assertEqual(lwvm.free_space(primary, snapshot), [(601, 352)])
        # A full table, as iOS leaves it today, has none.
        full = header()
        self.assertEqual(lwvm.free_space(full, lwvm.parse(full, full, MEDIA)), [])

    def test_the_new_entry_is_appended_and_nothing_else_changes(self):
        primary = header(SHRUNK)
        after, entry = lwvm.allocate_free(primary, primary, NEW_ID, 601, 352)
        self.assertEqual((entry['number'], entry['type'], entry['name'], entry['attributes']), (3, lwvm.LINUX_TYPE, 'Linux', 0))
        self.assertEqual(entry['extents'], [{'logical': 0, 'physical': 601 * CHUNK, 'bytes': 352 * CHUNK}])
        before, now = lwvm.parse(primary, primary, MEDIA), lwvm.parse(after, after, MEDIA)
        for old, new in zip(before['partitions'], now['partitions']):
            self.assertEqual({k: v for k, v in old.items()}, {k: v for k, v in new.items()})
        self.assertEqual(entry['begin'], before['partitions'][-1]['end'])
        # Only the count, the new descriptor, the new chunks' map words and the CRC differ.
        changed = {i for i in range(lwvm.HEADER_BYTES) if primary[i] != after[i]}
        allowed = {*range(0x28, 0x30), *range(0x300, 0x380), *range(0x800 + 601 * 2, 0x800 + 953 * 2)}
        self.assertLessEqual(changed, allowed)

    def test_allocated_or_repeated_or_full_is_refused(self):
        primary = header(SHRUNK)
        with self.assertRaisesRegex(lwvm.Error, 'not free'):
            lwvm.allocate_free(primary, primary, NEW_ID, 590, 20)
        with self.assertRaisesRegex(lwvm.Error, 'repeated'):
            lwvm.allocate_free(primary, primary, SYSTEM_ID, 601, 352)
        with self.assertRaisesRegex(lwvm.Error, 'disagrees'):
            lwvm.allocate_free(primary, header(), NEW_ID, 601, 352)


class Install(unittest.TestCase):
    def inspect(self, device):
        with device:
            return direct.inspect(None, '/dev/applesftl0')

    def test_free_space_is_offered_and_needs_a_typed_confirmation(self):
        layout = self.inspect(WritableDevice(header(SHRUNK)))
        self.assertEqual(layout['free'], [{'start': 601 * CHUNK // 512, 'blocks': 352 * CHUNK // 512}])
        (option,) = L.options(layout)
        self.assertEqual(option.kind, 'free')
        self.assertIn('create a Linux partition in free space', option.description)
        said, answers = [], iter(['1', 'create partition'])
        self.assertEqual(service.choose_target(layout, service.Clock(), input_fn=lambda _: next(answers), output=said.append), option)
        self.assertTrue(any('has not been tested yet' in line for line in said))
        answers = iter(['1', 'yes'])
        with self.assertRaisesRegex(ValueError, 'Not confirmed'):
            service.choose_target(layout, service.Clock(), input_fn=lambda _: next(answers), output=said.append)

    def test_today_no_free_space_is_offered(self):
        layout = self.inspect(WritableDevice(header()))
        self.assertEqual(layout['free'], [])
        self.assertEqual([o.kind for o in L.options(layout)], ['replace'])

    def test_both_copies_are_written_backup_first_then_the_kernel_shows_the_partition(self):
        device = WritableDevice(header(SHRUNK))
        layout = self.inspect(device)
        plan = L.plan(layout, L.options(layout)[0], IMAGE, ENTRY, LAYER)
        self.assertEqual((plan['root']['number'], plan['root']['create']), (3, True))
        with device:
            node, partitions = direct.lwvm_target(None, plan, '/dev/applesftl0')
        self.assertEqual(node, '/dev/applesftl0p3')
        self.assertEqual(device.writes, [CHUNK - lwvm.HEADER_BYTES, 0])
        self.assertEqual(device.primary, device.backup)
        self.assertEqual({p['partuuid'] for p in partitions if p['number'] == 3}, {plan['root']['partuuid']})
        self.assertEqual(device.kernel[3], (601 * CHUNK // 512, 352 * CHUNK // 512))
        # The init's lookup reads the new GUID from the written primary.
        self.assertEqual(uuid.UUID(bytes=device.primary[0x300 + 16:0x300 + 32]), uuid.UUID(plan['root']['partuuid']))

    def test_a_copy_that_reads_back_wrong_stops_before_the_primary(self):
        device = WritableDevice(header(SHRUNK), fail_readback=CHUNK - lwvm.HEADER_BYTES)
        layout = self.inspect(device)
        plan = L.plan(layout, L.options(layout)[0], IMAGE, ENTRY, LAYER)
        old = device.primary
        with device, self.assertRaisesRegex(D.InstallError, 'uncertain state'):
            direct.lwvm_target(None, plan, '/dev/applesftl0')
        self.assertEqual(device.writes, [CHUNK - lwvm.HEADER_BYTES])
        self.assertEqual(device.primary, old)

    def test_a_table_changed_since_inspection_is_not_written(self):
        device = WritableDevice(header(SHRUNK))
        layout = self.inspect(device)
        plan = L.plan(layout, L.options(layout)[0], IMAGE, ENTRY, LAYER)
        device.primary = device.backup = header((SHRUNK[0], (*SHRUNK[1][:4], list(range(21, 602)))))
        device.reread()
        with device, self.assertRaisesRegex(D.InstallError, 'changed since it was inspected'):
            direct.lwvm_target(None, plan, '/dev/applesftl0')
        self.assertEqual(device.writes, [])

    def test_a_kernel_that_does_not_add_it_stops_the_install(self):
        device = WritableDevice(header(SHRUNK))
        layout = self.inspect(device)
        plan = L.plan(layout, L.options(layout)[0], IMAGE, ENTRY, LAYER)
        device.reread = lambda: None      # the kernel keeps its old view
        with device, self.assertRaisesRegex(D.InstallError, 'kernel does not see'):
            direct.lwvm_target(None, plan, '/dev/applesftl0')

if __name__ == '__main__':
    unittest.main()
