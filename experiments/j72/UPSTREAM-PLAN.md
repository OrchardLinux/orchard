# Implementation contribution after the lab code release

PR #43 is submitted for interim review of the measured implementation and
bring-up evidence at the owner's request. It does not yet boot J72 through
Orchard's normal path. Native integration remains follow-up work; working
Wi-Fi will be proposed separately once qualified.

## Required production form

1. **S5L SPI:** port the measured explicit-transfer policy to Orchard's
   current `spi-apple` match-data design, add the compatible's binding and
   controller data, and keep existing T7000/S8000/T8010 behavior intact.
   Record the source commits and preserve authorship. The preserved local
   baseline targets Linux 7.0.12; current Orchard targets 7.3-rc1.
2. **Touch:** express J72 N1 personality and packet/layout differences as
   `apple_z2` match data. Add the binding and board DT. Model the shared
   analog supply, PWM clock, reset, attention, chip select and display sync
   through their owning drivers instead of direct overlay MMIO. Keep
   checksummed framing, bounded recovery and release-on-timeout behavior.
3. **Private-input recipe:** add the model firmware converter/recipe and
   calibration forwarding through the supported boot path. Validate with
   owner-local inputs; no embedded packet arrays or captured DT dump in
   the PR. The current include renderer deliberately does not implement
   firmware extraction or claim recipe completeness.
4. **Battery:** retain the tested UART protocol and restricted identity
   operations, but replace machine checks/provisional matching with
   bindings and compatible-selected data. Carry serial API and battery
   changes in logical commits, preserve original authors, resolve required
   options in both platform configs and explain every config change.
   Capacity pairing needs a data-driven quirk rather than a board check in
   the shared getter.
5. **Device and host path:** provide a J72 descriptor with features limited
   to what the submitted build demonstrates. Integrate the A7 boot method
   and required initramfs behavior. Leave installation disabled. Submit
   desktop/keyboard integration separately to omarchy-a with dependencies
   and merge order stated.
6. **Qualification:** apply/build the exact patch series, run Orchard's
   suite, then boot that exact revision on J72. Test physical touch,
   display, Home, battery and clean recovery; identify other devices using
   each shared driver/config and arrange their regression checks before
   merge. The old lab image does not qualify a port to the newer stack.

## Charging remains experimental

The shared 425 mA worker is a recorded development experiment, not a charger
driver. Source identification, protections, USB reconnect behavior and a desktop regression remain qualification work.
A fixed 1,050 mA / five-minute powered-hub diagnostic now passed with 140
positive samples at +153..+209 mA, live USB data and verified restoration.
The 850 mA follow-up now demonstrates brief positive net battery current
and automatic restoration. Its result is documented separately; the production policy remains
unqualified, as do source transitions and the desktop workload. Publishing this
source does not qualify a charger driver or internal-storage installation.
