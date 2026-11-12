"""Execute kernel entry points with host MMIO/OS mocks (no device access)."""
from pathlib import Path
import os
import subprocess
import tempfile
import unittest
def function(text, name):
    start = text.rfind('\n', 0, text.index(name + '(')) + 1
    opening = text.index('{', start)
    depth, end = 1, opening + 1
    while depth:
        depth += (text[end] == '{') - (text[end] == '}')
        end += 1
    return text[start:end]

def execute(code):
    with tempfile.TemporaryDirectory() as tmp:
        p=Path(tmp)/'test.c';p.write_text(code)
        subprocess.run(['cc','-std=gnu11','-Wall','-Werror','-Wno-unused-parameter',
                        str(p),'-o',str(p.with_suffix(''))],check=True)
        subprocess.run([str(p.with_suffix(''))],check=True)

# Point at the fully applied release kernel tree, never a hand-made struct fixture.
S=Path(os.environ['APPLIED_KERNEL_SOURCE'])
def source(n): return (S/n).read_text()
class KernelSafety(unittest.TestCase):
    def test_N1_probe_initializes_real_watchdog_then_restart(self):
        s=source('drivers/watchdog/apple_wdt.c')
        defines='\n'.join(x for x in s.splitlines() if x.startswith('#define APPLE_WDT_'))
        execute(r'''
#include <assert.h>
#include <stdbool.h>
#include <stdint.h>
#include <stdlib.h>
#include <stddef.h>
#include <errno.h>
typedef uint32_t u32;
#define BIT(n) (1U<<(n))
#define U32_MAX UINT32_MAX
#define GFP_KERNEL 0
#define WDOG_HW_RUNNING 1
#define REBOOT_WARM 1
#define REBOOT_SOFT 2
#define REBOOT_COLD 0
#define pr_emerg(...) ((void)0)
#define IS_ERR(p) false
#define PTR_ERR(p) (-1)
#define min(a,b) ((a)<(b)?(a):(b))
struct device { void *of_node; };
struct platform_device { struct device dev; void *data; };
struct watchdog_device {struct device *parent;void *ops,*info;unsigned max_hw_heartbeat_ms,timeout;unsigned long status;};
struct apple_wdt {struct watchdog_device wdd;unsigned char *regs;unsigned long clk_rate;};
struct clk {int x;};
static int apple_wdt_ops,apple_wdt_info,tethered,priority,writes,flushes,delay;
static struct clk clock_ref;
static struct watchdog_device *registered;
static unsigned char regs[64];
static int offsets[8],values[8];
static struct apple_wdt *to_apple_wdt(struct watchdog_device *w) {return (void *)((char*)w - offsetof(struct apple_wdt,wdd));}
static void *devm_kzalloc(struct device *d,size_t n,int f){return calloc(1,n);}
static void *devm_platform_ioremap_resource(struct platform_device *p,int i){return regs;}
static struct clk *devm_clk_get_enabled(struct device *d,void *n){return &clock_ref;}
static unsigned long clk_get_rate(struct clk *c){return 1000;}
static void platform_set_drvdata(struct platform_device *p,void *d){p->data=d;}
static int readl_relaxed(unsigned char *p){return 0;}
static int readl(unsigned char *p){flushes++;return 0;}
static void writel_relaxed(unsigned v,unsigned char *p){offsets[writes]=p-regs;values[writes++]=v;}
static void set_bit(int b,unsigned long *p){*p|=1UL<<b;}
static void watchdog_init_timeout(struct watchdog_device *w,int t,struct device *d){}
static void watchdog_stop_on_unregister(struct watchdog_device *w){}
static void watchdog_set_restart_priority(struct watchdog_device *w,int n){priority=n;}
static int devm_watchdog_register_device(struct device *d,struct watchdog_device *w){registered=w;return 0;}
static bool of_property_read_bool(void *np,const char *p){return np && tethered;}
static void mdelay(int ms){delay=ms;}
''' + defines+'\n'+function(s,'apple_wdt_set_timeout')+'\n'+function(s,'apple_wdt_probe')+'\n'+function(s,'apple_wdt_restart')+r'''
int main(void){
 struct platform_device p={.dev.of_node=&p};
 tethered=1;assert(!apple_wdt_probe(&p));
 assert(registered && registered->parent==&p.dev && priority==255);
 writes=0;assert(!apple_wdt_restart(registered,REBOOT_WARM,0));assert(!writes);
 assert(!apple_wdt_restart(registered,REBOOT_SOFT,0));assert(!writes);
 assert(!apple_wdt_restart(registered,REBOOT_COLD,0));
 assert(writes==3 && offsets[0]==APPLE_WDT_WD1_CTRL && values[0]==APPLE_WDT_CTRL_RESET_EN);
 assert(offsets[1]==APPLE_WDT_WD1_BITE_TIME && !values[1]);
 assert(offsets[2]==APPLE_WDT_WD1_CUR_TIME && !values[2] && flushes==1 && delay==150);
 free(p.data);tethered=0;assert(!apple_wdt_probe(&p));assert(priority==128);
 writes=0;apple_wdt_restart(registered,REBOOT_WARM,0);assert(writes==3);
 registered->parent=0;writes=0;apple_wdt_restart(registered,REBOOT_COLD,0);assert(writes==3);
 free(p.data);
}
''')

    def test_N2_each_pcie_failure_cold_resets_without_panic(self):
        body=function(source('drivers/pci/controller/pcie-apple-h9p.c'),'apple_h9p_s8003_shutdown')
        self.assertNotIn('panic(',body)
        self.assertNotIn('return apple_cold_reset(',body)
        execute(r'''
#include <assert.h>
#include <stdbool.h>
#include <stdint.h>
#include <string.h>
typedef uint32_t u32;
#define __iomem
#define BIT(n) (1U<<(n))
#define GENMASK(a,b) 0
#define READ_ONCE(x) (x)
#define H9P_NUM_PORTS 2
#define H9P_CFG_PORT_STRIDE 0
#define PCI_COMMAND 0
#define PCI_PRIMARY_BUS 0
#define S8003_RC_PORT_PWR(n) 0
#define S8003_RC_PORT_PWR_REFCLK_NOGATE 0
#define S8003_PORT_LINKSTS 0
#define S8003_PORT_LINKSTS_UP 1
#define S8003_PERST_TO_REFCLK_US 1
#define H9P_NVMMU_SART_CTRL 0
#define readl_poll_timeout(a,b,c,d,e) do {b=0;(void)b;}while(0)
#define dev_info(...) ((void)0)
struct device {void *of_node;};struct platform_device {struct device dev;};
struct apple_h9p_pcie {struct device *dev;bool ans_handoff_stopped;int late_work;unsigned enabled_ports;unsigned char *base_config,*base_rc,*base_port[2];void *perst[2];struct {bool ready;unsigned char *base;} nvmmu;};
static struct device dev;static struct apple_h9p_pcie pcie={.dev=&dev};
enum system_states { SYSTEM_RUNNING, SYSTEM_POWER_OFF, SYSTEM_RESTART };
static int system_state=SYSTEM_RUNNING;
static int kexec_in_progress=1,site,resets,powered_calls;
static unsigned offs;
static const char *reason;
static struct apple_h9p_pcie *apple_h9p_pcie_lookup(struct device *d){return &pcie;}
static bool of_device_is_compatible(void *n,const char *s){return true;}
static bool of_property_read_bool(void *n,const char *s){return !strcmp(s,"apple,ans-kexec-handoff");}
static void cancel_delayed_work_sync(int *w){}
static bool apple_s8003_port_powered(struct apple_h9p_pcie *p){return !(site==4 && ++powered_calls==2);}
static void writew(int v,unsigned char *p){}
static void h9p_rmw(unsigned char *p,int a,int b){}
static int gpiod_direction_output(void *p,int v){return site==2;}
static int apple_s8003_port_stop(struct apple_h9p_pcie *p,unsigned port){return site==3;}
static void udelay(int us){}
static void h9p_writel_flush(int v,unsigned char *p){}
static void apple_s8003_port_off(struct apple_h9p_pcie *p,unsigned port){offs|=BIT(port);}
static int gpiod_get_value_cansleep(void *p){return 1;}
static void apple_cold_reset(const char *s){resets++;reason=s;}
''' + body + r'''
int main(void){struct platform_device p;unsigned char regs[8];
 pcie.base_config=regs;pcie.base_rc=regs;pcie.base_port[0]=pcie.base_port[1]=regs;
 for(site=1;site<=4;site++){
  pcie.ans_handoff_stopped=site!=1;pcie.enabled_ports=1;powered_calls=0;resets=0;
  apple_h9p_s8003_shutdown(&p);assert(resets==1 && reason);
  assert(strstr(reason,site==1?"uncertified":site==2?"PERST":site==3?"LTSSM":"certificate"));
  assert(pcie.enabled_ports==1);
 }
 site=0;pcie.ans_handoff_stopped=true;pcie.enabled_ports=1;resets=0;
 apple_h9p_s8003_shutdown(&p);assert(!resets && !pcie.enabled_ports);
 kexec_in_progress=0;site=1;apple_h9p_s8003_shutdown(&p);assert(!resets);
 /* kexec keeps every port powered, the Wi-Fi port included. */
 kexec_in_progress=1;site=0;pcie.ans_handoff_stopped=true;pcie.enabled_ports=3;offs=0;resets=0;
 apple_h9p_s8003_shutdown(&p);assert(!resets && !pcie.enabled_ports && !offs);
 /* Restart: the same handoff, and the other ports powered off. */
 kexec_in_progress=0;system_state=SYSTEM_RESTART;pcie.enabled_ports=3;offs=0;
 apple_h9p_s8003_shutdown(&p);assert(!resets && !pcie.enabled_ports && offs==BIT(1));
 for(site=1;site<=4;site++){
  pcie.ans_handoff_stopped=site!=1;pcie.enabled_ports=3;powered_calls=0;resets=0;
  apple_h9p_s8003_shutdown(&p);assert(resets==1 && reason);
 }
 /* Power-off and halt leave the ports alone, as before. */
 site=0;system_state=SYSTEM_POWER_OFF;pcie.ans_handoff_stopped=true;pcie.enabled_ports=3;offs=0;resets=0;
 apple_h9p_s8003_shutdown(&p);assert(!resets && pcie.enabled_ports==3 && !offs);
}
''')

    def test_S1_second_sysfs_handover_succeeds_but_errors_survive(self):
        body=function(source('drivers/gpu/apple-g5p/teardown.inc'),'g5p_td_command')
        execute(r'''
#include <assert.h>
#include <string.h>
#include <errno.h>
struct g5p_diag {int unused;};
static int result;
#define sysfs_streq(a,b) (!strcmp(a,b))
static int g5p_td_capture(struct g5p_diag *g,const char *s){return result;}
static int g5p_td_final(struct g5p_diag *g,const char *s){return result;}
static int g5p_kq_run(struct g5p_diag *g,const char *s){return result;}
'''+body+r'''
int main(void){struct g5p_diag g;
 result=-EALREADY;assert(!g5p_td_command(&g,"td-kexec"));
 assert(g5p_td_command(&g,"td-final")==-EALREADY);
 result=-EIO;assert(g5p_td_command(&g,"td-kexec")==-EIO);
 result=0;assert(!g5p_td_command(&g,"td-kexec"));}
''')

    def test_S2_notifier_only_escalates_restart_not_halt_poweroff_or_retired(self):
        body=function(source('drivers/gpu/apple-g5p/teardown.inc'),'g5p_td_reboot_notify')
        execute(r'''
#include <assert.h>
#include <errno.h>
struct notifier_block {int x;};struct g5p_diag {void *dev;};
static struct g5p_diag gpu,*g5p_td_dev=&gpu;
static int result,resets;
#define READ_ONCE(x) (x)
#define SYS_RESTART 1
#define SYS_HALT 2
#define SYS_POWER_OFF 3
#define NOTIFY_DONE 0
#define dev_emerg(...) ((void)0)
static int g5p_kq_run(struct g5p_diag *g,const char *s){return result;}
static void apple_cold_reset(const char *s){resets++;}
'''+body+r'''
int main(void){result=-EBUSY;
 g5p_td_reboot_notify(0,SYS_HALT,0);g5p_td_reboot_notify(0,SYS_POWER_OFF,0);assert(!resets);
 g5p_td_reboot_notify(0,SYS_RESTART,0);assert(resets==1);
 result=-EALREADY;g5p_td_reboot_notify(0,SYS_RESTART,0);assert(resets==1);
 result=0;g5p_td_reboot_notify(0,SYS_RESTART,0);assert(resets==1);}
''')
        run=function(source('drivers/gpu/apple-g5p/teardown.inc'),'g5p_kq_run')
        retired=run[run.index('if (g5p_r8_is_retired())'):run.index('if (READ_ONCE(g->sleep_failed))')]
        self.assertIn('return -EALREADY;',retired)

    def test_S6_flush_precedes_dump_and_cold_restart(self):
        body=function(source('include/linux/soc/apple/cold-reset.h'),'apple_cold_reset')
        execute(r'''
#include <assert.h>
#include <stdbool.h>
static int step,reboot_mode;
#define pr_emerg(...) assert(step++==0)
#define KMSG_DUMP_PANIC 1
#define REBOOT_COLD 2
#define WRITE_ONCE(x,v) do {assert(step++==3);x=v;}while(0)
static void pr_flush(int ms,bool reset){assert(step++==1 && ms==1000 && reset);}
static void kmsg_dump_desc(int reason,const char *s){assert(step++==2);}
static void emergency_restart(void){assert(step++==4 && reboot_mode==REBOOT_COLD);}
'''+body+r'''
int main(void){apple_cold_reset("test");assert(step==5);}
''')

    def test_S4_no_incoming_probe_terminate_or_failed_put_on_the_incoming_kernel(self):
        s=source('drivers/media/platform/apple/vxd/apple-vxd-core.c')
        self.assertNotIn('apple_vxd_fw_probe_stop',s)
        self.assertNotIn('ProbeTerminate',source('drivers/media/platform/apple/vxd/apple-vxd-fw.c'))

    def test_S5_failed_shutdown_does_not_put_or_cross_kexec(self):
        body=function(source('drivers/media/platform/apple/vxd/apple-vxd-core.c'),'apple_vxd_shutdown')
        execute(r'''
#include <assert.h>
#include <stdbool.h>
struct variant {bool sleep_stop;};struct device {int x;};
struct platform_device {struct device dev;};
struct apple_vxd_dev {struct variant *variant;int watchdog_work,job_done_work;};
static struct variant variant={true};static struct apple_vxd_dev vxd={.variant=&variant};
static int result,kexec_in_progress,resets,puts;
static void *platform_get_drvdata(struct platform_device *p){return &vxd;}
static void cancel_delayed_work_sync(int *w){}
static void flush_work(int *w){}
static int apple_vxd_fw_shutdown_stop(struct apple_vxd_dev *v){return result;}
static int pm_runtime_force_suspend(struct device *d){puts++;return 0;}
static void apple_cold_reset(const char *s){resets++;}
#define dev_warn(...) ((void)0)
'''+body+r'''
int main(void){struct platform_device p;
 result=-1;kexec_in_progress=1;apple_vxd_shutdown(&p);assert(resets==1 && !puts);
 kexec_in_progress=0;apple_vxd_shutdown(&p);assert(resets==1 && !puts);
 result=0;kexec_in_progress=1;apple_vxd_shutdown(&p);assert(puts==1);
 variant.sleep_stop=false;apple_vxd_shutdown(&p);assert(puts==1);}
''')
        stop=function(source('drivers/media/platform/apple/vxd/apple-vxd-fw.c'),'apple_vxd_fw_shutdown_stop')
        failure=stop[stop.index('if (ret) {'):stop.index('apple_vxd_cmdbufs_fini')]
        self.assertIn('return ret;',failure)
        self.assertNotIn('pm_runtime_put',failure)
        self.assertNotIn('dma_free',failure)

if __name__=='__main__':unittest.main()
