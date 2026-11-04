# iPad Air 1 Cellular: experimental native Orchard path

Identity: iPad4,2 / J72AP, A7 CPID `0x8960`, BDID `0x12`.
The descriptor selects `apple-4k` and the inherited `s5l8960x-j72.dtb`.
Internal installation is disabled. One native Orchard RAM console boot now
passes USB serial and network/SSH checks. Desktop hardware remains unqualified.

The [lab report](../../docs/bring-up/j72.md) records the separate development
kernel that demonstrated a desktop, touch, keyboard, Wi-Fi and bounded
charging. Those measurements do not qualify the current native kernel,
device tree, firmware format or desktop image.

## First native RAM test: measured result

On 3 November 2026, integrated Orchard revision
`e9946443f4901125fc24a5895aa3c9a70629d665` built and booted `examples/usb-console`
on the owner's iPad4,2 through the checkout's normal `orchard build` and
`orchard run --no-build --yes` commands. The direct Pi 4 handoff used the
pinned checkra1n 0.1337.3 tool and explicit PongoOS 2.6.3 override.

Kernel, m1n1 and static tools came from the verified matching
`nightly-20261103-6b5fca68c568` components. The running kernel reported
`7.3.0-rc1-orchard-apple-4k`; the 11,852,965-byte payload SHA-256 was
`6291d9a311dfc743b47e8f3bc7efd507101b17ebbb0e4bbf69a1a6b304da9948`.
No development-kernel modules were used.

The prototype USB gadget exposed ACM and NCM. The first `orchard exec` pinned
SSH through the actual serial shell. Kernel identity, uptime, mount inventory
and dmesg reads passed. Standard `orchard push` and `orchard pull` returned
a 4 KiB synthetic file byte for byte; a separate `orchard pull --serial`
returned the same SHA-256. Mounts contained only rootfs and virtual/RAM
filesystems. A normal reboot request returned the iPad's iOS USB identity.

The Pi recorded transient USB protocol/reset errors during the A7 handoff,
including a reset of its onboard USB2 hub. The same attempt recovered without
a manual reconnect or a second bootstrap. This single boot does not qualify
handoff reliability or an external hub bootstrap. Kernel dummy-regulator
messages remain for the inherited external USB supplies. Raw logs and
identifiers stay private.

Display has not been visually qualified for this build. Touch, Wi-Fi,
battery and charging drivers are not qualified by this console result; the
new native SPI topic is not included in this published first-test kernel.

## Reproducing the console test

Use Linux aarch64 or x86_64 with the official checkra1n **0.1337.3** binary
named `checkra1n-0.1337.3` on `PATH` or in `~/.local/bin`. Orchard verifies
the architecture-specific SHA-256 before executing it. It does not download
the bootstrap tool. The existing palera1n path remains the default for other
descriptors. macOS A7 bootstrap is not implemented here.

On Pi 4, use a direct Pi USB port. Orchard checks the actual sysfs topology
and refuses an external hub before starting the bootstrap. One invocation
starts one process group; failure or uncertain outcome requires inspection
before a new attempt. No automatic A7 port reset or boot retry is added.

Build with an explicitly selected kernel tree and Pongo image:

```sh
/path/to/orchard/bin/orchard build /path/to/orchard/examples/usb-console \
    --device j72 --kernel-tree /path/to/kernel-tree --pongo /path/to/Pongo.bin
/path/to/orchard/bin/orchard run /path/to/orchard/examples/usb-console --no-build
```

The measured lab bootstrap used PongoOS 2.6.3; substituting another Pongo
image needs its own hardware test. A successful build is not a boot result.
The initial payload starts the standard Orchard USB serial/NCM/SSH session
without coldplug, optional driver loading, storage mounts or charging policy.
After checking readiness, use `orchard shell` or `orchard exec -- uname -a`.
Keep this first test bounded and return to iOS for charging.

## Remaining integration

1. Repeat the measured CLI/kernel/loader/payload combination to establish
   handoff reliability, visually qualify display, and retain both USB shells.
2. Add native A7 SPI match data and transfer handling, then J72 clock,
   regulator, reset and attention bindings and native `apple_z2` N1 support.
3. Extend the verified [firmware recipe](../../docs/bring-up/j72-firmware.md)
   with the matching native N1 output and private device calibration flow.
4. Port battery and source-aware charging into the existing power drivers;
   qualify source transitions and sustained desktop charging.
5. Port the demonstrated USB Wi-Fi path into the native DT/driver/profile,
   then qualify the omarchy-a build with touch, keyboard, Wi-Fi and power.
6. Qualify storage read-only before proposing any installation or local boot.

The kernel topics stay in source worktrees until their exact revisions work
on hardware, then become Orchard patch-series changes. Desktop changes go
to omarchy-a separately. A tethered launch after a shutdown remains distinct
from fully PC-free startup.
