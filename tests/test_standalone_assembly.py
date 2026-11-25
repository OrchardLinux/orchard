"""The assembly is m1n1's payload format: runnable without orchard, reproducible by hand."""
from pathlib import Path
import struct
import subprocess
import sys
import tempfile
import unittest

SCRIPT = Path(__file__).resolve().parents[1] / 'producers/assemble.py'


class StandaloneAssembly(unittest.TestCase):
    def test_by_path_without_orchard_equals_the_documented_bytes(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            parts = {'m1n1.bin': b'M1N1', 'a.dtb': b'DTB-A', 'b.dtb': b'DTB-B',
                     'Image.gz': b'KERNEL', 'initramfs.cpio.gz': b'INITRAMFS'}
            for name, data in parts.items():
                (root / name).write_bytes(data)
            # -I: no orchard package, no current directory on the path.
            subprocess.run([sys.executable, '-I', str(SCRIPT), '--bootargs', 'console=tty0 rdinit=/init',
                            '--loader', 'm1n1.bin', '--dtb', 'a.dtb', '--dtb', 'b.dtb',
                            '--kernel', 'Image.gz', '--initramfs', 'initramfs.cpio.gz',
                            '--output', 'linux.bin'], cwd=root, check=True, capture_output=True)
            by_hand = (b'M1N1' + b'chosen.bootargs=console=tty0 rdinit=/init\n' + b'DTB-A' + b'DTB-B'
                       + b'KERNEL' + b'm1n1_initramfs' + struct.pack('<I', 9) + b'INITRAMFS')
            self.assertEqual((root / 'linux.bin').read_bytes(), by_hand)

    def test_a_logo_comes_directly_after_m1n1_and_must_be_one(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            for name in ('m1n1.bin', 'a.dtb', 'Image.gz', 'initramfs.cpio.gz'):
                (root / name).write_bytes(name.encode())
            logo = b'm1n1_logo_256128' + bytes(4 * (256 * 256 + 128 * 128))
            (root / 'logo.bin').write_bytes(logo)
            (root / 'bad.bin').write_bytes(logo[:-1])
            argv = [sys.executable, '-I', str(SCRIPT), '--bootargs', 'x', '--loader', 'm1n1.bin',
                    '--dtb', 'a.dtb', '--kernel', 'Image.gz', '--initramfs', 'initramfs.cpio.gz']
            subprocess.run([*argv, '--logo', 'logo.bin', '--output', 'linux.bin'], cwd=root, check=True,
                           capture_output=True)
            self.assertTrue((root / 'linux.bin').read_bytes().startswith(b'm1n1.bin' + logo + b'chosen.bootargs=x\n'))
            bad = subprocess.run([*argv, '--logo', 'bad.bin', '--output', 'other.bin'], cwd=root,
                                 capture_output=True, text=True)
            self.assertNotEqual(bad.returncode, 0)
            self.assertIn('not an m1n1 logo payload', bad.stderr)

    def test_boot_arguments_are_one_line(self):
        with tempfile.TemporaryDirectory() as temporary:
            result = subprocess.run([sys.executable, '-I', str(SCRIPT), '--bootargs', 'a\nb',
                                     '--loader', 'x', '--dtb', 'x', '--kernel', 'x', '--initramfs', 'x',
                                     '--output', 'y'], cwd=temporary, capture_output=True, text=True)
            self.assertNotEqual(result.returncode, 0)
            self.assertIn('one line', result.stderr)


if __name__ == '__main__':
    unittest.main()
