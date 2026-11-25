#!/bin/sh
# build.sh: build the four programs into rootfs/usr/bin (git-ignored):
#   rootfs/usr/bin/arcade      the picker (src/arcade.c)
#   rootfs/usr/bin/hedgewick   the maze chase (src/hedgewick.c)
#   rootfs/usr/bin/voidmoths   the fixed shooter (src/voidmoths.c)
#   rootfs/usr/bin/glint       the tilt maze (src/glint.c)
# Static, stripped aarch64 binaries (the same ones run on apple-4k and
# apple-16k kernels), built in the kernel toolchain image with docker, then a
# short smoke test of the very binaries that go into the payload (the image
# is aarch64, so they run there): each autopilot plays a while, and ARCADE
# must say "no framebuffer" (status 4) without one.
# The sources go in on stdin as a tar (OrbStack's bind mount can serve a
# stale, short copy of a file that was rewritten a moment earlier); the
# binaries come back as a tar on stdout.
# Then, from the checkout: orchard build examples/arcade --platform apple-4k
# (J71, J85, J81) or --platform apple-16k (N66), or orchard run.
set -eu
here=$(cd "$(dirname "$0")" && pwd)
out=$here/../rootfs/usr/bin
progs="arcade hedgewick voidmoths glint"
mkdir -p "$out" /private/tmp/claude-501
tmp=$(mktemp -d /private/tmp/claude-501/build-arcade.XXXXXX 2>/dev/null || mktemp -d)
COPYFILE_DISABLE=1 tar -C "$here" -cf - arcade.c hedgewick.c voidmoths.c glint.c arcade_common.h hedgewick_art.h touch.h backlight.h |
docker run -i --rm localhost/orchard-hardware-kernel-toolchain:bookworm sh -c '
    set -eu
    mkdir -p /w && cd /w && tar xf - 2>/dev/null
    for p in '"$progs"'; do
        aarch64-linux-gnu-gcc -std=gnu11 -Wall -Wextra -Wshadow -Werror -static -O2 -s \
            -o $p $p.c -lm >&2
    done
    ./hedgewick --bot 3000 >/dev/null 2>log || { cat log >&2; exit 1; }
    grep -q "bad=0" log || { cat log >&2; exit 1; }
    ./voidmoths --bot 3000 --fakefb 1536x2048x32 >/dev/null 2>log || { cat log >&2; exit 1; }
    grep -q "bad=0" log || { cat log >&2; exit 1; }
    ./glint --bot 1200 --fakefb 1536x2048x32 --iio none --mute >/dev/null 2>log || { cat log >&2; exit 1; }
    grep -q "^stats:" log || { cat log >&2; exit 1; }
    st=0; ./arcade --fb /nonexistent --tty /dev/null >/dev/null 2>&1 || st=$?
    [ $st = 4 ] || { echo "arcade without a framebuffer: status $st" >&2; exit 1; }
    tar cf - '"$progs" | tar -xf - -C "$tmp"
for p in $progs; do
    cat "$tmp/$p" >"$out/$p.tmp"     # cat, not mv: the payload volume keeps no owners
    rm "$tmp/$p"
    chmod 755 "$out/$p.tmp"
    mv "$out/$p.tmp" "$out/$p"
done
rmdir "$tmp"
cd "$out" && ls -l $progs && shasum -a 256 $progs | cut -c1-16,65-
