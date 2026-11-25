#!/bin/sh
# All of the payload's headless tests, one after the other, in the kernel
# toolchain image (docker; nothing touches a device):
#   test-maze.sh       ARCADE and HEDGEWICK on the N66's 1080x1920 screen
#   test-voidmoths.sh  VOIDMOTHS: autopilot, text and framebuffer, sound
#   test-glint.sh      GLINT: physics, boards, the fake accelerometer, sound
#   test-ipad.sh       what differs per board: touch.h, the touchscreen in
#                      all four programs, iPad and landscape screens, no
#                      motion sensor, missing keys, the iPads' speaker cards,
#                      init on each board's device tree
# Frames: /private/tmp/claude-501/arcade-frames/ and
# /private/tmp/claude-501/maze-frames/ipad-*.png. Logs: /private/tmp/claude-501/arcade-test-*.log.
# Usage: src/test.sh
set -eu
here=$(cd "$(dirname "$0")" && pwd)
logs=/private/tmp/claude-501
mkdir -p "$logs"
for t in maze voidmoths glint ipad; do
    log=$logs/arcade-test-$t.log
    printf '%-20s ' "test-$t.sh"
    if sh "$here/test-$t.sh" >"$log" 2>&1 && grep -q '^ALL TESTS PASSED' "$log"; then
        echo "passed"
    else
        echo "FAILED, see $log"
        grep 'FAIL' "$log" | head -n 5
        exit 1
    fi
done
echo "ALL TESTS PASSED"
