# ipod-gpu-drm

A RAM payload that tests the A8 (T7000) GPU, a PowerVR GX6450, through powervr's A8 variant
(`kernel/patches/drivers/gpu/0063`–`0073`, `devices/t7000/0011` and `0012`). It is `minimal-boot` with a
constant USB serial number (`a8-gpu-drm`, so macOS does not ask to allow the accessory on every boot),
the kernel's modules, the GPU firmware, a test script and, optionally, a Vulkan bundle built from
OrchardLinux/mesa.

It builds for every A8 board: the iPod touch 6 (`n102`, where the GPU has been tested), the iPad mini 4
(`j96`, `j97`) and the iPhone 6 and 6 Plus (`n61`, `n56`). The A8X (iPad Air 2) has a different GPU.

## Build

Use the `orchard` launcher of this checkout (`bin/orchard`): the nightly `orchard` on PATH does not carry
the A8 GPU patches. `BOARD` below is one of the boards above.

1. Kernel: `bin/orchard build examples/minimal-boot --device BOARD`. This builds the apple-4k kernel in
   Orchard's shared tree, `~/.cache/orchard/kernel-work/apple-4k-<pin>/`.
2. Optional, for the `vk` step, the Vulkan ladder and vkmark: build the bundle on aarch64 Linux (an
   Arch Linux ARM container works) with `vk/make-bundle.sh MESA_BUILD OUT`. `MESA_BUILD` is a meson build
   of OrchardLinux/mesa's A8 branch with `-Dvulkan-drivers=imagination`; the script's header has the
   meson line. It also packs vkmark if the `vkmark` package is installed there.
3. Stage the payload's files:

   ```sh
   A8_VK_DIR=/path/to/OUT examples/ipod-gpu-drm/build.sh ~/.cache/orchard/kernel-work/apple-4k-<pin>
   ```

   `build.sh` runs `tools/extract-fw.py`, which reads the iPod touch 6's iOS 12.4 (16G77) kernelcache
   from Apple's public IPSW over HTTP ranges (about 15 MB, not the whole IPSW) and writes
   `gfx-t7000.bin` (the GPU firmware) and `gfx-t7000-border.bin` (its border colour table). Use that
   firmware on every A8 board: it is the one the driver was tested with. `tools/extract-fw.py DIR --ipsw
   URL BYTES BOARD` takes another IPSW, e.g. the iPad mini 4's, and warns when the firmware is not the
   tested one. Nothing from Apple is stored in this repository; everything `build.sh` writes is
   git-ignored.
4. `bin/orchard build examples/ipod-gpu-drm --device BOARD`

## Run

1. Put the device in DFU mode, then `bin/orchard run examples/ipod-gpu-drm --device BOARD --no-build`
   (checkm8, PongoOS, m1n1, Linux; RAM only, nothing is written to the device).
2. Open the USB serial console. powervr is not loaded at boot.
3. `a8-gpu-drm-test probe` loads powervr (`exp_hw_support=1`). The probe powers the GPU, reads its ID and
   boots the firmware to its alive event; the render node then reports BVNC 3.10.4.5. Every step prints
   one `RESULT` line, runs under a 30 s hardware watchdog (a frozen SoC resets) and saves its kernel lines
   to `/run/a8-gpu-drm/STEP.txt`. After a failed step nothing more runs until the next boot.
4. `a8-gpu-drm-test vk`: Vulkan probe, compute and a triangle through Mesa.
5. The Vulkan ladder, every pixel checked on the CPU:

   ```sh
   /opt/a8vk/a8-vk-ladder.sh list
   for r in $(/opt/a8vk/a8-vk-ladder.sh list | awk 'NR>1 && /^  /{print $1}'); do /opt/a8vk/a8-vk-ladder.sh $r; done
   ```

6. vkmark: `/opt/a8vk/vkmark.sh --size 256x256` and `--size 750x1334`.
7. A soak: `a8-gpu-soak 1000 0` runs the `vk` step 1000 times.

The `compute` and `triangle` steps replay two Mesa recordings from the bring-up through the DRM ioctls
(`src/a8-gpu-drm.c`). They need `A8_RECORDINGS` at build time, which this example does not provide;
without it `a8-gpu-drm-test all` skips them.

## Reporting

On a board other than the iPod touch 6, report the board, the output of `a8-gpu-drm-test probe` with
its kernel lines (`/run/a8-gpu-drm/probe.txt`, or `dmesg` if the device froze and the serial log is all
there is), and the ladder's `RESULT` lines. If the probe freezes the device, the last kernel lines
streamed over the serial console show how far it got.

## Files

| Path | What |
|---|---|
| `init` | PID 1: `minimal-boot` with the constant USB serial number |
| `rootfs/usr/bin/a8-gpu-drm-test` | the test steps: `probe`, `compute`, `triangle`, `vk`, `all` |
| `rootfs/usr/bin/a8-gpu-soak` | repeats the `vk` and `triangle` steps |
| `src/a8-gpu-drm.c` | the DRM replay program: static aarch64, no libc |
| `tools/extract-fw.py` | the GPU firmware and border table from an Apple kernelcache |
| `tools/gen-replay.py` | packs a Mesa recording into a replay file |
| `vk/` | the Vulkan smoke test, the Vulkan ladder and `make-bundle.sh` |
