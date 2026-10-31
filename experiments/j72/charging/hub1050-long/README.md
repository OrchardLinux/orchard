# J72 powered-hub five-minute 1,050 mA test

Exact source of the bounded five-minute test measured on J72 on 1 November
2026, using the same Linux #33 RAM boot as the successful short comparison.
The worker retained **140 positive samples at +153..+209 mA**, from -830 mA
baseline, over **300.006 seconds**. Maximum recorded temperature was 37.2 C;
maximum guard voltage was 4.139 V. There were 529 active-phase host status
reads, USB data stayed connected, no guard faults were recorded, and the
original PMIC input limit 0x04c0=0x02 was independently verified afterward.

This establishes sustained net charging for this five-minute diagnostic
workload on the qualified powered JCH377 port. Automatic source detection,
source changes and the full desktop workload remain unqualified. Percentage
jumps were observed, so a percentage increase is not claimed as charge gained.

This is a separate module identity because #33 cannot unload modules. The
preceding short worker remains resident, permanently used and disarmed. The
host verified a successful full-duration short capture, positive retained
samples, no faults and restored PMIC readback before staging and arming this
worker. Its duration is fixed at 300 seconds and input ceiling at nominal
1,050 mA (0x4e); it cannot be rearmed after use.

The battery bounds, fixed-register write restriction, source lease and USB
state guard match the [30-second worker](../hub1050/). The source allowance
was independently established as 5 V / 1.5 A. The host verifies the qualified
port before every lease renewal; the token itself is not a charger detector.
A separate deadline queue checks the lease/USB and never waits on the gauge.
The first five seconds are excluded from retained current statistics.
The exact-kernel build, board and owning battery/PMIC drivers are required.

The staging helpers, firmware, calibration, raw logs and binaries remain
outside this source snapshot. Build/test commands are the same as the short
worker, using this directory's sources and module Makefile. These tests use
synthetic data and do not contact an iPad. See VALIDATION.json for hashes and
measured bounds. This is development code, outside Orchard's applied series.
