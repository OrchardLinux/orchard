# iPod touch 6, N102

Hardware identity, boot assets and the firmware recipe are in
[`device.toml`](device.toml). This file tracks what still needs testing,
open work, and contributed code that is not in the tree yet.

## Needs testing

**GPU (A8, PowerVR GX6450).** powervr's A8 variant
(OrchardLinux/linux: the `drm/imagination: apple:` commits from "Describe the S8003 chip in a variant" on, and "arm64: dts: apple: t7000: Describe the GPU") probes,
boots the GPU firmware and runs compute and render jobs on this board:
Vulkan 1.2 through Mesa's powervr driver and OpenGL ES 3.1 through Zink,
no faults over a 514k-job soak. Experimental: dEQP has not run, so the
chip has no `[gpu]` entry yet. Test with
[`examples/ipod-gpu-drm`](../../examples/ipod-gpu-drm/README.md).

## Internal storage

The flash sits behind ANS1, as on the iPad Air 2, with iOS 12.4's ANS
firmware (RTKit 10.2). `asp` shows the user area as `asp0n1` and the nine
auxiliary namespaces (LLB, firmware, NVRAM, SysCfg, ...) read-only. The
write unlock opens the whole controller, so the driver refuses every
command it does not need: only the user area is written.

To keep iOS, shrink its APFS container first (resize-apfs) and install
Orchard into the free space of `asp0n1`. A DFU restore brings iOS back; it
keeps SysCfg, which Linux never writes.

## Open work

- GPU: per-job firmware overhead, parameter-manager pool growth inside a
  scene, and submitting a fragment job behind its geometry job
  (`a8_joint`) by default.
- Bluetooth: hci0 comes up with the module's patchram; no input or audio
  device has been paired yet.
- Battery: the charge is estimated from Apple's OCV curve for this
  battery family; it has not been checked against a full charge and
  discharge. The driver only reads: it reports the charge state and USB
  power, while the PMIC charges at whatever limit the boot loader left.
  Setting the charge current (charging) is open.

## Unmerged code

None.
