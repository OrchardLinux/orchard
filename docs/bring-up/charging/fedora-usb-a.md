# J71b charging from a Fedora USB-A host

Measured on an iPad 6 Wi-Fi (J71b), 2026-10-29, using USB-A to Lightning
on the same laptop port throughout. Host: Fedora 44, SELinux enforcing.
Hardware tree: `619b916`; kernel base:
`cee9395acd8043be0644b25c34bfa86623f2b935`, apple-16k series and config.
Kernel release: `7.3.0-rc1-orchard-apple-16k`.

The released 0.1.10 hardware stack used the older guarded 425 mA policy.
The current tree already has the Tristar charger and Apple USB host-grant
path. This test exercises that implementation, rather than adding another
production owner for the PMIC.

## Method and limits

The first RAM image capped the DT input limit at 500 mA. The built-in
charger was then unbound and replaced by a private loadable copy of the
same driver for withdrawal instrumentation and live iteration. Only one
charger owner was bound at a time. The replacement retained source
identification, ramping, pack guards and PMU event withdrawal. No raw I2C
scan or second reader of the read-to-clear event block was used.

The host used the Apple vendor OUT request (type `0x40`, request `0x40`,
equal extra-current values in wValue and wIndex). The USB gadget reports
500 mA plus the extra allowance through vbus_draw. This request supplies
an allowance to the device; it does not measure or qualify the laptop's
electrical capability. The same cable and port had previously charged
the battery at about +1.14 A in iPadOS with its fast-charge mode enabled.

All currents below are the fuel gauge's **net battery current**, positive
when charging. Input limits are nominal programmed limits. No external
meter measured VBUS or actual input current. State of charge was 21–22%,
pack temperature about 30.4–30.7 °C and pack voltage about 3.77–3.82 V.
The internal installation was not running during these measurements.

## Observations

| Host allowance / input limit | Screen brightness (max 2047) | Net battery current | Result |
|---|---|---|---|
| 500 mA fallback | 1518 | about −126 to −135 mA | Battery discharges |
| 500 mA fallback | 200 | about +82 to +94 mA | Slow net charge |
| 3000 mA allowance, board cap 2400 mA | 200 | about +165 to +775 mA in ten samples | Repeated ramp/fallback; not stable at the requested maximum |
| 1500 mA allowance | 200 | about +920 to +925 mA after settling | Held 1500 mA during the short observation |
| 1500 mA allowance | 1518 | Falls back to 500 mA | PMIC status `0x62` observed; elevated limit did not persist |
| 1000 mA allowance | 1518 | about +306 to +383 mA | Held 1000 mA over subsequent bring-up work |

The 2400 mA trial repeatedly reached code `0x9a` (2000 mA), then observed
status `0x62` and withdrew to fallback. While still in RAMPING, it retried
and cycled. With a 1500 mA target, a later `0x62` observation while in
CHARGING withdrew to fallback and stayed there. The meaning of bit 5 is
not established by this test. Do not mask it away or call it a confirmed
input-voltage fault based on these observations alone.

Revoking the host request returned current_max to 100 mA and the nominal
input limit to 500 mA. Battery current fell back to about +94 mA at dim
brightness. Sending a new 1000 mA allowance raised the input limit again.

## Disconnect and reconnect

Two physical Lightning unplug/replug tests were performed, with a pause
of at least three seconds. USB networking and authenticated SSH recovered
without DFU. After reconnect, the driver reported a fresh 100 mA USB
allowance and held its 500 mA fallback; the previous elevated allowance
was cleared. A new host request was required to raise it again.

The first test, at the initial 500 mA cap, verified a fallback register
write/readback 4.277 ms after the software PMU-line callback. In the second
test, begun from a 1500 mA grant, a poll had already seen status `0x62` and
withdrawn before the PMU-line callback; the armed fallback write/readback
then completed after 4.256 ms. These timings do **not** measure physical
cable-edge-to-withdrawal latency, nor the latency of a 1500→500 mA write
from the PMU-line callback.

## Follow-up

For this port, use a conservative 1000 mA allowance and keep the existing
status and pack guards. Investigate status `0x62`, and the repeated ramp
after an unexpected status while RAMPING, before claiming reliable 2.4 A
charging from this laptop. Wall chargers, USB-C cables, other host ports,
suspend/resume and charging near full capacity remain separate tests.

The private instrumentation module, boot image and port-specific host rule
are bring-up tools and are not production driver interfaces.
