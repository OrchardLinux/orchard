"""Execute applied ANS shutdown paths with injected failures and no hardware."""
from pathlib import Path
import os
import unittest
from test_kernel_safety import execute, function
SOURCE=Path(os.environ['APPLIED_KERNEL_SOURCE'])
class NVMeShutdownSafety(unittest.TestCase):
    def test_shutdown_certificates_cold_reset_without_teardown_after_failure(self):
        text=(SOURCE/'drivers/nvme/host/pci.c').read_text()
        disable=function(text,'nvme_dev_disable_paired')
        shutdown=function(text,'nvme_shutdown')
        self.assertNotIn('panic(',disable+shutdown)
        execute(r'''
#include <assert.h>
#include <stdbool.h>
#include <errno.h>
#include <string.h>
#define dev_info(...) ((void)0)
#define dev_err(...) ((void)0)
enum nvme_ctrl_state { NVME_CTRL_LIVE, NVME_CTRL_RESETTING };
struct nvme_ctrl { int queue_count; void *admin_q; };
struct pci_dev {int x;};
struct nvme_dev {struct nvme_ctrl ctrl; void *dev,*dma_ops; int shutdown_lock,queues[1];};
enum system_states { SYSTEM_RUNNING, SYSTEM_POWER_OFF, SYSTEM_RESTART };
static int system_state=SYSTEM_RUNNING;
static int kexec_in_progress=1,nvme_pci_apple_ans_ops,site,resets,teardown,locked;
static const char *reason;
static struct pci_dev pci;
static struct nvme_dev dev={.ctrl={.queue_count=1},.dma_ops=&nvme_pci_apple_ans_ops};
static enum nvme_ctrl_state nvme_ctrl_state(struct nvme_ctrl *c){return NVME_CTRL_LIVE;}
static struct pci_dev *to_pci_dev(void *d){return &pci;}
static void mutex_lock(int *p){assert(!locked);locked=1;}
static void mutex_unlock(int *p){assert(locked);locked=0;}
static bool nvme_pci_ctrl_is_dead(struct nvme_dev *d){return site==2;}
static bool pci_is_enabled(struct pci_dev *p){return true;}
static int nvme_wait_freeze_timeout(struct nvme_ctrl *c){return site==1?0:1;}
static void apple_cold_reset(const char *why){resets++;reason=why;}
static int apple_ans_nvmmu_drain(void *d){return site==3?-EIO:0;}
static int nvme_disable_ctrl(struct nvme_ctrl *c,bool shutdown){return 0;}
static int nvme_apple_ans_handoff_disable(struct nvme_dev *d,int ret){return ret;}
static int apple_ans_handoff_stopped(void *d){return 0;}
static bool blk_queue_dying(void *q){return false;}
#define NOOP(name,type) static void name(type *p){}
NOOP(nvme_start_freeze,struct nvme_ctrl)
NOOP(nvme_unfreeze,struct nvme_ctrl)
NOOP(nvme_quiesce_io_queues,struct nvme_ctrl)
NOOP(nvme_sync_io_queues,struct nvme_ctrl)
NOOP(nvme_cancel_tagset,struct nvme_ctrl)
NOOP(nvme_cancel_admin_tagset,struct nvme_ctrl)
NOOP(nvme_unquiesce_io_queues,struct nvme_ctrl)
NOOP(nvme_unquiesce_admin_queue,struct nvme_ctrl)
static void nvme_delete_io_queues(struct nvme_dev *d){teardown++;}
static void nvme_suspend_io_queues(struct nvme_dev *d){teardown++;}
static void nvme_reap_pending_cqes(struct nvme_dev *d){teardown++;}
static void nvme_suspend_queue(struct nvme_dev *d,int n){teardown++;}
static void pci_free_irq_vectors(struct pci_dev *p){teardown++;}
static void pci_disable_device(struct pci_dev *p){teardown++;}
static void nvme_poll_irqdisable(int *q){}
static struct nvme_dev *pci_get_drvdata(struct pci_dev *p){return &dev;}
static bool apple_ans_is_s8003(void *d){return true;}
static int apple_ans_handoff_begin(void *d){return 0;}
static bool nvme_wait_reset(struct nvme_ctrl *c){return site!=4;}
static void nvme_disable_prepare_reset(struct nvme_dev *d,bool shutdown){teardown++;}
'''+disable+'\n'+shutdown+r'''
int main(void){
 for(site=1;site<=3;site++){
  resets=teardown=locked=0;
  assert(nvme_dev_disable_paired(&dev,true,true)==-EIO);
  assert(resets==1 && reason && !teardown && !locked);
 }
 site=4;resets=teardown=0;nvme_shutdown(&pci);
 assert(resets==1 && !teardown);
 site=0;resets=teardown=0;nvme_shutdown(&pci);
 assert(!resets && teardown && !locked);
 kexec_in_progress=0;site=1;resets=teardown=0;
 assert(nvme_dev_disable_paired(&dev,true,true)==-EBUSY);
 assert(!resets && !locked && !teardown);
 site=0;resets=teardown=0;nvme_shutdown(&pci);
 assert(!resets && teardown);
 /* A restart that re-enters the boot loader takes the kexec certificate. */
 system_state=SYSTEM_RESTART;
 for(site=1;site<=3;site++){
  resets=teardown=locked=0;
  assert(nvme_dev_disable_paired(&dev,true,true)==-EIO);
  assert(resets==1 && reason && !teardown && !locked);
 }
 site=4;resets=teardown=0;nvme_shutdown(&pci);
 assert(resets==1 && strstr(reason,"restart") && !teardown);
 site=0;resets=teardown=0;nvme_shutdown(&pci);
 assert(!resets && teardown && !locked);
 /* Power-off keeps the plain shutdown. */
 system_state=SYSTEM_POWER_OFF;site=4;resets=teardown=0;nvme_shutdown(&pci);
 assert(!resets && teardown==1);
}
''')
