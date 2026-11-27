"""Execute the driver's control-stall and recovery paths with synthetic input."""
from pathlib import Path
import re
import shutil
import subprocess
import tempfile
import unittest

import kernel_source

DRIVER = 'drivers/input/touchscreen/apple_z2.c'


def function(source, name):
    return kernel_source.function(source, 'static ' + name)


class ControlStall(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cc = shutil.which('cc') or shutil.which('clang')
        if cc is None:
            raise unittest.SkipTest('A C compiler is required')
        cls.temp = tempfile.TemporaryDirectory()
        cls.addClassCleanup(cls.temp.cleanup)
        root = Path(cls.temp.name)
        src = kernel_source.read(DRIVER)
        constants = '\n'.join(re.findall(r'^#define APPLE_Z2_CONTROL_.*$', src, re.M))
        definitions = '\n'.join(function(src, name) for name in (
            'bool apple_z2_control_stalled(',
            'void apple_z2_control_rearm_work(',
            'void apple_z2_queue_control_recovery(',
        ))
        program = r'''
#include <assert.h>
#include <stdbool.h>
#include <stddef.h>
#include <stdint.h>
#include <limits.h>
#include <string.h>
#define APPLE_Z2_STATE_REPORTS_READY 7
#define msecs_to_jiffies(x) ((unsigned long)(x))
#define time_after(a,b) ((long)((unsigned long)(a)-(unsigned long)(b))>0)
#define time_after_eq(a,b) ((long)((unsigned long)(a)-(unsigned long)(b))>=0)
#define container_of(p,t,m) ((t *)((char *)(p)-offsetof(t,m)))
#define to_delayed_work(p) container_of(p,struct delayed_work,work)
#define dev_err(...) ((void)0)
#define dev_info(...) ((void)0)
#define dev_warn_ratelimited(...) ((void)0)
struct work_struct { int unused; };
struct delayed_work { struct work_struct work; };
struct spi_device { int irq; int dev; };
struct apple_z2_chip_info { bool recover_control_stall; };
struct apple_z2 {
    const struct apple_z2_chip_info *info;
    bool runtime_control_reply, runtime_frame_valid, runtime_queue_empty;
    unsigned control_reads, rearm_attempts;
    unsigned long control_since, control_last;
    int protocol_state, io_lock;
    bool stopping, recovering, booted, irq_on;
    struct spi_device *spidev;
    struct delayed_work rearm_work;
    void *reset_gpio;
};
static unsigned long jiffies, queued_delay;
static unsigned queued, masked, released, synchronised, powered_off, boots;
static int boot_error;
static struct apple_z2 *current;
static void mutex_lock(int *l) { assert(*l==0); *l=1; }
static void mutex_unlock(int *l) { assert(*l==1); *l=0; }
static void synchronize_irq(int irq) { (void)irq; assert(!current->io_lock); synchronised++; }
static void gpiod_direction_output(void *gpio, int value) {
    (void)gpio; assert(value==1 && synchronised>powered_off && !current->io_lock);
}
static void apple_z2_platform_power_off(struct apple_z2 *z) {
    assert(z==current && !z->io_lock); powered_off++;
}
static int apple_z2_boot(struct apple_z2 *z) {
    assert(!z->io_lock && !z->booted); boots++; z->booted=!boot_error;
    return boot_error;
}
static void schedule_delayed_work(struct delayed_work *w, unsigned long delay) {
    assert(w==&current->rearm_work && current->io_lock); queued++; queued_delay=delay;
}
static void disable_irq_nosync(int irq) { (void)irq; assert(current->io_lock); masked++; }
static void apple_z2_release_contacts(struct apple_z2 *z) { assert(z->io_lock); released++; }
'''
        program += constants + '\n' + definitions
        program += r'''
static void sample(struct apple_z2 *z, unsigned long now) { jiffies=now; }
int main(int argc, char **argv) {
    /* static: the stubs reach z through a global (gcc 13+ -Wdangling-pointer) */
    static const struct apple_z2_chip_info supported={true}, other={false};
    static struct spi_device spi={.irq=78};
    static struct apple_z2 z={.info=&supported,.protocol_state=APPLE_Z2_STATE_REPORTS_READY,
        .booted=true,.irq_on=true,.runtime_control_reply=true,.spidev=&spi};
    unsigned i;
    assert(argc==2); current=&z;
    if(!strcmp(argv[1],"stall")) {
        for(i=0;i<200;i++) { sample(&z,i*10); assert(!apple_z2_control_stalled(&z)); }
        sample(&z,1999); assert(!apple_z2_control_stalled(&z));
        sample(&z,2000); assert(apple_z2_control_stalled(&z));
        assert(z.control_reads==APPLE_Z2_CONTROL_STALL_READS);
    } else if(!strcmp(argv[1],"idle")) {
        z.runtime_control_reply=false;
        for(i=0;i<1000;i++) { sample(&z,i*10000UL); assert(!apple_z2_control_stalled(&z)); }
        assert(!z.control_reads);
        z.runtime_control_reply=true; z.runtime_queue_empty=true;
        for(i=0;i<1000;i++) { sample(&z,i*10000UL); assert(!apple_z2_control_stalled(&z)); }
        assert(!z.control_reads);
    } else if(!strcmp(argv[1],"sparse")) {
        for(i=0;i<1000;i++) { sample(&z,i*1000UL); assert(!apple_z2_control_stalled(&z)); }
        assert(z.control_reads==1);
    } else if(!strcmp(argv[1],"normal")) {
        for(i=0;i<1000;i++) {
            z.runtime_frame_valid=(i%16==0);
            sample(&z,i*20UL); assert(!apple_z2_control_stalled(&z));
        }
        z.control_reads=64; z.runtime_frame_valid=false; z.runtime_control_reply=false;
        assert(!apple_z2_control_stalled(&z) && !z.control_reads);
    } else if(!strcmp(argv[1],"scope")) {
        z.info=&other;
        for(i=0;i<1000;i++) { sample(&z,i*1000UL); assert(!apple_z2_control_stalled(&z)); }
        z.info=&supported; z.protocol_state=0;
        for(i=0;i<1000;i++) { sample(&z,i*1000UL); assert(!apple_z2_control_stalled(&z)); }
        assert(!z.control_reads);
    } else if(!strcmp(argv[1],"wrap")) {
        for(i=0;i<200;i++) { sample(&z,ULONG_MAX-999+i*10); assert(!apple_z2_control_stalled(&z)); }
        sample(&z,999); assert(!apple_z2_control_stalled(&z));
        sample(&z,1000); assert(apple_z2_control_stalled(&z));
    } else if(!strcmp(argv[1],"queue")) {
        mutex_lock(&z.io_lock);
        for(i=0;i<100;i++) apple_z2_queue_control_recovery(&z);
        mutex_unlock(&z.io_lock);
        assert(queued==1 && masked==1 && released==1 && z.recovering && !z.irq_on);
        assert(queued_delay==0);
        apple_z2_control_rearm_work(&z.rearm_work.work);
        assert(boots==1 && synchronised==1 && powered_off==1 && !z.recovering);
    } else if(!strcmp(argv[1],"stopped")) {
        z.stopping=true; z.recovering=true;
        mutex_lock(&z.io_lock); apple_z2_queue_control_recovery(&z); mutex_unlock(&z.io_lock);
        apple_z2_control_rearm_work(&z.rearm_work.work);
        assert(!boots && !queued && !masked && !released && !powered_off);
    } else if(!strcmp(argv[1],"bounded")) {
        z.recovering=true; boot_error=-5;
        for(i=0;i<3;i++) apple_z2_control_rearm_work(&z.rearm_work.work);
        assert(boots==3 && queued==2 && !z.recovering && queued_delay==1000);
        apple_z2_control_rearm_work(&z.rearm_work.work);
        assert(boots==3 && queued==2);
    } else { assert(0); }
    return 0;
}
'''
        (root / 'test.c').write_text(program)
        cls.exe = root / 'test'
        subprocess.run([cc, '-std=c11', '-Wall', '-Wextra', '-Werror', '-Wno-unused-parameter',
                        str(root / 'test.c'), '-o', str(cls.exe)], check=True, capture_output=True)

    def run_case(self, name):
        subprocess.run([str(self.exe), name], check=True, timeout=10)

    def test_sustained_control_loop_requires_both_duration_and_count(self): self.run_case('stall')
    def test_idle_and_empty_queues_do_not_restart(self): self.run_case('idle')
    def test_sparse_control_replies_do_not_accumulate_across_idle(self): self.run_case('sparse')
    def test_valid_frames_and_transient_controls_clear_the_run(self): self.run_case('normal')
    def test_other_devices_and_boot_initialization_are_unchanged(self): self.run_case('scope')
    def test_jiffies_wrap_is_handled(self): self.run_case('wrap')
    def test_one_restart_masks_irq_releases_contacts_and_runs_without_io_lock(self): self.run_case('queue')
    def test_stopping_does_not_queue_or_run_recovery(self): self.run_case('stopped')
    def test_boot_failures_have_a_finite_retry_budget(self): self.run_case('bounded')
