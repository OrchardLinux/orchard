"""Host-only framing checks for the J71t PSCI loader variables."""
from pathlib import Path
import json
import subprocess
import sys
import tempfile
import unittest

ROOT = Path(__file__).resolve().parents[1]
TOOL = ROOT / 'boot/m1n1/psci-loader'


class PSCILoader(unittest.TestCase):
    def test_payload_and_no_overwrite(self):
        with tempfile.TemporaryDirectory() as tmp:
            loader, output = Path(tmp) / 'm1n1.bin', Path(tmp) / 'psci.bin'
            original = b'bare loader\0'
            loader.write_bytes(original)
            cmd = [sys.executable, str(TOOL), '--device', 'j71t', '--loader', str(loader),
                   '--output', str(output)]
            result = subprocess.run(cmd, capture_output=True, text=True)
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertEqual(output.read_bytes(), original + b'psci=1\n')
            self.assertEqual(loader.read_bytes(), original)
            self.assertNotEqual(subprocess.run(cmd, capture_output=True).returncode, 0)
            self.assertEqual(output.read_bytes(), original + b'psci=1\n')

    def test_reset_and_off_variables(self):
        cases = {('--system-reset', 'proxy'): b'psci=1\npsci_reset=proxy\n',
                 ('--system-reset', 'off'): b'psci=1\npsci_reset=0\n',
                 ('--system-reset', 'boot'): b'psci=1\n',
                 ('--no-psci',): b'psci=0\n'}
        with tempfile.TemporaryDirectory() as tmp:
            loader = Path(tmp) / 'm1n1.bin'
            loader.write_bytes(b'bare')
            for n, (extra, records) in enumerate(cases.items()):
                output = Path(tmp) / f'out{n}.bin'
                result = subprocess.run([sys.executable, str(TOOL), '--device', 'j71t', '--loader',
                                         str(loader), '--output', str(output), *extra],
                                        capture_output=True, text=True)
                self.assertEqual(result.returncode, 0, result.stderr)
                self.assertEqual(output.read_bytes(), b'bare' + records)
            both = subprocess.run([sys.executable, str(TOOL), '--device', 'j71t', '--loader', str(loader),
                                   '--output', str(Path(tmp) / 'x.bin'), '--no-psci', '--system-reset',
                                   'proxy'], capture_output=True)
            self.assertNotEqual(both.returncode, 0)

    def test_series_has_reset_patches(self):
        names = sorted(p.name for p in (ROOT / 'boot/m1n1/patches/orchard').glob('*.patch'))
        for prefix in ('0015-psci-', '0016-psci-Turn-the-resident-EL3-PSCI-on-by-default',
                       '0017-psci-Trap-EL1-WFI-WFE', '0018-psci-Implement-SYSTEM_RESET',
                       '0019-bootmgr-Boot-the-default-entry'):
            self.assertTrue(any(n.startswith(prefix) for n in names), prefix)

    def test_manifest_psci_record_keeps_logo_first(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            for name, data in {'loader': b'M1N1', 'dtb': b'DTB', 'kernel': b'KERNEL',
                               'initrd': b'INITRD'}.items():
                (root / name).write_bytes(data)
            logo = b'm1n1_logo_256128' + bytes(4 * (256 * 256 + 128 * 128))
            (root / 'logo').write_bytes(logo)
            records = b'chosen.bootargs=console=tty0\npsci=1\n'
            (root / 'profile.json').write_text(json.dumps({'bootargs': records.decode()}))
            result = subprocess.run([sys.executable, '-I', str(ROOT / 'src/orchard/image/assemble.py'),
                                     '--manifest', str(root / 'profile.json'), '--loader', str(root / 'loader'),
                                     '--logo', str(root / 'logo'), '--dtb', str(root / 'dtb'),
                                     '--kernel', str(root / 'kernel'), '--initramfs', str(root / 'initrd'),
                                     '--output', str(root / 'payload')], capture_output=True, text=True)
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertTrue((root / 'payload').read_bytes().startswith(b'M1N1' + logo + records + b'DTB'))

    def test_board_requires_explicit_opt_in(self):
        for device in ([], ['--device', 'j71b']):
            result = subprocess.run([sys.executable, str(TOOL), *device, '--loader', 'unused',
                                     '--output', 'unused'], capture_output=True)
            self.assertNotEqual(result.returncode, 0)


if __name__ == '__main__':
    unittest.main()
