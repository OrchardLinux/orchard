# iPad 3 (Wi-Fi), J1

The first A5X (S5L8945X) board of the apple-armv7 platform. It runs Project
Cascadia's A5 port with the iPad mini 1's drivers, plus a few J1 changes. The
device tree is `s5l8945x-j1.dts` (kernel/patches/import/cascadia/0023), from
Cascadia's `devices/a5/dts/j1ap.dts`. Cascadia's
[j1-a5x.md](https://github.com/rzxvx/Project-Cascadia/blob/main/devices/a5/docs/research/j1-a5x.md)
records the bring-up: addresses, the boot chain, the PMU, touch and Wi-Fi.
Device data by Stephen Cox.

## What ran, and on which build

Everything below ran on one iPad3,1 (A1416) from RAM, with Cascadia's build of
the kernel (GCC, Cascadia's config, an initramfs built in) and the
[J1 boot kit](https://github.com/stephencox/cascadia-j1-boot). The tree was
this one on 2026-10-26, and Cascadia PR #35's on 2026-11-05. Orchard builds
the same tree with clang 19 and the platform's config. That build compiles,
but it has not been booted on a J1 yet, so `device.toml` leaves these features
`untested`.

| Feature | Seen on Cascadia's build | Needs |
|---|---|---|
| USB serial (`ttyGS0`) | 2026-10-26 and 2026-11-05 | `earlycon=j1fb` in the bootargs, or the kernel stops before the gadget |
| Display (simplefb on iBoot's scanout) | 2026-10-26 | iBEC's teardown calls patched out (below), so the scanout stays live |
| Touch (Z2, `/dev/input/event0`) | 2026-10-26 and 2026-11-05 | a userspace bootload through `HXT_IOC_SET_DL` / `HXT_IOC_READY`, with the unit's calibration |
| Wi-Fi (BCM4330 on HSIC: scans 2.4 and 5 GHz, joins WPA2) | 2026-10-26 and 2026-11-05 | the second USB complex brought up from userspace (no PHY driver yet), then ehci-platform bound again |

## Boot

Orchard's launcher does not run this chain yet. The `jailbreak-kdfu` method
names it, and `docs/platforms-and-builds.md` ("A5 boot") plans it. As the kit
runs it:

1. iOS 9.3.5 (13G36), jailbroken (EverPwnage), runs `kloader` with a
   signature-patched iBSS. For kDFU the iBSS has to be decrypted, because the
   GID key is unavailable once iOS has booted.
2. The host sends iBEC (iBoot-2817), patched in two places. The IMG3 parse in
   `go` becomes a store of `0x80000000` into the entry slot (8 bytes at file
   offset `0x119e`). The jump helper's teardown calls are NOPs (28 bytes at
   `0x1d8b0`).
3. Then `setenv loadaddr 0x80000000`, the ARM loader with the zImage and the
   DTB appended, and `go`. The loader stops the watchdog, cleans L1 and the
   PL310 and turns the MMU off. It also hooks the zImage decompressor, and
   those hook sites are offsets that hold for one compiler (GCC 11.4). A clang
   zImage needs them found again.

## Firmware

`firmware.json` pins the 13G36 IPSW and three inputs, measured from the unit's
iOS. It produces nothing yet (`outputs` is empty), for the same reason as on
the P105: orchard cannot read this IPSW's encrypted root filesystem, and the
Wi-Fi step is not in its recipe vocabulary. What the board needs:

| Destination | Made by |
|---|---|
| `lib/firmware/J1.mtprops` | a copy of `J1.mtprops` |
| `lib/firmware/brcm/brcmfmac4330.bin` | Cascadia's `scripts/trx-add-nvram.py` from `mike.trx` and `mike.txt`, with the unit's `macaddr=` |

Two things belong to one unit and stay out of public recipes and releases: the
touch calibration (`MtCl` from syscfg, captured from the unit's iOS) and the
Wi-Fi MAC address that goes into the nvram.

## Open work

- Boot orchard's build on the J1: find the loader's decompressor hooks for a
  clang zImage, then set the feature states from that boot.
- Upstream to Cascadia first, then import here. Open as Cascadia
  [PR #35](https://github.com/rzxvx/Project-Cascadia/pull/35): CPU ids
  `0x900`/`0x901` and both cores, the battery gauge (HDQ, UART5 powered), the
  backlight node and a portrait boot console. In progress, not published yet:
  a PHY driver for the second USB complex, so Wi-Fi comes up at boot without
  the userspace bring-up.
- Charging: iBEC leaves the D1974's input limit (`0x3e`) at about 100 mA, so
  the battery drains even on a charger. `0x4a` (1000 mA) charges, and today it
  is written from userspace. The tree has no charger node for Cascadia's
  `apple-pmu-charger`.
- Runtime: the Z2 bootload and the Wi-Fi bring-up are userspace steps today,
  like the P105's runtime services.
- The early fault that `earlycon=j1fb` hides, the eDP display path, audio,
  Bluetooth, buttons and NAND writes.
