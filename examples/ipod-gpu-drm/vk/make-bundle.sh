#!/bin/bash
# SPDX-License-Identifier: MIT
# make-bundle.sh MESA_BUILD OUT: the /opt/a8vk Vulkan bundle for build.sh's A8_VK_DIR.
# It follows how the tested bundle was built, but has not been run from scratch yet; the tested Mesa
# build also had -Dplatforms=x11,wayland.
#
# Run it on aarch64 Linux (an Arch Linux ARM container works) with a C compiler, glslangValidator, the Vulkan
# loader (libvulkan.so.1) and, for vkmark, the vkmark package installed. MESA_BUILD is a meson build directory of
# OrchardLinux/mesa's A8 branch with the imagination Vulkan driver, e.g.
#   meson setup MESA_BUILD MESA_SOURCE --buildtype=release -Dvulkan-drivers=imagination -Dgallium-drivers= \
#     -Dplatforms= -Dglx=disabled -Degl=disabled -Dgbm=disabled -Dllvm=disabled && ninja -C MESA_BUILD
# OUT gets a self-contained directory: nothing is installed on the iPod, every program starts through the
# bundled glibc loader (lib/ld-linux-aarch64.so.1 --library-path lib), so the initramfs only needs
# /dev/dri/renderD128, /sys and a writable /tmp.
#   a8-vk-smoke, bin/a8-vk-smoke        Vulkan probe, compute and triangle (a8-gpu-drm-test vk runs it)
#   a8-vk-ladder.sh, bin/a8-vk-ladder   the Vulkan ladder: ./a8-vk-ladder.sh list | RUNG
#   vkmark.sh, vkmark/                  vkmark, headless (only when vkmark is installed)
#   lib/                                libvulkan_powervr_mesa.so, its ICD manifest and every library they need
set -euo pipefail
here=$(cd "$(dirname "$0")" && pwd)
B=$(cd "${1:?usage: make-bundle.sh MESA_BUILD OUT}" && pwd)
OUT=${2:?usage: make-bundle.sh MESA_BUILD OUT}
[[ $(uname -m) == aarch64 ]] || { echo "make-bundle.sh: run it on aarch64 Linux" >&2; exit 1; }
ICD=$B/src/imagination/vulkan/libvulkan_powervr_mesa.so
[[ -f $ICD ]] || { echo "make-bundle.sh: no $ICD (build Mesa with -Dvulkan-drivers=imagination)" >&2; exit 1; }
T=$(mktemp -d)
trap 'rm -rf "$T"' EXIT
rm -rf "$OUT"
mkdir -p "$OUT/bin" "$OUT/lib"

glslangValidator -V --vn a8_smoke_kstore "$here/k_store_const.comp" -o "$T/a8_smoke_kstore.h" >/dev/null
glslangValidator -V --vn a8_smoke_vs "$here/tri.vert" -o "$T/a8_smoke_vs.h" >/dev/null
glslangValidator -V --vn a8_smoke_fs "$here/tri.frag" -o "$T/a8_smoke_fs.h" >/dev/null
for s in "$here"/ladder/*.vert "$here"/ladder/*.frag "$here"/ladder/*.comp; do
  b=$(basename "$s"); n=${b%.*}_${b##*.}
  glslangValidator -V --vn "$n" "$s" -o "$T/$n.h" >/dev/null
done
cc -O2 -Wall -I"$T" "$here/a8-vk-smoke.c" -o "$OUT/bin/a8-vk-smoke" -lvulkan -lm
cc -O2 -Wall -I"$T" "$here/ladder/a8-vk-ladder.c" -o "$OUT/bin/a8-vk-ladder" -lvulkan -lm
strip "$OUT/bin/a8-vk-smoke" "$OUT/bin/a8-vk-ladder"

strip --strip-unneeded -o "$OUT/lib/libvulkan_powervr_mesa.so" "$ICD"
cp -L "$(ldconfig -p | awk '/libvulkan.so.1 /{print $NF; exit}')" "$OUT/lib/libvulkan.so.1"
cat > "$OUT/lib/powervr_icd.json" <<'JSON'
{ "file_format_version": "1.0.1", "ICD": { "library_path": "./libvulkan_powervr_mesa.so", "api_version": "1.4.0" } }
JSON

vkm=
if command -v vkmark >/dev/null; then
  vkm=$(command -v vkmark)
  mkdir -p "$OUT/vkmark/bin" "$OUT/vkmark/winsys"
  cp "$vkm" "$OUT/vkmark/bin/"
  strip "$OUT/vkmark/bin/vkmark"
  winsys=$(dirname "$(find /usr/lib /usr/lib64 -name headless.so -path '*vkmark*' 2>/dev/null | head -1)")
  cp "$winsys/headless.so" "$OUT/vkmark/winsys/"
  cp -r /usr/share/vkmark "$OUT/vkmark/data"
fi

# every shared library the programs, the loader and the driver need, and the dynamic loader itself
for x in "$OUT"/bin/* "$OUT"/lib/libvulkan.so.1 "$OUT"/lib/libvulkan_powervr_mesa.so ${vkm:+"$OUT"/vkmark/bin/vkmark "$OUT"/vkmark/winsys/headless.so}; do
  ldd "$x" | awk '/=> \//{print $3}'
done | sort -u | while read -r l; do cp -nL "$l" "$OUT/lib/"; done
cp -nL /lib/ld-linux-aarch64.so.1 "$OUT/lib/"

wrapper() {  # wrapper NAME PROGRAM [EXTRA LIBRARY PATH] [ARGUMENTS...]
  local name=$1 prog=$2 extra=$3
  shift 3
  cat > "$OUT/$name" <<SH
#!/bin/sh
# $name: $prog with the bundle's libraries and Mesa's powervr Vulkan driver (load powervr first)
D=\$(cd "\$(dirname "\$0")" && pwd)
export VK_DRIVER_FILES=\$D/lib/powervr_icd.json VK_ICD_FILENAMES=\$D/lib/powervr_icd.json
export PVR_I_WANT_A_BROKEN_VULKAN_DRIVER=1 MESA_SHADER_CACHE_DISABLE=true
exec \$D/lib/ld-linux-aarch64.so.1 --library-path \$D/lib$extra \$D/$prog $* "\$@"
SH
  chmod 755 "$OUT/$name"
}
wrapper a8-vk-smoke bin/a8-vk-smoke ''
# the ladder's vkm* rungs read vkmark's SPIR-V from the bundle's vkmark/data/shaders (A8_VKMARK_SHADERS)
wrapper a8-vk-ladder.sh bin/a8-vk-ladder ''
if [[ -n $vkm ]]; then
  # B8G8R8A8_UNORM: vkmark's default pick is sRGB, which the A8 path does not render yet
  wrapper vkmark.sh vkmark/bin/vkmark ':$D/vkmark/lib' \
    '--winsys headless --winsys-dir $D/vkmark/winsys --data-dir $D/vkmark/data --pixel-format B8G8R8A8_UNORM'
fi
(cd "$OUT" && find . -type f | sort | xargs sha256sum > SHA256SUMS)
du -sh "$OUT"
