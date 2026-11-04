"""A7 host selection and process ownership; no physical USB operations."""
from contextlib import nullcontext
import hashlib
import os
from pathlib import Path
import subprocess
import tempfile
import time
import unittest
from unittest.mock import Mock, patch

from orchard.apple import bootstrap, boot
from orchard import boot as ram_boot


class A7Bootstrap(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.device = dict(mode='dfu', chip=0x8960, board=0x12,
                           ecid=123, location='3-7.2')
        self.description = {'device': {'name': 'iPad Air Cellular'},
                            'match': {'dfu': {'chip': 0x8960, 'board': 0x12}},
                            'boot': {'bootstrap': 'checkra1n-a7'}}

    def test_selection_is_explicit_and_old_descriptors_keep_palera1n(self):
        self.assertEqual(bootstrap.name({}), 'palera1n')
        self.assertEqual(bootstrap.name(self.description), 'checkra1n-a7')
        with self.assertRaisesRegex(ValueError, 'Unknown Pongo'):
            bootstrap.name({'boot': {'bootstrap': 'untrusted-command'}})

    def test_host_tool_is_hash_checked_without_execution(self):
        tool = self.root / 'checkra1n-0.1337.3'
        tool.write_bytes(b'not executable machine code')
        tool.chmod(0o700)
        expected = hashlib.sha256(tool.read_bytes()).hexdigest()
        with patch.object(bootstrap.platform, 'system', return_value='Linux'), \
                patch.object(bootstrap.platform, 'machine', return_value='arm64'), \
                patch.object(bootstrap.shutil, 'which', return_value=str(tool)), \
                patch.dict(bootstrap.CHECKRA1N_SHA256, {'aarch64': expected}):
            self.assertEqual(bootstrap.checkra1n_a7(), str(tool))
            tool.write_bytes(b'changed')
            with self.assertRaisesRegex(ValueError, 'checksum mismatch'):
                bootstrap.checkra1n_a7()

    def test_missing_unsupported_and_local_tool(self):
        with patch.object(bootstrap.platform, 'system', return_value='Linux'), \
                patch.object(bootstrap.platform, 'machine', return_value='aarch64'), \
                patch.object(bootstrap.shutil, 'which', return_value=None), \
                patch.object(Path, 'home', return_value=self.root):
            with self.assertRaisesRegex(ValueError, 'pinned checkra1n'):
                bootstrap.checkra1n_a7()
            tool = self.root / '.local/bin/checkra1n-0.1337.3'
            tool.parent.mkdir(parents=True)
            tool.write_bytes(b'test tool')
            tool.chmod(0o600)
            with self.assertRaisesRegex(ValueError, 'pinned checkra1n'):
                bootstrap.checkra1n_a7()
            tool.chmod(0o700)
            with patch.dict(bootstrap.CHECKRA1N_SHA256,
                            {'aarch64': hashlib.sha256(tool.read_bytes()).hexdigest()}):
                self.assertEqual(bootstrap.checkra1n_a7(), str(tool))
        for system, machine in [('Darwin', 'arm64'), ('Linux', 'armv7l')]:
            with patch.object(bootstrap.platform, 'system', return_value=system), \
                    patch.object(bootstrap.platform, 'machine', return_value=machine), \
                    self.assertRaisesRegex(ValueError, 'Linux aarch64 or x86_64'):
                bootstrap.checkra1n_a7()

    def test_dynamic_pi_topology_accepts_root_and_onboard_hub_only(self):
        model = self.root / 'model'
        model.write_bytes(b'Raspberry Pi 4 Model B Rev 1.5\0')
        sysfs = self.root / 'sysfs'
        sysfs.mkdir()
        root_hub = self.root / 'usb3'
        def node(path, vendor, product):
            path.mkdir(parents=True)
            (path/'idVendor').write_text(vendor)
            (path/'idProduct').write_text(product)
        node(root_hub, '1d6b', '0002')
        node(root_hub/'3-7', '2109', '3431')
        node(root_hub/'3-7'/'3-7.2', '05ac', '1227')
        node(root_hub/'3-8', '1234', '5678')
        node(root_hub/'3-8'/'3-8.2', '05ac', '1227')
        for location, target in [('3-7', root_hub/'3-7'),
                                 ('3-7.2', root_hub/'3-7'/'3-7.2'),
                                 ('3-8.2', root_hub/'3-8'/'3-8.2')]:
            (sysfs/location).symlink_to(target)
        for location in ['3-7', '3-7.2']:
            bootstrap.require_direct_pi(location, root=sysfs, model_file=model)
        for location in ['3-8.2', '3-9', '../usb3', None]:
            with self.subTest(location=location), self.assertRaises(ValueError):
                bootstrap.require_direct_pi(location, root=sysfs, model_file=model)
        model.write_bytes(b'Other Linux host')
        bootstrap.require_direct_pi('not a sysfs location', root=sysfs, model_file=model)

    def test_a7_argv_once_and_no_port_reset_watcher(self):
        with patch.object(boot, 'checkra1n_a7', return_value='/pinned/checkra1n') as tool, \
                patch.object(boot, 'require_direct_pi') as topology, \
                patch.object(boot.usb, 'selected') as selected, \
                patch.object(boot.replug, 'port_control') as control, \
                patch.object(boot.replug, 'PortReset') as watcher, \
                patch.object(boot, 'run_process_group') as run, \
                patch.object(boot, 'wait_mode', return_value={'mode': 'pongo'}) as wait:
            result = boot.start_pongo(self.device, Path('/Pongo.bin'), Mock(name='log'), Mock(),
                                      chip=0x8960, board=0x12, product='', bootstrap='checkra1n-a7')
            self.assertEqual(result, {'mode': 'pongo'})
            tool.assert_called_once_with()
            topology.assert_called_once_with('3-7.2')
            selected.assert_called_once_with('3-7.2', 123, chip=0x8960, board=0x12, mode='dfu')
            self.assertEqual(run.call_args.args[0],
                             ['/pinned/checkra1n', '-E', '-p', '-v', '-n', '-k', '/Pongo.bin'])
            run.assert_called_once()
            wait.assert_called_once()
            control.assert_not_called()
            watcher.assert_not_called()
            run.side_effect = subprocess.TimeoutExpired('test', 120)
            wait.reset_mock()
            with self.assertRaisesRegex(RuntimeError, 'outcome unknown'):
                boot.start_pongo(self.device, '/Pongo.bin', Mock(), Mock(),
                                 chip=0x8960, board=0x12, product='', bootstrap='checkra1n-a7')
            self.assertEqual(run.call_count, 2)  # two separate requests, no retry
            wait.assert_not_called()

    def test_wrong_chip_and_bad_topology_fail_before_usb_or_process(self):
        with patch.object(boot, 'checkra1n_a7', return_value='/tool'), \
                patch.object(boot, 'require_direct_pi', side_effect=ValueError('external hub')), \
                patch.object(boot.usb, 'selected') as selected, \
                patch.object(boot, 'run_process_group') as run:
            for chip, message in [(0x8010, 'A7 CPID'), (0x8960, 'external hub')]:
                with self.subTest(chip=chip), self.assertRaisesRegex(ValueError, message):
                    boot.start_pongo(self.device, '/Pongo.bin', Mock(), Mock(),
                                     chip=chip, board=0x12, product='', bootstrap='checkra1n-a7')
            selected.assert_not_called()
            run.assert_not_called()

    def test_successful_parent_cannot_leave_an_active_bootstrap_child(self):
        pidfile = self.root / 'child.pid'
        boot.run_process_group(['sh', '-c', f'sleep 300 & echo $! > {pidfile}; exit 0'], timeout=5)
        child = int(pidfile.read_text())
        for _ in range(50):
            try:
                state = Path(f'/proc/{child}/stat').read_text().rsplit(')', 1)[1].split()[0]
            except (FileNotFoundError, ProcessLookupError):
                return
            if state == 'Z':
                return
            time.sleep(0.02)
        os.kill(child, 9)
        self.fail('child survived successful bootstrap parent exit')

    def test_ram_dispatch_uses_matching_descriptor_and_pongo_needs_no_tool(self):
        receipt = dict(boards=['j72'], files={'linux.bin': 'digest'})
        with patch.object(ram_boot, 'verify_ram', return_value=receipt), \
                patch('orchard.hardware.device', return_value=self.description), \
                patch('orchard.state.operation_lock', return_value=nullcontext()), \
                patch.object(boot.usb, 'sole_target', return_value=self.device), \
                patch.object(boot.usb, 'selected'), \
                patch.object(ram_boot, 'checkra1n_a7', return_value='/tool') as tool, \
                patch.object(ram_boot, 'palera1n', return_value='/palera') as palera, \
                patch.object(boot, 'start_pongo') as start, \
                patch.object(boot.pongo, 'upload') as upload, patch('builtins.print'):
            ram_boot.boot_ram(self.root, yes=True)
            self.assertEqual(start.call_args.kwargs['bootstrap'], 'checkra1n-a7')
            tool.assert_called_once()
            palera.assert_not_called()
            self.assertTrue((self.root/'checkra1n-a7.log').exists())
            start.reset_mock()
            tool.reset_mock()
            self.device['mode'] = 'pongo'
            ram_boot.boot_ram(self.root, yes=True)
            start.assert_not_called()
            tool.assert_not_called()
            self.assertEqual(upload.call_count, 2)
            self.device.update(mode='dfu', chip=0x8010)
            ram_boot.boot_ram(self.root, yes=True)
            self.assertNotIn('bootstrap', start.call_args.kwargs)
            palera.assert_called_once()
            tool.assert_not_called()
