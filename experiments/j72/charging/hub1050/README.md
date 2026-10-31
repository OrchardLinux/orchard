# J72 powered-hub 1,050 mA short-test source

These are the exact worker/policy sources used in the successful 30-second
J72 #33 RAM test on 1 November 2026. All 12 retained samples were positive:
+177 to +213 mA battery current, from a -809 mA baseline. USB data remained
available; automatic restoration returned PMIC 0x04c0 to 0x02 with error=0.
This qualifies a short controlled comparison, not sustained charging or an
autonomous charger driver. The separate five-minute worker is not included.

The input ceiling is fixed at nominal 1,050 mA (0x4e). Source qualification
was performed independently on the powered JCH377 marked charging/data port,
which iOS reported as 5 V / 1.5 A. The host must verify the source topology
before each lease renewal; the token itself does not detect charger capacity.
The module also checks the exact DWC2 gadget's configured/high-speed state.
It does not establish whether an arbitrary hub or Pi port can supply this.

Only register 0x04c0 is writable. No current change occurs at load or arm.
The exact token arms one physical Home press; the attempt then expires at
30 seconds, on a 2.5-second source-lease timeout, or on a USB/battery guard.
The voltage bound remains 3.5..4.2 V, temperature 20..38 C, SOC 10..95%, and
absolute battery current at most 1.5 A. Failed guard readings are retained.
The deadline queue does not wait on the battery gauge. Original input-limit
restoration is read back, with three bounded attempts on failure.

This diagnostic requires the exact #33 target build, J72 device tree and
already bound battery/PMIC drivers. It includes that kernel's private DWC2
structure definitions and is not portable across kernel versions. Desktop,
USB-backed root and touch must be stopped first. #33 cannot unload modules;
the preceding trial remains terminal and disarmed under a separate identity.
The host staging/qualification helpers and unit-specific records are not
bundled here. Loading this module alone does not reproduce the experiment.

Build only with the matched target trees:

```sh
make -C "$J72_KERNEL_SRC" O="$J72_KERNEL_OUT" ARCH=arm64 \
  M="$PWD/module" W=1 modules
```

Offline tests compile the actual policy and source guard, without an iPad:

```sh
cc -Wall -Wextra -Werror -Wno-unused-function -fsanitize=undefined,bounds test-core.c -o /tmp/j72-hub1050-policy
/tmp/j72-hub1050-policy
cc -Wall -Wextra -Werror -Wno-unused-function -fsanitize=undefined,bounds test-guard-faults.c -o /tmp/j72-hub1050-faults
/tmp/j72-hub1050-faults
python3 test-worker-source.py
```
