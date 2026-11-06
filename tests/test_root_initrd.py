"""orchard's initrd finds the root by reading the GPT: the kernel's uevent has no PARTUUID."""
from pathlib import Path
import os
import re
import struct
import subprocess
import tempfile
import unittest
import uuid

INIT = Path(__file__).resolve().parents[1] / 'boot/initramfs/root/init'


def gpt(path, sector, guids):
    """A disk image with a GPT whose entries have GUIDS, entry array at LBA 2."""
    data = bytearray(sector * 40)
    header = bytearray(92)
    header[0:8] = b'EFI PART'
    struct.pack_into('<QII', header, 72, 2, 128, 128)
    data[sector:sector + 92] = header
    for index, guid in enumerate(guids):
        if guid:
            offset = 2 * sector + index * 128
            data[offset:offset + 16] = uuid.UUID('0fc63daf-8483-4772-8e79-3d69d8477de4').bytes_le
            data[offset + 16:offset + 32] = uuid.UUID(guid).bytes_le
    path.write_bytes(bytes(data))


class RootLookup(unittest.TestCase):
    def lookup(self, image, sector, number):
        function = re.search(r'^gpt_partuuid\(\) \{.*?^\}', INIT.read_text(), re.M | re.S).group(0)
        result = subprocess.run(['sh', '-c', function + '\ngpt_partuuid "$0" "$1" "$2"', str(image),
                                 str(sector), str(number)], capture_output=True, text=True)
        return result.stdout.strip() if result.returncode == 0 else None

    def test_every_entry_reads_as_its_partuuid_for_both_sector_sizes(self):
        guids = [str(uuid.uuid4()) for _ in range(3)]
        with tempfile.TemporaryDirectory() as directory:
            for sector in (512, 4096):
                image = Path(directory) / f'disk-{sector}'
                gpt(image, sector, [guids[0], None, guids[1], guids[2]])
                with self.subTest(sector=sector):
                    self.assertEqual([self.lookup(image, sector, n) for n in (1, 2, 3, 4)],
                                     [guids[0], None, guids[1], guids[2]])

    def test_a_disk_without_gpt_matches_nothing(self):
        with tempfile.TemporaryDirectory() as directory:
            image = Path(directory) / 'apfs'
            image.write_bytes(b'\0' * 4096 * 4)
            self.assertIsNone(self.lookup(image, 4096, 1))


class InstallHook(unittest.TestCase):
    def run_init(self, *, install, lwvm):
        """Run the init's setup and handoff with filesystem/command fixtures."""
        with tempfile.TemporaryDirectory() as directory:
            work = Path(directory)
            binary, runtime = work / 'bin', work / 'run'
            binary.mkdir()
            runtime.mkdir()
            trace = work / 'trace'
            for name in ('mount', 'ip', 'udhcpd', 'orchard-load-storage', 'switch_root'):
                path = binary / name
                path.write_text('#!/bin/sh\nprintf "%s\\n" "' + name + ' $*" >> "$TRACE"\n')
                path.chmod(0o755)
            busybox = binary / 'busybox'
            busybox.write_text('#!/bin/sh\n[ "$1" != sh ] || { shift; exec sh "$@"; }\n')
            busybox.chmod(0o755)
            for name in ('orchard-acm-rescue', 'dropbear', 'dropbearkey',
                         'recovery-authorize-key', 'recovery-start-ssh'):
                (binary / name).write_text('#!/bin/sh\necho serial-rescue >> "$TRACE"\n')
            if lwvm:
                (binary / 'orchard-lwvm').touch(mode=0o755)
            (work / 'modules').write_text('test-storage\n')
            (work / 'cmdline').write_text('' if install else 'root=PARTUUID=11111111-1111-4111-8111-111111111111\n')
            hook = work / 'install/run'
            if install:
                hook.parent.mkdir()
                hook.write_text('echo install-hook >> "$TRACE"\n'
                                'mount -t tmpfs tmpfs /run\n'
                                'mount -t devpts devpts /dev/pts\n'
                                'root=11111111-1111-4111-8111-111111111111\n')
            text = INIT.read_text()
            setup = text.split('# gpt_partuuid ')[0]
            setup = re.sub(r'^usb_gadget\(\) \{.*?^\}',
                           'usb_gadget() { echo installed-gadget >> "$TRACE"; }', setup, flags=re.M | re.S)
            setup = re.sub(r'^exec </dev/console.*$', '', setup, flags=re.M)
            handoff = text[text.index('moved=\n'):]
            # Rescue fixture children exit immediately; no real process is signalled.
            handoff = re.sub(r'^\[ -z .*?\|\| true$', ':', handoff, flags=re.M)
            script = setup + '\nwait\n' + handoff
            script = script.replace('export PATH=/bin', 'export PATH="$FIXTURE_BIN:$PATH"')
            paths = dict((
                    ('/etc/orchard/modules', str(work / 'modules')),
                    ('/proc/cmdline', str(work / 'cmdline')),
                    ('/install/run', str(hook)), ('/bin/', str(binary) + '/'),
                    ('/newroot', str(work / 'newroot')), ('/run', str(runtime))))
            script = re.sub('|'.join(re.escape(path) for path in paths),
                            lambda match: paths[match[0]], script)
            result = subprocess.run(['sh', '-c', script], stdin=subprocess.DEVNULL,
                                    capture_output=True, text=True, timeout=5,
                                    env={**os.environ, 'TRACE': str(trace), 'FIXTURE_BIN': str(binary)})
            self.assertEqual(result.returncode, 0, result.stderr)
            return trace.read_text().splitlines()

    def test_install_hook_sets_root_after_modules_and_owns_ram_and_usb(self):
        for lwvm in (False, True):
            with self.subTest(lwvm=lwvm):
                events = self.run_init(install=True, lwvm=lwvm)
                self.assertLess(events.index('orchard-load-storage test-storage'), events.index('install-hook'))
                self.assertEqual(sum(line.startswith('mount -t tmpfs') for line in events), 1)
                self.assertEqual(sum(line.startswith('mount -t devpts') for line in events), 1)
                self.assertNotIn('installed-gadget', events)
                self.assertNotIn('serial-rescue', events)
                self.assertFalse(any('mount --move' in line and '/run ' in line for line in events))
                self.assertTrue(events[-1].startswith('switch_root '))

    def test_ordinary_lwvm_root_keeps_early_rescue_and_moves_its_ram_state(self):
        events = self.run_init(install=False, lwvm=True)
        self.assertEqual(events.count('installed-gadget'), 1)
        self.assertEqual(events.count('serial-rescue'), 1)
        self.assertLess(events.index('installed-gadget'), events.index('orchard-load-storage test-storage'))
        self.assertEqual(sum('mount --move' in line and '/run ' in line for line in events), 1)


if __name__ == '__main__':
    unittest.main()
