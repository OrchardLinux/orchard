"""install_device.py: the storage fence, and writing orchard's platform layer into a new root."""
import hashlib
import os
from pathlib import Path
import shutil
import subprocess
import tempfile
import unittest
from unittest.mock import patch

from orchard.image import platform
from install_files import install_device as D


class StorageFence(unittest.TestCase):
    def setUp(self):
        work = tempfile.TemporaryDirectory()
        self.addCleanup(work.cleanup)
        self.root = Path(work.name)
        for name, path in (('STORAGE_FENCE', 'storage-fence'), ('STORAGE_READY', 'storage-ready'),
                           ('STORAGE_FAILED', 'storage-failed')):
            patcher = patch.object(D, name, self.root / path)
            patcher.start()
            self.addCleanup(patcher.stop)

    def test_no_wait_outside_the_recovery(self):
        D.wait_for_storage(seconds=0)

    def test_ready_marker_ends_the_wait(self):
        (self.root / 'storage-fence').touch()
        (self.root / 'storage-ready').touch()
        D.wait_for_storage(seconds=0)

    def test_failure_or_timeout_is_storage_unavailable(self):
        (self.root / 'storage-fence').touch()
        with self.assertRaisesRegex(D.InstallError, 'not ready after 0 s') as caught:
            D.wait_for_storage(seconds=0)
        self.assertEqual(caught.exception.code, 'storage-unavailable')
        (self.root / 'storage-failed').write_text('nvme0 never became live\n')
        with self.assertRaisesRegex(D.InstallError, 'nvme0 never became live'):
            D.wait_for_storage(seconds=5)


class PlatformLayer(unittest.TestCase):
    def setUp(self):
        work = tempfile.TemporaryDirectory()
        self.addCleanup(work.cleanup)
        self.work = Path(work.name)
        self.root = self.work / 'root'
        (self.root / 'usr/lib/firmware').mkdir(parents=True)
        (self.root / 'usr/lib/firmware/brcm.bin').write_bytes(b'distro firmware')
        patcher = patch.object(D.os, 'fchown')  # tests do not run as root
        patcher.start()
        self.addCleanup(patcher.stop)

    def layer(self, files):
        path = self.work / 'layer.tar.gz'
        path.unlink(missing_ok=True)
        expanded, _ = platform.write({name: (data, 0o644) for name, data in files.items()}, path)
        data = path.read_bytes()
        return path, {'size': len(data), 'sha256': hashlib.sha256(data).hexdigest(), 'expanded_limit': expanded}

    def test_the_layer_lands_next_to_the_root_image(self):
        path, receipt = self.layer({'usr/lib/modules/6.18-orchard/vmlinuz': b'kernel',
                                    'usr/lib/modules/6.18-orchard/dtb/t8010-j71b.dtb': b'dtb',
                                    'usr/lib/firmware/regulatory.db': b'regdb',
                                    'usr/share/alsa/ucm2/Orchard/j71b/HiFi.conf': b'ucm',
                                    'usr/share/wireplumber/wireplumber.conf.d/50-orchard.conf': b'wp'})
        self.assertEqual(D.install_layer(path, self.root, **receipt), 5)
        self.assertEqual((self.root / 'usr/lib/modules/6.18-orchard/vmlinuz').read_bytes(), b'kernel')
        self.assertEqual((self.root / 'usr/lib/firmware/brcm.bin').read_bytes(), b'distro firmware')
        self.assertEqual(D.kernel_version(self.root), '6.18-orchard')
        self.assertEqual((self.root / 'usr/share/alsa/ucm2/Orchard/j71b/HiFi.conf').read_bytes(), b'ucm')
        self.assertEqual((self.root / 'usr/share/wireplumber/wireplumber.conf.d/50-orchard.conf').read_bytes(),
                         b'wp')

    def test_executables_keep_their_exec_bit_and_nothing_more(self):
        path = self.work / 'layer.tar.gz'
        expanded, _ = platform.write({'usr/lib/orchard/a-script': (b'#!/bin/sh\n', 0o755),
                                      'usr/lib/systemd/system/a-service.service': (b'[Unit]\n', 0o644),
                                      'usr/lib/orchard/setuid': (b'x', 0o4755)}, path)
        data = path.read_bytes()
        D.install_layer(path, self.root, size=len(data), sha256=hashlib.sha256(data).hexdigest(),
                        expanded_limit=expanded)
        mode = lambda name: (self.root / name).stat().st_mode & 0o7777
        self.assertEqual(mode('usr/lib/orchard/a-script'), 0o755)
        self.assertEqual(mode('usr/lib/systemd/system/a-service.service'), 0o644)
        self.assertEqual(mode('usr/lib/orchard/setuid'), 0o755)

    def test_a_file_the_root_image_already_has_is_refused(self):
        path, receipt = self.layer({'usr/lib/firmware/brcm.bin': b'other'})
        with self.assertRaisesRegex(D.InstallError, 'already has /usr/lib/firmware/brcm.bin'):
            D.install_layer(path, self.root, **receipt)
        self.assertEqual((self.root / 'usr/lib/firmware/brcm.bin').read_bytes(), b'distro firmware')

    def test_only_files_under_usr_lib_and_only_the_planned_bytes(self):
        path, receipt = self.layer({'etc/passwd': b'root::0:0::/:/bin/sh'})
        with self.assertRaisesRegex(D.InstallError, 'only files under /usr/lib'):
            D.install_layer(path, self.root, **receipt)
        path, receipt = self.layer({'usr/lib/modules/x/vmlinuz': b'kernel'})
        with self.assertRaises(D.InstallError) as caught:
            D.install_layer(path, self.root, **{**receipt, 'sha256': '0' * 64})
        self.assertEqual(caught.exception.code, 'layer-mismatch')
        with self.assertRaises(D.InstallError) as caught:
            D.install_layer(path, self.root, **{**receipt, 'expanded_limit': 1})
        self.assertEqual(caught.exception.code, 'stream-too-long')


PARTUUID = '4d6c1a2b-0000-4000-8000-000000000001'
ENTRY = {'id': 'omarchy', 'title': 'Omarchy', 'options': 'rootwait', 'devicetree': 't8010-j71b.dtb'}


class BootEntries(unittest.TestCase):
    """boot-entries: the installer's entries and every later kernel update's, one script."""
    def setUp(self):
        work = tempfile.TemporaryDirectory()
        self.addCleanup(work.cleanup)
        self.work = Path(work.name)
        self.root, self.boot = self.work / 'root', self.work / 'boot'
        self.boot.mkdir()
        (self.root / 'etc/orchard').mkdir(parents=True)
        (self.root / D.BOOT_CONFIG).write_text(D.boot_config(ENTRY, PARTUUID, 'aaaa0000-0000-4000-8000-000000000002'))

    def kernel(self, version, image=b'kernel', kept=None):
        source = self.root / 'usr/lib/modules' / version
        (source / 'dtb').mkdir(parents=True)
        for name, data in (('vmlinuz', image), ('initrd', b'initrd ' + version.encode()),
                           ('dtb/t8010-j71b.dtb', b'dtb'), ('dtb/t8010-j171.dtb', b'other'),
                           ('modules.dep', b'')):
            (source / name).write_bytes(data)
        if kept is not None:
            (source / '.orchard-kept').write_text(f'{kept}\n')
        return source

    def script(self, *args):
        return subprocess.run(['sh', str(D.BOOT_ENTRIES), '--root', str(self.root), '--boot', str(self.boot), *args],
                              capture_output=True, text=True, check=True).stdout

    def entries(self):
        return sorted(path.name for path in (self.boot / 'loader/entries').glob('*.conf'))

    def test_the_entry_names_the_kernel_initrd_and_board_dtb(self):
        self.kernel('7.3-orchard')
        D.sync_entries(self.root, self.boot)
        conf = (self.boot / 'loader/entries/omarchy-4d6c1a2b-7.3-orchard.conf').read_text()
        self.assertEqual(conf, 'title Omarchy\nversion 7.3-orchard\nsort-key omarchy\n'
                               'linux /omarchy-4d6c1a2b/7.3-orchard/linux\n'
                               'initrd /omarchy-4d6c1a2b/7.3-orchard/initrd\n'
                               'devicetree /omarchy-4d6c1a2b/7.3-orchard/t8010-j71b.dtb\n'
                               f'options root=PARTUUID={PARTUUID} rw rootwait\narchitecture aa64\n')
        self.assertEqual((self.boot / 'omarchy-4d6c1a2b/7.3-orchard/t8010-j71b.dtb').read_bytes(), b'dtb')
        self.assertEqual((self.boot / 'omarchy-4d6c1a2b/7.3-orchard/linux').read_bytes(), b'kernel')
        self.assertFalse((self.boot / 'omarchy-4d6c1a2b/7.3-orchard/t8010-j171.dtb').exists())
        self.assertEqual(D.check_entry(self.boot, PARTUUID, 't8010-j71b.dtb'), 'omarchy-4d6c1a2b-7.3-orchard.conf')

    def test_the_system_splash_goes_next_to_its_kernel(self):
        self.kernel('7.3-orchard')
        (self.root / 'usr/share/orchard').mkdir(parents=True)
        (self.root / 'usr/share/orchard/splash').write_bytes(b'OSPL-image')
        self.script('sync')
        self.assertEqual((self.boot / 'omarchy-4d6c1a2b/7.3-orchard/splash').read_bytes(), b'OSPL-image')

    def test_a_32_bit_zimage_is_an_arm_entry(self):
        self.kernel('6.12', image=bytes(0x24) + b'\x18\x28\x6f\x01' + bytes(16))
        self.script('sync')
        self.assertIn('architecture arm\n', (self.boot / 'loader/entries/omarchy-4d6c1a2b-6.12.conf').read_text())

    def test_gone_kernels_lose_their_entries_and_other_systems_keep_theirs(self):
        self.kernel('7.3-orchard')
        self.kernel('7.4-orchard')
        self.script('sync')
        self.assertEqual(self.entries(), ['omarchy-4d6c1a2b-7.3-orchard.conf', 'omarchy-4d6c1a2b-7.4-orchard.conf'])
        other = self.boot / 'loader/entries/omarchy-99999999-7.3-orchard.conf'
        other.write_text('linux /omarchy-99999999/7.3-orchard/linux\noptions root=PARTUUID=99999999-x rw\n')
        (self.boot / 'omarchy-99999999/7.3-orchard').mkdir(parents=True)
        (self.boot / 'omarchy-99999999/7.3-orchard/linux').write_bytes(b'theirs')
        # Same token, another root (a copied entry): not this system's either.
        copied = self.boot / 'loader/entries/omarchy-4d6c1a2b-6.0.conf'
        copied.write_text('version 6.0\nlinux /omarchy-4d6c1a2b/6.0/linux\noptions root=PARTUUID=elsewhere rw\n')
        shutil.rmtree(self.root / 'usr/lib/modules/7.3-orchard')
        output = self.script('sync')
        self.assertIn('removed the entry omarchy-4d6c1a2b-7.3-orchard.conf', output)
        self.assertEqual(self.entries(), ['omarchy-4d6c1a2b-6.0.conf', 'omarchy-4d6c1a2b-7.4-orchard.conf',
                                          'omarchy-99999999-7.3-orchard.conf'])
        self.assertFalse((self.boot / 'omarchy-4d6c1a2b/7.3-orchard').exists())
        self.assertEqual((self.boot / 'omarchy-99999999/7.3-orchard/linux').read_bytes(), b'theirs')

    def test_a_kernel_without_the_board_dtb_gets_no_entry(self):
        source = self.kernel('7.3-orchard')
        (source / 'dtb/t8010-j71b.dtb').unlink()
        self.assertIn('t8010-j71b.dtb is missing', self.script('sync'))
        self.assertFalse((self.boot / 'loader/entries').exists() and self.entries())

    def test_an_upgrade_keeps_the_previous_kernel_and_prunes_to_two(self):
        self.kernel('7.3-orchard')
        self.script('sync')
        # pacman: save before, then the old version's files go and the new one's come.
        self.script('save')
        shutil.rmtree(self.root / 'usr/lib/modules/7.3-orchard')
        self.kernel('7.4-orchard')
        self.assertIn('keeping 7.3-orchard as the fallback kernel', self.script('sync'))
        modules = self.root / 'usr/lib/modules'
        self.assertTrue((modules / '7.3-orchard/.orchard-kept').is_file())
        self.assertEqual((modules / '7.3-orchard/initrd').read_bytes(), b'initrd 7.3-orchard')
        self.assertFalse((modules / '.orchard-saved').exists())
        self.assertEqual(self.entries(), ['omarchy-4d6c1a2b-7.3-orchard.conf', 'omarchy-4d6c1a2b-7.4-orchard.conf'])
        # The next upgrade keeps 7.4 and drops 7.3, which this script kept.
        self.script('save')
        shutil.rmtree(modules / '7.4-orchard')
        self.kernel('7.5-orchard')
        self.script('sync')
        self.assertEqual(sorted(p.name for p in modules.iterdir()), ['7.4-orchard', '7.5-orchard'])
        self.assertEqual(self.entries(), ['omarchy-4d6c1a2b-7.4-orchard.conf', 'omarchy-4d6c1a2b-7.5-orchard.conf'])
        self.assertFalse((self.boot / 'omarchy-4d6c1a2b/7.3-orchard').exists())

    def test_the_same_version_replaced_in_place_is_not_kept_twice(self):
        self.kernel('7.3-orchard')
        self.script('save')
        (self.root / 'usr/lib/modules/7.3-orchard/vmlinuz').unlink()
        (self.root / 'usr/lib/modules/7.3-orchard/vmlinuz').write_bytes(b'rebuilt')
        self.script('sync')
        self.assertFalse((self.root / 'usr/lib/modules/7.3-orchard/.orchard-kept').exists())
        self.assertEqual((self.boot / 'omarchy-4d6c1a2b/7.3-orchard/linux').read_bytes(), b'rebuilt')

    def test_only_kept_kernels_are_ever_removed(self):
        self.kernel('7.1-orchard', kept=100)
        self.kernel('7.2-orchard', kept=200)
        self.kernel('7.3-orchard')
        self.kernel('7.4-orchard')
        self.script('sync')
        self.assertEqual(sorted(p.name for p in (self.root / 'usr/lib/modules').iterdir()),
                         ['7.3-orchard', '7.4-orchard'])
        self.kernel('7.1-orchard', kept=100)
        self.kernel('7.2-orchard', kept=200)
        shutil.rmtree(self.root / 'usr/lib/modules/7.4-orchard')
        self.script('sync')
        self.assertEqual(sorted(p.name for p in (self.root / 'usr/lib/modules').iterdir()),
                         ['7.2-orchard', '7.3-orchard'])

    def test_save_does_nothing_on_a_system_orchard_did_not_install(self):
        (self.root / D.BOOT_CONFIG).unlink()
        self.kernel('7.3-orchard')
        self.script('save')
        self.assertFalse((self.root / 'usr/lib/modules/.orchard-saved').exists())

    def test_default_names_this_system_and_keeps_the_other_lines(self):
        self.kernel('7.3-orchard')
        self.script('default')
        self.assertEqual((self.boot / 'loader/loader.conf').read_text(), 'default omarchy-4d6c1a2b-*\n')
        (self.boot / 'loader/loader.conf').write_text('timeout 3\ndefault other-*\n# note\n')
        self.script('default')
        self.assertEqual((self.boot / 'loader/loader.conf').read_text(),
                         'timeout 3\ndefault omarchy-4d6c1a2b-*\n# note\n')

    def test_a_config_without_a_root_is_refused(self):
        (self.root / D.BOOT_CONFIG).write_text('token=omarchy-4d6c1a2b\ndevicetree=t8010-j71b.dtb\noptions=rw\n')
        self.kernel('7.3-orchard')
        result = subprocess.run(['sh', str(D.BOOT_ENTRIES), '--root', str(self.root), '--boot', str(self.boot), 'sync'],
                                capture_output=True, text=True)
        self.assertEqual(result.returncode, 1)
        self.assertIn('options lack root=PARTUUID=', result.stderr)

    def test_the_config_holds_what_every_entry_needs(self):
        self.assertEqual((self.root / D.BOOT_CONFIG).read_text().splitlines()[1:], [
            'token=omarchy-4d6c1a2b', 'title=Omarchy', 'sort_key=omarchy', 'devicetree=t8010-j71b.dtb',
            f'options=root=PARTUUID={PARTUUID} rw rootwait', 'boot_partuuid=aaaa0000-0000-4000-8000-000000000002'])
        with self.assertRaises(D.InstallError):
            D.boot_config({**ENTRY, 'title': 'two\nlines'}, PARTUUID, None)


class RemoveOldEntries(unittest.TestCase):
    def test_entries_of_roots_that_are_gone_and_the_files_only_they_name(self):
        with tempfile.TemporaryDirectory() as directory:
            boot = Path(directory)
            (boot / 'loader/entries').mkdir(parents=True)
            (boot / 'omarchy-4d6c1a2b/7.3').mkdir(parents=True)
            (boot / 'omarchy-4d6c1a2b/7.3/linux').write_bytes(b'k')
            (boot / 'loader/entries/omarchy-4d6c1a2b-7.3.conf').write_text(
                f'linux /omarchy-4d6c1a2b/7.3/linux\noptions root=PARTUUID={PARTUUID} rw\n')
            other = boot / 'loader/entries/other.conf'
            other.write_text('linux /shared/linux\noptions root=PARTUUID=other\n')
            (boot / 'shared').mkdir()
            (boot / 'shared/linux').write_bytes(b'k')
            elsewhere = boot / 'loader/entries/by-label.conf'
            elsewhere.write_text('linux /shared/linux\noptions root=LABEL=other\n')
            self.assertEqual(D.remove_old_entries(boot, {'OTHER'}), ['omarchy-4d6c1a2b-7.3.conf'])
            self.assertFalse((boot / 'omarchy-4d6c1a2b').exists())
            self.assertTrue((boot / 'shared/linux').exists())
            # An entry an earlier install left for a root that is gone goes too; one named another way stays.
            self.assertEqual(D.remove_old_entries(boot, {'new-root'}), ['other.conf'])
            self.assertTrue(elsewhere.exists())
            self.assertTrue((boot / 'shared/linux').exists())


class Timezone(unittest.TestCase):
    def test_links_an_existing_zone_and_ignores_others(self):
        with tempfile.TemporaryDirectory() as d:
            root = Path(d)
            (root / 'etc').mkdir()
            (root / 'usr/share/zoneinfo/Asia').mkdir(parents=True)
            (root / 'usr/share/zoneinfo/Asia/Makassar').write_bytes(b'TZif')
            (root / 'etc/localtime').symlink_to('../usr/share/zoneinfo/UTC')
            self.assertEqual(D.set_timezone(root, 'Asia/Makassar'), 'Asia/Makassar')
            self.assertEqual(os.readlink(root / 'etc/localtime'), '../usr/share/zoneinfo/Asia/Makassar')
            for zone in (None, 'Mars/Olympus', '../../etc/passwd', 'Asia/../x'):
                self.assertIsNone(D.set_timezone(root, zone))
            self.assertEqual(os.readlink(root / 'etc/localtime'), '../usr/share/zoneinfo/Asia/Makassar')


if __name__ == '__main__':
    unittest.main()
