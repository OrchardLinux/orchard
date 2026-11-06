"""Synthetic LwVM policy and interrupted-write tests; no device access."""
import copy
import io
import json
from pathlib import Path
import shutil
import struct
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import Mock, patch
import uuid

from orchard import install_device, install_layout, install_lwvm as I, lwvm as L
from orchard import install_remote
from orchard.progress import Clock
from orchard.service import choose_target
from orchard.hardware import ROOT

SYSTEM = '10000000-0000-4000-8000-000000000001'
DATA = '10000000-0000-4000-8000-000000000002'
LINUX = '10000000-0000-4000-8000-000000000003'
MEDIA = 64_000_000_000
CHUNK = 64 << 20
DEVICE = {'profile': 'fixture', 'ecid': 7}


def crc(header):
    struct.pack_into('<I', header, 0x2c, L.checksum(header))
    return bytes(header)


def fixture(*, partial=False):
    header = bytearray(4096)
    header[:16] = L.MAGIC
    header[16:32] = uuid.UUID('20000000-0000-4000-8000-000000000001').bytes
    struct.pack_into('<QI', header, 0x20, MEDIA, 2)
    end = 903 * CHUNK - (CHUNK - 4096 if partial else 0)
    for index, kind, identity, begin, stop, label in (
            (0, L.HFS_TYPE, SYSTEM, CHUNK, 3 * CHUNK, 'System'),
            (1, L.HFS_TYPE, DATA, 3 * CHUNK, end, 'Data')):
        pos = 0x200 + index * 128
        header[pos:pos + 16] = uuid.UUID(kind).bytes
        header[pos + 16:pos + 32] = uuid.UUID(identity).bytes
        struct.pack_into('<QQQ', header, pos + 32, begin, stop, 0x40000000)
        encoded = label.encode('utf-16-le')
        header[pos + 56:pos + 56 + len(encoded)] = encoded
    words = [0xf111 if 0 < i < MEDIA // CHUNK else 0xf3ff for i in range(1024)]
    words[0], words[1], words[2] = 0xf000, 0, 1
    physical = list(range(3, 903))
    physical[650], physical[651] = physical[651], physical[650]
    for logical, position in enumerate(physical):
        words[position] = 0x1000 | logical
    struct.pack_into('<1024H', header, 0x800, *words)
    return crc(header)


def hfs(size=40 << 30, *, mounted=False):
    raw = bytearray(512)
    raw[:4] = b'HX\0\5'
    struct.pack_into('>I', raw, 4, 0x2000 if mounted else 0x2100)
    struct.pack_into('>III', raw, 40, 4096, size // 4096, 100)
    raw[104:112] = bytes.fromhex('1234567890abcdef')
    return bytes(raw)


def proof(header):
    return L.shrink_proof(header, header, DATA, hfs(), hfs(), native_confirmation=hfs(mounted=True),
                          device=DEVICE, fsck={'unmounted': True, 'exit_code': 0})


class MemoryProvider:
    path = '/dev/fixture'
    device = {'major': 99, 'minor': 0}
    size = MEDIA

    def __init__(self, header, *, fail_write=0, fail_flush=0, bad_readback=False):
        self.copies = {0: header, CHUNK - 4096: header}
        self.writes, self.flushes = [], 0
        self.fail_write, self.fail_flush, self.bad_readback = fail_write, fail_flush, bad_readback

    def pair(self):
        return self.copies[0], self.copies[CHUNK - 4096]

    def close(self):
        pass

    def read_only(self):
        return False

    def read(self, offset, length):
        return bytes(length) if self.bad_readback else self.copies[offset]

    def write_header(self, offset, header):
        self.writes.append(offset)
        self.copies[offset] = header
        if len(self.writes) == self.fail_write:
            raise OSError('injected write failure after submission')

    def flush(self):
        self.flushes += 1
        if self.flushes == self.fail_flush:
            raise OSError('injected flush failure')


class Planner(unittest.TestCase):
    def test_geometry_matches_native_constructor(self):
        self.assertEqual(L.chunk_size(MEDIA), CHUNK)
        self.assertEqual(L.chunk_size(32_000_000_000), 32 << 20)
        for size in (0, 4096, 3 << 20, 4 << 20, MEDIA + 1, 1 << 43):
            with self.assertRaises(L.Error):
                L.chunk_size(size)

    def test_allocation_preserves_system_data_identity_and_unknown_bytes(self):
        before = fixture()
        after, root = L.allocate(before, before, proof(before), LINUX)
        self.assertEqual(before[0x200:0x280], after[0x200:0x280])
        self.assertEqual(before[0x280:0x2a8], after[0x280:0x2a8])
        self.assertEqual(before[0x2b0:0x300], after[0x2b0:0x300])
        self.assertEqual(root['bytes'], 260 * CHUNK)
        self.assertEqual(root['partuuid'], LINUX)
        self.assertEqual(root['attributes'], 0)
        self.assertGreater(len(root['extents']), 1)
        self.assertEqual(sum(p['bytes'] for p in root['extents']), root['bytes'])
        self.assertEqual(L.parse(after, after)['partitions'][1]['bytes'], 40 << 30)

    def test_partial_final_chunk_is_clipped(self):
        before = fixture(partial=True)
        after, root = L.allocate(before, before, proof(before), LINUX)
        self.assertEqual(root['bytes'], 259 * CHUNK + 4096)
        self.assertEqual(root['extents'][-1]['bytes'] % CHUNK, 4096)
        L.parse(after, after)

    def test_retained_data_is_rounded_up_to_chunk_boundary(self):
        before = fixture()
        native = hfs((40 << 30) + 4096)
        p = L.shrink_proof(before, before, DATA, native, native, native_confirmation=native,
                           device=DEVICE, fsck={'unmounted': True, 'exit_code': 0})
        after, root = L.allocate(before, before, p, LINUX)
        self.assertEqual(L.parse(after, after)['partitions'][1]['bytes'], (40 << 30) + CHUNK)
        self.assertEqual(root['bytes'], 259 * CHUNK)

    def test_snapshot_corruption_is_rejected_before_planning(self):
        before = fixture()
        cases = []
        bad = bytearray(before); bad[100] ^= 1; cases.append(bytes(bad))
        bad = bytearray(before); struct.pack_into('<H', bad, 0x800 + 4 * 2, 0x1000); cases.append(crc(bad))
        bad = bytearray(before); struct.pack_into('<H', bad, 0x800, 0xf111); cases.append(crc(bad))
        bad = bytearray(before); struct.pack_into('<H', bad, 0x800 + 1023 * 2, 0xf111); cases.append(crc(bad))
        bad = bytearray(before); bad[0x290:0x2a0] = uuid.UUID(SYSTEM).bytes; cases.append(crc(bad))
        for header in cases:
            with self.subTest(case=cases.index(header)), self.assertRaises(L.Error):
                L.parse(header, header)
        with self.assertRaises(L.Error):
            L.parse(before, bytes(4096))

    def test_native_proof_requires_checked_shrink_and_later_confirmation(self):
        before = fixture()
        for change in ({'fsck': {'unmounted': False, 'exit_code': 0}}, {'header_sha256': '0' * 64},
                       {'filesystem_bytes': 20 << 30}, {'data_uuid': SYSTEM}, {'device': {}},
                       {'native_hfs': {'primary': '', 'alternate': '', 'confirmation': ''}}):
            with self.subTest(change=change), self.assertRaises((L.Error, ValueError)):
                L.allocate(before, before, {**proof(before), **change}, LINUX)
        changed = proof(before)
        changed['native_hfs']['confirmation'] = __import__('base64').b64encode(hfs(41 << 30)).decode()
        with self.assertRaisesRegex(L.Error, 'confirmation'):
            L.allocate(before, before, changed, LINUX)

    def test_saved_plan_roundtrip_and_tampering(self):
        before = fixture()
        plan = json.loads(json.dumps(L.allocation_plan(before, before, proof(before), LINUX)))
        _, after, root = L.validate_plan(plan, before, before, MEDIA)
        self.assertEqual(root['partuuid'], LINUX)
        plan['root']['bytes'] += 4096
        with self.assertRaisesRegex(L.Error, 'plan fields'):
            L.validate_plan(plan, before, before, MEDIA)
        with self.assertRaisesRegex(L.Error, 'saved plan'):
            L.validate_plan(L.allocation_plan(before, before, proof(before), LINUX), after, after, MEDIA)

    def test_existing_linux_needs_explicit_replace_and_preserves_table(self):
        before = fixture()
        header, _ = L.allocate(before, before, proof(before), LINUX)
        layout = {'disk': {'path': '/dev/applesftl0', 'table': 'lwvm', 'sector': 512, 'bytes': MEDIA},
                  'lwvm': {'primary': L.b64(header), 'backup': L.b64(header)}}
        (option,) = install_layout.options(layout)
        self.assertEqual(option.kind, 'replace')
        replies = iter(['1', 'no'])
        with self.assertRaisesRegex(ValueError, 'Not confirmed'):
            choose_target(layout, Clock(), input_fn=lambda _: next(replies), output=lambda _: None)
        plan = install_layout.plan(layout, option, {'expanded_bytes': 1}, {'id': 'fixture'}, {'expanded_bytes': 1})
        self.assertEqual(plan['lwvm']['before'], plan['lwvm']['after'])
        self.assertEqual(plan['root']['partuuid'], LINUX)
        L.validate_plan(plan['lwvm'], header, header, MEDIA)
        with self.assertRaisesRegex(L.Error, 'existing Linux'):
            L.allocate(header, header, proof(before), str(uuid.uuid4()))

    def test_new_tail_is_hidden_without_native_proof(self):
        before = fixture()
        layout = {'disk': {'path': '/dev/applesftl0', 'table': 'lwvm', 'sector': 512, 'bytes': MEDIA},
                  'lwvm': {'primary': L.b64(before), 'backup': L.b64(before)}}
        self.assertEqual(install_layout.options(layout), [])
        layout['shrink_proof'] = proof(before)
        (option,) = install_layout.options(layout)
        self.assertEqual(option.kind, 'allocate')
        replies = iter(['1', 'allocate Data tail'])
        self.assertEqual(choose_target(layout, Clock(), input_fn=lambda _: next(replies), output=lambda _: None), option)


class MetadataCommit(unittest.TestCase):
    def setUp(self):
        self.before = fixture()
        self.plan = L.allocation_plan(self.before, self.before, proof(self.before), LINUX)

    def test_backup_flush_readback_precedes_primary(self):
        provider, records = MemoryProvider(self.before), []
        I.commit_headers(provider, self.plan, record=lambda r: records.append(copy.deepcopy(r)))
        self.assertEqual(provider.writes, [CHUNK - 4096, 0])
        self.assertEqual(provider.flushes, 2)
        self.assertEqual(records[-1]['outcome'], 'complete')
        self.assertEqual(provider.pair(), (L.unb64(self.plan['after']),) * 2)

    def test_uncertain_writes_and_flushes_stop_without_retry_or_restore(self):
        for arguments, writes in (({'fail_write': 1}, 1), ({'fail_write': 2}, 2),
                                  ({'fail_flush': 1}, 1), ({'fail_flush': 2}, 2), ({'bad_readback': True}, 1)):
            with self.subTest(arguments=arguments):
                provider, records = MemoryProvider(self.before, **arguments), []
                with self.assertRaisesRegex(L.Error, 'uncertain'):
                    I.commit_headers(provider, self.plan, record=lambda r: records.append(copy.deepcopy(r)))
                self.assertEqual(len(provider.writes), writes)
                self.assertEqual(records[-1]['outcome'], 'uncertain')
                self.assertEqual(provider.copies[CHUNK - 4096], L.unb64(self.plan['after']))

    def test_changed_live_pair_submits_no_write(self):
        provider = MemoryProvider(self.before)
        provider.copies[0] = bytes(4096)
        with self.assertRaises(L.Error):
            I.commit_headers(provider, self.plan, record=lambda r: None)
        self.assertEqual(provider.writes, [])

    def test_aliased_small_media_headers_are_refused_before_provider_io(self):
        for size in (3 << 20, 4 << 20):
            provider = Mock(size=size)
            provider.pair = lambda: I.Provider.pair(provider)
            with self.subTest(size=size), self.assertRaisesRegex(L.Error, 'geometry'):
                I.commit_headers(provider, self.plan, record=lambda _: self.fail('no intent expected'))
            provider.read.assert_not_called()
            provider.write_header.assert_not_called()

    def test_existing_partition_does_not_write_headers(self):
        after = L.unb64(self.plan['after'])
        root = L.public_partition(L.partition(L.parse(after, after), LINUX))
        plan = {'action': 'replace', 'before': L.b64(after), 'after': L.b64(after),
                'before_sha256': L.digest(after), 'after_sha256': L.digest(after), 'root': root}
        provider = MemoryProvider(after)
        I.commit_headers(provider, plan, record=lambda r: self.fail('no metadata write expected'))
        self.assertEqual(provider.writes, [])

    def test_identity_is_bound_to_recovery_boot_and_native_device(self):
        plan = {'identity': {**DEVICE, 'boot_id': 'fixture-boot'}, 'lwvm': self.plan}
        I.check_identity(plan, plan['identity'])
        with self.assertRaisesRegex(L.Error, 'recovery boot'):
            I.check_identity(plan, {**plan['identity'], 'boot_id': 'other'})
        plan['lwvm']['shrink_proof']['device']['ecid'] = 8
        with self.assertRaisesRegex(L.Error, 'different device'):
            I.check_identity(plan, plan['identity'])

    def test_saved_inputs_refuse_links_and_wrong_sizes(self):
        with tempfile.TemporaryDirectory() as directory:
            source, linked = Path(directory) / 'header', Path(directory) / 'link'
            source.write_bytes(self.before)
            linked.symlink_to(source)
            self.assertEqual(L.saved_file(source, 4096), self.before)
            with self.assertRaises(OSError): L.saved_file(linked, 4096)
            with self.assertRaises(L.Error): L.saved_file(source, 512)

    def test_intent_record_is_private_and_replaced_atomically(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / 'intent.json'
            I.save_intent({'stage': 'backup'}, path)
            I.save_intent({'stage': 'primary'}, path)
            self.assertEqual(json.loads(path.read_text()), {'stage': 'primary'})
            self.assertEqual(path.stat().st_mode & 0o777, 0o600)
            self.assertFalse(path.with_suffix('.part').exists())

    def test_uncertain_or_incomplete_saved_intent_requires_inspection(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / 'intent.json'
            I.check_previous_intent(path)
            I.save_intent({'kind': 'orchard.lwvm-write-intent', 'outcome': 'uncertain'}, path)
            with self.assertRaisesRegex(L.Error, 'requires inspection'):
                I.check_previous_intent(path)
            I.save_intent({'kind': 'orchard.lwvm-write-intent', 'outcome': 'complete'}, path)
            I.check_previous_intent(path)
            path.with_suffix('.part').write_text('incomplete')
            with self.assertRaisesRegex(L.Error, 'incomplete'):
                I.check_previous_intent(path)


class RootWrites(unittest.TestCase):
    def setUp(self):
        self.before = fixture()
        self.allocation = L.allocation_plan(self.before, self.before, proof(self.before), LINUX)
        self.after = L.unb64(self.allocation['after'])
        self.snapshot = L.parse(self.after, self.after)
        self.root = L.partition(self.snapshot, LINUX)
        self.device = {'major': 99, 'minor': 0}

    def test_mapper_refuses_a_receipt_for_another_range_before_any_root_write(self):
        root = self.root
        receipt = {'schema': 1, 'kind': 'orchard-lwvm-root', 'root_uuid': LINUX,
                   'partition_index': 2, 'header_crc32': f'{L.checksum(self.after):08x}',
                   'media_bytes': MEDIA, 'chunk_bytes': CHUNK, 'bytes': root['bytes'], 'disk': self.device,
                   'extents': [{'start_sector': p['logical'] // 512, 'sectors': p['bytes'] // 512,
                                'physical_sector': p['physical'] // 512} for p in root['extents']],
                   'mapping': {'device': '/dev/dm-7', 'mode': 'rw', 'name': 'orchard-lwvm-' + LINUX,
                               'uuid': 'ORCHARD-LWVM-' + LINUX}}
        for field, value in (('root_uuid', DATA), ('disk', {'major': 99, 'minor': 1}),
                             ('bytes', root['bytes'] + 4096), ('header_crc32', '00000000'),
                             ('extents', [{'start_sector': 0, 'sectors': root['bytes'] // 512, 'physical_sector': 0}])):
            api = Mock()
            api.run.return_value = json.dumps({**receipt, field: value}).encode()
            with self.subTest(field=field), patch.object(I, 'Provider') as provider, self.assertRaisesRegex(L.Error, 'mapper receipt'):
                I.mapped_root(api, '/dev/applesftl0', self.allocation, self.snapshot, self.root, self.device)
            provider.assert_not_called()

    def test_stream_and_formatter_only_receive_verified_linux_mapping(self):
        with tempfile.TemporaryDirectory() as directory:
            work = Path(directory)
            layer = work / 'layer.tar.gz'
            layer.write_bytes(b'fixture')
            api = Mock(InstallError=install_device.InstallError, TARGET=work / 'root')
            api.extract.return_value = {'files': 2}
            api.install_layer.return_value = 1
            api.kernel_version.return_value = 'fixture-kernel'
            identity = {**DEVICE, 'boot_id': 'fixture-boot'}
            plan = {'identity': identity, 'lwvm': self.allocation,
                    'io_policy': L.install_io_policy(self.root['bytes']),
                    'disk': {'path': '/dev/applesftl0', 'bytes': MEDIA, **self.device},
                    'root': {'partuuid': LINUX, 'blocks': self.root['bytes'] // 512, 'fs_uuid': LINUX, 'label': 'fixture'},
                    'image': {'bytes': 7, 'sha256': 'a' * 64, 'compression': 'gzip'},
                    'layer': {'path': str(layer), 'bytes': 7, 'sha256': 'b' * 64, 'expanded_bytes': 7},
                    'entry': {'devicetree': 'fixture.dtb', 'options': 'rootwait'}}
            provider = MemoryProvider(self.before)
            stream = io.BytesIO(b'payload')
            commit = I.commit_headers
            with patch.object(I, 'Provider', return_value=provider), \
                 patch.object(I, 'current_identity', return_value=identity), \
                 patch.object(I, 'check_previous_intent'), \
                 patch.object(I, 'enable_writes', return_value=False), \
                 patch.object(I, 'commit_headers', side_effect=lambda p, a: commit(p, a, record=lambda _: None)), \
                 patch.object(I, 'mapped_root', return_value=('/dev/dm-7', {
                     'name': 'fixture-map', 'root_uuid': LINUX, 'mode': 'rw', 'extents': [
                         {'start_sector': p['logical'] // 512, 'sectors': p['bytes'] // 512,
                          'physical_sector': p['physical'] // 512} for p in self.root['extents']]})), \
                 patch.object(I.subprocess, 'run', return_value=Mock(returncode=0)), \
                 patch.object(I.os, 'geteuid', return_value=0):
                I.install(api, plan, stream)
            format_command = next(call.args for call in api.run.call_args_list if call.args[0] == 'mke2fs')
            self.assertEqual(format_command[-1], '/dev/dm-7')
            self.assertEqual(api.extract.call_args.args, (stream, api.TARGET))
            self.assertEqual(api.extract.call_args.kwargs['expanded_limit'], self.root['bytes'])
            self.assertEqual(api.emit.call_args.kwargs['boot_preparation_required'], True)
            self.assertEqual(api.emit.call_args.kwargs['boot']['boot_assets_prepared'], False)

    def test_provider_rejects_regular_files_and_links(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / 'fake-disk'
            path.write_bytes(bytes(4096))
            with self.assertRaisesRegex(L.Error, 'block device'):
                I.Provider(path)
            link = path.with_name('link')
            link.symlink_to(path)
            with self.assertRaises(OSError):
                I.Provider(link)

    def test_provider_size_ioctl_uses_native_arm_pointer_width_with_u64_output(self):
        info = Mock(st_mode=I.stat.S_IFBLK, st_rdev=I.os.makedev(99, 0))
        for pointer_bytes, request in ((4, 0x80041272), (8, 0x80081272)):
            with self.subTest(pointer_bytes=pointer_bytes), \
                 patch.object(I.os, 'open', return_value=123), \
                 patch.object(I.os, 'fstat', return_value=info), \
                 patch.object(I.os, 'close') as close, \
                 patch.object(I.struct, 'calcsize', return_value=pointer_bytes), \
                 patch.object(I.fcntl, 'ioctl', return_value=struct.pack('Q', MEDIA)) as ioctl:
                provider = I.Provider('/dev/fixture')
                self.assertEqual(provider.size, MEDIA)
                ioctl.assert_called_once_with(123, request, bytes(8))
                provider.close()
                close.assert_called_once_with(123)

    def test_reprobed_read_only_backend_stops_before_clearing_fence_or_writing(self):
        identity = {**DEVICE, 'boot_id': 'fixture-boot'}
        plan = {'identity': identity, 'lwvm': self.allocation,
                'io_policy': L.install_io_policy(self.root['bytes']),
                'disk': {'path': '/dev/applesftl0', 'bytes': MEDIA, **self.device},
                'root': {'partuuid': LINUX, 'blocks': self.root['bytes'] // 512}}
        provider = MemoryProvider(self.before)
        provider.read_only = lambda: True
        api = Mock(InstallError=install_device.InstallError)
        with patch.object(I, 'Provider', return_value=provider), \
             patch.object(I, 'current_identity', return_value=identity), \
             patch.object(I, 'check_previous_intent'), \
             patch.object(I, 'enable_writes', return_value=True), \
             patch.object(I, 'commit_headers') as commit, \
             patch.object(I, 'mapped_root') as mapping, \
             patch.object(I.subprocess, 'run', return_value=Mock(returncode=0)), \
             patch.object(I.os, 'geteuid', return_value=0), \
             self.assertRaisesRegex(install_device.InstallError, 'no qualified write path'):
            I.install(api, plan, io.BytesIO())
        api.run.assert_not_called()
        commit.assert_not_called()
        mapping.assert_not_called()

    def test_wrong_recovery_identity_runs_no_storage_or_cleanup_command(self):
        identity = {**DEVICE, 'boot_id': 'fixture-boot'}
        plan = {'identity': {**identity, 'boot_id': 'different-boot'}, 'lwvm': self.allocation,
                'disk': {'path': '/dev/applesftl0'}}
        api = Mock(InstallError=install_device.InstallError)
        with patch.object(I, 'current_identity', return_value=identity), \
             patch.object(I, 'Provider') as provider, \
             patch.object(I.subprocess, 'run') as command, \
             patch.object(I.os, 'geteuid', return_value=0), \
             self.assertRaisesRegex(install_device.InstallError, 'different recovery boot'):
            I.install(api, plan, io.BytesIO())
        provider.assert_not_called()
        command.assert_not_called()

    def test_write_reload_uses_the_plan_budget_and_builtin_refusal_submits_no_write(self):
        policy = L.install_io_policy(self.root['bytes'])
        with tempfile.TemporaryDirectory() as directory:
            parameter = Path(directory) / 'write_enable'
            parameter.write_text('N\n')
            api = Mock()
            def command(*args, **kwargs):
                if 'write_enable=1' in args:
                    parameter.write_text('Y\n')
            api.run.side_effect = command
            with patch.object(I, 'WRITE_POLICY', parameter):
                changed = Mock()
                self.assertTrue(I.enable_writes(api, policy, changed=changed))
                changed.assert_called_once_with()
                self.assertEqual(api.run.call_args_list[0].args, ('modprobe', '-r', 'apple-sftl'))
                self.assertIn('max_page_reads=' + str(policy['max_page_reads']), api.run.call_args_list[1].args)
                refused = Mock()
                refused.run.side_effect = install_device.InstallError('command-failed', 'built-in module')
                unchanged = Mock()
                with self.assertRaisesRegex(install_device.InstallError, 'built-in'):
                    I.enable_writes(refused, policy, changed=unchanged)
                self.assertEqual(refused.run.call_count, 1)
                unchanged.assert_not_called()

    def test_write_policy_verification_errors_restore_the_default_module(self):
        identity = {**DEVICE, 'boot_id': 'fixture-boot'}
        plan = {'identity': identity, 'lwvm': self.allocation,
                'io_policy': L.install_io_policy(self.root['bytes']),
                'disk': {'path': '/dev/applesftl0', 'bytes': MEDIA, **self.device},
                'root': {'partuuid': LINUX, 'blocks': self.root['bytes'] // 512}}
        failures = ('N\n', FileNotFoundError('parameter vanished'), OSError('parameter read failed'),
                    KeyboardInterrupt('cancelled'), subprocess.TimeoutExpired('modprobe', 30))
        for failure in failures:
            with self.subTest(failure=repr(failure)):
                parameter = Mock()
                parameter.is_file.return_value = True
                if isinstance(failure, BaseException):
                    parameter.read_text.side_effect = failure
                else:
                    parameter.read_text.return_value = failure
                api = Mock(InstallError=install_device.InstallError)
                if isinstance(failure, subprocess.TimeoutExpired):
                    api.run.side_effect = [b'', failure]
                with patch.object(I, 'WRITE_POLICY', parameter), \
                     patch.object(I, 'Provider', return_value=MemoryProvider(self.before)), \
                     patch.object(I, 'current_identity', return_value=identity), \
                     patch.object(I, 'check_previous_intent'), \
                     patch.object(I, 'commit_headers') as commit, \
                     patch.object(I, 'mapped_root') as mapping, \
                     patch.object(I.subprocess, 'run', return_value=Mock(returncode=0)) as cleanup, \
                     patch.object(I.os, 'geteuid', return_value=0):
                    expected = type(failure) if isinstance(failure, BaseException) else install_device.InstallError
                    with self.assertRaises(expected) as caught:
                        I.install(api, plan, io.BytesIO())
                if isinstance(failure, BaseException):
                    self.assertIs(caught.exception, failure)
                commands = [call.args[0] for call in cleanup.call_args_list]
                self.assertIn(['modprobe', '-r', 'apple-sftl'], commands)
                self.assertIn(['modprobe', 'apple-sftl'], commands)
                self.assertEqual(commands[-1], ['blockdev', '--setro', '/dev/applesftl0'])
                commit.assert_not_called()
                mapping.assert_not_called()

    def test_raw_fence_failure_still_attempts_safe_policy_restoration(self):
        api = Mock(InstallError=install_device.InstallError)
        def command(argv, **kwargs):
            return Mock(returncode=1 if argv[0] == 'blockdev' else 0)
        with patch.object(I.subprocess, 'run', side_effect=command) as cleanup, \
             self.assertRaisesRegex(install_device.InstallError, 'cleanup requires inspection'):
            I.cleanup(api, '/dev/applesftl0', [], None, None, disk_verified=True, changed_policy=True)
        commands = [call.args[0] for call in cleanup.call_args_list]
        self.assertIn(['modprobe', '-r', 'apple-sftl'], commands)
        self.assertIn(['modprobe', 'apple-sftl'], commands)

    def test_host_saves_ios_handoff_without_a_gpt_start_request(self):
        from test_install_flow import InstallFlow
        helper = InstallFlow()
        self.addCleanup(helper.doCleanups)
        layout = {'disk': {'path': '/dev/applesftl0', 'table': 'lwvm', 'sector': 512,
                           'bytes': MEDIA, **self.device, 'table_sha256': L.digest(self.after)},
                  'partitions': [L.public_partition(p) for p in self.snapshot['partitions']], 'free': [],
                  'lwvm': {'primary': L.b64(self.after), 'backup': L.b64(self.after)}}
        report, store, prompts, output = helper.run_install(layout, ['', '', 'erase 3', ''])
        self.assertFalse(report['started'])
        self.assertTrue(report['boot_preparation_required'])
        self.assertEqual([call.args[0] for call in helper.status.call.call_args_list], ['status'])
        self.assertIn('Prepare its iOS launcher', output)
        self.assertEqual(json.loads((store / 'plan.json').read_text())['lwvm']['action'], 'replace')
        for name in ('__init__.py', 'lwvm.py', 'install_lwvm.py', 'install_device.py'):
            self.assertTrue((store / name).is_file())

    def test_matching_recovery_bundle_runs_without_installed_orchard(self):
        with tempfile.TemporaryDirectory() as directory:
            package = Path(directory) / 'orchard'
            package.mkdir()
            (package / '__init__.py').write_bytes(b'')
            for source in [install_remote.SCRIPT, *install_remote.SUPPORT_SCRIPTS]:
                (package / source.name).write_bytes(source.read_bytes())
            result = subprocess.run([sys.executable, '-I', '-S', '-c',
                                     'import runpy, sys; namespace = runpy.run_path(sys.argv[1], run_name="shipping-test"); '
                                     'print(namespace["lwvm"].MAGIC.hex())', str(package / 'install_device.py')],
                                    capture_output=True, text=True, timeout=15)
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertEqual(result.stdout.strip(), L.MAGIC.hex())

    def test_cleanup_still_fences_after_unmount_or_verified_unmap_failure(self):
        for failing_tool, mounts in (('umount', ['/target']), (I.MAPPER, [])):
            with self.subTest(failing_tool=failing_tool):
                commands = []
                def command(argv, **kwargs):
                    commands.append(argv)
                    if argv[0] == failing_tool:
                        raise FileNotFoundError('injected missing cleanup executable')
                    return Mock(returncode=0)
                api = Mock(InstallError=install_device.InstallError)
                provider = Mock()
                mapping = {'name': 'fixture-map', 'root_uuid': LINUX, 'mode': 'rw',
                           'extents': [{'start_sector': 0, 'sectors': 8192, 'physical_sector': 16384}]}
                with patch.object(I.subprocess, 'run', side_effect=command), \
                     self.assertRaisesRegex(install_device.InstallError, 'requires inspection'):
                    I.cleanup(api, '/dev/applesftl0', mounts, mapping, provider,
                              disk_verified=True, changed_policy=True)
                provider.close.assert_called_once_with()
                self.assertIn(['blockdev', '--setro', '/dev/applesftl0'], commands)
                self.assertFalse(any(argv[0] == 'modprobe' for argv in commands))
                if failing_tool == I.MAPPER:
                    self.assertEqual(commands[0], [I.MAPPER, 'unmap', '--disk', '/dev/applesftl0',
                                                  '--root', LINUX, '--mode', 'rw', '--extent', '0:8192:16384'])

    def test_cleanup_cancellation_still_fences_and_preserves_the_original_cancel(self):
        for cancelled_tool, mounts in (('umount', ['/target']), (I.MAPPER, [])):
            with self.subTest(cancelled_tool=cancelled_tool):
                commands = []
                cancelled = KeyboardInterrupt('injected cleanup cancellation')
                def command(argv, **kwargs):
                    commands.append(argv)
                    if argv[0] == cancelled_tool:
                        raise cancelled
                    return Mock(returncode=0)
                api = Mock(InstallError=install_device.InstallError)
                provider = Mock()
                mapping = {'name': 'fixture-map', 'root_uuid': LINUX, 'mode': 'rw',
                           'extents': [{'start_sector': 0, 'sectors': 8192, 'physical_sector': 16384}]}
                with patch.object(I.subprocess, 'run', side_effect=command), self.assertRaises(KeyboardInterrupt) as raised:
                    I.cleanup(api, '/dev/applesftl0', mounts, mapping, provider,
                              disk_verified=True, changed_policy=True)
                self.assertIs(raised.exception, cancelled)
                provider.close.assert_called_once_with()
                self.assertIn(['blockdev', '--setro', '/dev/applesftl0'], commands)
                self.assertFalse(any(argv[0] == 'modprobe' for argv in commands))


class MapperInteroperability(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        source = ROOT / 'boot/lwvm'
        compiler = shutil.which('cc')
        if not compiler or not (source / 'lwvm.c').is_file():
            raise unittest.SkipTest('requires the LwVM mapper dependency and a C compiler')
        cls.temporary = tempfile.TemporaryDirectory()
        cls.work = Path(cls.temporary.name)
        cls.binary = cls.work / 'orchard-lwvm'
        subprocess.run([compiler, '-std=c11', '-O2', '-Wall', '-Wextra', '-Werror',
                        str(source / 'lwvm.c'), str(source / 'main.c'), '-o', str(cls.binary)],
                       check=True, timeout=60)

    @classmethod
    def tearDownClass(cls):
        cls.temporary.cleanup()

    def test_planned_header_maps_identically_in_real_c_helper(self):
        for partial in (False, True):
            with self.subTest(partial=partial):
                before = fixture(partial=partial)
                after, root = L.allocate(before, before, proof(before), LINUX)
                image = self.work / 'media.img'
                with image.open('wb') as stream:
                    stream.truncate(MEDIA)
                    stream.write(after)
                    stream.seek(CHUNK - 4096)
                    stream.write(after)
                result = subprocess.run([str(self.binary), 'inspect', '--disk', str(image), '--root', LINUX, '--json'],
                                        capture_output=True, text=True, timeout=15)
                self.assertEqual(result.returncode, 0, result.stderr)
                receipt = json.loads(result.stdout)
                self.assertEqual(receipt['bytes'], root['bytes'])
                self.assertEqual(receipt['extents'], [
                    {'start_sector': p['logical'] // 512, 'sectors': p['bytes'] // 512,
                     'physical_sector': p['physical'] // 512} for p in root['extents']])


if __name__ == '__main__':
    unittest.main()
