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

## Open work

- Disassemble the one unclassified action on this path. Its call path suggests
  a PMGR action at `0xfffffff006bd6328` in the 19H422 cache, under the PMGR
  command gate, from a timing helper for `state-config` records with byte
  `+0x57` set; the action itself has not been disassembled. Then check whether
  any J81 `state-config` row names GFX (`0x7c`).
- Trace the conditional display/MCC busy callback, which runs on a busy 0→1
  edge when framebuffer caching is enabled.
- Check what iBoot or iBEC leaves configured for GFX that a checkm8/PongoOS
  boot does not.
- Do not repeat the RGX core-ID read, guess clocks or resets, or write MCC
  registers until one of these gives a concrete register difference.
