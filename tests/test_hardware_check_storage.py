"""Exercise the hardware-check RAM project's storage scripts against fixture disks."""
from pathlib import Path
import subprocess
import tempfile
import unittest


EXAMPLE = Path(__file__).resolve().parents[1] / 'examples/hardware-check/rootfs/usr'


class HardwareCheckStorage(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.root = Path(temp.name)
        for directory in ('sys/class/block', 'dev', 'proc/device-tree', 'proc/self', 'run'):
            (self.root / directory).mkdir(parents=True)
        self.compatible = self.root / 'proc/device-tree/compatible'
        self.compatible.write_bytes(b'apple,p105ap\0apple,s5l8942x\0')
        self.mountinfo = self.root / 'proc/self/mountinfo'
        self.mountinfo.write_text('24 1 0:18 / /run rw - tmpfs tmpfs rw\n')

        # Map the absolute RAM-root paths to the fixture. Only blockdev's
        # kernel ioctls and the one-second clock tick need substitutes.
        for name in ('libexec/sftl-storage', 'bin/hardware-check'):
            source = (EXAMPLE / name).read_text()
            for path in ('/sys/class/block', '/proc/device-tree', '/proc/self/mountinfo', '/run/', '/dev/applesftl',
                         '/dev/nvme', '/dev/${disk##*/}'):
                source = source.replace(path, str(self.root) + path)
            source = source.replace('/usr/libexec/sftl-storage', str(self.root / 'sftl-storage'))
            (self.root / Path(name).name).write_text(source)

        self.shell = '''
blockdev() {
    case "$1" in
        --getro) cat "$2.ro" ;;
        --setro) [ -e "$2.ro" ] && { [ -e "$2.ignore" ] || echo 1 > "$2.ro"; } ;;
        *) return 1 ;;
    esac
}
sleep() { :; }
dd() { touch run/media-read; command dd "$@"; }
'''

    def disk(self, name, ro=0, data=b''):
        block = self.root / 'sys/class/block' / name
        block.mkdir()
        minor = name.removeprefix('applesftl0').removeprefix('p') or '0'
        (block / 'dev').write_text(f'253:{minor}\n')
        node = self.root / 'dev' / name
        node.write_bytes(data)
        node.with_suffix('.ro').write_text(f'{ro}\n')
        return node

    def run_shell(self, script):
        return subprocess.run(['sh', '-c', self.shell + script], cwd=self.root,
                              capture_output=True, text=True, timeout=15)

    def report(self):
        return self.run_shell('set -- storage; . ./hardware-check')

    def test_late_disk_and_partition_are_fenced(self):
        result = self.run_shell('''
set -eu
. ./sftl-storage
coldplug() { :; }
sleep() {
    case "$i" in
        0) [ ! -e run/storage-fence ]
           mkdir sys/class/block/applesftl0; echo 0 > dev/applesftl0.ro ;;
        1) [ -e run/storage-fence ]; [ "$(cat dev/applesftl0.ro)" = 1 ]
           mkdir sys/class/block/applesftl0p3 ;;
        2) [ ! -e run/storage-fence ]; echo 0 > dev/applesftl0p3.ro ;;
        3) [ -e run/storage-fence ] && [ "$(cat dev/applesftl0p3.ro)" = 1 ]; exit $? ;;
    esac
}
watch_sftl
''')
        self.assertEqual(result.returncode, 0, result.stderr)

    def test_report_waits_for_verified_readonly_and_reads_full_block(self):
        self.disk('applesftl0', ro=1, data=bytes(4096))
        partition = self.disk('applesftl0p1')
        partition.with_suffix('.ignore').touch()  # BLKROSET succeeds without taking effect.
        self.assertNotEqual(self.run_shell('. ./sftl-storage; sftl_readonly fence').returncode, 0)
        self.assertNotEqual(self.report().returncode, 0)
        partition.with_suffix('.ro').write_text('1\n')
        result = self.report()
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn('[PASS] read 4096 bytes from applesftl0', result.stdout)
        (self.root / 'dev/applesftl0').write_bytes(bytes(512))
        result = self.report()
        self.assertNotEqual(result.returncode, 0)
        self.assertIn('[FAIL] short or failed read', result.stdout)

    def test_missing_disk_fails_only_on_p105(self):
        result = self.report()
        self.assertNotEqual(result.returncode, 0)
        self.assertIn('[FAIL] P105 SFTL', result.stdout)
        self.compatible.write_bytes(b'apple,j71b\0')
        result = self.report()
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn('[SKIP] no NVMe or ANS1 storage', result.stdout)

    def test_mount_aliases_block_the_read_but_unrelated_mounts_do_not(self):
        self.disk('applesftl0', ro=1, data=bytes(4096))
        self.disk('applesftl0p1', ro=1)
        read = self.root / 'run/media-read'
        for number in ('253:0', '253:1'):
            with self.subTest(mounted=number):
                self.mountinfo.write_text(f'28 1 {number} / /ios ro - hfsplus /dev/disk/by-label/iOS ro\n')
                result = self.report()
                self.assertNotEqual(result.returncode, 0)
                self.assertIn('unmounted', result.stdout)
                self.assertFalse(read.exists())
        self.mountinfo.write_text('28 1 253:10 / /other ro - ext4 /dev/disk/by-label/other ro\n')
        self.assertEqual(self.report().returncode, 0)
        self.assertTrue(read.exists())

    def test_missing_or_invalid_mount_evidence_fails_before_io(self):
        self.disk('applesftl0', ro=1, data=bytes(4096))
        for contents in ('', 'bad mount data\n', None):
            with self.subTest(contents=contents):
                if contents is None:
                    self.mountinfo.unlink()
                else:
                    self.mountinfo.write_text(contents)
                self.assertNotEqual(self.report().returncode, 0)
                self.assertFalse((self.root / 'run/media-read').exists())
        self.mountinfo.write_text('24 1 0:18 / /run rw - tmpfs tmpfs rw\n')
        (self.root / 'sys/class/block/applesftl0/dev').unlink()
        self.assertNotEqual(self.report().returncode, 0)
        self.assertFalse((self.root / 'run/media-read').exists())

    def test_partition_registration_during_read_invalidates_the_probe(self):
        self.disk('applesftl0', ro=1, data=bytes(4096))
        self.shell += '''
dd() {
    mkdir sys/class/block/applesftl0p2
    echo 253:2 > sys/class/block/applesftl0p2/dev
    echo 1 > dev/applesftl0p2.ro
    command dd "$@"
}
'''
        result = self.report()
        self.assertNotEqual(result.returncode, 0)
        self.assertIn('state changed during the read', result.stdout)
