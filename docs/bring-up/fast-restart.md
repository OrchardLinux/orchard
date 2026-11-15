# Fast restart (warm restart): Linux to Linux without the tethered boot chain

What Berkay Kösebay (brkosebay) found and built in November 2026 so that an
iPad 5 (J71t, S8003/A9) restarts from Linux straight into Linux. The work was
proposed in the pull requests listed in [Sources](#sources-pull-requests-commits-and-tags)
and is **not on main**: the PRs were not merged. This document keeps the
mechanisms, measurements and open points so that the feature can be rebuilt
without reading the PR history. Every branch head is kept as a `contrib/` tag.

Everything here was run on one board only: iPad 5 Wi-Fi (J71t, iPad6,11,
S8003). Where a statement was read from code and not run, it says so.

## Goal and what a user sees

Restart in the system menu brings the tablet back to the Linux desktop on its
own, with no computer attached and no DFU key combination.

Measured on J71t on 2026-11-12 (release build of the series, PSCI framing
`psci=1 psci_reset=0`):

| What | Result |
|---|---|
| Warm restarts that day | 20+, several right after a glmark2 run, none froze |
| Time from Restart to back up | 7 to 30 seconds (the 9 timed that morning) |
| GPU after the restart | came up warm in all 9 restarts logged on the console |
| Root filesystem | NVMe root after cold boot and after warm restart, write / fsync / drop caches / read-back hash check (2026-11-11) |
| Touch and Wi-Fi after warm restart | working (2026-11-11) |
| Sensors after warm restart (2026-11-13, install image from main `8ea3182` plus the open PRs) | accelerometer, gyroscope, pressure and light sensor present in `/sys/bus/iio` with live readings on the cold boot and on the warm restart; rotation follows the device |
| USB identity after warm restart (2026-11-13) | the Mac keeps the same `BOARD:j71t ID:…` serial and the same `cu.usbmodem`/NCM interface; no Allow prompt |

Not covered on hardware: every failure path (the cold-reset escapes), PSCI
`SYSTEM_RESET` (never qualified, off in the shipped framing), HEVC decode after
a warm restart, and any board other than J71t.

## The boot chain it bypasses

A7 to A11 devices boot Orchard tethered: the tablet is put into DFU by hand, a
host computer runs the checkm8 exploit, loads Pongo, Pongo starts m1n1, and
m1n1 starts Linux. A plain `reboot` resets the SoC, and after a SoC reset
Apple's own chain runs: the tablet lands in iOS, not Linux. Getting back needs
the DFU key combination, the cable and the host again. That is the cost a
warm restart removes; the PRs give no timing for the cold path itself.

The wording in the patches follows from this: a "cold reset" is the escape
that leaves Linux for iOS, and it is the safe state every failure falls back
to. "Tethered boot" is the device-tree fact that a SoC reset does not return
to Linux.

## Architecture

Two routes were built. Only the first was used and tested.

1. **kexec** (used): the running kernel loads the next kernel and jumps to it.
   Nothing below Linux runs again, so every piece of hardware the next kernel
   needs must be handed over in a state it can start from.
2. **PSCI `SYSTEM_RESET` into m1n1** (implemented, never qualified): the
   kernel's ordinary reboot calls the resident firmware, which restarts m1n1
   with the original iBoot boot arguments; m1n1 brings storage up and boots the
   default BLS entry itself. Off in the shipped framing (`psci_reset=0`).

| Layer | Part | PR |
|---|---|---|
| m1n1 | Resident EL3 PSCI: `CPU_ON`/`CPU_OFF` so the kernel can park secondaries for kexec; `SYSTEM_RESET`/`SYSTEM_OFF`; re-entry; default BLS entry after a reset | orchard #102 |
| Kernel, storage | Hand the ANS NVMe controller over stopped but powered, DART and NVMMU retired; cold reset on any failure | orchard #105 |
| Kernel, peripherals | GPIO interrupt modes, PIO SPI controller and touch controller left idle and taken over idle | orchard #106 |
| Kernel, boot state | Retained pstore region, display parked on the boot buffer, PMGR domains kept on in the incoming kernel, pristine device tree per board, retained AOP reload | orchard #107 |
| Kernel, reset | Watchdog cold reset ahead of PSCI on a tethered board, warm/soft modes delegated | orchard #116 |
| Kernel, GPU | Bounded G5P handover (`td-kexec`), cold reset when it fails | orchard #125 |
| Kernel, video | VXD firmware stop proven before kexec | orchard #117 (commit `ce7d3b25`) |
| Userspace | `orchard-restart` / `orchard-kexec`: prepare the default BLS entry, matched device tree, GPU quiesce, dispatch, health check | omarchy-a #38, #43 |

The shared contract between the layers:

- `linux,booted-from-kexec` in `/chosen` tells the incoming kernel that
  hardware is retained and not at the cold iBoot baseline. kexec's file loader
  adds it; m1n1 adds it after a PSCI re-entry (patch 0020); the userspace DT
  merge adds it for the legacy loader. Its readers in the series: G5P, PMGR,
  PCIe/ANS and AOP.
- A failed handover never continues into the next kernel and never panics
  (the warm path runs with `panic=0`, where a panic is an indefinite hang).
  It ends in `apple_cold_reset()`: log at emergency level, `pr_flush(1000)`,
  `kmsg_dump_desc(KMSG_DUMP_PANIC, reason)`, `reboot_mode = REBOOT_COLD`,
  `emergency_restart()`.

## CPUs: resident EL3 PSCI in m1n1 (orchard #102)

**Problem.** A7 to A9 have EL3 and no EL2, and no PSCI firmware. m1n1 hands
secondaries over through a spin table, which has no `cpu_die`. Without it the
kernel refuses kexec on an SMP system, cannot offline CPUs, and a crash kernel
inherits CPUs stuck in the old kernel.

**Mechanism** (m1n1 patches 0015 to 0020 of the PR):

- On A7 to A9 m1n1 is entered at EL3 from Pongo and stays resident: its image
  and per-CPU EL3 stacks are memreserved and `VBAR_EL3` points at its vectors
  while Linux runs at EL1. A lower-EL synchronous exception with
  `ESR_EL3.EC = 0x17` (SMC from AArch64) and `imm16 = 0` goes to the PSCI
  dispatcher; everything else takes the existing path.
- PSCI 1.0 (`PSCI_VERSION` returns `0x10000`): `CPU_ON`, `CPU_OFF`,
  `AFFINITY_INFO`, `FEATURES`, `MIGRATE_INFO_TYPE` (returns 2, no trusted OS).
  `CPU_SUSPEND` and `SMCCC_VERSION` return `NOT_SUPPORTED`, so callers assume
  SMCCC 1.0.
- **`CPU_OFF` does not power the core down.** The core stops its timers
  (`CNTP_CTL_EL0`, `CNTV_CTL_EL0` = 0), cleans and invalidates its L1D by
  set/way (L2 is shared and coherent), marks itself OFF and waits in WFE at EL3
  with the MMU off on a one-cache-line mailbox (`struct psci_cpu`, 0x80 bytes:
  state, entry, context). A9 has no way to reset a core from another core;
  Apple and m1n1 power a core down from the core itself.
- **`CPU_ON`** posts an entry point and sends an event. The core leaves the pen
  with `ic iallu`, `tlbi alle1`, `SCTLR_EL1 = 0x30d00800` (RES1 bits only, MMU
  and caches off), `SPSR_EL3 = 0x3c5` (EL1h, DAIF masked), `x0` = context ID,
  and `SP_EL3` on a static per-CPU stack (4 CPUs max, 0x4000 each).
- Mailboxes and settings live in `.data`, not `.bss`, so they survive m1n1
  being re-entered (the re-entry clears `.bss`).
- kboot parks every live secondary in the pen before starting the kernel
  (100 ms per CPU; one that does not park is pruned from the tree), sets
  `enable-method = "psci"` on every CPU, adds `/psci` (`compatible =
  "arm,psci-1.0", "arm,psci-0.2"`, `method = "smc"`), and records
  `linux-a9,m1n1-reentry` (address of `_reentry`) and `linux-a9,m1n1-boot-args`
  in `/chosen`.
- **Re-entry from a kexec trampoline** (`_reentry`: EL1, MMU off, `x0` = copy
  of the boot arguments): drops this image's lines from the caches
  (`dc civac` over `_base.._end`, `ic iallu`), sets a re-entry flag, restarts
  m1n1. A re-entered m1n1 skips the payload area (stale), takes parked cores
  back with `CPU_ON` and runs the proxy.

**Reboot and power-off through PSCI** (patches 0017 to 0019):

- An ordinary arm64 reboot does not call `CPU_OFF`: `smp_send_stop()` leaves
  secondaries in `wfe; wfi` at EL1 with interrupts masked. To collect them,
  m1n1 sets `SCR_EL3.TWI` (bit 12) and `TWE` (bit 13) through a private SiP
  call (`0xc200ff00`) made on the boot CPU right before the kernel starts. A
  trapped WFE returns at once; a trapped WFI waits at EL3 with IRQ/FIQ routed
  there only around the `wfi`, so an EL1 interrupt still wakes it. While a
  rendezvous is pending, a trapped CPU parks itself in the pen as `CPU_OFF`
  would.
- `SYSTEM_RESET` / `SYSTEM_OFF`: the caller sets the rendezvous flag, sends an
  event and an AIC IPI to the other CPUs and waits up to 1 s
  (`RENDEZVOUS_TIMEOUT_US 1000000`). If one does not arrive it clears the flag
  and returns unchanged, so the kernel goes on to its next restart method.
  Once parked it disarms the primary watchdog (`WDT_CTL`, offset 0x1c), stops
  its timers and drops the traps. `SYSTEM_RESET` then turns the EL1 MMU and
  caches off, writes back every data cache level by set/way, invalidates TLB
  and I-cache and restarts m1n1 at EL1h with the original iBoot boot
  arguments, saved in `.data` on the first cold start. `SYSTEM_OFF` parks the
  caller too: there is no PMU driver in m1n1, so it halts, it does not power
  off. Needs an AIC v1 and a watchdog in the ADT.
- After a `SYSTEM_RESET` m1n1 brings main storage up, takes the first bootable
  entry in BLS order and starts it with the board DTB name of the last launch
  (kept in `.data`). If nothing was launched before, it does not guess the
  board and stays in the proxy. It keeps the picture on screen until the next
  splash and leaves SEP alone (never reset). A 96x96 square at the top left
  shows progress: cyan `0x00b7ff` back in m1n1, green `0x00c853` loading the
  entry, magenta `0xff00ff` no previous launch, red `0xff1744` storage or
  entry failure. Any failure ends in the USB proxy, never in a cold reset.

**Payload variables** (appended to `m1n1.bin`, tool `boot/m1n1/psci-loader`,
which accepts `--device j71t` only):

| Variable | Effect |
|---|---|
| (none) | PSCI on whenever m1n1 was entered at EL3 (patch 0016); `SYSTEM_RESET` boots the default entry |
| `psci=0` | spin table, no `/psci` node |
| `psci_reset=0` | CPU lifecycle only: `SYSTEM_RESET`/`SYSTEM_OFF` return `NOT_SUPPORTED`, no WFI/WFE traps |
| `psci_reset=proxy` | after a reset m1n1 stops in the proxy |

**Tested.** CPU hotplug and kexec parking on J71t with `psci=1 psci_reset=0`
(the 20+ warm restarts). `SYSTEM_RESET` and `SYSTEM_OFF` were never qualified
on the device. Userspace refuses an SMP kexec unless `/psci` has
`method = "smc"` and every enabled CPU has `enable-method = "psci"`;
`nr_cpus=1` is the diagnostic alternative (`maxcpus=1` is not enough).

## Storage: ANS, NVMMU, DART and the PCIe port (orchard #105)

**Problem.** On J71t the NVMe controller (ANS) sits behind port 0 of the H9P
PCIe host. Resetting that port, the controller or its translations in the
wrong state loses the controller until a cold boot, and with it the root
filesystem.

**Outgoing kernel, in order** (`nvme_shutdown` with `kexec_in_progress`,
board opts in with `apple,ans-kexec-handoff` on the PCIe node and
`apple,ans-dart-handoff` on the DART):

1. Freeze and wait for outstanding I/O, sync the I/O queues.
2. Wait for the NVMMU invalidate FIFO to drain (port 0 block, 20 ms bound;
   iBoot sets `invalidate-fifo-workaround = 1` on `nvme-mmu0` on S8003; iOS's
   AppleNVMeMMU waits up to 20 ms before every invalidate and never reads the
   invalidate register back).
3. NVMe shutdown notification, then require `CSTS.SHST` = complete and no
   `CFS`.
4. Clear `CC.EN` and the SHN field, wait up to 2 s for `CSTS.RDY` to clear,
   read `CC` back, clear bus mastering.
5. Retire all NVMMU translations (unmap every tag), wait for the last
   invalidate to leave the FIFO, stop the DART ("Apple ANS: DART stopped for
   handoff"), latch `ans_handoff_stopped`.
6. PCIe host shutdown, per enabled port: clear `PCI_COMMAND`, clear the
   secondary/subordinate bus numbers, set refclk no-gate, assert PERST#, stop
   the LTSSM, wait 60 us (ADT `perst-to-refclk-delay-us`), disable the NVMMU
   SART. Port 0 stays **powered with its reference clock running**; the host
   then checks a "powered port certificate": port enabled, power on, refclk
   on, not in reset, refclk no-gate, ready done, PERST# reads asserted.

Any failed step ends in `apple_cold_reset()`. The first version panicked
instead, which with `panic=0` hung forever; patches 0036 to 0038 replaced that.

**Incoming kernel.** It brings port 0 up with the full cold-boot sequence:
port power cycle, NVMe mode, port enable, then link training. On S8003 the
NVMMU is reprogrammed in iOS's order: FIFO drained, SART aperture off before
its address changes, zeroed scratch page and TCBs visible, then scratch page,
TCB base, init (250 ms bound), SART.

**What failed first.** Retraining the powered port by toggling PERST# alone,
which iOS never does, left the root port in `Detect.Quiet` with the LTSSM
enable not latched: every warm boot logged `powered reset failed -110` and
paid the retrain timeout. After the change to a full port cycle the message
was gone on all restarts of 2026-11-12. Earlier boots also showed the storage
link failing to retrain after a handover (`POLL_COMPLIANCE`, see
[bootloader/README.md](bootloader/README.md)).

**The "NVMe crawl".** His name for a state after kexec in which NVMe
completions are polled, not signalled (a lost MSI): the system runs but
commands wait for a timeout. The userspace health check exists for this
(below); the analysis is in his tree (`ds-kexec-nvme-crawl.md`). On
the ANS timeout path of the PR, the driver dumps host-only state (no endpoint
MMIO or config reads) and, **on the second timeout of a boot, moves the
controller to `DELETING`**: the disk is removed and the root filesystem is
gone until a restart.

**Restart through the boot loader** (orchard #117, patches 0044 and 0045,
host tests only): a plain shutdown sets SHN but keeps `CC.EN`, and
power-cycling the port in that state hangs the SoC. So a restart takes the
same paired stop as kexec, and ports other than storage are powered off
(Wi-Fi would otherwise keep bus mastering into memory the next kernel
reuses; the boot loader touches only the storage port). A separate opt-in,
`apple,ans-kexec-link-flush`, waits up to 100 ms for link-up to fall under
PERST# before the LTSSM stop (0043).

**System sleep uses the same stop** (written 2026-11-15, untested):
before a sleep the NVMe driver takes the controller through the shutdown
notification and then clears `CC.EN`, and only then does the root complex
power the storage port off, which is the state the finding above asks for.
Unlike the handover here, sleep keeps nothing powered: the port goes off and
resume runs the cold-boot sequence. See
[suspend.md](suspend.md#storage-and-pcie-over-sleep).

**Crash kernel.** In a kdump kernel on `apple,s8003-pcie` a `crash_quiesce`
sysfs attribute clears `CC.EN` without a shutdown notification (an unknown
queue must never be rung), waits up to 2 s for `RDY`, blocks bus mastering
and parks the port. Not run as part of the release tests.

## Peripherals: GPIO, SPI, touch (orchard #106)

After a kexec the next kernel found:

- **GPIO** (`apple,s8000-pinctrl`): pins still in an interrupt mode armed by
  the previous kernel. Once the chained parent interrupts are enabled such a
  pin raises an interrupt nobody maps or acknowledges, and it fires forever.
  Fix: at probe, before the parents are enabled, switch every GPIO interrupt
  mode (`IN_IRQ_HI`..`IN_IRQ_ANY`, non-peripheral pins) to off and clear every
  group latch; outputs, mux, pulls, data and grouping are kept and consumers
  re-arm their own pins. In the handler, a pending pin without a mapping is
  masked and its latch cleared with a rate-limited warning. This also covers
  a panic that ran no shutdown callbacks. Log line: `inherited GPIO IRQ
  baseline: masked N inherited inputs`.
- **SPI** (S8000 PIO controller): queued transfers, counters and an asserted
  chip select. Fix: on shutdown suspend the SPI core queue, deassert the GPIO
  chip selects and write the PIO registers to idle while powered (`CTRL = 0`,
  `CFG` = base, `RXCNT = TXCNT = 0`, `PIN = CS`, status cleared, `CTRL` read
  back as 0); the same at probe, so a controller inherited in any state starts
  idle. Other SoCs keep their behaviour (different DMA and interrupt
  registers).
- **Touch** (Apple Z2): the controller still running, its interrupt thread
  possibly active. Fix: a shutdown callback stops the controller and waits for
  its interrupt. SPI children shut down before their controller, so the SPI
  driver can then drain.

Symptom before: touch did not come back after a warm restart. After: touch
and Wi-Fi worked after warm restart (2026-11-11).

## Watchdog and reset (orchard #116)

- `apple,tethered-boot` on the watchdog node (J71t only) records that a SoC
  reset leaves Linux.
- With it the watchdog's restart handler registers at priority 255, above
  PSCI's 129 (other devices keep 128). In cold mode it does the native reset:
  `WD1_CTRL = RESET_EN`, `WD1_BITE_TIME = 0`, `WD1_CUR_TIME = 0`. In warm and
  soft modes it does not program the watchdog and returns success, so the
  restart chain continues to PSCI (an error would stop the chain).
- Probe now sets the watchdog's parent before registering; the restart
  callback dereferenced it and it was never set.
- Userspace selects the mode through `/sys/kernel/reboot/mode` (or the older
  `apple_wdt.cold_restart` parameter) and treats the presence of
  `apple,tethered-boot` on an enabled `apple,wdt` node as proof that the
  kernel has the guard.

With `psci_reset=0`, as shipped, a cold reset through the watchdog is the only
restart path besides kexec.

## GPU quiesce (orchard #125, omarchy-a #43)

The G5P render work itself is recorded in [gpu/a9-g5p.md](gpu/a9-g5p.md);
only the handover is described here. It is not on main.

**Symptom.** A warm restart from a running desktop hung in the incoming
kernel's GPU warm start (device journal 2026-11-08: `origin=warm-uncycled ...
cmd8-consumed`, then silence). A restart could also jump with GPU DMA still
in flight, or hang in an unbounded wait for the firmware.

**Mechanism.** Userspace writes `td-kexec` to the driver's sysfs `command`
once every GPU client is stopped; the reboot notifier runs the same quiesce
as a fallback. The quiesce:

1. closes new admission (the sleep gate) and waits a bounded time for users
   inside, then forces on;
2. sends the firmware a detach if it is alive (never to a stopped or parked
   firmware);
3. freezes, acknowledges and masks the interrupt, and waits at most 20 ms for
   a running interrupt thread (no unbounded `synchronize_irq()`);
4. cycles the gfx power gate through PMGR.

Only a successful handover is latched; a second `td-kexec` reports success.
A successful gate cycle supersedes a failed detach, because DMA is then
stopped. If the quiesce fails on a restart the driver takes the cold reset;
halt and power-off are never turned into a reboot. The incoming kernel
classifies how the GPU was left (cold, kexec, gate seen off) and starts it
accordingly.

**Userspace side** (`quiesce-gpu`): before touching sysfs it writes a failure
receipt, `/run/orchard-kexec/gpu-quiesce-failed`, and removes it only on
success, so a timeout or interruption also counts as failure (10 s timeout,
1 s kill). Dispatch reads the receipt and cold-resets instead of jumping. It
is matched by driver, not by board: without a G5P `command` file nothing is
armed.

**Measured.** GPU warm in 9 of 9 logged restarts on 2026-11-12; 20+ restarts
with the GPU in use, several right after glmark2, without a freeze. The cold
escape after a failed handover was not run on hardware.

## Video decoder (orchard #117, commit `ce7d3b25`)

The VXD driver had no shutdown callback. A decoder firmware left running
keeps writing into buffers that become the next kernel's RAM. The patch sends
Terminate at shutdown on the S8000 variant and checks it: on success it frees
the firmware and command buffers and powers down; on failure it keeps
firmware, buffers and power, and a kexec takes the cold reset. Read from the
code and host-tested, not run on the device in this form.

## AOP (sensors) across a restart (orchard #107, commit `011ab650`)

This is the largest and most fragile part (about 2,800 patch lines).

**Finding.** The AOP firmware cannot simply be started a second time. Every
halt followed by an SRAM reload of an AOP that had run crashed the fresh
firmware in `AOPSPIMaster_channelFaultIRQ` (firmware address `0102f252`, DFAR
`0x4c`): the AOP-private SPI0 controller kept channel 3's fault bit (`+0x8d0`
bit 6) while the new firmware's channel table was still empty. So a warm
restart either leaves the AOP alone and loses the sensors (the
`module_blacklist=apple_aop` profiles), or it has to undo the old firmware's
hardware state first. Main starts the AOP once per boot.

**What he built for J71t** (selected by `apple,certified-reload` on the AOP
node, with `memory-region` pointing at a reserved 16 KiB page at
`0x8_583fc000`, `apple,observe-domains` for the five AOP power domains in lock
order, and `apple,spi0-domain`):

- *Outgoing kernel:* set the sensor rates to zero, stop each AFK endpoint
  (2 s per endpoint, 10 s in total, never force a halt after a missing ACK),
  disable endpoint routing, send the AP and IOP power-state messages and wait
  for their acknowledgements, wait for stable RAM and an idle mailbox, halt
  the CPU, drain. Each step is written to a
  checksummed transcript in the reserved page (magic `A9SL`, FNV-1a, 13
  steps from `begin` to `drain`).
- *Incoming kernel:* read the transcript and the wrapper state and reload
  only for the one state proven on the device: **the previous kernel quiesced
  and halted the firmware** (`prev-quiesce-halted`, run "wake16", 2026-10-29).
  Every other retained state is refused before the first write, leaving the
  firmware as found. A fresh start is accepted only while the wrapper is at
  its reset state (outbox enable at `+0x4020` set: 12 of 12 DFU cold
  snapshots).
- *The reload itself*, with the AOP CPU halted and under a read-only check
  that all five power domains are on: disable the latched SPI0 interrupt
  (`0x10014`, group 1 bit 20, judged by the mask readback at `+0xa00`) in the
  AOP interrupt controller; replay the firmware's own SPI controller reset
  (`AOPSPIMaster_reset`, control bit 8); replay its channel teardown for
  channel 3 (MPU6900 FIFO client) and every other running channel (channel 7,
  the pressure sensor client); reset once more because the channel-3 stop
  itself latches the SCM state from 0 to 3; copy the image while sampling the
  fault words every 5 ms; watch at least 150 ms of quiet (an earlier re-latch
  took about 105 ms) and only then release RUN once.

**Measurements from the run series** (SPI0 at `0x2_10100000`, wrapper at
`0x2_10800000`):

- Channel 3 idles when stopped within about 2 ms of the controller reset: 5
  of 5; 0 of 2 at 11 ms and 33 ms (a `pr_flush` sat in between).
- Channel 7 never idles after a stop: 0 of 3, at 0.3 ms, 1.9 ms and 15 ms. It
  is accepted as "stopped without idle" only when 20 consecutive 1 ms samples
  are byte-identical with the expected route, rate and config.
- The firmware's own idle bound is 10 ms (`SCMto` panic); the kernel polls in
  1 ms slices for up to 20 ms.
- After the controller reset channel 3 reads `0x00010447`.

**State.** Sensors and rotation worked after a warm restart on 2026-11-13.
The reload refuses a second attempt in one boot; a refused or failed reload
means no sensors until a cold boot ("recover with DFU"). The A9 AOP also
needs boot arguments and factory calibration from m1n1 (`apple,aop-target`,
`apple,chip-id`, `apple,chip-revision`, per-sensor calibration; m1n1 patch
0021, also proposed alone in orchard #135).

## Display (orchard #107)

- The display pipe is adopted from iBoot and cannot be programmed from
  scratch, so its state must survive. On shutdown the ADBE driver stops its
  flip timer and parks the pipe on the boot framebuffer, also when the CRTC
  was never enabled.
- The framebuffer description reaches the next kernel through the device
  tree: m1n1's fixups are carried over (see the next section).
- In the incoming kernel (`linux,booted-from-kexec` set), every PMGR domain
  of `apple,s8000-pmgr-pwrstate` is held (`keep_on`): power-off returns
  `-EBUSY`, the clock gate is not disabled, auto-PM is not enabled,
  `apple,min-state` is not applied to manual-PM domains, and a domain found
  on is set to active explicitly.
- Warm boots force `fbcon=map:9` and `vt.global_cursor_default=0`, so no
  console text is drawn over the picture.
- m1n1, after a re-entry, keeps the picture until the next system's splash.

## USB

No USB-specific handover was needed. Results: the gadget comes back with the
same serial after a warm restart (the stable-serial part is on main,
orchard #110), and a failed PSCI reset path ends in m1n1's USB proxy, where a
host can take over. `CONFIG_NVME_APPLE_ANS_EARLY_USB` stays off. For
diagnosis, userspace can route the outgoing kernel's log to the host over
`usb0` with netconsole (172.16.42.1 to 172.16.42.2, UDP 6666).

## Boot state carried across kexec (orchard #107)

**The pristine device tree.** A kexec has no m1n1 to fill in the device tree.
The running tree is the board's pristine DTB plus everything m1n1 added
(memory, reserved memory, framebuffer, MAC addresses, calibration, CPU
enable-method, `/psci`, `/chosen`). To build the next tree userspace must know
exactly which pristine DTB the running kernel started from, and `/boot` may
have been updated since.

- The platform layer embeds every supported board's pristine DTB and a map
  (`apple,<board> <board>.dtb`) in the initrd (`/etc/orchard/dtbs`,
  `/etc/orchard/pristine-map`).
- Before switching root the initrd copies the running board's tree to
  `/run/orchard-kexec/pristine.dtb` and the boot ID next to it. After a warm
  restart it takes the tree the previous kernel named with
  `orchard_warm.dtb=/var/tmp/orchard-warm.*/pristine.dtb` instead (path
  pattern checked, `..` refused).
- If the next kernel's pristine tree is canonically equal to the running one,
  `kexec -s` (file loader) is used: the kernel copies its live tree and adds
  `linux,booted-from-kexec`. If it changed, `fdt-merge` computes
  `merged = new + (running - old)` node by node with phandles normalised by
  path, and `kexec -c --dtb` loads it; conflicts, unknown reference layouts
  and dangling references fail closed. Memory reservations come from the
  running tree (m1n1's image, stacks and tree buffer). There is no silent
  fallback from one loader to the other.

**pstore.** `apple,reserve-pstore` on the root node (S8000/S8003) makes the
kernel reserve an aligned 2 MiB no-map ramoops area before paging
(`record-size` and `console-size` 1 MiB, `max-reason = 2`) and write the node
into a copy of the FDT, so the address travels in the kexec tree and is never
chosen again. A capture kernel leaves the records alone
(`apple,preserve-in-capture`). The AOP reload requires this console log.
`of_reserved_mem` is adjusted to count reservations added by early fixups.

**Command line.** Every warm entry gets `panic=0 reboot=warm loglevel=7
fbcon=map:9 vt.global_cursor_default=0 crash_kexec_post_notifiers
orchard_warm.dtb=<stage>/pristine.dtb`; `quiet` is dropped so checkpoints
reach ramoops. The Orchard service file adds `panic=0` to J71t's BLS entry.

## Userspace: `orchard-restart` and `orchard-kexec` (omarchy-a #38, #43)

**Offer.** The menu entry stays "Restart" on every device. Warm restart is
used when `orchard-kexec supported` succeeds: `/sys/kernel/kexec_loaded`
exists, `kexec` is installed, and the kernel has the reset guard
(`apple,tethered-boot`). Otherwise Restart is `systemctl reboot`. There is no
board list; an earlier version asked a feature-state helper, which Orchard
does not ship, so Restart was always cold.

**Flow.**

1. *At boot:* `orchard-kexec-provenance` saves the pristine tree under
   `/var/lib/orchard-kexec/boots/<boot id>/`; `orchard-kexec-load` prepares
   and loads the default BLS entry (kernel, initrd, tree, options) into the
   kernel and writes `/run/orchard-kexec/restart-ready` with the boot ID.
2. *Kept current:* a pacman hook reloads after changes under `/boot` or
   `/usr/lib/modules/*/{vmlinuz,initrd,dtb}`; a path unit reloads when
   `/boot/loader/loader.conf` or `entries` change. The old image is unloaded
   first, so a failed preparation never leaves a stale image armed.
3. *Restart pressed:* `orchard-restart` runs the load unit once more as a
   preflight while the desktop still exists (a refusal shows a notification
   and Linux keeps running), then `systemctl kexec`.
4. *Final shutdown step* (drop-in on `systemd-kexec.service`): `quiesce-gpu`,
   then `orchard-kexec dispatch`. Dispatch never loads (`/boot` may be
   unmounted). It requires a loaded image, a `restart-ready` marker matching
   this boot, no GPU failure receipt and no active watchdog, selects warm
   mode, writes `panic=0`, and runs `systemctl --force kexec`.
5. *After the restart:* `orchard-kexec-health` reads 16 KiB from
   `/dev/nvme0n1` with `O_DIRECT` at a random offset, compares the `nvme0q`
   interrupt count before and after (a count that did not move means the
   completion was polled: lost MSI), times `sudo -n true` (limit 5 s) and the
   read (limit 3 s), and scans this boot's log for NVMe timeout or recovery
   lines. It passes only after 120 s of uptime and 60 s of clean probes.

A plain `reboot` always selects cold mode (drop-in on
`systemd-reboot.service`). `orchard-kexec ios` is the explicit way out to
iOS. Default entry selection follows the same ordering as Orchard's m1n1
(`sort-key`, `machine-id`, version, file name). The local active user in
`wheel` may start the load unit (polkit rule). Package added: `kexec-tools`.

**Cold escape ladder** when dispatch refuses: select cold mode through the
guard; else write `cold` to `/sys/kernel/reboot/mode`; else arm the native
watchdog (`/dev/watchdog0`, identity "Apple SoC Watchdog", 1 s timeout,
never the magic close); then unload the image and `systemctl --force
reboot`, and as last resorts sysrq `b`. A failed kexec never falls back to a
warm reboot that was not prepared.

**Failure found on the device.** 2026-11-08: six restarts, none jumped. The
refresh unit that wrote `restart-ready` had been moved out of the shutdown
transaction (a unit that needs `/boot` conflicts with `umount.target`), so
dispatch refused and halted every time. Fix: the marker is written at load
time.

**Diagnostic profiles** (`orchard-kexec stage … --profile`): `safe`
(`nr_cpus=1`, display pipe disabled, no AOP), `smp`, `display`, `gpu-bind`,
`gpu`, `full`; `--outgoing-no-ans-handoff`, `--outgoing-quiesce-gpu`,
`--outgoing-offline-cpu1` bisect the outgoing side.

## Which chips this can apply to

| Chips | CPU hand-over | Notes |
|---|---|---|
| A7, A8, A8X, A9, A9X | The resident PSCI can apply: these SoCs have EL3, m1n1 is entered there from Pongo and can stay resident. | Only S8003 (J71t) ran it. The monitor assumes one cluster with Aff0 as the core number, at most 4 CPUs, AIC v1 and a watchdog in the ADT. |
| A10, A10X, A11 | The resident PSCI cannot apply: these SoCs have neither EL3 nor EL2, m1n1 runs at EL1 and nothing can stay resident above the kernel. | With the spin table the kernel refuses an SMP kexec. A warm restart there needs another way to park secondaries, or a single-CPU hand-over. |

The CPU part is the smaller half. Storage, GPU, AOP, display and touch each
need their own hand-over per SoC family, and the code is specific:

- storage: `apple,s8003-pcie` only (ANS behind H9P with the S8003 NVMMU);
- GPIO and SPI: `apple,s8000-pinctrl` and the S8000 PIO SPI controller;
- PMGR hold: `apple,s8000-pmgr-pwrstate`;
- AOP reload: J71t's firmware, addresses and channel assignments;
- GPU: the G5P driver of the PRs, not the shared driver on main;
- reset guard: `apple,tethered-boot`, set on J71t only.

Other boards that share these drivers were not tested.

## Review notes and what a mainline-quality version needs

Raised in Orchard's review:

1. **A10 and A11 are out of reach for the PSCI design** (no EL3/EL2). A
   product feature has to work on the iPad 6 and 7 as well, or be clearly a
   per-chip feature.
2. **The AOP cannot be reloaded by a plain restart of its firmware.** The
   reload that works replays firmware-internal SPI0 and interrupt-controller
   sequences from the AP, proven for one retained state on one firmware
   build. A version for main must keep the AOP running across the restart
   and attach to it, or accept no sensors after a warm restart, or carry this
   replay per firmware.
3. **PSCI was on by default on every A7 to A9 board** (m1n1 patch 0016),
   changing how CPUs are started on boards that never ran it; with a bare
   `m1n1.bin` it also arms the WFI/WFE traps and `SYSTEM_RESET`. It should be
   opt-in per board until each has run it, with `SYSTEM_RESET` qualified
   separately.
4. **The S8003 NVMe timeout path removes the controller after the second
   timeout.** On a tablet whose root is that disk, a slow boot becomes a dead
   system. Recovery needs to be a bounded reset.
5. **Failure-path checks belong in the patches as KUnit tests** (comment on
   orchard #128; the host tests ran outside CI against an extra applied
   tree).
6. **Feature states do not switch behaviour.** Addressed in the last
   revision: warm restart is offered from the mechanisms present.

Further observations from reading the code for this document:

- **Failure means leaving Linux.** Every refused hand-over ends in a cold
  reset to iOS, and `panic=0` means a real panic hangs. None of the failure
  paths ran on hardware.
- **Board policy in the device tree.** `apple,tethered-boot`,
  `apple,certified-reload`, `apple,ans-kexec-handoff`, `apple,reserve-pstore`
  and `linux-a9,m1n1-*` describe software policy or one boot setup, not
  hardware. Upstream-style code would derive the behaviour from
  `linux,booted-from-kexec` and the compatible, and keep retained memory in
  ordinary `reserved-memory` nodes written by the boot loader.
- **Core changes.** The series exports `pr_flush` and `kmsg_dump_desc`, adds
  Apple-specific early reservation code to `arch/arm64/mm/init.c`, and
  changes `of_reserved_mem` and `fs/pstore/ram.c`. Each needs a generic
  justification or a different approach.
- **Diagnostics in the drivers.** Evidence dumps, run names ("release6",
  "wake16", "Lab 0018") and per-step logging served the bring-up and would
  not stay.
- **The lost-MSI state is detected, not explained** in the PRs: the health
  check reports it after the restart.
- **One board.** Nothing ran on J71b or any other device, including the
  shared-driver changes (pinctrl, SPI, PMGR, watchdog priority).

A possible order for a rebuild: PSCI CPU lifecycle as an opt-in (smallest,
useful for CPU hotplug on its own); peripheral idle-at-probe fixes (useful
after any unclean start); storage hand-over with a bounded recovery; display
park; GPU hand-over on the shared driver; AOP last.

## Sources: pull requests, commits and tags

All commits are by Berkay Kösebay. Branches are stacked: each tag contains
the work of the PRs before it.

| PR | Title | State | Own commits | Branch | Tag |
|---|---|---|---|---|---|
| orchard #102 | m1n1: Resident EL3 PSCI for A7-A9, on by default | not merged | `7d41c329` (earlier: `06e94b2a`, `419fe19d`, `6c1601e6`) | `j71t/psci` | `contrib/brkosebay-j71t-psci` (`8c737f86`) |
| orchard #105 | PCI/NVMe: Keep the ANS handoff across kexec, cold-reset when it fails | not merged | `d2247b1c` (earlier: `3717b5ec`, `28c7d41f`, `e4ea0b7c`) | `j71t/warm-storage` | `contrib/brkosebay-j71t-warm-storage` (`bfb58aa8`) |
| orchard #106 | pinctrl/spi/input: Quiesce S8000 peripherals for kexec | not merged | `8286fdfd` (earlier: `8e614751`, `a76d3760`, `33047d22`) | `j71t/warm-peripherals` | `contrib/brkosebay-j71t-warm-peripherals` (`86c47eea`) |
| orchard #107 | kernel: Retain the tethered warm boot state across kexec | not merged | `57e91ab3`, `2c93befe` (m1n1 AOP arguments), `011ab650` (AOP reload), `d32d990d` (pristine DTBs); earlier: `df8cccac`, `0886d8cc` | `j71t/warm-kernel` | `contrib/brkosebay-j71t-warm-kernel` (`d32d990d`) |
| orchard #116 | watchdog: apple: Prefer a cold reset and guard the warm reset path | not merged | `92d22c85` | `j71t/reset-guard` | `contrib/brkosebay-j71t-reset-guard` (`172c2879`) |
| orchard #117 | j71t: Suspend to idle with retained ANS power and early driver admission | referenced only | restart parts: PCI patches 0043 to 0045 in `40a8b18d`, VXD stop `ce7d3b25` | `j71t/suspend` | contained in `contrib/brkosebay-j71t-gpu-handover` |
| orchard #125 | drm/apple-g5p: Bound the kexec handover and fix two lost-GPU races | closed (GPU work superseded by orchard #138) | `c57f87b7` | `j71t/gpu-handover` | `contrib/brkosebay-j71t-gpu-handover` (`7f43bad4`) |
| omarchy-a #38 | Warm restart: on-device warm restart through Orchard BLS | not merged | `736dccd`, `25ee03a` (earlier: `f3becdb`) | `j71t/warm-restart` | `contrib/brkosebay-j71t-warm-restart` (`25ee03a4`) |
| omarchy-a #43 | Warm restart: report a GPU quiesce failure to the caller | not merged | `78ec070`, `e49b01e` | `j71t/warm-restart-shared` | `contrib/brkosebay-j71t-warm-restart-shared` (`e49b01e7`) |

Related tags in orchard: `contrib/brkosebay-j71t-aop` (`a54e2049`, first
form of the AOP sensors with the retained reload),
`contrib/brkosebay-j71t-feature-states` (`883f1eaf`, the `warm_restart`
feature state), `contrib/brkosebay-j71t-kernel-safety-tests` (host tests for
the hand-over failure paths, orchard #128).

Where to look in the tags:

| Topic | Files |
|---|---|
| PSCI | `boot/m1n1/patches/orchard/0015` to `0020`, `boot/m1n1/psci-loader`, `tests/test_m1n1_psci.py` |
| Storage | `kernel/patches/drivers/pci/0033` to `0038` (and `0043` to `0045` in the gpu-handover tag), `drivers/iommu/0011`, `0012`, `devices/s8000/0008` |
| Peripherals | `kernel/patches/drivers/pinctrl/0004`, `drivers/spi/0009`, `drivers/input/0030` |
| Boot state | `kernel/patches/drivers/pstore/0001`, `0002`, `drivers/of/0001`, `drivers/pmdomain/0002`, `drivers/drm/0008`, `boot/initramfs/root/init`, `src/orchard/image/platform.py` |
| AOP | `kernel/patches/drivers/soc-apple/0022`, `0023`, `devices/s8000/0015`, `boot/m1n1/patches/orchard/0021` |
| Reset | `kernel/patches/drivers/watchdog/0001` to `0003`, `devices/s8003/0001` |
| GPU | `kernel/patches/drivers/drm/0042`, `drivers/gpu/0015` to `0017` (gpu-handover tag) |
| Userspace | omarchy-a: `docs/warm-restart.md`, `system/overlay/usr/lib/orchard-kexec/`, `system/overlay/usr/bin/orchard-restart`, the systemd units and drop-ins, `tests/test_warm_restart.py` |

The patches cite longer notes that live in Berkay's own tree and were not
part of the PRs: `docs/boot/warm-reboot.md`, `docs/boot/ds-kexec-nvme-crawl.md`,
`docs/boot/ds-nvme-d-iboot-handoff-state.md`,
`docs/periph/ds-aop-reload-crash-and-wake.md`, `ds-aop-spi0-ch3-round3.md`,
`ds-aop-spi0-ch3-round4.md` and `docs/gpu/experiments/recovery108-driver.md`.
