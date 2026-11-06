# P105 USB handoff

The proposed S5L8942X USB topic starts from the current ARMv7 kernel base
and ends at kernel commit `55132508c`. It contains separate binding commits
for the PHY reference clock and controller clock ownership. The controller
quirk is selected by `apple,s5l8942x-dwc2`; the shared PHY initialization and
OTG connection callbacks use `apple,s5l8942x-otgphy` match data. P105 retains
its existing S5L8940X fallback compatibles.

## Hardware observations

An earlier installed P105 build reached the Omarchy desktop after the owner
started its iOS app with the cable unplugged. USB initially stayed detached.
The PHY power and tuning registers were active, while the register at offset
`0x1c` held `0x4`. Native iBEC enables bit 1 before releasing the controller's
soft disconnect. A guarded live update from `0x4` to `0x6` restored USB serial
and ECM in the same boot.

That observation establishes the connection bit's effect in the running
build. The proposed callback revision still awaits an exact-revision device
boot. Its source also handles a firmware handoff that leaves the common PHY
and controller clocks stopped. Cold initialization enables the measured
parent gates before accessing PHY state and checks the 24 MHz reference
selection, tuning and reset completion.

## Scope on other consumers

The imported J1 device tree selects `apple,s5l8940x-otgphy` and
`apple,s5l8940x-dwc2`. It continues to use firmware-established tuning and
the existing controller startup path. The shared PHY helper now reports a
parent-gate failure instead of continuing after an unacknowledged request;
J1 therefore remains a required regression target. Its existing boot-kit
observations are recorded in `devices/j1/README.md`.

OTG and HSIC share one initialization count on S5L8942X. Switching the OTG
enable bit leaves the HSIC output independent. The legacy host PHY retains
its existing initialization contract.

## Software checks

The actual callback bodies passed 181 fake-MMIO assertions under
AddressSanitizer and UndefinedBehaviorSanitizer. The fixture exercises parent
acknowledgment failures and reference-clock selection, as well as shared
initialization lifetime and OTG register readback. Legacy-compatible checks
preserve the supplied tuning values.

Run the fixture against the topic kernel tree:

```sh
python3 kernel/tests/test_s5l_usbphy.py /path/to/OrchardLinux/linux
```

Both changed ARMv7 objects compiled with warnings treated as errors, with the
existing `include/linux/vmstat.h` enum-arithmetic warning exception. This
focused check used generated headers from the earlier qualified Linux 6.12
build mounted read-only. A full build against the current base remains open.
The P105 device tree compiled; its PHY and controller nodes passed their
binding checks. Both changed binding documents passed schema validation.

## Unmerged code

The kernel topic consists of five logical commits:

- `197d7cd0a`: PHY binding.
- `205ae912c`: DWC2 binding.
- `979ba7553`: PHY initialization and OTG power callbacks.
- `22c8da37a`: controller clock restoration.
- `55132508c`: P105 reference clock and compatibles.

Orchard's draft export places these in the PHY and USB driver layers, with
the device tree in `devices/s5l8942x`. Adjacent platform scopes select the
patches for ARMv7. The imported Cascadia patches remain unchanged.

## Open work

- Build the exact current-base candidate through Orchard's kernel producer.
- Qualify USB serial and ECM on P105 from the on-device iOS handoff and the
  established recovery entry, including reconnecting the cable.
- Verify that HSIC Wi-Fi remains available through OTG connection changes.
- Check the shared helper change on J1 while preserving its current boot kit.
- Complete hardware qualification before leaving draft or promoting device
  capability states.
