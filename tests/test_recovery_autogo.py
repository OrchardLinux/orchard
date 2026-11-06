# SPDX-License-Identifier: GPL-2.0-only
import copy
import hashlib
import json
from pathlib import Path
import struct
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import Mock, patch
import zlib

from orchard.apple import cascadia, iboot32


def img3(payload, encrypted=True):
    tag = iboot32.TAG
    data = tag.pack(b'ATAD', 12 + len(payload), len(payload)) + payload
    if encrypted:
        data += tag.pack(b'GABK', 44, 32) + bytes(32)
    return iboot32.HEADER.pack(b'3gmI', len(data) + 20, len(data), 0, b'cebi') + data


def fixture():
    base, hook, target = 0x9ff00000, 0x50, 0x100
    raw = bytearray(0x300)
    struct.pack_into('<I', raw, 0x20, base)
    raw[hook:hook + 4] = iboot32.thumb_branch(base + hook, base + target)
    template = img3(bytes(32))
    selected = {'build': 'synthetic', 'device': 'Synthetic1,1', 'image_base': base,
                'autogo_hook': hook, 'autogo_trampoline': target, 'payload_address': 0x80000000,
                'maximum_payload_bytes': 1 << 20, 'template_sha256': hashlib.sha256(template).hexdigest(),
                'prepared_ibec': {'bytes': len(raw), 'sha256': hashlib.sha256(raw).hexdigest()}}
    return bytes(raw), template, selected


def payload():
    size = 0x8200
    data = bytearray(size)
    data[:4] = bytes.fromhex('c0000cf1')
    struct.pack_into('<I', data, 0x8024, 0x016f2818)
    values = [b'CB32', 1, cascadia.CONFIG.size, 0x80000000, 0x8000, 0x8080, 0x80,
              0x8100, 0x100, 0x88000000, size, 0x1000, 0x2000, 0x80000000, 0xa0000000,
              0x90000000, 0]
    record = cascadia.CONFIG.pack(*values)
    values[-1] = zlib.crc32(record[:-4])
    data[cascadia.CONFIG_AT:cascadia.CONFIG_AT + len(record)] = cascadia.CONFIG.pack(*values)
    return bytes(data)


class IMG3Tests(unittest.TestCase):
    def test_plaintext_wrapper_preserves_payload_and_removes_encryption(self):
        raw, template, selected = fixture()
        output = iboot32.pack_plain(template, raw)
        ident, tags = iboot32.tags(output)
        self.assertEqual(ident, b'cebi')
        self.assertEqual([name for name, _, _ in tags], [b'ATAD'])
        self.assertEqual(tags[0][1], raw)
        self.assertEqual(iboot32.HEADER.unpack_from(output)[3], 0)
        iboot32.verify_raw(raw, selected)

    def test_malformed_and_repeated_data_tags_are_refused(self):
        for bad in (b'', img3(b'abc')[:-1], img3(b'abc') + b'\0',
                    iboot32.HEADER.pack(b'3gmI', 44, 24, 0, b'cebi') +
                    iboot32.TAG.pack(b'ATAD', 12, 0) * 2):
            with self.subTest(length=len(bad)), self.assertRaises(ValueError):
                iboot32.tags(bad)

    def test_exact_native_hook_is_required_even_with_matching_input_hash(self):
        raw, _, selected = fixture()
        bad = bytearray(raw)
        bad[selected['autogo_hook']] ^= 1
        selected['prepared_ibec']['sha256'] = hashlib.sha256(bad).hexdigest()
        with self.assertRaisesRegex(ValueError, 'hook'):
            iboot32.verify_raw(bad, selected)

    def test_private_preparation_roundtrip_and_tamper_refusal(self):
        raw, template, selected = fixture()
        with TemporaryDirectory() as temporary, patch.object(iboot32, 'recipe', return_value=selected):
            root = Path(temporary)
            (root / 'raw').write_bytes(raw)
            (root / 'template').write_bytes(template)
            record = iboot32.prepare('synthetic', root / 'template', root / 'raw', root / 'out')
            stage, _, checked = iboot32.verified('synthetic', root / 'out')
            self.assertEqual(record, checked)
            self.assertEqual(record['device_operations'], 0)
            self.assertEqual((root / 'out').stat().st_mode & 0o777, 0o700)
            self.assertEqual(stage.stat().st_mode & 0o777, 0o600)
            stage.write_bytes(stage.read_bytes() + b'bad')
            with self.assertRaisesRegex(ValueError, 'receipt'):
                iboot32.verified('synthetic', root / 'out')
            with self.assertRaises(ValueError):
                iboot32.prepare('synthetic', root / 'template', root / 'raw', root / 'out')


    def test_prepared_cache_requires_private_owner_modes(self):
        raw, template, selected = fixture()
        with TemporaryDirectory() as temporary, patch.object(iboot32, 'recipe', return_value=selected):
            root = Path(temporary)
            (root / 'raw').write_bytes(raw)
            (root / 'template').write_bytes(template)
            iboot32.prepare('synthetic', root / 'template', root / 'raw', root / 'out')
            (root / 'out').chmod(0o755)
            with self.assertRaisesRegex(ValueError, 'private'):
                iboot32.verified('synthetic', root / 'out')
            (root / 'out').chmod(0o700)
            (root / 'out/ibec.img3').chmod(0o644)
            with self.assertRaises(ValueError):
                iboot32.verified('synthetic', root / 'out')

    def test_source_mismatch_stops_before_output(self):
        raw, template, selected = fixture()
        with TemporaryDirectory() as temporary, patch.object(iboot32, 'recipe', return_value=selected):
            root = Path(temporary)
            (root / 'raw').write_bytes(raw)
            (root / 'template').write_bytes(b'wrong')
            with self.assertRaises(ValueError):
                iboot32.prepare('synthetic', root / 'template', root / 'raw', root / 'out')
            self.assertFalse((root / 'out').exists())


class PayloadTests(unittest.TestCase):
    def test_complete_bundle_is_validated_without_rewriting_payload(self):
        data = payload()
        with TemporaryDirectory() as temporary:
            file = Path(temporary) / 'bundle'
            file.write_bytes(data)
            result = cascadia.bundle(file, hashlib.sha256(data).hexdigest(), {'payload_address': 0x80000000, 'maximum_payload_bytes': 1 << 20})
            self.assertEqual(result, data)
            with self.assertRaisesRegex(ValueError, 'manifest'):
                cascadia.bundle(file, '0' * 64, {'payload_address': 0x80000000, 'maximum_payload_bytes': 1 << 20})
            broken = bytearray(data)
            broken[cascadia.CONFIG_AT + 8] ^= 1
            file.write_bytes(broken)
            with self.assertRaisesRegex(ValueError, 'configuration'):
                cascadia.bundle(file, hashlib.sha256(broken).hexdigest(), {'payload_address': 0x80000000, 'maximum_payload_bytes': 1 << 20})


class TransportTests(unittest.TestCase):
    def setUp(self):
        self.directory = TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.root = Path(self.directory.name)
        environment = patch.dict('os.environ', {'ORCHARD_HOME': str(self.root)})
        environment.start()
        self.addCleanup(environment.stop)
        self.attempts = self.root / 'private/test-device/boot-attempts'
        self.data = payload()
        self.recovery = self.root / 'recovery'
        self.recovery.write_bytes(self.data)
        self.stage = self.root / 'stage'
        self.stage.write_bytes(b'private stage')
        self.device = {'mode': 'recovery', 'ecid': 123, 'chip': 0x8942, 'board': 10,
                       'location': 456, 'resumed': False}
        self.recipe = {'device': 'Synthetic1,1', 'build': 'test', 'payload_address': 0x80000000, 'maximum_payload_bytes': 1 << 20}
        self.source_receipt = {'ibec': {'sha256': hashlib.sha256(self.stage.read_bytes()).hexdigest()}}
        self.endpoint = {'mode': 'linux', 'ecid': 123, 'boot_id': 'current-boot',
                         'location': 789, 'product': 'Orchard Recovery', 'profile': 'synthetic',
                         'serial_port': '/dev/fake'}
        self.manifest = {'boot_method': 'recovery-autogo',
                         'assets': {'recovery': {'sha256': hashlib.sha256(self.data).hexdigest()}}}
        self.patches = [
            patch.object(cascadia.iboot32, 'verified', return_value=(self.stage, self.recipe, self.source_receipt)),
            patch.object(cascadia, 'tool', return_value='irecovery'),
            patch.object(cascadia, 'selected', return_value=({'pwned': True}, {'mode': 'recovery'})),
            patch.object(cascadia, 'DeviceState', return_value=Mock(root=self.root / 'private/test-device')),
            patch.object(cascadia.usb, 'recovery', return_value=self.endpoint),
            patch.object(cascadia.usb, 'devices', return_value=[self.endpoint]),
            patch.object(cascadia.subprocess, 'run', return_value=Mock(returncode=0)),
        ]
        self.mocks = [item.start() for item in self.patches]
        for item in self.patches:
            self.addCleanup(item.stop)

    def run_boot(self):
        cascadia.boot(self.device, {'recovery': self.recovery}, self.manifest, Mock(),
                      profile='synthetic', chip=0x8942, board=10, product='Orchard Recovery')

    def test_uploads_each_payload_once_and_binds_the_finished_boot(self):
        self.run_boot()
        calls = self.mocks[-1].call_args_list
        self.assertEqual(len(calls), 1)
        self.assertTrue(all(call.args[0][1:4] == ['-i', '0x7b', '-f'] for call in calls))
        self.assertEqual(self.device['boot_id'], 'current-boot')
        self.assertTrue(self.device['resumed'])
        receipts = list(self.attempts.glob('*.json'))
        self.assertEqual(len(receipts), 1)
        self.assertEqual(json.loads(receipts[0].read_text())['phase'], 'complete')
        self.assertFalse(list(self.attempts.glob('*-loader.bin')))

    def test_failed_upload_is_not_repeated(self):
        self.mocks[-1].return_value.returncode = 1
        with self.assertRaisesRegex(RuntimeError, 'outcome is unknown'):
            self.run_boot()
        self.assertEqual(self.mocks[-1].call_count, 1)
        receipt = next(self.attempts.glob('*.json'))
        self.assertEqual(json.loads(receipt.read_text())['phase'], 'recovery-upload-outcome-unknown')


    def test_pwned_dfu_uploads_prepared_ibec_once_before_payload(self):
        self.device['mode'] = 'dfu'
        sent = []
        def sending(argv, **kwargs):
            sent.append(Path(argv[-1]).read_bytes())
            return Mock(returncode=0)
        self.mocks[-1].side_effect = sending
        def ready(*args, **kwargs):
            self.device.update(mode='recovery', location=777)
        with patch.object(cascadia, 'wait_recovery', side_effect=ready) as wait:
            self.run_boot()
            wait.assert_called_once()
        self.assertEqual(sent, [b'private stage', self.data])
        self.assertEqual(self.mocks[-1].call_count, 2)

    def test_plain_dfu_is_refused_before_any_upload(self):
        self.device['mode'] = 'dfu'
        self.mocks[2].return_value = ({'pwned': False}, {'mode': 'dfu'})
        with self.assertRaisesRegex(ValueError, 'Plain DFU'):
            self.run_boot()
        self.mocks[-1].assert_not_called()

    def test_payload_hash_mismatch_prevents_mutations(self):
        self.recovery.write_bytes(self.data + b'changed')
        with self.assertRaisesRegex(ValueError, 'manifest'):
            self.run_boot()
        self.mocks[-1].assert_not_called()

    def test_another_linux_device_stops_completion(self):
        self.mocks[4].return_value = dict(self.endpoint, ecid=124)
        with self.assertRaisesRegex(RuntimeError, 'Another device'):
            self.run_boot()
        self.assertEqual(self.mocks[-1].call_count, 1)


    def test_concurrent_rebuild_does_not_replace_frozen_uploads(self):
        content = []
        def sending(argv, **kwargs):
            content.append(Path(argv[-1]).read_bytes())
            self.recovery.write_bytes(b'next build')
            self.stage.write_bytes(b'next stage')
            return Mock(returncode=0)
        self.mocks[-1].side_effect = sending
        self.run_boot()
        self.assertEqual(content, [self.data])
        self.assertFalse(list(self.attempts.glob('*.bin')))
        self.assertFalse(list(self.attempts.glob('*.img3')))

    def test_linux_observation_timeout_does_not_repeat_uploads(self):
        self.mocks[4].side_effect = ValueError('temporarily absent')
        with patch.object(cascadia.time, 'monotonic', side_effect=[0, 121]):
            with self.assertRaisesRegex(RuntimeError, 'did not enumerate'):
                self.run_boot()
        self.assertEqual(self.mocks[-1].call_count, 1)


    def test_persistent_recovery_remains_observation_only(self):
        self.mocks[5].return_value = [self.device]
        with patch.object(cascadia.time, 'monotonic', side_effect=[0, 121]):
            with self.assertRaisesRegex(RuntimeError, 'did not enumerate'):
                self.run_boot()
        self.assertEqual(self.mocks[-1].call_count, 1)

    def test_ambiguous_identity_stops_without_another_upload(self):
        self.mocks[5].return_value = [self.endpoint, dict(self.endpoint, ecid=124)]
        with self.assertRaisesRegex(RuntimeError, 'Ambiguous'):
            self.run_boot()
        self.assertEqual(self.mocks[-1].call_count, 1)

    def test_resumed_boot_does_not_prepare_or_upload(self):
        self.device.update(mode='linux', boot_id='current-boot')
        self.run_boot()
        self.mocks[0].assert_not_called()
        self.mocks[-1].assert_not_called()


class QueryTests(unittest.TestCase):
    def test_query_binds_all_identity_fields_and_rejects_duplicates(self):
        device = {'ecid': 123, 'chip': 0x8942, 'board': 10}
        text = 'ECID: 0x7b\nCPID: 0x8942\nBDID: 0x0a\nPRODUCT: Synthetic1,1\nMODE: Recovery\n'
        with patch.object(cascadia.subprocess, 'run', return_value=Mock(returncode=0, stdout=text)) as run:
            self.assertEqual(cascadia.query(device, 'irecovery')['mode'], 'recovery')
            self.assertEqual(run.call_args.kwargs['timeout'], 15)
        for changed in (text + 'ECID: 0x7b\n', text.replace('0x7b', '0x7c'), text.replace('0x8942', '0x8950')):
            with patch.object(cascadia.subprocess, 'run', return_value=Mock(returncode=0, stdout=changed)):
                with self.assertRaises(ValueError):
                    cascadia.query(device, 'irecovery')


class AdapterTests(unittest.TestCase):
    def test_generic_adapter_selects_the_declared_chain(self):
        from orchard import hardware
        from orchard.apple.device import Device
        p105 = Device(hardware.device('p105'))
        with patch.object(cascadia, 'boot') as boot:
            p105.boot({'ecid': 123}, {'recovery': Path('payload')},
                      {'boot_method': 'recovery-autogo', 'assets': {}}, Mock())
            self.assertEqual(boot.call_args.kwargs['profile'], 'p105')
        with patch('orchard.apple.boot.boot') as boot:
            j71b = Device(hardware.device('j71b'))
            j71b.boot({'ecid': 123}, {'recovery': Path('payload')}, {'assets': {}}, Mock())
            boot.assert_called_once()
        with patch.object(cascadia, 'boot') as boot:
            with self.assertRaises(ValueError):
                p105.boot({'ecid': 123}, {}, {'boot_method': 'checkm8-pongo', 'assets': {}}, Mock())
            boot.assert_not_called()

    def test_ram_boot_routes_cascadia_receipt_without_pongo(self):
        from orchard import boot
        with TemporaryDirectory() as temporary:
            output = Path(temporary).resolve()
            receipt = {'loader_format': 'cascadia', 'boards': ['p105'], 'files': {'linux.bin': 'a' * 64}}
            with patch.object(boot, 'verify_ram', return_value=receipt), patch.object(cascadia, 'boot_ram') as run:
                boot.boot_ram(output, yes=True)
                run.assert_called_once_with(output, receipt, yes=True)


if __name__ == '__main__':
    unittest.main()
