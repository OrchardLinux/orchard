# A9 GPU (G5P, iPad 5): what the powervr backend took and what it left

The iPad 5's GPU (S8003/A9, PowerVR GT7600, Apple's "G5P") was brought up by
Berkay Kösebay in his own driver, `apple-g5p` (orchard #74, with #119, #120
and #125 on top). Orchard runs the GPU from the upstream `powervr` driver
instead, as a second chip next to the A10 (`pvr_apple_s8003.c`). This page
records everything of his boot work that the backend does **not** carry, with
the reason it existed and what he measured, so none of it has to be found
again. It also says where the ported parts live now.

Everything below about the hardware and the firmware is his finding, read from
his code and its comments (`drivers/gpu/apple-g5p/` in #74). Firmware
addresses (`0x1000....`) are the META image's, kext addresses (`c9....`,
`6c9....`) are the iOS 16.7.10 (20H350) AGX kext's, as he cites them. Nothing
here was re-measured: the backend has not run on hardware yet.

State of the backend: it boots Apple's unmodified firmware, waits for its
first "alive" event, gives it one empty device-control command, and stops it
again when idle. It writes no job, so nothing renders.

## Where his boot code lives now

| apple-g5p | powervr backend |
|---|---|
| `ldr.h` `g5p_ldr_walk()` (own LDR parser) | not ported: `pvr_meta_ldr_process()` in `pvr_fw_meta.c`, the driver's own walker, with a target per firmware |
| `pack.h` `g5p_image_offset()`, `g5p_pack_record()` | `s8003_ldr_mem()`, `s8003_ldr_config()`, `s8003_ldr_start_threads()` |
| `boot-layout.h` `g5p_plan_layout()` | `s8003_layout_plan()` (objects by their CPU-side kext number) |
| `boot-layout.h` `g5p_make_boot_config()` | `s8003_image_configure()` |
| `boot-layout.h` `g5p_make_init()` | `s8003_init_build()`; the register addresses and the interrupt come from the device tree (`s8003_parse_dt()`) |
| `boot-layout.h` `g5p_meta_address()`, entry encoders | `s8003_meta_addr()`; the page-table entries are built by `pvr_mmu.c` |
| `boot.inc` `g5p_boot_map_page()`, `g5p_boot_region()` (private page tables) | the kernel VM context: `s8003_map()`, `pvr_vm_map()`, `pvr_vm_map_io()` |
| `perf-envelope.h` `g5p_perf_install()`, `g5p_perf_seed()` | `s8003_init_perf()` |
| `core.inc` `g5p_fender_wake_once()` | `pvr_apple_s8003_boot()` |
| `fender.h` idle / hold / start, `boot.inc` `g5p_boot_program_stream()` | `s8003_core_off()`, `s8003_release()` |
| `boot.inc` `g5p_boot_drain()`, `boot-r73b.inc` `g5p_r73b_aux_pass()` | `s8003_drain()`, on a timer instead of the interrupt |
| `boot.inc` `g5p_boot_events()` (ALIVE wait) | `s8003_wait_alive()` |
| `lifecycle.inc` control prepare / submit / wait, `kick.inc` | `s8003_first_command()`, `s8003_dc_doorbell()` |
| `lifecycle.inc` `g5p_boot_park()` | `pvr_apple_s8003_stop()` |

## The boot, as ported

Registers: R0 is the Rogue core (`rgx`), R1 is Fender (`fender`).

1. **Wake** (`c93fe8..c93ff8`): R1+0x10000 = 0xffff0000 (an empty boot stream),
   R1+0 = 0x40000000, R1+0x1000 = 0x11 (power state 1), wait for bit 4 of
   R1+0x1000 to clear. R0 answers afterwards; the core ID is R0+0x18 =
   0x011b0602, revision R0+0x20 = 0x00090f00 (BVNC 9.15.6.2).
2. **Idle** (`c9fa10`): R1+0x20000 = 0, wait R1+0x1000 bit 4 clear,
   R1+0x1000 = 0x10 (state 0), wait for state 0.
3. **Hold** (`c9fc10`): R1+0x20000 = 0x20, wait for it to read back, state
   still 0.
4. **Boot stream** (`c9feb4`): R1+0x10 = 0, R1+0 = 0x40000000, then pairs of
   words from R1+0x10000 (at most 0x200 bytes): a Rogue register offset with a
   flag byte above it (0xff on the last or only half of a register) and its
   value. The writes: SOFT_RESET 0x00e7fffffffffc1d, then
   0x00e7fe071ffffc1d, then 0x100000000; META_BOOT = 1;
   MMU_CBASE_MAPPING_CONTEXT = 0; MMU_CBASE_MAPPING = catalogue >> 12;
   SLC3_CTRL_MISC = 3; SLC3_SCRAMBLE and SCRAMBLE2 = 0xb42dd24b4bd22db4;
   R0+0xe160 = the address of a spare page; GARTEN_SLC = 0; SOFT_RESET = 0;
   the end marker 0xffff0000. Between them R1+0x2010 = 0xbf and
   R1+0x2014 = 0x233. Each stream word reads back as written; that is no
   proof that Fender ran it.
5. **Release** (`c9f954`): R1+0x20000 = 0, state 0 confirmed,
   R1+0x1000 = 0x10 | q, wait for state q. Fender replays the stream; its
   last write releases the reset and the META boots itself from the image
   mapped at GPU address 0xe1c0000000.
6. **Alive**: the firmware posts event type 0 to the event ring.
7. **First command**: device-control command 8 with no payload
   (control+0x1020 = 0, ring entry {8, 0, 0, 0, 0}), R1+0x14 = 7 and its
   read-back, R0+0xb00 (MTS_SCHEDULE) = 0x10; the firmware advances the ring's
   consumer index.

Not known: what R1+0x2010 / R1+0x2014 and R0+0xe160 mean, what distinguishes
the two SOFT_RESET masks (the first is the other register layout's full
mask, see "R46" below), and what command 8 does beyond being consumed.

The META slave port is not used for the boot. Polling MSLVCTRL1 while the
reset is held locked the SoC; his driver forbids slave-port access from the
first boot stream on for the rest of the boot.

### Memory

| GPU address | size | contents | leaf attributes |
|---|---|---|---|
| 0xe1c0000000 | 0x30000 | the unpacked image | PM/META protect |
| 0xe1c0200000 | 0x60000 | "shared": control, rings | protect, cache coherent |
| 0xe1c3200000 | 0x8000 | "root": init, channels, policy, platform | protect, cache coherent |
| 0xe1c4200000 | 4 pages | Fender (2), AIC+0 (time), AIC+0x4000 (software interrupts) | protect, SLC bypass |

The image: LDR address 0x10000000 (0x18000) at +0x4000, 0x20000000 (0x4000) at
+0x1c000, 0xc0000000 (0x4000) at +0, 0xe0001000 (0x4000) at +0x20000, core
memory 0x80000080 (0x55dc) at +0x24000; ZEROMEM commands for
0x80000000..0x88000000 are not applied by the host. The bootloader's
configuration starts at +0x200 (13 segment-MMU words, the image's 27 register
writes, a zero pair, the address of +0x3ff8); +0x3ff8 and +0x3ffc hold the
META addresses of the init object and the control object.

The rings (256 entries each; indices in a 0x20-byte control object, consumer
at +0, producer at +0x10): events (0x70/0x74, 44-byte entries), four log lanes
(0x71/0x75), statistics (0x72/0x76, 44 bytes), trace (0x73/0x77, 36 bytes),
device control (0x6c/0x6d, 20 bytes), three work rings (0x60/0x61, 0x64/0x65,
0x68/0x69, 20 bytes). Apple's order per interrupt: acknowledge it, the four
log lanes, events, statistics, trace; the consumer index goes back only after
the entries were read, with a full barrier.

Events: type 0 alive (word 1 was 0x8000a120 in his logs), type 1 a job's
stamp (two per job), recovery (the firmware stopped; iOS restarts the GPU), 7
page request, 8 channel error. A healthy firmware posts about 290 alive events
and raises its interrupt about 575 times a second, idle or not.

### The governor ("policy", object 0x4a) and why it is pinned

The firmware has its own DVFS governor. Its inputs: the clock table at
platform+0x710 (highest index), +0x714 (count), +0x718.. (MHz per Fender state:
0, 340, 474, 550, 723, iOS's values on J71t, captured 2026-11-01); limits at
control+0x1028 (3348, the power envelope; iOS has 3851), +0x102c (floor),
+0x1030 (ceiling 400); overrides control+0x1038 / +0x1040, which the firmware
copies to policy+0x68c / +0x6f4 on every pass. It requests state
`min(policy+0x6f0, policy+0x688, control+0x102c) / 100`.

The backend uses his "pinned" record, the only one every boot of his consumed
command 8 with: floor = override = 100 * q, ceilings 400, the sampler off
(policy+4 = 0), and for q > 1 the record pre-set to the target (policy+0x28 =
policy+0x2c = q, +0x30 = 0, +8 = 0) so the firmware's boot sampler finds the
core where it wants it; the host powers the core up in state q itself.
Pinned, the firmware never changes the state and never powers the core off.

What he measured on the way:

- The firmware copies the clock table at boot and its scheduler reloads its
  timer from the entry of the current state; with the table left zero that
  entry is 0. iOS has 340 for state 1 (platform+0x71c), and his default
  boots install it ("latency114").
- platform+0x784 is added to the AIC's time to form the firmware's clock. iOS
  writes its abstime offset; an absolute counter value put the firmware's
  clock an uptime ahead of every host stamp. Linux writes 0.
- policy+0x8f8 is the governor's last-sample time. With policy+0 = 1 and
  policy+4 = 16 (iOS's persisted record) and a stale stamp, the boot sampler
  ran the load governor on empty accumulators and went to state 0: no ALIVE
  ("seed 2", 2026-11-03). Re-stamping just before the release fixed the path
  but is not needed with the sampler off.
- policy+4 = 16 with control+0x20 = 0 wedged the first TA ("latency123"
  T2/T3); installing iOS's whole record at map with the release at state 1
  came ALIVE but never consumed command 8 ("integration-g", 2026-11-11).
- platform+0x6c8 must be 1 on this path (with 0 the firmware skips its policy
  loop, "dc variant 7"); platform+0x6d4 = 1 turns on per-tick cache
  maintenance and is left 0.

## Not ported

### The phase runner and its stages

`apple-g5p` runs the boot as named phases (`validate`, `prepare`, `wake`,
`id`, `sample`, `map`, `arm`, `load`, `release`, `events`, `control-*`) from a
sysfs `command` file or its autoboot work, each latching a result. The backend
has one function per step and no runner. Dropped with it:

- **`sample`**: reads T0/T1 TXENABLE/TXSTATUS (0x04800000/10, 0x04801000/10)
  through the slave port before the first boot stream. Diagnostic.
- **`arm`**: stores the stream without its last write and powers up with the
  reset still held, then reads SOFT_RESET back as 0x100000000 to prove Fender
  executes the stream. Proven; the backend releases directly.
- **`load`** as a stage: a memcmp of the mapped image against the packed one.
- Snapshots of Fender (0, 8, 0x10, 0x14, 0x1000, 0x1004, 0x2010, 0x2014,
  0x20000) and of R0 around each stage, the PMGR gfx word decode, the FIFO
  dump, every `G2 ...` log line.
- The SHA-256 of the LDR and of the packed image checked in the kernel, and
  the parser's pins (exactly 119112 bytes, 94 records, one start record of 32
  bytes). The firmware recipe pins the file's digest when it extracts it.

### Alternate boot paths and their knobs

All of these were boot-time switches ("latched at map"); the defaults were
r73b case 1, fast 1, r73e 1, r74 1, r74f 1, r74g 1, r74h 0, r87b policy 1,
"nopatch" 1. The backend has no switch.

- **r73b** (crash-first interrupt diagnostics): serves all rings from the
  interrupt thread with breadcrumb rows kept in RAM (256 rows), an interrupt
  cap (8192, later 1048576) and an optional watchdog. Its functional part,
  the ring order and the acknowledge-first rule, is ported; the rows, caps
  and watchdog are not.
- **r73c / r73e** ("fast control", "first-ALIVE submit"): command 8 is
  prepared before the release and published from the interrupt at the first
  ALIVE, to measure release-to-ALIVE and release-to-submit times and to close
  the window before the firmware's first power decision. The backend
  publishes it from process context after its ALIVE poll (1 ms steps); the
  staged phases before r73c did the same with longer gaps and the firmware
  consumed it.
- **r74e and "wake112"** (the power proof): the release polls R1+0x1000 for
  the requested state for 1.8 ms (10 ms above state 1) and stops as soon as
  ALIVE is seen, because after ALIVE only one owner may read R1. On warm
  boots ALIVE could arrive before the first read (off 1500 ns, on 375 ns,
  nothing sampled); that case and "request still in flight" are deferred to
  the first-ALIVE handler instead of counted as a failed power-on. See the
  second race fix below. The backend's poll simply continues for up to
  100 ms regardless of ALIVE.
- **r74 / r74b** ("forced-ready epoch", RAM-only waits): raises the caps and
  makes the wait loops read RAM only.
- **r74f** (deferred doorbell): if the power state is not "on" when a DC3
  attach pair or a TA is published, the host repeats the off/on transition
  (bounds 1000 us off, 1800 us on) and the first ALIVE afterwards owns one
  doorbell attempt (bound 250 ms). Belongs to render attach; not needed to
  boot.
- **r74g** (firmware-owned MMU pass): the firmware's DC3 handler
  (0x1000218c..0x10002218) writes R0+0xe140/0xe148 for the slot, copies the
  host's generation to its cached one and issues the catalogue invalidation
  R0+0xe138 = (N << 3) | 4; a consumed DC3 pair is the receipt, so the host
  does no MMU register access. The TA is then published without a doorbell
  and given a grace of four ALIVEs or 20 ms to be taken; root+0x3980 = 2
  means the firmware entered recovery. This is how a client's page tables
  reach the firmware and will be needed for jobs.
- **r74h, r80..r96**: observers (trace tail, register banks read from the
  interrupt). Diagnostic.
- **r87b** (attach policy): in two campaigns the DC3 attach read the power
  state as 0x10/0x11, the host's off/on repair timed out (-110) and the
  machine froze, while every retained case shows the firmware consuming the
  pair by itself. Policy 1 ("self-drain"): no repair, no bell, wait for the
  consume in RAM for 3 s. Again for render attach.
- **heartbeat116**: replaced the lifetime interrupt/event caps (which made an
  idle, healthy GPU fail after about 30 minutes) by per-window rates. The
  backend has no cap.
- **r73f cases 1..4, r75, r79** (patched firmware images) and **firmware
  patching** as such: `apple-g5p` could boot a modified image; "nopatch"
  (Apple's original image on the r74/r74f/r74g path) became the default
  after its device pass on 2026-11-03. Only the names and the refusal logic
  are in #74; the patches themselves are not, so their contents are not
  recorded here. The backend boots Apple's image only and checks nothing
  that a patched one would need.
- **R46**: the first SOFT_RESET value with bits 56..59 added (VBS/VCE/TE3/
  TILING_CORE in the other register layout). Expected to be a no-op; off.
- **Release ACK**: after the release, clear EVENT_STATUS bit 9 (0x200) once
  through R0+0x138. A notification acknowledge only; off by default.
- **dc variants 1, 5, 6, 8, 9** of the init data (cache-bypass aliases for
  the device-control pointers, control+0x30 = 0, iOS's DVFS words). Only
  variant 7 flew by default, and its one change (platform+0x6c8) is undone
  again by the default path: the init data is as `g5p_make_init()` builds it.

### The overclock

`gpu-oc.h`, `boot_gpu_oc_mhz`: PMGR's PSINFO table (PMGR+0x70000 + 0x10 * n)
holds what Fender applies for power state n; LLB writes slots 0..4 once per SoC
reset. Word 0 = core-rail DAC code [31:24], PLL valid [21], multiplier
[20:12], predivider [8:4], postdivider-1 [3:0]; word 1 [7:0] = buck8 code;
word 2 = timing. f = 24 MHz * mult / prediv / postdiv. Record 4 on this chip
is 0x8a2f1041 (723 MHz, core 881.25 mV, buck8 987.5 mV). His overclock wrote
a record 5 from record 4 with only the multiplier moved (726..1080 MHz, VCO
at most 2160 MHz, Apple's own 804/850/900 MHz descriptors preferred),
extended the firmware's table to state 5 and powered up there, with a
thermal stop that rebooted the firmware at state 4.

Not ported: it changes a volatile PMGR table outside the GPU, and the product
bar is the stock clock. The table decode is the useful part if the GPU's OPP
table is ever filled from PSINFO.

### The firmware's own idle park ("self-park")

With control+0x20 = 1 (iOS: platform word 0x00200545 & 1) the firmware parks
itself when idle (0x1001cec8: drain, R1+0x1004 = wake target, R1+0 |= bit 29,
then bit 30, state 0) and reboots on the next doorbell; the kext then only
kicks (R1+0x14 = 7, R0+0xb00 at any state) instead of power-cycling. A parked
firmware wakes into its sampler, which divides by policy+4: the pinned record
(policy+4 = 0) must not be parked. His driver armed the park only after a
complete boot, writing iOS's coefficient block (47 words of policy), the
3851 mW envelope, policy+4 = 16 and then control+0x20 = 1, in that order.
R1+0[30:29] = 11 with state 0 and R1+8 = 0 is what Apple's kext accepts as
quiescent before sleep.

Not ported: the backend stops the firmware from the host when idle (runtime
PM), as on the A10. If that proves too slow or does not repeat, this is the
alternative, and the coefficient block is in `perf-envelope.h`
(`g5p_park_policy[]`).

### Power cycling, recovery, dirty start

- **r33**: hold the gfx power gate (PMGR 0x80340) off for a while before the
  first Fender write, because R0+0x418/0x41c keep the previous boot's value
  through a 10 us cycle. Auto-PM (bit 28) is cleared with the off target and
  set again after the on target is reached, as pmgr-pwrstate does.
- **In-boot recovery** (`recover.inc`): freeze, mask the interrupt, cycle the
  gfx gate (10 ms off), then wake / map / ... / control again with a new set
  of firmware memory. Measured to bring back a wedged device. iOS does the
  same: stop, platform power cycle, new boot. The backend's restart is a
  runtime-PM cycle, which also turns the gate off and on, but it does not
  enforce the 10 ms and a hang recovery (`pvr_apple_reset()`) does not cycle
  the gate at all yet.
- **dirtystart108**: a state machine that classifies how the GPU was left
  (cold, kexec, gate seen off) and refuses a release or a DRM claim on bad
  evidence. Dropped with kexec.
- **Teardown / kexec quiesce** (`teardown.inc`, #125): leaves a known state
  for the next kernel. Dropped unless kexec becomes a product feature.
- **r41**: replay of PMGR bridge settings (0x201f00000, 0x201f80000). Off by
  default; an experiment.

### The two lost-GPU races of #125

1. **A phase's doorbell skipped** ("3d-silent"). The publish-time doorbell of
   a render phase was skipped when the engine's completion-event count had
   moved since the job was armed. With two jobs in flight the previous job's
   3D completes during this job's TA, the count moves, and this job's 3D was
   taken for retired: no doorbell, the firmware did not consume the command
   until something else woke its scheduler, and after a second of silence the
   job and its context were lost (glmark2 terrain: 2 of about 11 runs).
   Apple rings after every ring entry without any check, and a doorbell for a
   consumed entry adds no work. His fix: skip only when the phase's own stamp
   shows it retired. For the backend's job path: ring after every entry.
2. **A power-on request that ALIVE overtook.** The firmware boots without
   waiting for the cores' power-on, so ALIVE can arrive while R1+0x1000 still
   has bit 4 set next to the target (0x14 for state 4). Counting that as a
   failed power-on left the GPU unusable for the boot (twice in a day on
   J71t, cold and warm; last reads 0x14 after 43 and 52 polls, good power-ons
   settle after 13 to 54 polls of 10 us). Apple's setGPCPerformanceState and
   setGPUCorePower wait for bit 4 for up to 30 s. In the backend: the release
   waits for the state up to 100 ms whether or not the firmware is alive, and
   the doorbell waits for bit 4 (`s8003_core_ready()`).

### Register access rules ("MMIO licence")

`mmio-licence.h`: iOS allows an R0 access when the gfx gate is on, the wrapper
is up, R1+0 bit 29 is clear (the firmware raises it before it powers the core
down) and the state is 1..max. His driver adds: no request in flight (bit 4)
and R1+0 != 0. R0 must not be touched with the core off or in transition, and
no Fender register with the gfx gate off. The backend touches R0 only for the
core ID after the wake and for the doorbell after the state settled, and
Fender only while the device is runtime-active; it has no general accessor,
because it reads no GPU register while the firmware runs.

### Smaller things

- **The page at GPU address 0** ("sink", "round 12"): one zeroed page mapped
  at address 0 of the firmware's context before the release, a containment
  for stray accesses; iOS optionally maps a dummy tree for all addresses. It
  was installed on every boot of his. The backend does not map it in the
  kernel VM (user VMs get a zero page at 0 as on the A10); if the firmware
  faults at 0 on release, this is the first thing to add.
- **Cleaning the page tables' cache lines** (`dcache_clean_poc` on catalogue,
  directory and leaves before the release): marked experimental; `pvr_mmu.c`
  syncs its tables through the DMA API.
- **The firmware's interrupt** (AIC 174, the fifth of the node): the firmware
  sets it through the AIC's software-set register (AIC+0x4014, bit 14) and
  the host clears it at AIC+0x4094 before draining. The backend does not take
  the interrupt yet (the AIC driver cannot clear a software-set interrupt)
  and reads the rings every 100 ms.
- **Interrupt self-test** (host sets the software interrupt itself): dropped.
- **PM pages** (two pages after the shared memory, eight per-context PM sets)
  and the event/recovery bookkeeping of the render path: come with jobs.
- **Timing values**: Fender polls 10 us / 100 ms; ALIVE wait 3 s; command 8
  wait 3 s; attach consume wait 3 s; first-ALIVE settle 2 ms; render waits
  20 s soft, 45 s, 60 s hard; quiesce lock 2 s.

## Open on hardware

- Does the boot work at all in this shape (kernel VM page tables, shmem
  pages instead of DMA-coherent ones, no page at address 0, polling)?
- Does stop, gate off, boot repeat (runtime PM), and how long does it take?
- Does the firmware mind that its interrupt is never acknowledged?
- Which of the first four interrupts fire, and for what?
