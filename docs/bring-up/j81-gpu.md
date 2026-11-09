# iPad Air 2 (J81) GPU: bring-up status

This is a field report on GPU work for the iPad Air 2 Wi-Fi (`iPad5,3`, J81,
Apple A8X/T7001, Imagination PowerVR GXA6850). It records experiments on one
physical device and offline analysis of Apple kernelcaches through
8 November 2026. It is not a driver, a device-profile change or a procedure.
J81's `gpu = "missing"` in `devices/j81/device.toml` is unchanged.

The work was done in [mattonik/ipad-nixos](https://github.com/mattonik/ipad-nixos)
on Linux 7.3.0-rc1 (Hoolock `6831bc7`, the same pin as Orchard's `apple-4k`
layer). The detailed notes are under that repository's
[`research/`](https://github.com/mattonik/ipad-nixos/tree/main/research)
directory. Start from
[`research/gpu-status.md`](https://github.com/mattonik/ipad-nixos/blob/main/research/gpu-status.md).

## Measured on the J81 device

| Step | Observation | Boundary |
| --- | --- | --- |
| GFX power | A guarded PMGR power cycle of the GFX domain finished and the PMGR register read back `0x3ff` (target and actual state 15). | Shows only that the power state was reached. It does not show that the clocks or resets the core needs are correct. |
| Bridge tables | The GFX bridge selectors 9 and 10 were replayed as 66 32-bit stores (33 per selector) to `0x201f00018`, `0x201f40000`–`0x201f4007c`, `0x201f80018` and `0x201fc0000`–`0x201fc007c`. Every software checkpoint passed. | Only `0x201f00018 = 0x80010004` was read back from the table targets. The other 65 stores are unverified. |
| GPC | GPC init (reg index 1, `0x201e00000`) reported state 1. | — |
| RGX identity | The first core-ID read at `0x201000020` (reg index 0, offset `+0x20`) never completed. The device hung and USB networking dropped. | Cause unknown. No subsequent core-ID test has been run. |
| MCC | One later, watchdog-supervised read returned `0x200000830 = 0`. | The value does not show the MCC state. |

No GPU firmware has run, no job has been submitted and no frame has been
rendered. A8X has no EL2, so m1n1 hypervisor MMIO tracing of iOS is not
possible on this SoC.

### GFX perf-state table: snapshot with GFX off

A separate read-only PMGR snapshot on 2026-11-08 was taken after a boot that
had only run the MCC read-once test. No GFX power-up, bridge write, GPC or RGX
access happened in that boot. PMGR reported GFX off (`0x20e020320 = 0x300`,
target and actual state 0); `0x20e01c200` was `0x80000000` (bit 31 only).
The table entries decoded as:

| Entry | Observed clock | Validity |
|---|---:|---|
| 0 | raw word `0` | Invalid; valid bit clear, not a selected state or a 0 MHz clock |
| 1–4 | 365, 450, 500, 550 MHz | Valid entries |
| 5–8 | 182.5, 225, 250, 275 MHz | Valid entries |
| 9–15 | raw words `0` | Invalid; valid bit clear |

The eight valid frequencies match the saved ADT `perf-states`. The snapshot
confirms the table while GFX was off; it does not show which state is selected
after power-up. The control word's selection-field layout is unknown, so its
bit 31 value does not identify a selected state.

An attempt to boot the guarded power-up payload and then read the table did
not reach power-up: `gpu-pmgr-once.ko` returned `EINVAL` before register
access because the MCC payload's device tree lacked the guard property the
module requires. A subsequent read in that same boot still showed GFX off and
control word `0x80000000`. No power transition occurred in that attempt, and
no post-power perf-table snapshot is available. The [public snapshot and its
boundaries](https://github.com/mattonik/ipad-nixos/blob/ae0d176abb73a67a2ef4c5b8cf2b8150a7ab97df/research/2026-11-08-j81-gfx-perf-state-live-read.md)
records the measurement.

## Offline comparison with Apple's sequence

These findings come from static analysis of the iPad5,3 iOS 8.1 (12B410) and
iPadOS 15.8.8 (19H422) kernelcaches. They do not come from register traces.

- **Before the ID read, the G4G probe makes one clock enable and one power
  enable for `clock-gates[0]`/`power-gates[0]`.** iOS 8.1 and iOS 15 agree on
  this. Neither return value is checked. iOS 15 reads identity through
  `reg` index 0, not through a separate mapping.
- **On iOS 15 the clock enable appears to use the shared ApplePMGR state
  machine.** The J81 ADT names its PMGR `pmgr1,t7001`, matching the
  `AppleT7001PMGR` personality; this is a static personality/ADT inference,
  not a runtime binding trace. The older `AppleT7000PerformanceController`
  bridge handler matches `pmgr,t7000`, which does not match that J81 node.
- **The GFX `devices` record has no parent power IDs, no flags and no
  related-device index.** No other domain is powered first because of GFX.
- **Bridge settings are written in the same power-up transition, right after
  PMGR reports state 15.** Selector 9 is written in full before selector 10.
  The iOS 15 ADT `bridge-settings-9`/`-10` tables are byte-identical to the
  8.1 tables. The ADT has no `bridge-settings-version` property; the available
  code suggests an absent property favors a zeroed allocation of the legacy
  format, but the runtime selection is unknown. The static tables' address,
  value and order sequence matches the Linux test4 replay. Its software
  checkpoints passed, but only one table target was read back.
- **The `0x2700c001`/`0x2700c002` codes around the PMGR transition are
  kdebug trace points.** They have no hardware effect.
- The pre-state and post-state helpers appear to only disable interrupts and
  take locks. They contain no PMGR register access.

The traced path identifies a PMGR state change and selector 9/10 bridge writes.
Linux test4 attempted the matching power cycle and writes, but most bridge
targets were not read back. Other indirect targets or boot-time state may also
matter. These observations do not establish that every relevant effect was
reproduced; the core-ID hang remains unexplained.

### What LLB and iBoot do with the perf-state table

Offline analysis of the iPad5,3 iOS 8.1 (12B410) boot stages found that LLB
programs the 16 table entries at `0x20e01c000 + n*0x10` and their `+4` words;
iBoot reads the entries to build the ADT `perf-states`. LLB sets bit 31 of
`0x20e01c200`, matching the pre-power snapshot, and also writes `0x40000001`
to `0x20e01c300` (meaning unknown). No state-select write was found in LLB or
iBoot. This narrows the bootloader comparison but does not establish the
selected state after GFX power-up; the available live snapshot was taken with
GFX off. See the [public
LLB/iBoot analysis](https://github.com/mattonik/ipad-nixos/blob/74683abec98fe22b14af0e840531a55a38cfca35/research/2026-11-08-j81-iboot-llb-gfx-perf-state.md)
and the [follow-up perf-table analysis](https://github.com/mattonik/ipad-nixos/blob/ae0d176abb73a67a2ef4c5b8cf2b8150a7ab97df/research/2026-11-08-j81-gfx-perf-state-live-read.md).

### Other LLB PMGR writes and Linux handoff limits

A bounded offline scan of the iPad5,3 iOS 8.1 (12B410) LLB found additional
PMGR operations in the same setup routine. The sequence at `0x20e020400` is
at PMGR offset `0x20400`, which the [T7001 PMGR device-tree description][pmgr-dt]
names `ps_sep`/`sep`. The [Linux PMGR driver][pmgr-driver] defines bits 7:4 as
the actual state for these described state words. LLB clears selected bits and
polls until that field is `0xf`. This maps to SEP power-state handling; it is
not a GFX register or evidence of a GFX dependency.

In that routine, LLB also sets bit 31 at five page bases from `0x20e010000`
through `0x20e014000`, copies a static 47-word table to `0x20e010000`–
`0x20e0100b8`, and copies six words to `0x20e00f208`–`0x20e00f21c`. The
available T7001 DT does not name these offsets or the helper's other indexed
pages. Their domain and purpose remain unknown; proximity to GFX setup does
not identify them as GFX or CPU configuration. The [bounded scan][llb-scan]
describes only static disassembly, not runtime traces; no extracted firmware
or private artifacts are included here.

The LLB setup routine is directly reachable from its relocated normal startup
path, with no boot-type branch around that call in the inspected sequence.
That establishes static reachability, not execution in the Linux test boot.
The documented Linux handoff runs from DFU/checkm8 through PongoOS and m1n1
to Linux ([boot script](https://github.com/OrchardLinux/orchard/blob/j81-gpu-status-clean/boot/run_m1n1_debug.sh));
it does not record an LLB or iBoot handoff. A post-boot PMGR snapshot matching
the LLB table establishes that those values were present in that Linux
session, not who last wrote them or whether they persisted continuously from
LLB. There is no pre-DFU snapshot or write trace to establish their origin or
continuity across the transition.

Orchard's existing [A7–A11 m1n1 importer][orchard-perf-import] represents
each ADT `perf-states` row as two `u32` values, frequency in Hz and voltage in
mV, then maps rows to OPP frequency, microvoltage, and level. J81's reported
table shape is compatible with that 8-byte pair format; valid entries are
indices 1–8. This is a static format comparison, not an integration test on
J81's raw ADT blob. The importer requires an existing FDT `gpu` node with an
empty `operating-points-v2` table; it does not create a J81 GPU node or prove
that any performance level is selected. PMGR domain state 15 (`0x3ff`) and a
GPU performance level are separate state concepts. See the [format comparison][perf-layout].

[pmgr-dt]: https://lkml.iu.edu/hypermail/linux/kernel/2502.0/01346.html
[pmgr-driver]: https://github.com/torvalds/linux/blob/master/drivers/pmdomain/apple/pmgr-pwrstate.c
[llb-scan]: https://github.com/mattonik/ipad-nixos/blob/main/research/2026-11-08-j81-llb-pmgr-write-scan.md
[perf-layout]: https://github.com/mattonik/ipad-nixos/blob/main/research/2026-11-08-j81-orchard-perf-state-layout.md
[orchard-perf-import]: https://github.com/mattonik/orchard/blob/j81-gpu-status-clean/boot/m1n1/patches/orchard/0011-kboot-Fill-the-A7-A11-GPU-s-performance-states-from-.patch

## Open work

- Check whether runtime PMGR configuration supplies a GFX `state-config`
  record eligible for the optional selector-1 counter read. Offline analysis
  shows that the `bcc56c` path calls `bd6328` with selector 1, which reads
  PMGR counter values and performs no register store; `bcc56c` does not use
  the other selectors. The captured ADT's energy-counter list omits GFX and
  has no `state-config` property, which suggests but does not prove that no
  eligible GFX record exists. The runtime object/class at `pmgr+0x160` and
  the record values remain inferred or unknown. See the [selector-1 and
  SoC-tuner analysis](https://github.com/mattonik/ipad-nixos/blob/1ed4af067c1be8fc2bce5a37fbf69504add9e32b/research/2026-11-08-j81-ios15-soc-tuner-device-sets.md)
  and [PMGR timing-call review](https://github.com/mattonik/ipad-nixos/blob/799304d07a26c3b88d0a1b6a02b2e790db77d459/research/2026-11-08-j81-ios15-bcc56c-timing-accounting.md).
- Determine whether the conditional display-idle/MCC callback is reached
  before the first RGX ID read. Static iOS 15 analysis places GFX in SoC-tuner
  device set 0 and recovers conditional tuner policy, but does not establish
  whether or when the busy notification reaches that policy on the pre-ID
  path. The G4G clock-enable path itself does not call the tuner, and test4
  did not reproduce this callback. See the [SoC-tuner analysis](https://github.com/mattonik/ipad-nixos/blob/1ed4af067c1be8fc2bce5a37fbf69504add9e32b/research/2026-11-08-j81-ios15-soc-tuner-device-sets.md).
- Obtain a read-only perf-table snapshot immediately after the guarded GFX
  power-up, with the required `gpu-pmgr-once` device-tree guard. The current
  pre-power snapshot cannot answer which state is selected after power-up.
- Check any remaining iBoot/iBEC state that a checkm8/PongoOS boot does not
  preserve; the LLB/iBoot analysis confirms that LLB programs the table and
  iBoot reads it, but it did not identify a software state-select write.
- Do not repeat the RGX core-ID read, guess clocks or resets, or write MCC
  registers until one of these gives a concrete register difference.
