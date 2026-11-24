# iPad Pro 2 (12.9-inch, Wi-Fi), J120

Hardware identity, the boot method and the firmware recipe are in
[`device.toml`](device.toml). This file tracks what needs testing, known
issues and the cleanup still open.

The J120 was brought up by tompta1 (#205). His drivers were reworked to
match each other board with the same hardware: the device tree lives in
`t8011-pro2.dtsi`, which J121, J207 and J208 share (their iPadOS 17.7.11
device trees differ only in panel, touch personality, Wi-Fi module and
baseband), and the shared drivers are keyed on the SoC or chip, not on
the J120.

The parts first held back (speakers, charging, light sensors, 120 Hz, the
Smart Cover driver, his storage limits and HDQ transport) are in as he
built them, with Orchard's adaptations and cleanups on top; his #205 commit
is tagged `contrib/tompta1-j120`.

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
  (`BCM.apple,j120-USI.hcd`, `-MUR`), the vendor read from the Wi-Fi OTP by
  brcmfmac (the same record and key as tompta1's OTP layout read); the
  unit's address and RF calibration from m1n1.
- Touch: renamed to the K1 controller; logging trimmed.
- Charging: the unit's input-current calibration comes from m1n1 (ADT
  `/charger usb-input-limit-calibration-hv`) instead of a file the
  installer extracted, so the charger also binds on a RAM boot.
- Light sensors: each CT709 finds its calibration record by
  `apple,als-orientation` (6 for 0x29, 5 for 0x39), no longer by board.
- Display: the 60/120 Hz switch is keyed on the H9G display and panel, not
  on the board.
- PMGR reset read-back: on the A10X only.
- Smart Cover and speakers: described in the shared iPad Pro 2 device tree.

Storage keeps tompta1's write limits (namespace 1 only), the battery his
bounded HDQ transport and the Smart Cover his two-sensor polled driver, as
in his working setup.

## Cleanup

What still needs a J120 is tracked in
[#225](https://github.com/OrchardLinux/orchard/issues/225):

1. Speakers on the upstream MAX98927 codec driver with per-board
   `speaker-config`, and a speaker protection plan.
2. 120 Hz in the adbe display driver instead of simpledrm.
3. The Smart Cover as a general driver over the D2400 GPIO lines.
4. A generic HDQ write-opcode filter instead of the J120 transport.
5. The PMGR reset read-back on the other SoCs (J71b first).
6. Boot from internal storage: an m1n1 NVMe backend for `apcie,t8011`.
7. The storage write limits and the charging window, once qualified.
8. Firmware recipes for J121, J207 and J208 (Wi-Fi joyb/angera/angerb,
   touch J121/J207/J208.mtprops, Bluetooth).
