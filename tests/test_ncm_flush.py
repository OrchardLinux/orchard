"""Execute the actual NCM timer callback, including exhausted TX requests."""
import os
from pathlib import Path
import subprocess
import tempfile
import unittest
import re


def function(source, name):
    """Extract this production callback without depending on a charging topic."""
    start = re.search(r'^.*\b' + name + r'\([^;]*?\)\n\{', source, re.M).start()
    brace = source.index('{', start)
    depth, end = 1, brace + 1
    while depth:
        depth += (source[end] == '{') - (source[end] == '}')
        end += 1
    return source[start:end] + '\n'

class NcmFlush(unittest.TestCase):
    @unittest.skipUnless(os.environ.get('ORCHARD_KERNEL_SOURCE'), 'select prepared kernel with ORCHARD_KERNEL_SOURCE')
    def test_busy_flush_retries_after_interval_then_drains(self):
        source = (Path(os.environ['ORCHARD_KERNEL_SOURCE'])/'drivers/usb/gadget/function/f_ncm.c').read_text()
        callback = function(source, 'ncm_tx_timeout')
        c = r'''
#include <assert.h>
#include <stddef.h>
#include <stdio.h>
#define READ_ONCE(a) (a)
#define container_of(p,t,m) ((t*)((char*)(p)-offsetof(t,m)))
#define TX_TIMEOUT_NSECS 300000
#define NETDEV_TX_BUSY 1
#define NETDEV_TX_OK 0
struct hrtimer {unsigned long expiry;};
enum hrtimer_restart {HRTIMER_NORESTART,HRTIMER_RESTART};
struct net_device;
struct net_device_ops {int (*ndo_start_xmit)(void *,struct net_device *);};
struct net_device {struct net_device_ops *netdev_ops;};
struct f_ncm {struct hrtimer task_timer;struct net_device *netdev;};
static unsigned calls, free_requests, pending=1, submissions;
static unsigned long now;
static void hrtimer_forward_now(struct hrtimer *t,unsigned long d){assert(d==300000);t->expiry=now+d;}
static int xmit(void *skb,struct net_device *n){calls++;assert(!skb);if(!free_requests)return NETDEV_TX_BUSY;pending=0;submissions++;return NETDEV_TX_OK;}
'''+callback+r'''
int main(void){
 struct net_device_ops ops={.ndo_start_xmit=xmit};struct net_device net={.netdev_ops=&ops};
 struct f_ncm n={.netdev=&net};
 for(unsigned i=0;i<100;i++){
  assert(ncm_tx_timeout(&n.task_timer)==HRTIMER_RESTART);
  assert(pending&&n.task_timer.expiry==now+300000);now=n.task_timer.expiry;
 }
 free_requests=1;assert(ncm_tx_timeout(&n.task_timer)==HRTIMER_NORESTART);
 assert(!pending&&submissions==1&&calls==101);
 n.netdev=NULL;assert(ncm_tx_timeout(&n.task_timer)==HRTIMER_NORESTART&&calls==101);
 puts("PASS production NCM callback: repeated BUSY, future expiry, recovered request, successful drain and detached netdev");
}
'''
        with tempfile.TemporaryDirectory() as tmp:
            p=Path(tmp);(p/'test.c').write_text(c)
            subprocess.run(['cc','-Wall','-Werror','-fsanitize=address,undefined',str(p/'test.c'),'-o',str(p/'test')],check=True)
            subprocess.run([str(p/'test')],check=True)

if __name__=='__main__':unittest.main()
