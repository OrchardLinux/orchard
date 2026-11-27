"""32-bit boards on the device: the initrd finds a root on LwVM partitions, and the install's
last steps inside a root without a boot partition. No device."""
from pathlib import Path
import re
import subprocess
import tempfile
import unittest
from unittest import mock
import uuid

from install_files import install_device as D
from host_tables import DATA_ID, LINUX_ID, SYSTEM_ID, header

ROOT = Path(__file__).resolve().parents[1]


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
        with mock.patch.object(D, 'install_kernel', return_value={'files': 0}), mock.patch.object(D, 'emit'):
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
        # The system just installed starts next.
        self.assertEqual((root / 'boot/loader/loader.conf').read_text(), 'default omarchy-33333333-*\n')


if __name__ == '__main__':
    unittest.main()
