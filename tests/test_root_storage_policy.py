"""Validate the initrd's explicit storage policy without loading a real module."""
import os
from pathlib import Path
import subprocess
import tempfile
import unittest

from orchard.hardware import ROOT


class StoragePolicy(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.work = Path(temporary.name)
        self.capture = self.work / 'arguments'
        probe = self.work / 'modprobe'
        probe.write_text('#!/bin/sh\nprintf "%s\\n" "$@" > "$CAPTURE"\n')
        probe.chmod(0o755)
        self.cmdline = self.work / 'cmdline'

    def invoke(self, commandline, module='apple-sftl'):
        self.capture.unlink(missing_ok=True)
        self.cmdline.write_text(commandline + '\n')
        shell = [os.environ['ORCHARD_TEST_BUSYBOX'], 'sh'] if os.environ.get('ORCHARD_TEST_BUSYBOX') else ['sh']
        return subprocess.run([*shell, str(ROOT / 'boot/initramfs/root/load-storage'), module, str(self.cmdline)],
                              env={**os.environ, 'PATH': str(self.work) + ':' + os.environ['PATH'],
                                   'CAPTURE': str(self.capture)}, capture_output=True, text=True)

    def test_rw_mount_option_keeps_the_default_backend_gate(self):
        result = self.invoke('root=PARTUUID=1234 rw quiet')
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(self.capture.read_text().splitlines(), ['apple-sftl'])

    def test_explicit_uint32_policy_reaches_modprobe_as_separate_arguments(self):
        result = self.invoke('apple_sftl.write_enable=1 apple_sftl.max_page_reads=4294967295 '
                             'apple_sftl.max_io_time_ms=4294967295')
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(self.capture.read_text().splitlines(), [
            'apple-sftl', 'write_enable=1', 'max_page_reads=4294967295', 'max_io_time_ms=4294967295'])

    def test_write_opt_in_requires_an_explicit_read_and_lifetime_budget(self):
        for tail in ('', ' apple_sftl.max_page_reads=1', ' apple_sftl.max_io_time_ms=1'):
            result = self.invoke('apple_sftl.write_enable=1' + tail)
            self.assertNotEqual(result.returncode, 0)
            self.assertFalse(self.capture.exists())

    def test_invalid_or_repeated_policy_never_loads_the_module(self):
        for argument in ('write_enable=true', 'write_enable=2', 'max_page_reads=0',
                         'max_io_time_ms=-1', 'max_page_reads=01', 'max_page_reads=4294967296',
                         'max_page_reads=9999999999999999999999', 'max_page_reads=1x',
                         'max_page_reads=*', 'unknown=1', 'max_page_reads', 'max_io_time_ms='):
            result = self.invoke('apple_sftl.' + argument)
            self.assertNotEqual(result.returncode, 0, argument)
            self.assertFalse(self.capture.exists(), argument)
        for argument in ('write_enable=0', 'max_page_reads=1', 'max_io_time_ms=1'):
            result = self.invoke(f'apple_sftl.{argument} apple-sftl.{argument}')
            self.assertNotEqual(result.returncode, 0, argument)
            self.assertFalse(self.capture.exists(), argument)

    def test_explicit_read_only_policy_and_other_storage_modules(self):
        result = self.invoke('apple-sftl.write_enable=0 apple-sftl.max_page_reads=100 apple-sftl.max_io_time_ms=200')
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(self.capture.read_text().splitlines(), [
            'apple-sftl', 'write_enable=0', 'max_page_reads=100', 'max_io_time_ms=200'])
        result = self.invoke('apple_sftl.write_enable=1', module='nvme')
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(self.capture.read_text().splitlines(), ['nvme'])

    def test_module_name_cannot_be_a_modprobe_option(self):
        result = self.invoke('', module='--remove')
        self.assertNotEqual(result.returncode, 0)
        self.assertFalse(self.capture.exists())
