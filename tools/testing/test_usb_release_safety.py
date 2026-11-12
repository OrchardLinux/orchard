"""Execute applied charger and role-switch policy with host-only mocks."""
from pathlib import Path
import os
import unittest
from test_kernel_safety import execute, function
S=Path(os.environ['APPLIED_KERNEL_SOURCE'])
class USBReleaseSafety(unittest.TestCase):
    def test_reconnect_after_long_unplug_keeps_data_off_for_identification(self):
        text=(S/'drivers/power/supply/apple_tristar_charger.c').read_text()
        poll=function(text,'tristar_poll')
        start=poll.index('\tif (!status) {');end=start;depth=0
        for end in range(poll.index('{',start),len(poll)):
            depth+=(poll[end]=='{')-(poll[end]=='}')
            if not depth:break
        empty=poll[start:end+1].replace('goto again;','return;')
        execute(r'''
#include <assert.h>
#include <stdbool.h>
#include <stdint.h>
typedef uint64_t u64;typedef int64_t s64;
#define TRISTAR_HOLD_MS 1000
#define NSEC_PER_MSEC 1000000ULL
#define TRISTAR_EMPTY 0
#define POWER_SUPPLY_USB_TYPE_UNKNOWN 0
#define TRISTAR_CHG_HANDOFF_CODE 0
#define dev_info(...) ((void)0)
#define dev_dbg(...) ((void)0)
#define dev_crit(...) ((void)0)
struct tristar_charger {bool usb_on,present;u64 usb_off_ns;int phase,source_ma,usb_ma,usb_type;char source[4];void *dev;};
static int enables;
static void tristar_set_usb(struct tristar_charger *c,bool on){c->usb_on=on;enables+=on;}
static void tristar_no_source(struct tristar_charger *c,unsigned status,u64 now){}
static int tristar_set_code(struct tristar_charger *c,int code){return 0;}
'''+function(text,'tristar_release_usb')+'\nstatic void poll_empty(struct tristar_charger *chg,unsigned status,u64 now){\n'+empty+'\n}\n'+r'''
int main(void){
 struct tristar_charger c={.usb_on=false,.usb_off_ns=0};
 poll_empty(&c,0,5000*NSEC_PER_MSEC);
 assert(c.usb_off_ns==5000*NSEC_PER_MSEC);
 tristar_release_usb(&c,5010*NSEC_PER_MSEC,0x40);assert(!c.usb_on&&!enables);
 tristar_release_usb(&c,5999*NSEC_PER_MSEC,0x40);assert(!c.usb_on&&!enables);
 tristar_release_usb(&c,6000*NSEC_PER_MSEC,0x40);assert(c.usb_on&&enables==1);
 c.usb_on=false;poll_empty(&c,0,9000*NSEC_PER_MSEC);
 tristar_release_usb(&c,9001*NSEC_PER_MSEC,0x40);assert(!c.usb_on&&enables==1);
}
''')

    def test_manual_host_expires_but_accessory_host_does_not(self):
        text=(S/'drivers/usb/roles/apple-s8000-usb-role.c').read_text()
        execute(r'''
#include <assert.h>
#include <stdbool.h>
#include <errno.h>
#include <stddef.h>
#ifndef EUCLEAN
#define EUCLEAN 117
#endif
#define HZ 100
#define READ_ONCE(x) (x)
#define WRITE_ONCE(x,v) ((x)=(v))
#define dev_warn(...) ((void)0)
struct work_struct {int x;};struct delayed_work {struct work_struct work;};
enum usb_role {USB_ROLE_NONE,USB_ROLE_HOST,USB_ROLE_DEVICE};
struct apple_usb_role {bool dying,monitor_fault,accessory_claim,accessory,accessory_power_prepared;enum usb_role role;int lock;struct delayed_work revert_work;void *dev;struct usb_role_switch *sw;};
struct usb_role_switch {struct apple_usb_role *ur;};
static struct apple_usb_role ur;
static struct usb_role_switch sw={.ur=&ur};
static int system_wq,timer,delay,entered,entry_error;
#define container_of(p,t,m) ((t *)((char *)(p)-offsetof(t,m)))
#define to_delayed_work(w) ((struct delayed_work *)(w))
static void mutex_lock(int *p){assert(!*p);*p=1;}
static void mutex_unlock(int *p){assert(*p);*p=0;}
static struct apple_usb_role *usb_role_switch_get_drvdata(struct usb_role_switch *s){return s->ur;}
static void cancel_delayed_work(struct delayed_work *w){timer=0;}
static void mod_delayed_work(int q,struct delayed_work *w,int ticks){timer=1;delay=ticks;}
static int apple_usb_role_enter_host(struct apple_usb_role *u,bool accessory){return entry_error;}
static void apple_usb_role_enter_device(struct apple_usb_role *u){entered++;}
static int usb_role_switch_set_role(struct usb_role_switch *s,enum usb_role r);
'''+function(text,'apple_usb_role_revert')+'\n'+function(text,'apple_usb_role_set')+r'''
static int usb_role_switch_set_role(struct usb_role_switch *s,enum usb_role r){return apple_usb_role_set(s,r);}
int main(void){
 ur.role=USB_ROLE_DEVICE;ur.sw=&sw;
 assert(!apple_usb_role_set(&sw,USB_ROLE_HOST));assert(timer&&delay==60*HZ);
 apple_usb_role_revert(&ur.revert_work.work);assert(ur.role==USB_ROLE_DEVICE&&!timer&&entered==1);
 ur.accessory_claim=true;assert(!apple_usb_role_set(&sw,USB_ROLE_HOST));assert(ur.accessory&&!timer);
 apple_usb_role_revert(&ur.revert_work.work);assert(ur.role==USB_ROLE_HOST&&entered==1);
 apple_usb_role_set(&sw,USB_ROLE_DEVICE);apple_usb_role_set(&sw,USB_ROLE_HOST);assert(timer);
 ur.accessory_claim=true;apple_usb_role_set(&sw,USB_ROLE_HOST);assert(!timer&&ur.accessory);
 ur.dying=true;apple_usb_role_revert(&ur.revert_work.work);assert(ur.role==USB_ROLE_HOST);
 assert(apple_usb_role_set(&sw,USB_ROLE_DEVICE)==-ESHUTDOWN);
}
''')
