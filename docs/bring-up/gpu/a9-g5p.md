# A9 GPU (G5P, iPad 5): what the powervr backend took and what it left

The iPad 5's GPU (S8003/A9, PowerVR GT7600, Apple's "G5P") was brought up by
Berkay Kösebay in his own driver, `apple-g5p` (orchard #74, with #119, #120
and #125 on top). Orchard runs the GPU from the upstream `powervr` driver
instead, as a second chip next to the A10 (`pvr_apple_s8003.c`). This page
records everything of his boot work that the backend does **not** carry, with
the reason it existed and what he measured, so none of it has to be found
again. It also says where the ported parts live now, and it defines what
userspace hands the kernel for a job on the A9 ("The job interface for
userspace"): the Mesa port is built from that section.

Everything below about the hardware and the firmware is his finding, read from
his code and its comments (`drivers/gpu/apple-g5p/` and
`drivers/gpu/drm/apple-g5p/` in #74, with #119 and #125). Firmware
addresses (`0x1000....`) are the META image's, kext addresses (`c9....`,
`6c9....`) are the iOS 16.7.10 (20H350) AGX kext's, as he cites them. Nothing
here was re-measured: the backend has not run on hardware yet.

State of the backend: it boots Apple's unmodified firmware, attaches VMs,
writes geometry, fragment and compute jobs, completes them from the
firmware's stamps, restarts the firmware when a job hangs, and stops it when
idle and for sleep. The core counts as supported, so the GPU starts on every
boot of a board whose device tree enables it. None of it has run on hardware:
the first run is Berkay's test of `main` on his J71t.

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
| `lifecycle.inc` control prepare / submit / wait, `kick.inc` | `s8003_first_command()`, `s8003_device_control()`, `s8003_doorbell()` |
| `lifecycle.inc` `g5p_boot_park()` | `pvr_apple_s8003_stop()` |
| `boot.inc` `g5p_boot_sink_init()` (zero page at GPU address 0) | `s8003_map()`: `zero`, in the kernel VM, plain leaf flags as his |

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
| 0 | 1 page | zeroes ("sink", his round 12): catches an access at address 0 while the firmware starts; iOS can map a dummy page for every unmapped address | none |

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
  doorbell attempt (bound 250 ms). The backend does not repeat a power
  transition: it rings the doorbell when no request is in flight and waits
  for the firmware either way (`s8003_doorbell()`).
- **r74g** (firmware-owned MMU pass): the firmware's DC3 handler
  (0x1000218c..0x10002218) writes R0+0xe140/0xe148 for the slot, copies the
  host's generation to its cached one and issues the catalogue invalidation
  R0+0xe138 = (N << 3) | 4; a consumed DC3 pair is the receipt, so the host
  does no MMU register access. The TA is then published without a doorbell
  and given a grace of four ALIVEs or 20 ms to be taken; root+0x3980 = 2
  means the firmware entered recovery. This is how a client's page tables
  reach the firmware; in the backend `s8003_slots_load()`, with a doorbell
  and without the grace.
- **r74h, r80..r96**: observers (trace tail, register banks read from the
  interrupt). Diagnostic.
- **r87b** (attach policy): in two campaigns the DC3 attach read the power
  state as 0x10/0x11, the host's off/on repair timed out (-110) and the
  machine froze, while every retained case shows the firmware consuming the
  pair by itself. Policy 1 ("self-drain"): no repair, no bell, wait for the
  consume in RAM for 3 s. The backend's attach waits 3 s the same way and
  rings the doorbell first unless a power request is in flight.
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
  same: stop, platform power cycle, new boot. In the backend a hang
  recovery (`pvr_apple_reset()`) stops the firmware, runtime-suspends the
  GPU, which turns the gate off, waits 10 ms and leaves the boot to the next
  job. If another job holds the GPU powered at that moment the gate stays
  on and the firmware is restarted in place. An idle runtime-PM cycle
  usually lasts far longer than 10 ms, but nothing enforces it: a job that
  arrives right after the GPU went idle boots the firmware after a shorter
  gate-off time.
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
   shows it retired. The backend does the same (`s8003_publish()`).
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

- **Cleaning the page tables' cache lines** (`dcache_clean_poc` on catalogue,
  directory and leaves before the release): not ported as a step of its own,
  because `pvr_mmu.c` does it for every table it changes
  (`dma_sync_single_for_device()`, which is that clean on arm64).
- **The firmware's interrupt** (AIC 174, the fifth of the node): the firmware
  sets it through the AIC's software-set register (AIC+0x4014, bit 14) and
  the host clears it at AIC+0x4094 before draining. The backend does not take
  the interrupt yet (the AIC driver cannot clear a software-set interrupt)
  and reads the rings whenever it looks for a finished job (every 100 us
  while one is in flight) and every 100 ms otherwise.
- **Interrupt self-test** (host sets the software interrupt itself): dropped.
- **The device PM** (control page at shared+0x5c000, list and free list in
  the two pages after the shared memory): the first context's PM in his
  driver. The backend has PM sets only, starting right after the shared
  memory.
- **Events beyond "alive" and a job's stamp**: recovery (the firmware
  stopped), page request (type 7), channel error (type 8) with his fault
  bookkeeping per context. The backend logs such an event and goes on
  reading; a job the firmware gave up times out into the reset.
- **Timing values**: Fender polls 10 us / 100 ms; ALIVE wait 3 s; command 8
  wait 3 s; attach consume wait 3 s; first-ALIVE settle 2 ms; render waits
  20 s soft, 45 s, 60 s hard; quiesce lock 2 s.

## The job path, as ported

His compute and render paths are not the A10's ring-of-commands model. In the
backend the A9 therefore submits and completes its own jobs
(`pvr_apple_s8003_submit()`, `pvr_apple_s8003_engine_done()`); the fences and
their polling, the scheduler queues and their timeout, the order of scenes
across VMs, runtime PM, sleep and the reset are `pvr_apple.c`'s, shared with
the A10.

| apple-g5p | powervr backend |
|---|---|
| `render-user-layout.h` `g5p_ru_channel_init()`, `compute-user-layout.h` `g5p_cu_channel_init()` | `s8003_channel_init()` |
| `render-user-layout.h` `g5p_ru_fill_ta()`, `g5p_ru_fill_ta_v3()`, the TA half of `g5p_ru_rt_fill()` | `s8003_fill_ta()` |
| `g5p_ru_fill_3d()`, `g5p_ru_fill_3d_v2()`, `g5p_ru_fill_3d_width()`, the 3D half of `g5p_ru_rt_fill()`, `render-user-msaa.h` `g5p_ru_fill_msaa()` | `s8003_fill_3d()`, `s8003_fill_output()` |
| `render-user-geometry.h` `g5p_ru_rt_derive_samples()` | `s8003_rt_derive()`, from the HWRT dataset's width, height and samples |
| `render-user-layout.h` `g5p_ru_job_at()` (record links, microsequence, command, pointer ring) | `s8003_job_write()` |
| `compute-user-layout.h` `g5p_cu_job()`, `g5p_cu_sku()` | `s8003_submit_compute()` |
| `drm-render-v3.inc` `g5p_rv3_publish_ta()`, `g5p_rv3_publish_3d()`, `drm-compute-v1.inc` publish | `s8003_publish()` |
| `render-user-wake.h` `g5p_wk_ring()`, `drm-render-v3.inc` `g5p_rv3_dc_kick()`, `kick.inc` | `s8003_doorbell()` |
| `render-user-layout.h` `g5p_ru_retired_pl()`, `g5p_cu_retired()` | `pvr_apple_s8003_engine_done()` |
| `drm-render-v3.inc` `g5p_rv3_fw_touch()` | `s8003_touch()`, at most once a millisecond while a job is waited for |
| `render-user-bind.h`, `drm-render-v3.inc` `g5p_rv3_bind()`, `g5p_rv3_unpark()`, `g5p_rv3_dc_wait()` | `s8003_slots_load()`, `s8003_vm_attach()`, `s8003_device_control()` |
| `render-user-mmu.h`, `drm-render.inc` `g5p_render_invalidate()` | `pvr_apple_s8003_mmu_flush()` |
| `render-user-window.h`, `drm-render.inc` `g5p_render_fw_entry()` (firmware window) | `pvr_apple_s8003_vm_init()`: the PM sets mapped in every VM at the firmware's addresses |
| `render-user-pmset.h`, `render-user-layout.h` `g5p_ru_pm()`, `drm-render-v3.inc` `g5p_rv3_pmctx_build()` | `s8003_pm_build()`, `pvr_apple_s8003_vm_scenes()` |
| `render-user-pmset.h` park rule, `render_user_close_park` | `pvr_apple_s8003_vm_fini()` and the park VM |
| `recover.inc`, `power-r33.inc` (gate cycle, 10 ms) | `pvr_apple_reset()` with the chip's `gate_off_ms` |
| #119 `render_user_pipeline_depth` 2, `render_user_joint`, job slots | two scenes per VM (`scenes`, `vm_scenes()`), the fragment job submitted with its geometry job, two record slots per channel |
| #125 "ring a phase's doorbell unless its own stamp ..." | `s8003_publish()` |

### Channels and records

Each engine has one channel in the shared memory: geometry at shared+0x54000,
fragment at +0x58000 (a page each), compute at +0x5b000 (0x1000 bytes). In a
channel: control words at +0x100 (five watermarks 16 bytes apart, the host's
at +0x140; the ring's size, 16, at +0x150), a ring of 16 pointers at +0x200,
a timestamp queue (+0x1800; compute +0x900), two stamp words (+0x1840;
compute +0x940), a dependency object (+0x1900; compute +0x980).

A render job takes two pointer-ring entries, a 0x40-byte command and its work
record W (geometry 0x560 bytes, fragment 0x8c0), and has a microsequence
(geometry 0xc0 bytes, fragment 0x180). Odd jobs have command, W and
microsequence at +0x300, +0x400 and +0xa00 (fragment +0xd00), even jobs
packed from +0x1c00, so a job never rewrites what the firmware still reads
for the job before it. A compute job takes one entry: W (0x290 bytes) at
+0x400, microsequence at +0x800.

The command of a geometry job is `{6, slot, PM id, PM set}`: the firmware
takes the parameter manager's record from the set and rebuilds its free
list. His driver sends that only when the PM state is not known clean and
otherwise `{4, own stamp, seq - 1}` (`render_user_pm_record`); the backend
always sends it, his older and longer-flown form. The command of a fragment
job is `{4, address of the geometry channel's second stamp, n}`: wait until
that stamp reached n, the number of its geometry job. The firmware compares
signed, so a later geometry job's stamp does not block it.

To publish: an entry `{engine, channel, end}` in the engine's work ring
(0x61, 0x65, 0x69; `end` is how far the pointer ring is filled), the host's
watermark = `end`, the work ring's producer index, then the doorbell:
Fender+0x14 = 7 and its read-back, wait for no power request in flight,
R0+0xb00 = 0x10 (geometry, compute, device control) or 0x11 (fragment). The
doorbell is skipped only if both of the channel's stamps already reached the
job's number. His compute path rang no doorbell; the backend rings the
geometry engine's, which wakes the firmware's scheduler.

A job is done when both stamps reached its number and every watermark lies
between its end of the pointer ring and that of the last job submitted. His
driver also looked at the engine's idle words (channel object +0x724..), the
completion events and a 500 ms hold, to tell a job the firmware finished from
one it gave up; the backend takes the stamps alone, and a job whose stamps
never come times out after 5 s, which restarts the firmware.

### VMs: slots and tags

A VM gets two of the firmware's slots (7 to 255) with its first job, one for
geometry and compute jobs and one for fragment jobs: the page-table root's
address goes to object 0x86[slot] (64 bits), a tag to 0x84[slot], then two
device-control commands `{3, slot}` (the fragment slot's first). The
firmware's handler loads the root and copies the tag to 0x85[slot]; the
commands being consumed and the tag copied is the receipt. No job runs while
a slot is loaded.

The tag's top ten bits changing make the firmware drop the slot's
translations from the catalogue down when the slot's next job starts
(bits 21:12 from the directory, 11:0 the tables only). The backend adds
0x400000 to the tag of every loaded slot whenever powervr flushes the MMU
(any VM's page tables changed); his driver kept a counter per VM and level.

When a VM goes its slots are not emptied but loaded with the park VM's root
(a VM that maps only what every VM maps) and tag 0x500000, and slots and PM
sets are handed out in turn: the PM kept writing the free list of a context
through its geometry slot after the context was closed, two job switches
later at the latest (his release8 runs, page faults in the closed slot).
A parked slot is emptied (root 0, tag 0, `{3, slot}`) before a new VM takes
it.

After every start of the firmware all of this is told again: slots with the
VM's next job, PM sets with its next geometry job.

### The parameter manager's memory

A PM set is three pages in the firmware's GiB, behind the shared memory
(GPU address 0xe1c0260000 + 0xc000 * set; 12 sets, his driver had 8 and a
"device PM" the backend does not have):

- control page: +0x10 the META address of the last block's list entry, +0x14
  the free list's GPU address, +0x1c its bytes, +0x20 its entries, +0x24
  entries - 1, +0x34 0x4000, +0x40 the list page's META address, +0x54 and
  +0x58 the entries again. At +0x80 the scene's record (0xc0 bytes): +0xa8
  macrotile array, +0xa0 MList, +0xb0 VHEAP (GPU addresses in the VM), +0xb8
  the META address of list+0x40; the firmware saves the PM's state in the
  rest between a geometry job and its fragment job.
- list page: +0x60 = 1; from +0x80 one entry of 0x24 bytes per block: the
  META address of the entry before it, then the block's eight 16 KiB pages
  as 4 KiB page numbers.
- free list page: four consecutive 4 KiB page numbers per pool page.

A set's PM id is its number + 1 (W+0x10 of the geometry record, both
microsequences, the command).

The pool is `blocks` blocks of 128 KiB, kernel pages that are mapped nowhere.
The PM's own tables (5 pages: macrotile array 0x80 bytes, MList 0x10000,
VHEAP 0x600) are kernel pages mapped in the VM at
0x101000000 + 0x20000 * set, protected (PM/firmware only, bit 62) and without
a CPU mapping for userspace. His driver mapped them at 0x101000000 without
bit 62; see "Open on hardware".

All sets' three pages are mapped in every VM at the firmware's addresses
with the firmware's attributes (protected, cache coherent): the PM reaches
them through the job's slot. His driver shared one kernel-built directory
under the firmware's catalogue entry for that; the backend maps the same
pages through the VM's own tables.

### What is not ported from the job path

- His own UAPI, VM, buffer objects, resource lists and their validation
  (`drm-vm.inc`, `render-user-validate.h`, `compute-user-validate.h`): the
  backend takes addresses from the stream unchecked, as on the A10. An
  address userspace gets wrong faults in its own slot.
- The serial executor with its phases, the r74f/r74g/r87b attach machinery,
  job logs, latency and job-time accounting, receipts, PM snapshots, fault
  readers, the wake A/B, `job_slots`/`pm_once`/`pm_ctx` switches, spare
  pairs, the per-boot fault budget and frozen contexts.
- The render target's "main ring" of sixteen slices: the HWRT dataset has a
  region header buffer per RT data, which userspace alternates.
- Typed profiles V1 and V2 (allocator pools, `vdm_control`).
- The firmware's self-park and the retained-RAM sleep of #120: the firmware
  is stopped for runtime suspend and for sleep and booted again.

## The job interface for userspace

The A9 uses powervr's UAPI (`include/uapi/drm/pvr_drm.h`) unchanged: VM
contexts and maps, buffer objects, contexts of type `RENDER` and `COMPUTE`,
`CREATE_FREE_LIST`, `CREATE_HWRT_DATASET`, `SUBMIT_JOBS`, sync objects.
What differs from the A10 is listed here. The structures are in
`drivers/gpu/drm/imagination/pvr_apple.h`, the header values in
`pvr_rogue_fwif_stream.h`, the stream definitions (order of the fields in the
stream) at the end of `pvr_stream_defs.c`.

### Device

- The device query's GPU info reports BVNC 9.15.6.2. The heaps are the A10's
  (`pvr_apple_heaps`): general at 0x0800000000, PDS at 0x2a00000000, USC at
  0x2c00000000, region headers at 0x2ef0000000, all below 2^38.
- PDS code addresses in the firmware's program words are relative to the PDS
  heap's base, USC code addresses to the USC heap's base: the firmware is
  given those bases at boot. (apple-g5p used one base, 0x300000000, for
  both, and a 4 GiB window from it.)
- Reserved in every VM, outside the heaps: 0 (a zeroed page),
  0x200000 (a kernel page, unused on the A9), 0x101000000 to 0x101180000
  (the PM's tables), 0xe1c0260000 to 0xe1c02f0000 (PM sets).
- Context creation: the static context state is optional and only
  validated, as on the A10.

### Free list

`CREATE_FREE_LIST` is validated as on the A10 (the free-list buffer must be
a `PM_FW_PROTECT` object that userspace cannot map), but the list holds no
pages on the A9 and its buffer is not written. `initial_num_pages` (4 KiB
pages) is the size of the parameter manager's pool for the VM's renders:
`blocks = max(initial_num_pages / 32, (50 + tiles) / 32)`, both rounded up,
at most 128 (16 MiB); `tiles` is the render target's 32x32 tiles. A VM pins
one pool with its first render and a second one with its first render while
a scene of its own is still in the firmware, if the firmware has more than
half of its 12 PM sets left. There is no growth and no partial render: a
scene that needs more than the pool stops the firmware and the job times
out.

### HWRT dataset

`CREATE_HWRT_DATASET` is validated as on the A10 (MList, macrotile array and
VHEAP in protected objects), single layer, 1 or 4 samples, at most
8192x8192. Used on the A9:

- `width`, `height`, `samples`: the kernel derives the geometry words;
- `rt_data_args[i].region_header_dev_addr`: the region headers of RT data i
  (geometry W+0x28, fragment W+0x20). `region_header_size` must be at least
  `px * py * 5` bytes;
- `geom_data_args.tpc_dev_addr`, `tpc_size`: the tail pointer cache, at
  least `p2 * p2 * 8` bytes;

with `tx = ceil(width / 32)`, `ty = ceil(height / 32)`,
`mx = align(ceil(tx / 4), 4)`, `my` alike, `s = 2` for 4 samples else 1,
`px = mx * s * 4`, `py = my * s * 4`, `p2 = roundup_pow_of_two(max(px, py))`.
A job on a smaller buffer fails with `-EINVAL`.

Not used on the A9: `pm_mlist_dev_addr`, `macrotile_array_dev_addr`,
`vheap_table_dev_addr`, `rtc_dev_addr`, the ISP merge values, the free
list's own buffer. The PM's tables are the kernel's (above).

Userspace alternates the two RT datas between consecutive renders to one
target, as upstream does: the kernel may hold two scenes of a VM, and each
needs its own region headers.

### Jobs

A render is a geometry job and a fragment job on the same HWRT data, in one
`SUBMIT_JOBS` call or two. The kernel submits the fragment job to the
firmware right after its geometry job. A fragment job whose geometry job was
not submitted (or failed) ends with `-ECANCELED`. Partial-render fragment
jobs are refused (`-EOPNOTSUPP`), transfer contexts do not exist, as on the
A10. A compute job needs no render before it.

Each command stream is the Rogue command's stream followed by one extension
header and its data. The header word is `type << 29 | BIT(0)`; all three
A9 extensions have only bit 0. The kernel does not insist on the extension:
without it those words of the record are zero. The A10's extension types (1
for geometry and fragment) are refused on the A9.

**Geometry**: `PVR_STREAM_EXTHDR_TYPE_GEOM_G5P` = 2,
`struct pvr_apple_g5p_geom_regs`. From the Rogue command the kernel reads
only `regs.vdm_ctrl_stream_base` (W+0x98, low two bits cleared).

| field | drm_g5p_render_pass(_v3) | goes to (geometry W) |
|---|---|---|
| `u64 scratch_base` | `scratch_va` | +0x90, low three bits cleared |
| `u64 scratch_ptr[6]` | `scratch_ptr[6]` | [0..3] +0x468.., [4] +0x48 and +0x488, [5] +0x50 |
| `u64 ta_program` | `ta_program_va` - PDS base | +0x88 (R0+0x470): byte offset of the TA state PDS program from the PDS heap's base |
| `u64 sample_positions` | `sample_positions` | +0x40; 0 = the standard pattern (0x88, or 0xeaa26e26 for 4 samples) |
| `u32 context_pds[2][6]` | `context_pds` | +0x58, +0x70 |
| `u32 resume_pds[2][4]` | `resume_pds` | +0xa0, +0xb0 |
| `u32 ta_pds[4]` | `ta_pds` | +0xc0 (three words; the fourth is zero) |

**Fragment**: `PVR_STREAM_EXTHDR_TYPE_FRAG_G5P` = 2,
`struct pvr_apple_g5p_frag_regs`. From the Rogue command the kernel reads
`regs.isp_scissor_base` (W+0x60 with the low two bits cleared, +0x4a0),
`regs.isp_dbias_base` (+0x68, +0x490), `regs.isp_oclqry_base` (+0x70,
+0x78), `regs.isp_bgobjdepth` (+0x3a4, +0x6a8) and `regs.isp_bgobjvals`
(+0x3a8, +0x6ac); these were `scissor_va`, `dbias_va`,
`occlusion_query_va`, `bgobj_depth` and `bgobj_vals` of
`drm_g5p_render_pass_ext`. A zero depth or values word is taken as given
(apple-g5p's defaults for a pass without them were 1.0f and 0x300).

| field | drm_g5p_render_pass(_ext) | goes to (fragment W) |
|---|---|---|
| `u64 background_texture` | `background_texture` | +0xb8, +0x5f0 |
| `u64 zls[6]` | `zls[6]` | +0x80/+0x618, +0x88/+0x628, +0x90/+0x630, +0x98/+0x648, +0xa0/+0x650, +0x138/+0x6d0 |
| `u64 sample_positions` | `sample_positions` | +0x58; 0 = the standard pattern |
| `u32 background[8]` | `background` | +0x5b8..; [0], [1] also +0xb0, +0xb4; [4..7] also +0xc0.. |
| `u32 eot[4]` | `eot` | [1], [0], [2] at +0x3b4..; [0..2] at +0x680.. |
| `u32 eot_resume[4]` | `eot_resume` | [0..2] at +0x694.. |
| `u32 restore[4]` | `restore` | +0x600, +0x604, +0x610, +0x614 |
| `u32 pixel_output_width` | `pixel_output_width` | 2, 4 or 8 (0 = 2): +0x6b0 and the partition words |
| `u32 flags` | `msaa_flags` | bit 0 `PVR_APPLE_G5P_FRAG_MSAA_RESOLVE`, with 4 samples only |

The sample count is the HWRT dataset's; there is no `samples` field. With 4
samples the kernel writes the AA mode, ISP_CTL and the output partitions as
`g5p_ru_fill_msaa()` did.

**Compute**: `PVR_STREAM_EXTHDR_TYPE_COMPUTE_G5P` = 1,
`struct pvr_apple_g5p_compute_regs`. From the Rogue command the kernel reads
only `regs.cdm_ctrl_stream_base` (W+0x70). Compute jobs are submitted on a
`COMPUTE` context; they run on the VM's geometry slot.

| field | drm_g5p_compute_pass | goes to (compute W) |
|---|---|---|
| `u64 state[4]` | `state_va[4]` | +0x1ec.. |
| `u64 tail` | `state_tail_va` | +0x60, +0x20c |
| `u64 terminate` | `cdm_va + cdm_end - 4` | +0x214, +0x23c: the address of the stream's terminate word |
| `u64 pds_store` | `store.code \| store.data << 32` | +0x21c |
| `u64 pds_resume` | `resume.code \| resume.data << 32` | +0x22c |
| `u64 pds_terminate` | `terminate.code \| terminate.data << 32` | +0x68 |
| `u32 config[2]` | `config` | +0x10, +0x14 |
| `u32 pds_store1` | `store.control` | +0x224 |
| `u32 pds_resume1` | `resume.control` | +0x234 |

Fields in the stream follow the order of the tables, each naturally aligned;
an array is copied as bytes, aligned to 8.

What apple-g5p's passes had and the stream does not: `vdm_va` (the Rogue
command), `vdm_size`, `vdm_end`, `ta_program_size`, `scratch_size`, the
pools and `vdm_control` of the older profiles, `samples`, the resource list
with its access flags, `zls_address_mask`, `occlusion_query_size`,
`scissor_count`, `dbias_count`: the kernel does not validate ranges.

### Errors and loss

A job the firmware does not finish in 5 s ends with `-ETIMEDOUT`, as does
every other job in flight; the context it was on takes no more jobs (the
A10's rule), the firmware is stopped, the GPU's power domain goes off for at
least 10 ms if no other job holds it on, and the next job boots the firmware
again. Other contexts go on.

## Open on hardware

- Does the boot work at all in this shape (kernel VM page tables, shmem
  pages instead of DMA-coherent ones, polling)?
- Does stop, gate off, boot repeat (runtime PM), and how long does it take?
- Does the firmware mind that its interrupt is never acknowledged?
- Which of the first four interrupts fire, and for what?
- Does a render complete with the PM's tables mapped protected (bit 62)? His
  driver mapped them without it. If not, the fallback is his attribute for
  those five pages; a compute shader writing them must then be shown to
  fault or the pages need another guard.
- Does the firmware take PDS and USC code bases that differ (powervr's
  heaps, 0x2a00000000 and 0x2c00000000), and data addresses up to 2^38? His
  driver ran with one base at 0x300000000 and low addresses.
- Does a geometry job's `{6, ...}` command on every job, the stamps alone as
  completion, and a doorbell for compute behave as assumed above?
- Do two scenes of one VM in flight work with one PM set each from the
  start (his second set came with the first pipelined job)?
- Is 3 MiB at platform+0x77c the limit for PM sets (12 used, he had 8)?
- Does a reset's gate cycle through runtime PM bring a hung firmware back
  as his direct PMGR cycle did, and what happens when another job keeps the
  domain on (then the firmware is restarted without the cycle)?
- The cost of the page reads (`s8003_touch()`) per millisecond while jobs
  run, and whether they are needed with the backend's memory (his was
  DMA-coherent memory mapped non-cached; the backend's is shmem mapped
  write-combined).
