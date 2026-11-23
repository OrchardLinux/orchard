#!/usr/bin/env bash
# build.sh KERNEL_WORK: stage what the payload needs beside the kernel. Writes, all git-ignored:
#   rootfs/lib/firmware/apple/gfx-t7000.bin          the GPU firmware      } tools/extract-fw.py, from Apple's
#   rootfs/lib/firmware/apple/gfx-t7000-border.bin   its border colours    } public iPod touch 6 iOS 12.4 IPSW
#   rootfs/usr/bin/a8-gpu-drm                        the DRM test program (src/a8-gpu-drm.c; static aarch64, no libc)
#   rootfs/opt/a8vk/                                 optional: a copy of A8_VK_DIR, the Vulkan bundle (README.md)
#   rootfs/usr/share/a8-gpu-drm/{compute,triangle}.rpl  optional: replays packed from A8_RECORDINGS
#
#   KERNEL_WORK     Orchard's apple-4k kernel work directory after `bin/orchard build examples/minimal-boot --device BOARD`
#                   (~/.cache/orchard/kernel-work/apple-4k-<pin>): its source/ and output/ give the UAPI headers.
#   A8_KERNELCACHE  optional: a decompressed kernelcache to take the firmware from instead of fetching the
#                   tested one (tools/extract-fw.py).
#   A8_VK_DIR       optional: a directory laid out as /opt/a8vk (README.md); without it the vk step is skipped.
#   A8_RECORDINGS   optional: the Mesa recordings for the compute and triangle replays (tools/gen-replay.py).
# Then, from the checkout: bin/orchard build examples/ipod-gpu-drm --device BOARD (n102, j96, j97, n61, n56)
set -euo pipefail
here=$(cd "$(dirname "$0")" && pwd)
work=${1:?usage: build.sh KERNEL_WORK}
work=$(cd "$work" && pwd)
[[ -d $work/source && -d $work/output ]] || { echo "build.sh: $work has no source/ and output/" >&2; exit 1; }

mkdir -p "$here/rootfs/usr/bin" "$here/rootfs/lib/firmware/apple"
python3 -I "$here/tools/extract-fw.py" "$here/rootfs/lib/firmware/apple" \
  ${A8_KERNELCACHE:+--kernelcache "$A8_KERNELCACHE"}

rm -rf "$here/rootfs/opt/a8vk"
if [[ -n ${A8_VK_DIR:-} ]]; then
  [[ -x $A8_VK_DIR/a8-vk-smoke && -f $A8_VK_DIR/lib/libvulkan_powervr_mesa.so ]] ||
    { echo "build.sh: $A8_VK_DIR is not a Vulkan bundle (a8-vk-smoke, lib/libvulkan_powervr_mesa.so)" >&2; exit 1; }
  mkdir -p "$here/rootfs/opt"
  cp -R "$A8_VK_DIR" "$here/rootfs/opt/a8vk"
else
  echo "build.sh: no A8_VK_DIR: the payload has no Vulkan bundle; the vk step is skipped"
fi

rm -rf "$here/rootfs/usr/share/a8-gpu-drm"
if [[ -n ${A8_RECORDINGS:-} ]]; then
  mkdir -p "$here/rootfs/usr/share/a8-gpu-drm"
  for step in compute triangle; do
    python3 -I "$here/tools/gen-replay.py" "$step" "$A8_RECORDINGS" "$here/rootfs/usr/share/a8-gpu-drm/$step.rpl"
  done
fi

podman run --rm --network none \
  -v "$work/source:/src:ro" -v "$work/output:/kout:ro" -v "$here:/work:rw" \
  localhost/orchard-hardware-kernel-toolchain:bookworm \
  clang-19 --target=aarch64-linux-gnu -O2 -Wall -Werror -ffreestanding -fno-builtin -nostdlib -static \
    -fno-pic -fno-pie -fno-stack-protector -fno-asynchronous-unwind-tables -mgeneral-regs-only -D__user= -D__EXPORTED_HEADERS__ \
    --ld-path=/usr/bin/ld.lld-19 -Wl,--build-id=none \
    -I/kout/arch/arm64/include/generated/uapi -I/src/arch/arm64/include/uapi -I/src/include/uapi \
    -o /work/rootfs/usr/bin/a8-gpu-drm /work/src/a8-gpu-drm.c
chmod 755 "$here/rootfs/usr/bin/a8-gpu-drm"
cd "$here/rootfs"
shasum -a 256 usr/bin/a8-gpu-drm usr/bin/a8-gpu-drm-test lib/firmware/apple/*.bin
