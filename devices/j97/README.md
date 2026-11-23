# iPad mini 4 (Cellular), J97

Hardware identity, boot assets and the firmware recipe are in
[`device.toml`](device.toml). This file tracks what still needs testing,
open work, and contributed code that is not in the tree yet.

## Needs testing

**GPU (A8, PowerVR GX6450).** The board has the iPod touch 6's A8 GPU, and
its device tree enables it (`kernel/patches/devices/t7000/0012`). The
driver has only run on the iPod touch 6 (N102) so far. Test with
[`examples/ipod-gpu-drm`](../../examples/ipod-gpu-drm/README.md)
(`--device j97`) and report the output of `a8-gpu-drm-test probe` with
its kernel lines, and the ladder's `RESULT` lines.

## Unmerged code

None.
