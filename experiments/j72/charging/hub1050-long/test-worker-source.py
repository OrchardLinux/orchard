#!/usr/bin/env python3
"""Compile the actual source guard against simulated USB lifetime/state."""
from pathlib import Path
import subprocess,tempfile
b=Path(__file__).resolve().parent
s=(b/'module/j72_input_limit_trial1050_long.c').read_text()
a=s.index('static int source_guard(void)');z=s.index('\nstatic int lease_set',a)
head=r'''
#include <assert.h>
#include <stdbool.h>
#include <string.h>
#include <stdio.h>
#include "module/j72-limit-core.h"
#define READ_ONCE(x) (x)
#define USB_STATE_CONFIGURED 7
#define USB_SPEED_HIGH 3
struct dwc2_hsotg {struct {int state,speed;} gadget;};
struct device {void *driver; const char *name; void *data; bool available;};
static struct dwc2_hsotg hs;
static struct device dev,*udc;
static u64 source_lease_until,clock_ms;
static int locks,unlocks;
static u64 now_ms(void) {return clock_ms;}
static int device_trylock(struct device *d) {if(d->available){locks++;return 1;}return 0;}
static void device_unlock(struct device *d) {(void)d;unlocks++;}
static const char *dev_driver_string(struct device *d) {return d->name;}
static void *dev_get_drvdata(struct device *d) {return d->data;}
'''
end=r'''
static void reset(void) {
 hs.gadget.state=USB_STATE_CONFIGURED;hs.gadget.speed=USB_SPEED_HIGH;
 dev=(struct device){.driver=&dev,.name="dwc2",.data=&hs,.available=true};
 udc=&dev;clock_ms=1000;source_lease_until=3500;locks=unlocks=0;
}
int main(void) {
 reset();assert(source_guard()==0);
 reset();clock_ms=3500;assert(source_guard()==-ETIME && !locks);
 reset();source_lease_until=3501;assert(source_guard()==-ETIME && !locks);
 reset();udc=NULL;assert(source_guard()==-EBUSY && !locks);
 reset();dev.available=false;assert(source_guard()==-EBUSY && !locks);
 reset();dev.driver=NULL;assert(source_guard()==-ENOTCONN && locks==unlocks);
 reset();dev.name="other";assert(source_guard()==-ENOTCONN && locks==unlocks);
 reset();dev.data=NULL;assert(source_guard()==-ENOTCONN && locks==unlocks);
 reset();hs.gadget.state=0;assert(source_guard()==-ENOTCONN && locks==unlocks);
 reset();hs.gadget.speed=1;assert(source_guard()==-ENOTCONN && locks==unlocks);
 puts("PASS: 10 actual worker source-guard cases");
 return 0;
}
'''
with tempfile.TemporaryDirectory() as d:
 p=Path(d);(p/'test.c').write_text(head+s[a:z]+end)
 subprocess.run(['cc','-Wall','-Wextra','-Werror','-Wno-unused-function','-fsanitize=undefined,bounds','-I',str(b),str(p/'test.c'),'-o',str(p/'test')],check=True)
 subprocess.run([str(p/'test')],check=True)
