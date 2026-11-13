"""Boot identity publication from bounded ADT fixtures; no device activation."""
import importlib.util
import hashlib
import json
import os
from pathlib import Path
import re
import stat
import subprocess
import struct
import sys
import tempfile
import unittest
from unittest.mock import patch

from orchard.apple import adt

ROOT = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location('orchard_identity_test', ROOT/'runtime/recovery/identity.py')
identity = importlib.util.module_from_spec(spec)
with patch.dict(sys.modules, {'adt':adt}):
    spec.loader.exec_module(identity)
BOOT = '11111111-1111-4111-8111-111111111111'


def node(name, properties=(), children=()):
    entries = [('name',name.encode()+b'\0'), *properties]
    result = struct.pack('<II',len(entries),len(children))
    for key,value in entries:
        result += key.encode().ljust(32,b'\0') + struct.pack('<I',len(value)) + value + b'\0'*(-len(value)%4)
    return result + b''.join(children)


class Identity(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        for name,value in (('RUN',self.root/'run'),('DEV',self.root),('MTD',self.root/'mtd'),
                           ('BOOT_ID',self.root/'boot-id'),('COMPATIBLE',self.root/'compatible')):
            patched=patch.object(identity,name,value)
            patched.start()
            self.addCleanup(patched.stop)
        identity.BOOT_ID.write_text(BOOT)
        identity.COMPATIBLE.write_bytes(b'apple,j71b\0apple,t8010\0apple,arm-platform\0')
        blob=node('device-tree',children=[node('chosen',[('unique-chip-id',(123).to_bytes(8,'little'))])])
        identity.MTD.write_text(f'dev: size erasesize name\nmtd0: {len(blob):08x} 00001000 "adt"\n')
        (self.root/'mtd0').write_bytes(blob)

    def owner(self, path):
        result = self.original_lstat(path)
        if path.name == 'identity.json':
            values=list(result)
            values[4]=0  # Fixtures may run as a non-root macOS user.
            result=os.stat_result(values)
        return result

    def publish_fixture(self):
        with patch.object(identity.stat,'S_ISCHR',return_value=True):
            return identity.initialize()

    def test_published_record_matches_live_adt_and_is_boot_bound_and_private(self):
        self.original_lstat=Path.lstat
        with patch.object(Path,'lstat',lambda path: self.owner(path)):
            record=self.publish_fixture()
            self.assertEqual(record,{'profile':'j71b','ecid':123,'boot_id':BOOT})
            self.assertEqual(identity.cached(),record)
            self.assertEqual(stat.S_IMODE((identity.RUN/'identity.json').stat().st_mode),0o600)
            with self.assertRaisesRegex(ValueError,'already published'):
                identity.initialize()
            identity.BOOT_ID.write_text('22222222-2222-4222-8222-222222222222')
            with self.assertRaisesRegex(ValueError,'differs from this boot'):
                identity.cached()

    def test_missing_duplicate_and_truncated_adt_never_publish(self):
        original=identity.MTD.read_text()
        for table in ('',original+original):
            identity.MTD.write_text(table)
            with self.assertRaisesRegex(ValueError,'one live ADT'):
                self.publish_fixture()
            self.assertFalse((identity.RUN/'identity.json').exists())
        identity.MTD.write_text(original)
        (self.root/'mtd0').write_bytes(b'short')
        with self.assertRaisesRegex(ValueError,'truncated'):
            self.publish_fixture()
        self.assertFalse((identity.RUN/'identity.json').exists())

    def test_unprotected_or_linked_record_is_refused(self):
        identity.RUN.mkdir()
        path=identity.RUN/'identity.json'
        path.write_text('{}')
        path.chmod(0o644)
        with self.assertRaisesRegex(ValueError,'not protected'):
            identity.cached()
        path.unlink()
        path.symlink_to(self.root/'missing')
        with self.assertRaisesRegex(ValueError,'not protected'):
            identity.cached()

    def test_profile_comes_from_the_board_compatible(self):
        identity.COMPATIBLE.write_bytes(b'apple,j81\0apple,t7001\0')
        self.assertEqual(identity.profile(),'j81')
        identity.COMPATIBLE.write_bytes(b'raspberrypi,4-model-b\0')
        with self.assertRaisesRegex(ValueError,'Not an Apple board'):
            identity.profile()

    def test_gadget_publishes_identity_before_binding_usb(self):
        source=(ROOT/'boot/initramfs/recovery/usb-gadget').read_text()
        self.assertLess(source.index('identity.py'),source.index('/serialnumber'))
        self.assertLess(source.index('/serialnumber'),source.index('"$g/UDC"'))


class GadgetIdentity(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.tree = self.root / 'proc/device-tree'
        self.tree.mkdir(parents=True)
        (self.tree / 'compatible').write_bytes(b'apple,j71t\0apple,s8003\0')
        boot = self.root / 'proc/sys/kernel/random/boot_id'
        boot.parent.mkdir(parents=True)
        boot.write_text(BOOT)
        self.script = (ROOT / 'src/orchard/usb-identity').read_text().replace(
            '/proc/sys/kernel/random/boot_id', str(boot))

    def serial(self):
        return subprocess.run(['sh', '-ec', self.script], check=True, capture_output=True, text=True,
                              env={**os.environ, 'ORCHARD_DEVICE_TREE': str(self.tree)})

    def test_hardware_serial_is_hashed_stable_across_boots_and_distinct_per_unit(self):
        number = self.tree / 'serial-number'
        number.write_bytes(b'HardwareSerialA\0')
        first = self.serial()
        expected = hashlib.sha256(b'orchard-HardwareSerialA').hexdigest()[:12]
        self.assertEqual(first.stdout.strip(), f'BOARD:j71t ID:{expected}')
        self.assertNotIn('HardwareSerialA', first.stdout + first.stderr)
        (self.root / 'proc/sys/kernel/random/boot_id').write_text('22222222-2222-4222-8222-222222222222')
        self.assertEqual(self.serial().stdout, first.stdout)
        number.write_bytes(b'HardwareSerialB\0')
        self.assertNotEqual(self.serial().stdout, first.stdout)

    def test_missing_or_empty_serial_preserves_rescue_usb_with_a_warning(self):
        for value in (None, b'', b'\0'):
            if value is not None:
                (self.tree / 'serial-number').write_bytes(value)
            result = self.serial()
            self.assertEqual(result.stdout.strip(), f'BOOT:{BOOT} BOARD:j71t')
            self.assertIn('no device-tree serial-number', result.stderr)

    def test_installed_and_ram_gadgets_use_the_same_stable_serial_before_binding(self):
        (self.tree / 'serial-number').write_bytes(b'HardwareSerialA\0')
        expected = self.serial().stdout.strip()
        for relative in ('boot/initramfs/root/init', 'src/orchard/usb-session'):
            with self.subTest(path=relative):
                source = (ROOT / relative).read_text()
                if relative.endswith('/init'):
                    start = source.index('usb_gadget() {')
                    source = source[start:source.index('\nusb_gadget ||', start)] + '\nusb_gadget'
                else:
                    source = source[source.index('name=$(cat'):source.index('\nifconfig ')]
                for directory in ('sys/kernel/config/usb_gadget', 'sys/class/udc/usbdev',
                                  'newroot/etc', 'newroot/usr/lib', 'etc', 'usr/libexec/orchard'):
                    (self.root / directory).mkdir(parents=True, exist_ok=True)
                (self.root / 'newroot/etc/os-release').write_text('NAME="Linux"\n')
                (self.root / 'etc/hostname').write_text('ram-test\n')
                for helper in ('etc/orchard/usb-identity', 'usr/libexec/orchard/usb-identity'):
                    path = self.root / helper
                    path.parent.mkdir(parents=True, exist_ok=True)
                    path.write_text(self.script)
                source = re.sub(r'/(sys|newroot|etc|usr)/', lambda m: str(self.root) + m.group(), source)
                source = source.replace('mkdir "$g"', 'mkdir -p "$g/functions"')
                subprocess.run(['sh', '-ec', 'mount() { :; };\n' + source], check=True, capture_output=True,
                               env={**os.environ, 'ORCHARD_DEVICE_TREE': str(self.tree)})
                name = 'orchard-system' if relative.endswith('/init') else 'ram-test'
                gadget = self.root / 'sys/kernel/config/usb_gadget' / name
                self.assertEqual((gadget / 'strings/0x409/serialnumber').read_text().strip(), expected)
                self.assertEqual((gadget / 'UDC').read_text().strip(), 'usbdev')
                for function in ('ncm.usb0', 'acm.usb0'):
                    self.assertTrue((gadget / 'configs/c.1' / function).is_symlink())


if __name__ == '__main__':
    unittest.main()


class MacCurrentIdentity(unittest.TestCase):
    def test_installed_systems_use_the_mac_current_product_id_only_with_an_orchard_serial(self):
        from orchard.apple import linux_usb
        self.assertEqual(linux_usb.mode(0x1014, 'BOARD:j71b ID:0123456789ab'), 'system')
        self.assertEqual(linux_usb.mode(0x1014, 'BOOT:11111111-1111-4111-8111-111111111111 BOARD:j71b'), 'system')
        self.assertEqual(linux_usb.mode(0x1014, 'DWH12345678'), 'other')
        self.assertEqual(linux_usb.mode(0x1014, 'BOARD:j71b'), 'other')
        self.assertEqual(linux_usb.mode(0x4145, 'BOARD:j71b ID:0123456789ab'), 'system')
        self.assertEqual(linux_usb.mode(0x4144, ''), 'ram')

    def test_the_installed_gadget_asks_for_the_mac_current_identity(self):
        init = (ROOT / 'boot/initramfs/root/init').read_text()
        gadget = init[init.index('usb_gadget() {'):init.index('\nusb_gadget ||')]
        self.assertIn('echo 0x1014 > "$g/idProduct"', gadget)
        self.assertIn('echo 0xef > "$g/bDeviceClass"', gadget)

    def test_linux_hosts_get_access_to_the_mac_current_identity(self):
        from orchard import host_setup
        self.assertIn('ATTRS{idProduct}=="1014"', host_setup.usb_rules('plugdev'))
