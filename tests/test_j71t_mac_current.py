"""Host-only tests; ORCHARD_KERNEL_SOURCE selects the prepared patched kernel."""
import os
from pathlib import Path
import plistlib
import re
import subprocess
import tempfile
import unittest
from unittest.mock import patch
from orchard.apple import usb, linux_usb

HW = Path(__file__).resolve().parents[1]
BOOT = '12345678-1234-1234-1234-123456789abc'


def function(source, name):
    start = re.search(r'^.*\b' + name + r'\([^;]*?\)\n\{', source, re.M).start()
    brace = source.index('{', start)
    depth = 1
    end = brace + 1
    while depth:
        depth += (source[end] == '{') - (source[end] == '}')
        end += 1
    return source[start:end] + '\n'


class MacCurrent(unittest.TestCase):
    def test_gadget_profile_preserves_network_serial_and_safe_default(self):
        source = (HW / 'boot/initramfs/root/init').read_text()
        start = source.index('usb_gadget() {')
        script = source[start:source.index('\nusb_gadget ||', start)]
        for board, flag, expected in [('j71t', '1', '0x1014'), ('j71t', '', '0x4145'),
                                      ('j71t', '2400', '0x4145'), ('j71b', '1', '0x4145')]:
            with self.subTest(board=board, flag=flag), tempfile.TemporaryDirectory() as tmp:
                root = Path(tmp)
                for n in ['sys/kernel/config/usb_gadget', 'sys/class/udc/usbdev',
                          'newroot/etc/orchard', 'newroot/usr/lib', 'proc/device-tree',
                          'proc/sys/kernel/random']:
                    (root / n).mkdir(parents=True)
                (root / 'proc/device-tree/compatible').write_bytes(f'apple,{board}\0apple,s8003\0'.encode())
                (root / 'proc/sys/kernel/random/boot_id').write_text(BOOT)
                (root / 'newroot/etc/os-release').write_text('NAME="Omarchy"\n')
                if flag:
                    (root / 'newroot/etc/orchard/mac-usb-current').write_text(flag + '\n')
                run = script
                run = re.sub(r'/(sys|newroot|proc)/', lambda m: str(root) + m.group(), run)
                run = run.replace('mkdir \"$g\" &&', 'mkdir -p \"$g/functions\" &&')
                subprocess.run(['sh', '-ec', 'mount() { :; };\n' + run + '\nusb_gadget'], check=True,
                               capture_output=True)
                g = root / 'sys/kernel/config/usb_gadget/orchard-system'
                for key, value in [('idProduct', expected), ('idVendor', '0x05ac'),
                                   ('bDeviceClass', '0xef' if expected == '0x1014' else '0x00'),
                                   ('bDeviceSubClass', '0x02' if expected == '0x1014' else '0x00'),
                                   ('bDeviceProtocol', '0x01' if expected == '0x1014' else '0x00'), ('configs/c.1/MaxPower', '500'),
                                   ('UDC', 'usbdev')]:
                    self.assertEqual((g/key).read_text().strip(), value)
                for name in ['ncm.usb0', 'acm.usb0']:
                    self.assertTrue((g/'configs/c.1'/name).is_symlink())
                self.assertEqual((g/'strings/0x409/serialnumber').read_text().strip(), f'BOOT:{BOOT} BOARD:{board}')

    def test_compatibility_pid_requires_orchard_identity_on_both_hosts(self):
        for serial, mode in [(f'BOOT:{BOOT} BOARD:j71t', 'system'), ('AppleSerial', 'other'),
                             (f'BOOT:{BOOT} BOARD:j71b', 'other')]:
            raw = plistlib.dumps([{'IOObjectClass': 'IOUSBHostDevice', 'idVendor': 0x05ac,
                                  'idProduct': 0x1014, 'locationID': 123, 'USB Serial Number': serial}])
            with patch.object(usb.sys, 'platform', 'darwin'), patch.object(usb.subprocess, 'run',
                    return_value=subprocess.CompletedProcess([], 0, raw, b'')):
                self.assertEqual(usb.devices()[0]['mode'], mode)
            with tempfile.TemporaryDirectory() as tmp:
                node = Path(tmp)/'1-2'; node.mkdir()
                for key, value in [('idVendor', '05ac'), ('idProduct', '1014'), ('serial', serial)]:
                    (node/key).write_text(value)
                self.assertEqual(linux_usb.devices(Path(tmp))[0]['mode'], mode)

    @unittest.skipUnless(os.environ.get('ORCHARD_KERNEL_SOURCE'), 'set ORCHARD_KERNEL_SOURCE for production C tests')
    def test_production_grant_and_charger_policy(self):
        source = Path(os.environ['ORCHARD_KERNEL_SOURCE'])
        composite = (source/'drivers/usb/gadget/composite.c').read_text()
        charger = (source/'drivers/power/supply/apple_tristar_charger.c').read_text()
        start = composite.index('if ((ctrl->bRequestType & ~USB_DIR_IN)')
        match = composite[start+4:composite.index(' {', start)-1]
        match = match.replace('ctrl->bRequestType', 'type').replace('ctrl->bRequest', 'request').replace('le16_to_cpu(cdev->desc.idVendor)', 'vendor')
        c = r'''
#include <assert.h>
#include <stdbool.h>
#include <stdint.h>
#include <stdio.h>
typedef uint16_t u16; typedef int16_t s16;
#define max(a,b) ((a)>(b)?(a):(b))
#define min(a,b) ((a)<(b)?(a):(b))
#define READ_ONCE(a) (a)
#define USB_DIR_IN 0x80
#define USB_TYPE_VENDOR 0x40
#define USB_RECIP_DEVICE 0
#define USB_APPLE_REQ_CURRENT_LIMIT 0x40
#define USB_APPLE_VENDOR_ID 0x05ac
#define dev_info(...) ((void)0)
#define dev_dbg(...) ((void)0)
#define dev_info_ratelimited(...) ((void)0)
#define EINVAL 22
#define TRISTAR_CHG_FALLBACK_CODE 34
#define TRISTAR_RAMPING 2
#define TRISTAR_CHARGING 3
struct usb_gadget {int dummy;};
struct usb_composite_dev {struct usb_gadget *gadget; unsigned vbus_draw_ma; int apple_extra_ma; bool suspended;};
static unsigned draw;
static void usb_gadget_vbus_draw(struct usb_gadget *g, unsigned ma){draw=ma;}
struct tristar_charger {unsigned usb_epoch, epoch, usb_ma, source_ma, max_ma, target_code, code, phase, step_ns; bool present,usb_on,stopping; void *dev,*wq; int lock,poll_work;};
struct power_supply {struct tristar_charger *chg;};
union power_supply_propval {int intval;};
enum power_supply_property {POWER_SUPPLY_PROP_INPUT_CURRENT_LIMIT};
static unsigned notifications;
static struct tristar_charger *power_supply_get_drvdata(struct power_supply *p){return p->chg;}
static void mutex_lock(int *p){assert(!*p);*p=1;}
static void mutex_unlock(int *p){assert(*p);*p=0;}
static void mod_delayed_work(void *wq,int *work,unsigned d){}
static void power_supply_changed(struct power_supply *p){notifications++;}
static void tristar_withdraw(struct tristar_charger *c,unsigned code){c->code=code;}
'''
        for n in ['composite_vbus_draw', 'composite_apple_current_limit']:
            c += function(composite, n)
        for n in ['tristar_code_ma', 'tristar_ma_code', 'tristar_usb_ma', 'tristar_target_code', 'tristar_update_target', 'tristar_set_property']:
            c += function(charger, n)
        c += f'int match(unsigned type,unsigned request,unsigned w_length,unsigned vendor){{return {match};}}\n'
        c += r'''
static void report(struct power_supply *p,int ma){union power_supply_propval v={.intval=ma*1000};assert(!tristar_set_property(p,POWER_SUPPLY_PROP_INPUT_CURRENT_LIMIT,&v));}
int main(void){
 for(unsigned t=0;t<256;t++)for(unsigned r=0;r<256;r++)for(unsigned l=0;l<2;l++)for(unsigned v=0;v<2;v++)
  assert(match(t,r,l,v?0x05ac:0x1234)==((t==0x40||t==0xc0)&&r==0x40&&!l&&v));
 struct usb_gadget g={0};struct usb_composite_dev d={.gadget=&g};
 composite_vbus_draw(&d,500);assert(draw==500);
 composite_apple_current_limit(&d,500,1900);assert(draw==2400);
 composite_vbus_draw(&d,500);assert(draw==2400); /* configuration preserves grant */
 for(unsigned i=0;i<65536;i++){
  composite_apple_current_limit(&d,0,i);assert(draw==(unsigned)max(500+(s16)i,0));
 }
 d.suspended=true;composite_vbus_draw(&d,2);
 composite_apple_current_limit(&d,500,1900);assert(draw==2);
 d.suspended=false;composite_vbus_draw(&d,500);assert(draw==2400);
 d.apple_extra_ma=0;composite_vbus_draw(&d,100);assert(draw==100);
 composite_vbus_draw(&d,0);composite_apple_current_limit(&d,0,1900);assert(draw==0);
 struct tristar_charger ch={.epoch=7,.usb_epoch=7,.max_ma=2400,.present=true,.usb_on=true,.phase=TRISTAR_CHARGING,.code=34};
 struct power_supply p={.chg=&ch};
 report(&p,2400);assert(ch.usb_ma==2400&&ch.target_code==186&&ch.phase==TRISTAR_RAMPING&&notifications==1);
 ch.code=186;ch.phase=TRISTAR_CHARGING;report(&p,500);assert(ch.code==34&&ch.target_code==34);
 for(int ma=501;ma<=33267;ma++){
  report(&p,ma);assert(ch.usb_ma<=(unsigned)ma&&ch.usb_ma<=2400);
  assert(tristar_code_ma(ch.target_code)<=min((unsigned)ma,2400));
 }
 report(&p,-1);assert(!ch.usb_ma&&ch.target_code==34);
 ch.usb_on=false;report(&p,2400);assert(!ch.usb_ma);
 ch.usb_on=true;ch.present=false;report(&p,2400);assert(!ch.usb_ma);
 ch.present=true;report(&p,2400);ch.epoch++;assert(!tristar_usb_ma(&ch)&&tristar_target_code(&ch)==34);
 puts("PASS 262144 request tuples, all signed grants, suspend, board cap, ramp/revoke, attachment and epoch gates");
}
'''
        # Lifecycle invalidations remain in the actual callbacks.
        for name in ['composite_reset', 'composite_disconnect', 'composite_suspend']:
            self.assertIn('cdev->apple_extra_ma = 0;', function(composite, name))
        with tempfile.TemporaryDirectory() as tmp:
            p = Path(tmp); (p/'test.c').write_text(c)
            subprocess.run(['cc','-std=gnu11','-Wall','-Werror','-fsanitize=address,undefined', str(p/'test.c'),'-o',str(p/'test')],check=True)
            subprocess.run([str(p/'test')], check=True)

if __name__ == '__main__':
    unittest.main()
