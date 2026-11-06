#!/usr/bin/env python3
# SPDX-License-Identifier: GPL-2.0-only
"""Exercise S5L8942X USB callbacks from a supplied kernel tree with fake MMIO.

Run with a C compiler supporting AddressSanitizer and UndefinedBehaviorSanitizer.
No device access occurs. An explicit output directory retains generated test
sources and a hash receipt; the default uses a temporary directory.
"""
from pathlib import Path
import argparse
import hashlib
import json
import os
import re
import shlex
import subprocess
import tempfile


def function(source, name):
    start = source.rfind('\nstatic ', 0, source.index(name + '(')) + 1
    brace = source.index('{', source.index(name + '('))
    depth, end = 1, brace + 1
    while depth:
        depth += (source[end] == '{') - (source[end] == '}')
        end += 1
    return source[start:end]


parser = argparse.ArgumentParser()
parser.add_argument('tree', type=Path)
parser.add_argument('--output', type=Path)
parser.add_argument('--cc', default=os.environ.get('CC', 'cc'))
args = parser.parse_args()
temporary = tempfile.TemporaryDirectory(prefix='orchard-s5l-usb-') if args.output is None else None
if temporary is not None:
    args.output = Path(temporary.name)
args.output.mkdir(exist_ok=True, parents=True)
phy_path = args.tree / 'drivers/phy/phy-apple-s5l-usb.c'
dwc_path = args.tree / 'drivers/usb/dwc2/platform.c'
phy = phy_path.read_text()
dwc = dwc_path.read_text()
types = phy[phy.index('#define OPHYPWR'):phy.index("/* XNU's clock_gate_switch")]
callbacks = '\n\n'.join(function(phy, name) for name in (
    's5l_pmgr_on', 's5l_usbphy_clk_sel', 's5l_usbphy_is_ready',
    's5l_usbphy_cold_init', 's5l_usbphy_init', 's5l_usbphy_exit',
    's5l_usbphy_set_otg', 's5l_usbphy_power_on', 's5l_usbphy_power_off',
    's5l_hostphy_init', 's5l_hostphy_exit'))
quirks = dwc[dwc.index('struct dwc2_platform_quirks {'):dwc.index('/**\n * dwc2_driver_probe()')]
prelude = r'''
#include <assert.h>
#include <stdbool.h>
#include <stdint.h>
#include <stdio.h>
#include <string.h>
#include <errno.h>
typedef uint32_t u32;
#define __iomem
#define BIT(n) (1u << (n))
#define ARRAY_SIZE(a) (sizeof(a) / sizeof((a)[0]))
#define dev_err(...) ((void)0)
struct device_node { const char *compatible; };
struct device { struct device_node *of_node; };
struct of_device_id { const char *compatible; const void *data; };
struct regulator;
struct mutex { int held; };
struct clk { unsigned long rate; int enabled; int fail; };
struct phy { void *data; };
struct dwc2_hsotg { void *regs; struct device *dev; };
#define PCGCTL 0xe00
#define PCGCTL_GATEHCLK BIT(1)
#define PCGCTL_STOPPCLK BIT(0)
static unsigned checks, writes, phy_writes, pmgr_writes;
static u32 phy_regs[1024], pmgr_regs[2048], dwc_regs[1024];
static void *ignored_write;
static bool blocked_parent, stuck_reset;
static unsigned parent_order[16], parent_count;
#define CHECK(x) do { checks++; assert(x); } while (0)
static void mutex_lock(struct mutex *m) { CHECK(!m->held); m->held = 1; }
static void mutex_unlock(struct mutex *m) { CHECK(m->held); m->held = 0; }
static void *phy_get_drvdata(struct phy *p) { return p->data; }
static unsigned long clk_get_rate(struct clk *c) { return c->rate; }
static int clk_prepare_enable(struct clk *c) {
    if (c->fail) return c->fail;
    c->enabled++; return 0;
}
static void clk_disable_unprepare(struct clk *c) { CHECK(c->enabled == 1); c->enabled--; }
static void udelay(unsigned n) { (void)n; }
static void usleep_range(unsigned a, unsigned b) { CHECK(a <= b); }
static u32 readl(const void *p) { return *(const u32 *)p; }
static void writel(u32 value, void *p) {
    uintptr_t address = (uintptr_t)p;
    writes++;
    if (address >= (uintptr_t)phy_regs && address < (uintptr_t)(phy_regs + ARRAY_SIZE(phy_regs))) phy_writes++;
    if (address >= (uintptr_t)pmgr_regs && address < (uintptr_t)(pmgr_regs + ARRAY_SIZE(pmgr_regs))) {
        pmgr_writes++;
        CHECK(parent_count < ARRAY_SIZE(parent_order));
        parent_order[parent_count++] = (unsigned)(address - (uintptr_t)pmgr_regs);
        if (!blocked_parent) value = (value & ~0xf0u) | ((value & 0xfu) << 4);
    }
    if (p == ignored_write) return;
    if (p == &phy_regs[0x08 / 4] && stuck_reset) value |= 1;
    *(u32 *)p = value;
}
#define ioread32 readl
#define iowrite32 writel
#define readl_poll_timeout(addr, value, condition, delay, timeout) ({ \
    unsigned loops = (timeout) / (delay) + 1; int result = -ETIMEDOUT; \
    while (loops--) { (value) = readl(addr); if (condition) { result = 0; break; } } result; })
static const struct of_device_id *of_match_node(const struct of_device_id *matches, struct device_node *node) {
    if (!node) return NULL;
    for (; matches->compatible; matches++)
        if (!strcmp(matches->compatible, node->compatible)) return matches;
    return NULL;
}
'''
tests = r'''
static void setup(struct s5l_usbphy *p, struct clk *clock, struct phy *otg, struct phy *host) {
    memset(phy_regs, 0, sizeof(phy_regs)); memset(pmgr_regs, 0, sizeof(pmgr_regs));
    memset(dwc_regs, 0, sizeof(dwc_regs)); memset(p, 0, sizeof(*p));
    *clock = (struct clk){ .rate = 24000000 };
    p->base = phy_regs; p->pmgr = pmgr_regs; p->refclk = clock;
    p->data = &s5l8942x_usbphy_data;
    otg->data = host->data = p;
    ignored_write = NULL; blocked_parent = stuck_reset = false;
    writes = phy_writes = pmgr_writes = parent_count = 0;
}
int main(void) {
    struct s5l_usbphy p; struct clk clock; struct phy otg, host;
    const unsigned long rates[] = {12000000, 24000000, 48000000};
    for (unsigned i = 0; i < ARRAY_SIZE(rates); i++) {
        setup(&p, &clock, &otg, &host); clock.rate = rates[i]; phy_regs[OPHYCLK/4] = 0xa4;
        CHECK(s5l_usbphy_init(&otg) == 0); CHECK(p.init_users == 1); CHECK(clock.enabled == 1);
        CHECK(s5l_usbphy_is_ready(&p)); CHECK(phy_regs[OPHYCLK/4] == (0xa4 | i));
        CHECK(parent_order[0] == PMGR_UPERF); CHECK(parent_order[1] == PMGR_USB_OTG);
        CHECK(parent_order[2] == PMGR_USBREG); CHECK(phy_regs[OPHYUNK1/4] == 4);
        CHECK(s5l_usbphy_exit(&otg) == 0); CHECK(clock.enabled == 0);
    }
    setup(&p, &clock, &otg, &host);
    CHECK(s5l_hostphy_init(&host) == 0); unsigned first_writes = writes;
    CHECK(s5l_usbphy_init(&otg) == 0); CHECK(writes == first_writes);
    CHECK(p.init_users == 2); CHECK(clock.enabled == 1);
    phy_regs[OPHYUNK1/4] |= 0xa000;
    CHECK(s5l_usbphy_power_on(&otg) == 0); CHECK(phy_regs[OPHYUNK1/4] == 0xa006);
    CHECK(s5l_usbphy_power_off(&otg) == 0); CHECK(phy_regs[OPHYUNK1/4] == 0xa004);
    CHECK(s5l_usbphy_exit(&otg) == 0); CHECK(clock.enabled == 1); CHECK(p.init_users == 1);
    CHECK(s5l_hostphy_exit(&host) == 0); CHECK(clock.enabled == 0); CHECK(p.init_users == 0);
    CHECK(s5l_usbphy_power_on(&otg) == -EIO);
    setup(&p, &clock, &otg, &host); clock.rate = 26000000;
    CHECK(s5l_usbphy_init(&otg) == -EINVAL); CHECK(phy_writes == 0);
    CHECK(p.init_users == 0); CHECK(clock.enabled == 0);
    setup(&p, &clock, &otg, &host); clock.fail = -EIO;
    CHECK(s5l_usbphy_init(&otg) == -EIO); CHECK(writes == 0); CHECK(p.init_users == 0);
    setup(&p, &clock, &otg, &host); blocked_parent = true;
    CHECK(s5l_usbphy_init(&otg) == -ETIMEDOUT); CHECK(phy_writes == 0);
    CHECK(p.init_users == 0); CHECK(clock.enabled == 0);
    setup(&p, &clock, &otg, &host); ignored_write = &phy_regs[OPHY_UOTGTUNE1/4];
    CHECK(s5l_usbphy_init(&otg) == -EIO); CHECK(p.init_users == 0); CHECK(clock.enabled == 0);
    setup(&p, &clock, &otg, &host); stuck_reset = true;
    CHECK(s5l_usbphy_init(&otg) == -ETIMEDOUT); CHECK(p.init_users == 0); CHECK(clock.enabled == 0);
    setup(&p, &clock, &otg, &host); CHECK(s5l_usbphy_init(&otg) == 0);
    ignored_write = &phy_regs[OPHYUNK1/4]; CHECK(s5l_usbphy_power_on(&otg) == -EIO);
    ignored_write = NULL; CHECK(s5l_usbphy_exit(&otg) == 0);
    setup(&p, &clock, &otg, &host); p.data = NULL;
    phy_regs[OPHYPWR/4] = 0x22; phy_regs[OPHY_UOTGTUNE1/4] = 0x1234;
    CHECK(s5l_hostphy_init(&host) == 0); CHECK(writes == 0);
    CHECK(s5l_usbphy_init(&otg) == 0); CHECK(clock.enabled == 0);
    CHECK(pmgr_writes == 1); CHECK(parent_order[0] == PMGR_USB_OTG);
    CHECK(phy_regs[OPHYPWR/4] == 0x22); CHECK(phy_regs[OPHY_UOTGTUNE1/4] == 0x1234);
    unsigned legacy_writes = writes; CHECK(s5l_usbphy_power_on(&otg) == 0);
    CHECK(s5l_usbphy_power_off(&otg) == 0); CHECK(writes == legacy_writes);
    CHECK(s5l_usbphy_exit(&otg) == 0); CHECK(s5l_hostphy_exit(&host) == 0);
    struct device_node apple = {.compatible="apple,s5l8942x-dwc2"}, other = {.compatible="snps,dwc2"};
    struct device dev = {.of_node=&other}; struct dwc2_hsotg dwc = {.regs=dwc_regs, .dev=&dev};
    writes = 0; dwc_regs[PCGCTL/4] = 0xa3;
    CHECK(dwc2_restore_firmware_clocks(&dwc) == 0); CHECK(writes == 0); CHECK(dwc_regs[PCGCTL/4] == 0xa3);
    dev.of_node = &apple; CHECK(dwc2_restore_firmware_clocks(&dwc) == 0);
    CHECK(dwc_regs[PCGCTL/4] == 0xa0); CHECK(writes == 1);
    CHECK(dwc2_restore_firmware_clocks(&dwc) == 0); CHECK(writes == 1);
    dwc_regs[PCGCTL/4] = 3; ignored_write = &dwc_regs[PCGCTL/4];
    CHECK(dwc2_restore_firmware_clocks(&dwc) == -EIO);
    printf("USB callbacks: %u checks passed\n", checks);
    return 0;
}
'''
c = args.output / 'usb-callbacks.c'
c.write_text(prelude + '\n' + types + '\n' + callbacks + '\n' + quirks + '\n' + tests)
binary = args.output / 'usb-callbacks'
compile_command = shlex.split(args.cc) + ['-std=gnu11', '-Wall', '-Wextra', '-Werror',
                   '-Wno-unused-function', '-fsanitize=address,undefined', '-g',
                   str(c), '-o', str(binary)]
subprocess.run(compile_command, check=True)
result = subprocess.run([str(binary)], check=True, capture_output=True, text=True)
print(result.stdout, end='')
(args.output / 'callbacks-result.json').write_text(json.dumps({
    'phy_sha256': hashlib.sha256(phy_path.read_bytes()).hexdigest(),
    'dwc2_sha256': hashlib.sha256(dwc_path.read_bytes()).hexdigest(),
    'harness_sha256': hashlib.sha256(c.read_bytes()).hexdigest(),
    'actual_callback_bodies': True,
    'asan': True, 'ubsan': True, 'output': result.stdout.strip(),
    'limits': 'Fake MMIO establishes callback control flow; physical timing and USB enumeration need hardware.'
}, indent=2) + '\n')

if temporary is not None:
    temporary.cleanup()
