# iPad Pro 2 (12.9-inch, Wi-Fi), J120

Hardware identity, the boot method and the firmware recipe are in
[`device.toml`](device.toml). This file tracks what needs testing, known
issues, open work and contributed code that is not in the tree yet.

The J120 was brought up by tompta1 (#205). His drivers were reworked to
match each other board with the same hardware: the device tree lives in
`t8011-pro2.dtsi`, which J121, J207 and J208 share (their iPadOS 17.7.11
device trees differ only in panel, touch personality, Wi-Fi module and
baseband), and the shared drivers are keyed on the SoC or chip, not on
the J120.

## Known issues

- **About half of the boots fail early.** tompta1 booted Linux on his J120
  twice through m1n1; roughly one boot in two stops early, before he could
  see why. A system that came up ran without freezes and slept and woke.
  Open: a serial log of a failed boot. Candidates from the review: the
  storage link (now one failed port no longer takes the controller down;
  `apple,link-retry-after-power-cycle` is available), state the boot
  loader leaves in the PCIe PHY, and the NVMe host memory buffer m1n1 must
  fill in.

## Needs testing

These changed between tompta1's build and the integrated one:

- Wi-Fi: powered by the BCM4355 sequence the iPad 6 uses, with the ADT's
  device-wake GPIO 164, instead of REG_ON only.
- Bluetooth: Patchram and regulatory table now named by module vendor
  (`BCM.apple,j120-USI.hcd`, `-MUR`), the vendor read through the Wi-Fi
  node; the unit's address and RF calibration from m1n1.
- Storage: handled as on the A10 iPads, without the bring-up write limits
  (namespace 1 only, no discard, admin allow-list).
- Battery: the shared HDQ transport, without the J120 read budget,
  command allow-list and stop-on-first-error.
- Smart Cover: D2400 GPIO 0 through gpio-keys, with both hall events as
  wake events (cover-open wake is new).
- Touch: renamed to the K1 controller; logging trimmed.

## Open work

1. Speakers: the four MAX98723 amplifiers (I2C1/I2C2 at 0x31/0x32, shared
   reset GPIO 119) as a codec driver (the register map is MAX98927's) with
   per-board `speaker-config` in the device tree, and the T8011 MCA2
   (eight 16-bit slots, 256-clock frame, V/I feedback capture). Speaker
   protection needs a stated plan.
2. Charging: the D2355 Tristar charger with its per-unit input-current
   calibration copied by m1n1 from the ADT (`/charger
   usb-input-limit-calibration-hv`), battery limits from
   `monitored-battery`, the Orion VBUS ADC guard, and a check of the
   "input disabled" handoff code on unplug, remove and suspend.
3. Light sensors: a CT709 (TCS3400 family) IIO driver keyed on the
   sensor, with the calibration record per sensor node.
4. Display: 120 Hz through an H9G display driver, not simpledrm.
5. Boot from internal storage: an m1n1 NVMe backend for `apcie,t8011`
   (the J120 m1n1 changes were not published).
6. HDQ: a generic write-opcode filter for every HDQ master and w1 master
   parent registration, if still needed on J120.
7. `pmdomain: apple: verify PMGR reset state`: check on J71b first.
8. Firmware recipes for J121, J207 and J208 (Wi-Fi joyb/angera/angerb,
   touch J121/J207/J208.mtprops, Bluetooth).

## Unmerged code

From #205 (tompta1, commit `09038adf`), to be tagged
`contrib/tompta1-j120` when it lands:

| Path in #205 | Contents |
| --- | --- |
| `kernel/patches/drivers/sound/0028`-`0034` | T8011 MCA2 and the J120 speaker amplifier driver (open work 1) |
| `kernel/patches/drivers/power/0044`, `0045`, `0047`, `0049`, `0053`-`0058` | D2355 Tristar charger, VBUS guard (open work 2) |
| `kernel/patches/drivers/iio/0016`, `0017` | Orion Bellatrix VBUS ADC (open work 2) |
| `kernel/patches/drivers/iio/0010`, `0011`, `0013`-`0015` | CT709 light sensors (open work 3) |
| `kernel/patches/drivers/drm/0012` | simpledrm 60/120 Hz cadences (open work 4) |
| `kernel/patches/drivers/w1/0003`, `0004`, `0006`, `drivers/power/0048` | bounded HDQ transport, stale-reading expiry (open work 6) |
| `kernel/patches/drivers/pmdomain/0002` | PMGR reset read-back (open work 7) |
| `kernel/patches/drivers/input/0036`, `0037` | polled cover driver (replaced by the D2400 GPIO) |
| `kernel/patches/drivers/nvmem/*`, `drivers/wifi/0011`, `0012` | OTP module-vendor layout (replaced by the existing vendor path) |
| `src/orchard/apple/adt.py`, `install_remote.py`, `service.py`, `boot-entries`, udev rule | per-unit charger calibration at install (open work 2: m1n1 instead) |
| `src/orchard/install_direct.py` | FAT flush after unmount (all devices; its own change) |
