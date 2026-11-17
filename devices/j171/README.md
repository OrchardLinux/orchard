# iPad 7 (Wi-Fi), J171

Hardware identity, boot assets and the firmware recipe are in
[`device.toml`](device.toml). This file tracks what still needs testing,
open work, and contributed code that is not in the tree yet.

## Needs testing

**Charging (PR #46).** The Bellatrix port supply and the D2365 charger are
enabled, and the charger grants 500 mA to any source
(`kernel/patches/drivers/power/0014`). The earlier test in #46 ran with a
fixed 1425 mA and does not cover this. Test with the 500 mA build, following
the test plan in
[`docs/bring-up/charging/adaptive-charging.md`](../../docs/bring-up/charging/adaptive-charging.md)
§4.3:

- a plain computer USB-A port, a wall charger, and a host that grants more;
- unplug and replug (after 1 s and after 10 s);
- near full charge.

Record `input_current_limit`, the gauge current, the screen brightness and
the source. The J172 shares `t8010-ipad7.dtsi` and gets the same change;
untested there.

## Open work

- Charging above 500 mA: source identification on Bellatrix, a measured
  unplug withdrawal signal (the iPad 7 device tree has no PMU event line),
  then the USB host grant. Steps 3–8 in `adaptive-charging.md` §4.2. (#46)
- D2365 charger: lower an unexplained input limit at probe instead of
  refusing, and read the limit back every second as the Tristar charger
  does. Step 2 in `adaptive-charging.md` §4.2. (#46)
- The board maximum (`usb-input-limit-max`) from the J171 ADT, as
  `input-current-limit-microamp` in the device tree. (#46)

- Suspend: on main `89c9640` (0.3.0 rootfs) every s2idle attempt from
  `omarchy-powerd` ends in "PM: Some devices failed to suspend, or early wake
  event detected" and `Device or resource busy`. Each retry (about every 11 s
  while the screen is dark) also resets the USB gadget link. The failing device
  is not identified yet.
- AOP sensors and microphones: the AOP probe fails with -2 when
  `apple/aop-ipad7c.bin` is missing (a system installed before the AOP
  firmware entries), and the PDM microphones wait on AOP. Needs a retest on a
  system with the current firmware recipe.

- Video decode: on main `89c9640` the decoder binds and loads its firmware,
  but every H.264 and HEVC frame through `v4l2slh264dec`/`v4l2slh265dec`
  fails with `TO_MTX indices out of range: rd 41967704 wr 331531435
  (capacity 390)` and `submit failed: -5`; output is garbage (PSNR ~5 dB
  against a software decode). The ring indices read back as nonsense, so the
  shared mailbox layout or its address probably differs from the A9/A10 boards
  where #121 was tested.

## Unmerged code

None.
