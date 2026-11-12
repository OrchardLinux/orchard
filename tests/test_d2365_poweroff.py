#!/usr/bin/env python3
"""Offline D2365 shutdown regression checks; never access a device.

Compile the driver's actual prepare/final functions against an in-memory bus.
These tests establish software contracts, not PMIC electrical behavior.
"""
from pathlib import Path
import difflib
import re
import runpy
import shutil
import subprocess
import tempfile
import unittest

DRIVER = 'drivers/power/reset/apple-d2365-poweroff.c'
ROOT = Path(__file__).resolve().parents[1]
J71B_DTS = 'arch/arm64/boot/dts/apple/t8010-j71b.dts'
J71B_UPSTREAM = 'cee9395acd8043be0644b25c34bfa86623f2b935'


def patched_source(path, every=False):
    """Read the added/context text of the last patch that changes this file, or of every one."""
    found = []
    for patch in sorted(p for layer in ('drivers', 'devices')
                        for p in (ROOT / 'kernel/patches' / layer).rglob('*.patch')):
        lines = patch.read_text().splitlines()
        marker = '+++ b/' + path
        if marker not in lines:
            continue
        output = []
        for line in lines[lines.index(marker) + 1:]:
            if line.startswith('diff --git ') or line == '-- ':
                break
            if line.startswith(('+', ' ')):
                output.append(line[1:])
        found.append('\n'.join(output) + '\n')
    if not found:
        raise AssertionError('No patch changes ' + path)
    # The power driver is a new file; GPIO assertions only need final hunks.
    return ''.join(found) if every else found[-1]


def driver_source():
    return patched_source(DRIVER)


def resolved_j71b_dts(extra_patches=()):
    """Apply the real platform series to a pinned upstream file, offline."""
    lock = dict(line.split('=', 1) for line in
                (ROOT / 'kernel/upstream.lock').read_text().splitlines())
    if lock['commit'] != J71B_UPSTREAM:
        raise AssertionError('Refresh the J71b DTS fixture for kernel/upstream.lock')
    series = runpy.run_path(str(ROOT / 'kernel/series.py'))['patches']('j71b')
    with tempfile.TemporaryDirectory() as temporary:
        source = Path(temporary) / J71B_DTS
        source.parent.mkdir(parents=True)
        shutil.copyfile(ROOT / 'tests/fixtures/d2365/t8010-j71b.dts', source)
        for patch in [*series, *extra_patches]:
            # Skip other files, but include deletions of this one as well.
            lines = patch.read_text().splitlines()
            if not {f'--- a/{J71B_DTS}', f'+++ b/{J71B_DTS}'}.intersection(lines):
                continue
            result = subprocess.run(['git', 'apply', f'--include={J71B_DTS}', str(patch)],
                                    cwd=temporary, capture_output=True, text=True, timeout=10)
            if result.returncode:
                raise AssertionError(f'{patch}: {result.stdout}{result.stderr}')
        return source.read_text()


class D2365DeviceTree(unittest.TestCase):
    def test_j71b_poweroff_node_has_supply(self):
        source = resolved_j71b_dts()
        self.assertIn('compatible = "apple,d2365-poweroff";', source)
        self.assertIn('power-supplies = <&tristar_charger>;', source)
        self.assertIn('simple-audio-card,name = "J71b Speakers";', source)

    def test_ipad6_boards_share_the_tdm_speaker_link(self):
        # Several patches add to the iPad 6 boards' shared file; the speaker link is in one of them.
        source = patched_source('arch/arm64/boot/dts/apple/t8010-ipad6.dtsi', every=True)
        self.assertIn('maxim,tdm;', source)
        self.assertIn('simple-audio-card,format = "dsp_a";', source)

    def with_later_patch(self, before, after):
        with tempfile.TemporaryDirectory() as temporary:
            patch = Path(temporary) / '9999-later-j71b-change.patch'
            patch.write_text(''.join(difflib.unified_diff(
                before.splitlines(keepends=True), after.splitlines(keepends=True),
                fromfile='a/' + J71B_DTS, tofile='b/' + J71B_DTS)))
            return resolved_j71b_dts([patch])

    def test_later_speaker_edit_preserves_poweroff_node(self):
        before = resolved_j71b_dts()
        after = before.replace('"J71b Speakers"', '"J71b Test Speakers"')
        self.assertNotEqual(before, after)
        # The last patch has only sound-card hunks, not the poweroff node.
        resolved = self.with_later_patch(before, after)
        self.assertEqual(resolved, after)
        self.assertIn('compatible = "apple,d2365-poweroff";', resolved)
        self.assertIn('power-supplies = <&tristar_charger>;', resolved)

    def test_later_deletion_is_not_hidden_by_earlier_added_lines(self):
        before = resolved_j71b_dts()
        supply = '\t\tpower-supplies = <&tristar_charger>;\n'
        self.assertIn(supply, before)
        after = before.replace(supply, '')
        resolved = self.with_later_patch(before, after)
        self.assertEqual(resolved, after)
        self.assertNotIn('power-supplies = <&tristar_charger>;', resolved)

    def test_unapplicable_later_patch_is_reported(self):
        source = resolved_j71b_dts()
        before = source.replace('"J71b Speakers"', '"Missing Speakers"')
        after = before.replace('"Missing Speakers"', '"Updated Speakers"')
        with self.assertRaisesRegex(AssertionError, '9999-later-j71b-change.patch'):
            self.with_later_patch(before, after)



class D2365Poweroff(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.source = driver_source()
        compiler = shutil.which('cc') or shutil.which('clang')
        if compiler is None:
            raise unittest.SkipTest('A host C compiler is required for shutdown checks')
        cls.temporary = tempfile.TemporaryDirectory()
        cls.addClassCleanup(cls.temporary.cleanup)
        directory = Path(cls.temporary.name)
        # Include the actual algorithm and data, not a Python reimplementation.
        algorithm = cls.source.split('static const struct of_device_id')[0]
        algorithm = re.sub(r'^#include.*$', '', algorithm, flags=re.M)
        fixture = ROOT / 'tests/fixtures/d2365'
        program = (fixture / 'mock.h').read_text() + algorithm
        program += (fixture / 'checks.c').read_text()
        source = directory / 'check.c'
        source.write_text(program)
        cls.binary = directory / 'check'
        result = subprocess.run([compiler, '-std=c11', '-Wall', '-Wextra',
                                 '-Wno-unused-parameter', '-Wno-sign-compare',
                                 str(source), '-o', str(cls.binary)],
                                capture_output=True, text=True, timeout=30)
        if result.returncode:
            raise AssertionError(result.stdout + result.stderr)

    def check(self, scenario):
        result = subprocess.run([str(self.binary), scenario], capture_output=True,
                                text=True, timeout=5)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)

    def test_standby_order_and_event_read_acknowledgement(self):
        self.check('order')

    def test_all_gpio_values_and_paired_bytes_preserved(self):
        self.check('gpio')

    def test_boot_state_preserves_only_verified_bits(self):
        self.check('boot')

    def test_every_nonzero_source_status_and_unknown_state_restarts(self):
        self.check('sources')

    def test_source_changes_before_final_request(self):
        self.check('changes')

    def test_each_transaction_failure_prevents_successful_standby(self):
        self.check('failures')

    def test_normal_restart_only_acknowledges_events_and_clears_boot_stage(self):
        self.check('restart')

    def test_final_callback_never_touches_bus_or_sleeps(self):
        self.check('final')

    def test_driver_enabled_in_both_platform_configs(self):
        for platform in ('apple-16k', 'apple-4k'):
            config = (ROOT / 'kernel/configs' / platform / 'config').read_text()
            self.assertIn('CONFIG_POWER_RESET_APPLE_D2365=y\n', config)

    def test_both_prepare_and_atomic_final_handlers_are_registered(self):
        for mode, callback in (
                ('SYS_OFF_MODE_RESTART_PREPARE', 'apple_d2365_restart_prepare'),
                ('SYS_OFF_MODE_POWER_OFF_PREPARE', 'apple_d2365_poweroff_prepare'),
                ('SYS_OFF_MODE_POWER_OFF', 'apple_d2365_poweroff_final')):
            self.assertRegex(self.source,
                             r'devm_register_sys_off_handler\(dev,\s*' + mode +
                             r',\s*SYS_OFF_PRIO_DEFAULT,\s*' + callback + r',\s*pmic\)')
        final = self.source.split('static int apple_d2365_poweroff_final(')[1]
        final = final.split('\n}\n', 1)[0]
        self.assertNotRegex(final, r'\b(?:regmap_\w+|msleep|usleep_range|mdelay)\s*\(')


class D2365EventQuiesce(unittest.TestCase):
    def test_shutdown_drains_irq_and_retry_after_closing_gate(self):
        # Every patch of the driver: later ones (Smart Cover wake) change other parts.
        post = patched_source('drivers/gpio/gpio-apple-d2365.c', every=True)
        start = post.index('void apple_d2365_gpio_quiesce(')
        shutdown = post[start:post.index('\n}', start) + 2]
        self.assertNotIn('.shutdown = apple_pmic_gpio_shutdown', post)
        self.assertIn('if (!retry || gpio->shutting_down)', post)
        self.assertIn('!gpio->removing && !gpio->shutting_down &&', post)
        self.assertIn('if (gpio->removing || gpio->shutting_down)', post)
        self.assertRegex(post, r'if \(gpio->shutting_down\) \{\s*'
                              r'mutex_unlock\(&gpio->irq_lock\);\s*return;')
        self.assertNotRegex(re.sub(r'/\*.*?\*/', '', shutdown, flags=re.S),
                            r'\b(?:free_irq|regmap_\w+|apple_pmic_event_sync)\s*\(')
        compiler = shutil.which('cc') or shutil.which('clang')
        if compiler is None:
            self.skipTest('A host C compiler is required for shutdown checks')
        # Compile the actual new shutdown callback. The helper assertions verify
        # lock ownership, closing the gate before holding IRQ, and drain order.
        program = r'''
#include <assert.h>
#include <stdbool.h>
struct apple_pmic_gpio {
    void *domain;
    bool shutting_down;
    int irq_lock, parent_irq, irq_retry;
};
struct device { struct apple_pmic_gpio *data; };
static unsigned step;
static struct apple_pmic_gpio *dev_get_drvdata(struct device *p)
{ return p->data; }
static void mutex_lock(int *lock) { assert(step++ == 0 && !*lock); *lock = 1; }
static void apple_pmic_irq_hold(struct apple_pmic_gpio *gpio, bool retry)
{ assert(step++ == 1 && gpio->irq_lock && gpio->shutting_down && !retry); }
static void mutex_unlock(int *lock) { assert(step++ == 2 && *lock); *lock = 0; }
static void synchronize_irq(int irq) { assert(step++ == 3 && irq == 7); }
static void cancel_delayed_work_sync(int *work) { assert(step++ == 4); }
'''
        program += shutdown + r'''
int main(void)
{
    struct apple_pmic_gpio gpio = { .parent_irq = 7 };
    struct device dev = { &gpio };
    apple_d2365_gpio_quiesce(&dev);
    assert(!step && !gpio.shutting_down);
    gpio.domain = &gpio;
    apple_d2365_gpio_quiesce(&dev);
    assert(step == 5 && gpio.shutting_down && !gpio.irq_lock);
    return 0;
}
'''
        with tempfile.TemporaryDirectory() as temporary:
            source = Path(temporary) / 'check.c'
            binary = Path(temporary) / 'check'
            source.write_text(program)
            subprocess.run([compiler, '-std=c11', str(source), '-o', str(binary)],
                           check=True, capture_output=True, text=True, timeout=30)
            subprocess.run([str(binary)], check=True, capture_output=True,
                           text=True, timeout=5)


class D2365Providers(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        compiler = shutil.which('cc') or shutil.which('clang')
        if compiler is None:
            raise unittest.SkipTest('A host C compiler is required for provider checks')
        cls.temporary = tempfile.TemporaryDirectory()
        cls.addClassCleanup(cls.temporary.cleanup)
        directory = Path(cls.temporary.name)
        source = driver_source()
        helpers = source[source.index('static void apple_d2365_device_put('):
                         source.index('static struct platform_driver apple_d2365_poweroff_driver')]
        fixture = (ROOT / 'tests/fixtures/d2365/probe.c').read_text()
        program = directory / 'probe.c'
        program.write_text(fixture.replace('/* DRIVER */', helpers))
        cls.binary = directory / 'probe'
        result = subprocess.run([compiler, '-std=c11', '-Wall', '-Wextra',
                                 '-Wno-unused-parameter', '-Wno-sign-compare',
                                 str(program), '-o', str(cls.binary)],
                                capture_output=True, text=True, timeout=30)
        if result.returncode:
            raise AssertionError(result.stdout + result.stderr)

    def check(self, scenario):
        result = subprocess.run([str(self.binary), scenario], capture_output=True,
                                text=True, timeout=5)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)

    def test_supply_is_a_distinct_same_pmic_child(self):
        self.check('topology')

    def test_probe_failures_release_provider_supply_and_handler_references(self):
        self.check('failures')

    def test_provider_readiness_rejects_already_unbinding_suppliers(self):
        self.check('lifetime')


if __name__ == '__main__':
    unittest.main()
