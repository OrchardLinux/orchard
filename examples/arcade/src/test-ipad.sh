#!/bin/sh
# Headless tests of what differs between the boards: the iPads (J71, J85,
# J81) and the N66, in the kernel toolchain image under ASan and UBSan.
# Nothing here touches a device:
#  - touch.h on its own (src/touch_test.c): the capability rule, the mapping
#    onto letterboxed canvases, tap, swipe, drag, long press, the corner;
#  - ARCADE on fake screens of 1536x2048 (the iPads), 1080x1920 (N66) and
#    2048x1536 (landscape): no motion sensor (GLINT grey, cannot be chosen,
#    a tap on it says why), a fake IIO "apple-aop-accel" (GLINT live), the
#    mute switch ignored, boards without volume keys, with one button only,
#    with no buttons;
#  - recorded touch streams (src/touchrec.py; multitouch protocol B as
#    apple_z2 sends it, in the J71's own units and in pixels): ARCADE tap to
#    choose and tap to play, a tap in the letterbox; HEDGEWICK tap to start,
#    swipes to turn, tap the top bar to pause, the corner held to the title
#    and back to the arcade; VOIDMOTHS tap to start, drags to steer, the
#    corner; GLINT swipe to choose a board, tap to start, a finger as
#    gravity without a sensor, tap to pause, the corner;
#  - screen off per board (src/backlight.h, a fake sysfs with an iPad-style
#    backlight name): on the J85 (no volume keys) Power held 0.8 s darkens
#    and a short press still moves to the next game; taps on the cards while
#    dark do nothing; with one key or none, Power keeps its old meaning;
#  - sound: the iPads' cards ("iPad Air Speakers", "iPad mini Speakers",
#    "J81 Speakers", ALSA id "Speakers") are never opened by any program;
#  - the phone builds carry no test code (FAKE_TOUCH, FAKE_FB, FAKE_BL_ROOT,
#    FAKE_KEYS);
#  - init: on fake device trees of the N66, J71, J85 and J81 (and a disabled
#    touchscreen, a wrongly named speaker card), which modules it loads.
# Frames: /private/tmp/claude-501/maze-frames/ipad-*.png (only those are
# replaced there).
# Usage: src/test-ipad.sh [FRAMES_DIR]
set -eu
set -o pipefail     # a FAIL inside the container fails the script
here=$(cd "$(dirname "$0")" && pwd)
frames=${1:-/private/tmp/claude-501/maze-frames}
mkdir -p "$frames"
rm -f "$frames"/ipad-*.png
COPYFILE_DISABLE=1 tar -C "$here" -cf - arcade.c hedgewick.c voidmoths.c glint.c arcade_common.h hedgewick_art.h \
    touch.h backlight.h touch_test.c touchrec.py fake_alsa.c ppm2png.py -C "$here/.." init |
docker run -i --rm localhost/orchard-hardware-kernel-toolchain:bookworm sh -c '
set -eu
exec 3>&1 1>&2
mkdir -p /w /out && cd /w && tar xf - 2>/dev/null
fail() { echo "FAIL: $*"; exit 1; }
CF="-std=gnu11 -Wall -Wextra -Wshadow -Werror -O1 -g -fsanitize=address,undefined -fno-sanitize-recover=all -fno-omit-frame-pointer"
PF="-std=gnu11 -Wall -Wextra -Wshadow -Werror -static -O2"

echo "== builds"
gcc $CF -DTOUCH_TEST -o touch_test touch_test.c
gcc $CF -DMZ_TEST -o ar arcade.c -lm
gcc $CF -DMZ_TEST -DMZ_FAKE_ALSA -o ar-snd arcade.c -lm
gcc $CF -DMZ_TEST -o hw hedgewick.c -lm
gcc $CF -DMZ_TEST -DMZ_FAKE_ALSA -o hw-snd hedgewick.c -lm
gcc $CF -DTOUCH_TEST -o vm voidmoths.c -lm
gcc $CF -DVM_FAKE_ALSA -o vm-snd voidmoths.c -lm
gcc $CF -DTOUCH_TEST -o gl glint.c -lm
gcc $CF -DGL_FAKE_ALSA -o gl-snd glint.c -lm
for p in arcade hedgewick voidmoths glint; do
    aarch64-linux-gnu-gcc $PF -o $p-phone $p.c -lm
    if grep -q FAKE_TOUCH $p-phone; then fail "fake touchscreen code in the $p phone build"; fi
    if grep -q FAKE_FB $p-phone; then fail "fake framebuffer code in the $p phone build"; fi
    if grep -q "FAKE_BL_ROOT\|FAKE_KEYS" $p-phone; then fail "fake backlight or key code in the $p phone build"; fi
    if nm $p-phone | grep -q "fake_open\|fake_ioctl"; then fail "fake ALSA code in the $p phone build"; fi
done
echo "phone builds clean, no test code in them"

png() { python3 -I ppm2png.py png "$1" "/out/$2.png"; echo "frame: $2.png"; }
# colour share in permille: share FILE RRGGBB
share() {
    python3 -I - "$1" "$2" <<"EOF"
import sys
sys.path.insert(0, "/w")
from ppm2png import read_ppm
w, h, px = read_ppm(sys.argv[1]); c = bytes.fromhex(sys.argv[2]); n = 0; step = 5
for i in range(0, w * h, step):
    p = px[i * 3:i * 3 + 3]
    if all(abs(p[k] - c[k]) <= 10 for k in range(3)): n += 1
print(n * 1000 * step // (w * h))
EOF
}
val() { grep -o "\b$2=[-0-9.]*" "$1" | tail -1 | cut -d= -f2; }
REC="python3 -I touchrec.py"
J71=0:14620:0:19538          # the J71 reports its own units (touchscreen-size 14621 x 19539)
IPAD=0:1535:0:2047           # J85, J81: pixels
N66=0:1079:0:1919
LAND=0:2047:0:1535

echo "== touch.h unit tests"
./touch_test

echo "== fake games and IIO trees"
mkdir -p /tmp/bin /tmp/run /tmp/noiio "/tmp/iio/iio:device2" "/tmp/iio/iio:device0"
echo apple-orion-vbus >"/tmp/iio/iio:device0/name"
echo apple-aop-accel >"/tmp/iio/iio:device2/name"
for g in voidmoths hedgewick glint; do
    printf "#!/bin/sh\necho \"%s \$*\" >>/tmp/children.log\nexit 0\n" $g >/tmp/bin/$g
    chmod +x /tmp/bin/$g
done
sed -i "s/^exit 0/: --from-picker\nexit 0/" /tmp/bin/hedgewick /tmp/bin/glint /tmp/bin/voidmoths
touch /tmp/tty
AR="./ar --tty /tmp/tty --bin-dir /tmp/bin --run-dir /tmp/run --notice none"

echo "== ARCADE 1536x2048, all keys, no motion sensor, no touchscreen"
rm -f /tmp/fb /tmp/children.log
FAKE_FB=/tmp/fb:1536x2048x32 $AR --iio-root /tmp/noiio \
    --script "10:voldown,20:voldown,30:voldown,40:volup,50:mute*20,80:home" --ticks 120 \
    --shot 5:/tmp/a1.ppm >/dev/null 2>log || { tail log; fail "arcade, iPad keys"; }
grep -q "canvas 270x480 scaled 4x at 228,64" log || { grep canvas log; fail "iPad canvas placement"; }
grep -q "none yet, GLINT is grey" log || fail "no motion sensor not reported"
grep -q "^motion=0 touch=0 " log || fail "motion/touch state"
grep -q "launches=1 " log || fail "launches"
[ "$(cat /tmp/children.log)" = "hedgewick --tty /tmp/tty --hi /tmp/run/hedgewick.hi --from-picker" ] ||
    fail "VOL- VOL- VOL- VOL+ must skip the grey GLINT and end on HEDGEWICK: $(cat /tmp/children.log)"
echo "keys skip the grey GLINT card; the mute switch does nothing; $(grep -o "canvas.*" log | head -1)"
[ "$(share /tmp/a1.ppm 8a5230)" = 0 ] || fail "the GLINT card is not grey (wood colour seen)"
png /tmp/a1.ppm ipad-arcade-1536x2048-keys-no-sensor

echo "== ARCADE 1536x2048, touch in J71 units"
# card centres: x 768; VOIDMOTHS y 484, HEDGEWICK 948, GLINT 1412; 100,100 is letterbox
$REC /tmp/t1 $J71 1536x2048 "tap 500 768 484; tap 1200 768 484; tap 2500 768 1412; tap 3000 100 100; tap 3600 768 948; tap 4200 768 948"
rm -f /tmp/fb /tmp/children.log
FAKE_TOUCH=/tmp/t1:$J71 FAKE_FB=/tmp/fb:1536x2048x32 $AR --iio-root /tmp/noiio --script "1:mute" --ticks 100 \
    --shot 10:/tmp/a2.ppm --shot 98:/tmp/a3.ppm >/dev/null 2>log || { tail log; fail "arcade touch 1"; }
grep -q "touch recorded (event-1) x 0..14620 y 0..19538" log || { grep -i touch log; fail "touch device"; }
grep -q "msg=.GLINT NEEDS A MOTION SENSOR" log || { grep msg= log; fail "a tap on the grey GLINT card says nothing"; }
grep -q "^motion=0 touch=1 contacts=4 taps=4 .* picks=1 starts=1" log || { grep ^motion log; fail "touch picks/starts (part 1)"; }
[ "$(cat /tmp/children.log)" = "voidmoths --tty /tmp/tty --hi /tmp/run/voidmoths.hi --from-picker" ] || fail "tap, tap: VOIDMOTHS"
png /tmp/a2.ppm ipad-arcade-1536x2048-touch
png /tmp/a3.ppm ipad-arcade-1536x2048-touch-needs-sensor
rm -f /tmp/fb /tmp/children.log
FAKE_TOUCH=/tmp/t1:$J71 FAKE_FB=/tmp/fb:1536x2048x32 $AR --iio-root /tmp/noiio --script "1:mute" --ticks 170 >/dev/null 2>log || fail "arcade touch 2"
grep -q "^motion=0 touch=1 contacts=6 taps=6 .* picks=2 starts=2" log || { grep ^motion log; fail "touch picks/starts"; }
[ "$(cut -d" " -f1 /tmp/children.log | tr "\n" " ")" = "voidmoths hedgewick " ] || fail "tap, tap: VOIDMOTHS, then HEDGEWICK"
echo "tap a card, tap it again: $(cut -d" " -f1 /tmp/children.log | tr "\n" " "); the letterbox tap hit nothing"

echo "== ARCADE 1536x2048, touch in pixels (J85, J81), motion sensor present"
$REC /tmp/t2 $IPAD 1536x2048 "tap 500 768 1412; tap 1100 768 1412"
rm -f /tmp/fb /tmp/children.log
FAKE_TOUCH=/tmp/t2:$IPAD FAKE_FB=/tmp/fb:1536x2048x32 $AR --iio-root /tmp/iio --script "1:mute" --ticks 60 >/dev/null 2>log || fail "arcade touch, sensor"
grep -q "^motion=1 " log || fail "the fake apple-aop-accel was not found"
[ "$(cat /tmp/children.log)" = "glint --tty /tmp/tty --hi /tmp/run/glint.best --from-picker --fb-mmap" ] || fail "GLINT by touch: $(cat /tmp/children.log)"
echo "with a motion sensor GLINT is live: $(cat /tmp/children.log)"

echo "== ARCADE 1080x1920 (N66), touch and sensor"
$REC /tmp/t3 $N66 1080x1920 "tap 500 540 420; tap 1000 540 884; tap 1500 540 884"
rm -f /tmp/fb /tmp/children.log
FAKE_TOUCH=/tmp/t3:$N66 FAKE_FB=/tmp/fb:1080x1920x32 $AR --iio-root /tmp/iio --script "1:mute" --ticks 70 \
    --shot 10:/tmp/a4.ppm --shot 25:/tmp/a4b.ppm >/dev/null 2>log || fail "arcade N66 touch"
grep -q "canvas 270x480 scaled 4x at 0,0" log || fail "N66 canvas"
[ "$(cat /tmp/children.log)" = "hedgewick --tty /tmp/tty --hi /tmp/run/hedgewick.hi --from-picker" ] || fail "N66 taps: $(cat /tmp/children.log)"
[ "$(share /tmp/a4.ppm 8a5230)" -ge 3 ] || fail "GLINT card not live with a sensor"
png /tmp/a4b.ppm ipad-arcade-1080x1920-n66-touch
echo "N66: tap VOIDMOTHS, tap HEDGEWICK, tap again: $(cut -d" " -f1 /tmp/children.log)"

echo "== ARCADE landscape 2048x1536"
$REC /tmp/t4 $LAND 2048x1536 "tap 500 1024 363"
rm -f /tmp/fb
FAKE_TOUCH=/tmp/t4:$LAND FAKE_FB=/tmp/fb:2048x1536x32 $AR --iio-root /tmp/noiio --script "1:mute" --ticks 30 \
    --shot 25:/tmp/a5.ppm >/dev/null 2>log || fail "arcade landscape"
grep -q "canvas 270x480 scaled 3x at 619,48" log || { grep canvas log; fail "landscape canvas"; }
grep -q "^ticks=30 sel=0 " log || fail "landscape tap on VOIDMOTHS"
png /tmp/a5.ppm ipad-arcade-landscape-2048x1536
echo "landscape: x3 at 619,48, a tap chose VOIDMOTHS"

echo "== ARCADE with keys missing"
rm -f /tmp/fb /tmp/children.log
FAKE_FB=/tmp/fb:1536x2048x32 $AR --iio-root /tmp/noiio --keys home,power \
    --script "10:power,20:power,30:power,40:home" --ticks 70 --shot 5:/tmp/a6.ppm >/dev/null 2>log || fail "no volume keys"
[ "$(cut -d" " -f1 /tmp/children.log)" = voidmoths ] || fail "no volume keys: Power next, Home play: $(cat /tmp/children.log)"
png /tmp/a6.ppm ipad-arcade-no-volume-keys
echo "Home + Power only: Power = next game (GLINT skipped), Home = play"
for k in home power; do
    rm -f /tmp/fb /tmp/children.log
    FAKE_FB=/tmp/fb:1536x2048x32 $AR --iio-root /tmp/iio --keys $k \
        --script "10:$k*5,30:$k*5,60:$k*30" --ticks 120 --shot 5:/tmp/a7.ppm >/dev/null 2>log || fail "one key $k"
    [ "$(cut -d" " -f1 /tmp/children.log)" = voidmoths ] || fail "one key ($k): tap, tap, hold must play VOIDMOTHS: $(cat /tmp/children.log)"
    grep -q "launches=1 " log || fail "one key ($k): one launch, its release no next"
    grep -q "^ticks=120 sel=0 " log || fail "one key ($k): the release after the hold moved the selection"
    echo "one key only ($k): tap = next game, hold 0.6 s = play"
done
png /tmp/a7.ppm ipad-arcade-one-key
rm -f /tmp/fb
FAKE_FB=/tmp/fb:1536x2048x32 $AR --iio-root /tmp/noiio --keys none --script "1:mute" --ticks 10 --shot 5:/tmp/a8.ppm >/dev/null 2>log || fail "no keys"
png /tmp/a8.ppm ipad-arcade-no-keys-no-touch
$REC /tmp/t5 $J71 1536x2048 "tap 500 768 484; tap 1000 768 484"
rm -f /tmp/fb /tmp/children.log
FAKE_TOUCH=/tmp/t5:$J71 FAKE_FB=/tmp/fb:1536x2048x32 $AR --iio-root /tmp/noiio --keys none --script "1:mute" --ticks 45 \
    --shot 10:/tmp/a9.ppm >/dev/null 2>log || fail "touch only"
[ "$(cut -d" " -f1 /tmp/children.log)" = voidmoths ] || fail "touch only: $(cat /tmp/children.log)"
png /tmp/a9.ppm ipad-arcade-touch-only
echo "no buttons at all, touch only: tap, tap starts VOIDMOTHS"

echo "== screen off per board (fake sysfs; the backlight found by listing, any name)"
scv() { grep -o "screen: offs=.*" "$1" | grep -o "\b$2=[-0-9.]*" | cut -d= -f2; }
BL=/tmp/bl; BLD=$BL/sys/class/backlight/ipad-lcd-backlight
mkdir -p $BL/run $BLD; echo 0 >$BLD/bl_power; echo 180 >$BLD/brightness; echo 255 >$BLD/max_brightness
# J85: Home and Power, no volume keys. Power taps move on, Power held 1 s
# darkens; while dark a tap on a card and Home do nothing but (Home) light
# it; then Power moves on and Home plays.
$REC /tmp/tb $IPAD 1536x2048 "tap 3000 768 484; tap 3300 768 484"
rm -f /tmp/fb /tmp/children.log
FAKE_BL_ROOT=$BL FAKE_TOUCH=/tmp/tb:$IPAD FAKE_FB=/tmp/fb:1536x2048x32 $AR --iio-root /tmp/noiio --keys home,power \
    --script "10:power,40:power*30,130:home,150:power,170:home" --ticks 200 --shot 5:/tmp/a10.ppm >/dev/null 2>log || { tail log; fail "J85 screen off"; }
grep -q "backlight: ipad-lcd-backlight (bl_power)" log || fail "the backlight was not found by listing"
grep -q "screen off: Power held 0.8 s" log || fail "J85: no long-press mapping"
grep -q "screen: off (Power held 0.8 s): ipad-lcd-backlight bl_power=4" log || fail "J85: Power held did not darken"
[ "$(scv log longs)/$(scv log taps)/$(scv log offs)/$(scv log ons)/$(scv log eaten)" = 1/2/1/1/1 ] || fail "J85: $(grep -o "offs=.*" log)"
[ "$(val log picks)" = 0 ] && [ "$(val log tdropped)" -gt 0 ] || fail "J85: taps while dark were seen: $(grep "^motion=" log)"
[ "$(cat $BLD/bl_power)" = 0 ] || fail "J85: left dark"
got=$(cat /tmp/children.log); rm -f /tmp/children.log
FAKE_FB=/tmp/fb:1536x2048x32 $AR --iio-root /tmp/noiio --keys home,power --script "10:power,150:power,170:home" --ticks 200 >/dev/null 2>log || fail "J85 control"
[ "$got" = "$(cat /tmp/children.log)" ] || fail "J85: the hold, the taps and Home while dark changed the choice: $got / $(cat /tmp/children.log)"
png /tmp/a10.ppm ipad-arcade-j85-screen-off-hint
echo "J85 (no volume keys): Power tap = next, Power held 1 s = dark, taps on a card while dark ignored, Home lights it, then Power next, Home play"
# J81, J71 with volume keys: a Power press darkens
rm -f /tmp/fb /tmp/children.log
FAKE_BL_ROOT=$BL FAKE_FB=/tmp/fb:1536x2048x32 $AR --iio-root /tmp/noiio --keys home,power,vol \
    --script "10:power,40:home,60:home" --ticks 90 >/dev/null 2>log || fail "J81 screen off"
grep -q "screen off: a Power press" log && [ "$(scv log offs)/$(scv log ons)" = 1/1 ] && grep -q "launches=1 " log ||
    fail "J81: Power press dark, Home lights, Home plays: $(grep -o "offs=.*" log)"
echo "J81/J71 (volume keys): Power press = dark, Home lights it (and only that), Home plays"
# one key, or none: no screen off, Power as before
for k in home power; do
    rm -f /tmp/fb /tmp/children.log
    FAKE_BL_ROOT=$BL FAKE_FB=/tmp/fb:1536x2048x32 $AR --iio-root /tmp/iio --keys $k \
        --script "10:$k*5,30:$k*5,60:$k*30" --ticks 120 >/dev/null 2>log || fail "one key $k, backlight"
    grep -q "screen off: none" log && [ "$(scv log offs)" = 0 ] || fail "one key ($k): $(grep "screen off" log)"
    [ "$(cut -d" " -f1 /tmp/children.log)" = voidmoths ] || fail "one key ($k) with a backlight: $(cat /tmp/children.log)"
done
echo "one key only: no screen off, tap = next, hold = play as before"
# a brightness-only iPad backlight, in a game: kept value back on exit
rm -f $BLD/bl_power
FAKE_BL_ROOT=$BL FAKE_KEYS="home,power/1000:home,3000:power*1000" ./hw --bot 300 --no-pilot >/dev/null 2>log || fail "brightness-only J85"
grep -q "screen: off (Power held 0.8 s): ipad-lcd-backlight brightness 180->0" log || { grep "screen" log; fail "brightness-only: not darkened"; }
[ "$(cat $BLD/brightness)" = 180 ] && [ ! -e $BL/run/arcade-backlight ] || fail "brightness-only: $(cat $BLD/brightness) after exit"
[ "$(cat $BLD/max_brightness)" = 255 ] || fail "max_brightness touched"
echo "brightness only (no bl_power): 180 -> 0 while dark, 180 again at exit"

echo "== HEDGEWICK 1536x2048, touch only (J71 units), from the arcade"
# 3.0 s tap: start; play from about 5.5 s; swipes 180 px (30 canvas units);
# 11 s tap the top bar: pause; 12 s tap: on; 13 s corner 2.5 s: title;
# 17 s corner 2.5 s: back to the arcade
$REC /tmp/t6 $J71 1536x2048 "tap 3000 768 1024; swipe 6000 768 1024 588 1024 100; swipe 7000 768 1024 768 844 100; swipe 8000 768 1024 948 1024 100; swipe 9000 768 1024 768 1204 100; swipe 9600 768 1024 588 1024 100; tap 11000 768 124; tap 12000 768 1024; long 13000 10 10 2500; long 17000 10 10 2500"
rm -f /tmp/fb
FAKE_TOUCH=/tmp/t6:$J71 FAKE_FB=/tmp/fb:1536x2048x32 ./hw --bot 900 --no-pilot --from-picker \
    --shot title:/tmp/h1.ppm --shot play:/tmp/h2.ppm --shot pause:/tmp/h3.ppm --shot hold:/tmp/h4.ppm >/dev/null 2>log || { tail log; fail "hedgewick touch"; }
grep -E "^(ticks|quits|touch)=" log
grep -q "canvas 180x320 scaled 6x at 228,64" log || fail "hedgewick iPad canvas"
grep -q "games=1 " log || fail "a tap did not start a game"
[ "$(val log tswipes)" -ge 4 ] || fail "swipes in play not taken"
[ $(( $(val log turns) + $(val log uturns) )) -ge 1 ] || fail "no turn or U-turn from the swipes"
[ "$(val log pauses)" -ge 1 ] || fail "a tap on the top bar did not pause"
[ "$(val log quits)" = 1 ] || fail "the corner held in play did not go to the title"
grep -q "exit=1 " log || fail "the corner held on the title did not go back to the arcade"
grep -q "back to the arcade" log || fail "hedgewick exit log"
for s in 1 2 3 4; do [ -s /tmp/h$s.ppm ] || fail "hedgewick shot $s missing"; done
png /tmp/h1.ppm ipad-hedgewick-title-touch
png /tmp/h2.ppm ipad-hedgewick-play-touch
png /tmp/h3.ppm ipad-hedgewick-pause-touch
png /tmp/h4.ppm ipad-hedgewick-corner-hold
echo "HEDGEWICK: tap start, $(val log tswipes) swipes -> $(val log turns) turns + $(val log uturns) U-turns, pause, corner: title, corner: arcade"

echo "== HEDGEWICK 1080x1920 (N66 pixels): tap to start"
$REC /tmp/t7 $N66 1080x1920 "tap 3000 540 960"
rm -f /tmp/fb
FAKE_TOUCH=/tmp/t7:$N66 FAKE_FB=/tmp/fb:1080x1920x32 ./hw --bot 200 --no-pilot >/dev/null 2>log || fail "hedgewick N66 touch"
grep -q "games=1 " log || fail "N66: a tap did not start HEDGEWICK"

echo "== VOIDMOTHS 1536x2048, touch only (J71 units), from the arcade"
# 2.5 s tap: start; 3.5 s drag right; 6.5 s drag left (the ship moves 0.75
# columns a tick, so the finger leads); 9 s tap: fire; 9.5 s corner 2.5 s:
# title; 13 s corner 2.5 s: back to the arcade
$REC /tmp/t8 $J71 1536x2048 "tap 2500 768 1000; drag 3500 400 1800 1300 1800 2000; drag 6500 1300 1700 300 1700 2000; tap 9000 768 1000; long 9500 10 10 2500; long 13000 10 10 2500"
rm -rf /tmp/vmd; mkdir -p /tmp/vmd
FAKE_TOUCH=/tmp/t8:$J71 ./vm --bot 600 --fakefb 1536x2048x32 --no-pilot --from-picker --dump /tmp/vmd --dump-at 60,170,270 >/dev/null 2>log || { tail log; fail "voidmoths touch"; }
grep -E "^(ticks|quits|touch)=" log
grep -q "games=1 " log || fail "a tap did not start VOIDMOTHS"
[ "$(val log tdrag)" -gt 30 ] || fail "drags did not steer"
awk "BEGIN{exit !($(val log px) < 6)}" || fail "the ship did not follow the finger to the left (px $(val log px))"
[ "$(val log quits)" = 1 ] || fail "the corner held in play did not go to the title"
grep -q "exit=1" log || fail "the corner held on the title did not go back to the arcade"
png /tmp/vmd/frame-000060.ppm ipad-voidmoths-title-touch
png /tmp/vmd/frame-000170.ppm ipad-voidmoths-drag-right
echo "VOIDMOTHS: tap start, drag right then left (px $(val log px)), corner: title, corner: arcade"
rm -rf /tmp/vmd; mkdir -p /tmp/vmd
$REC /tmp/t9 $N66 1080x1920 "tap 1500 540 900; drag 2500 100 1700 1000 1700 1500"
FAKE_TOUCH=/tmp/t9:$N66 ./vm --bot 150 --fakefb 1080x1920x32 --no-pilot >/dev/null 2>log || fail "voidmoths N66 touch"
grep -q "games=1 " log || fail "N66: a tap did not start VOIDMOTHS"
awk "BEGIN{exit !($(val log px) > 38)}" || fail "N66: the ship did not follow right (px $(val log px))"
echo "N66 1080x1920: tap, drag right: px $(val log px)"

echo "== GLINT 1080x1920 (N66), touch, no motion sensor, from the arcade"
# 2 s swipe left: board 2; 3 s tap: start; 4-6 s a finger as gravity;
# 6.5 s tap the top bar: pause; 7.5 s tap: on; 8.5 s corner: title;
# 12 s corner: back to the arcade
$REC /tmp/t10 $N66 1080x1920 "swipe 2000 700 1000 550 1000 100; tap 3000 540 1000; long 4000 540 1700 2000; tap 6500 540 40; tap 7500 540 1000; long 8500 10 10 2500; long 12000 10 10 2500"
rm -rf /tmp/gld; mkdir -p /tmp/gld
FAKE_TOUCH=/tmp/t10:$N66 ./gl --bot 1000 --fakefb 1080x1920x32 --no-pilot --from-picker --iio none --mute --dump /tmp/gld >/dev/null 2>log || { tail log; fail "glint touch"; }
grep -E "^(stats|touch=)" log
grep "^stats:" log | grep -q " calib=1 " || fail "a tap on the title did not calibrate and start"
grep "^stats:" log | grep -q " pauses=1 " || fail "a tap on the top bar did not pause"
grep "^stats:" log | grep -q " quits=1 " || fail "the corner held in play did not go to the title"
grep "^stats:" log | grep -q " picker_exit=1 " || fail "the corner held on the title did not go back to the arcade"
[ "$(val log tswipes)" = 1 ] || fail "one swipe on the title"
[ "$(val log tsteer)" -gt 30 ] || fail "the finger did not steer gravity"
grep -q "board 2 " log || { grep board log; fail "the swipe did not choose board 2"; }
for f in touch-title touch-play touch-pause; do [ -s /tmp/gld/$f.ppm ] || fail "glint $f missing"; done
png /tmp/gld/touch-title.ppm ipad-glint-n66-title-touch
png /tmp/gld/touch-pause.ppm ipad-glint-n66-pause-touch
echo "GLINT: swipe to board 2, tap start, finger gravity, pause, corner: title, corner: arcade"

echo "== sound: the iPad cards are never opened"
for card in "iPad Air Speakers" "iPad mini Speakers" "J81 Speakers"; do
    R=/tmp/snd-$(echo $card | tr -d " ")
    mkdir -p $R/proc/asound $R/dev/snd
    printf " 0 [Speakers       ]: simple-card - %s\n                      %s\n" "$card" "$card" >$R/proc/asound/cards
    touch $R/dev/snd/controlC0 $R/dev/snd/pcmC0D0p
    rm -f /tmp/fb
    FAKE_ALSA_ROOT=$R FAKE_FB=/tmp/fb:1536x2048x32 ./ar-snd --tty /tmp/tty --bin-dir /tmp/bin --run-dir /tmp/run --notice none \
        --script "10:voldown,20:volup,30:voldown" --ticks 60 >/dev/null 2>log || { tail log; fail "arcade, $card"; }
    if grep -q "fake: open" log; then fail "arcade opened a device of $card"; fi
    FAKE_ALSA_ROOT=$R ./hw-snd --bot 600 >/dev/null 2>log || fail "hedgewick, $card"
    if grep -q "fake: open" log; then fail "hedgewick opened a device of $card"; fi
    grep -q "sound=3 " log || fail "hedgewick not silent with $card"
    FAKE_ALSA_ROOT=$R ./vm-snd --bot 600 >/dev/null 2>log || fail "voidmoths, $card"
    if grep -q "fake: open" log; then fail "voidmoths opened a device of $card"; fi
    FAKE_ALSA_ROOT=$R ./gl-snd --bot 1200 >/dev/null 2>log || fail "glint, $card"
    if grep -q "fake: open" log; then fail "glint opened a device of $card"; fi
    echo "\"$card\" (id Speakers): no program opens it"
done
R=/tmp/snd-n66
mkdir -p $R/proc/asound $R/dev/snd
printf " 0 [Speaker        ]: simple-card - iPhone Speaker\n                      iPhone Speaker\n" >$R/proc/asound/cards
touch $R/dev/snd/controlC0 $R/dev/snd/pcmC0D0p
FAKE_ALSA_ROOT=$R ./hw-snd --bot 600 >/dev/null 2>log || fail "hedgewick, N66 card"
grep -q "Speaker Volume := 164 (-20.0 dB)" log || fail "the N66 speaker card is no longer found"
echo "\"iPhone Speaker\" (id Speaker): still found, -20 dB"

echo "== init: what it loads on each board (fake device trees)"
sh -n init || fail "init: syntax"
# the device-tree blocks of init, with the paths moved into a fake root
sed -n "/^dt=\/proc\/device-tree/,/^# Storage:/p" init |
    sed -e "s|/proc/device-tree|\$R/dt|; s|/sys/bus/platform/devices|\$R/plat|g; s|/lib/firmware|\$R/fw|g" \
        -e "s|/run/|\$R/run/|g; s|/sys/class/input|\$R/input|g; s|/sys/bus/iio|\$R/iio|g; s|sleep [0-9]*|:|" >/tmp/init-dt.sh
grep -q "has_compatible apple,s8000-aop" /tmp/init-dt.sh || fail "init: no AOP gate"
str() { mkdir -p "$(dirname "$1")"; printf "%s\000" "$2" >"$1"; }
board() {   # board NAME TOUCHFW AOP(0/1) SPEAKERNODE CARDNAME [TOUCHSTATUS]
    R=/tmp/dt-$1; rm -rf $R; mkdir -p $R/plat $R/fw/apple $R/run $R/input/event0/device
    echo gpio-keys >$R/input/event0/device/name
    str $R/dt/aliases/touchscreen0 /soc/spi@1/touchscreen@0
    str $R/dt/soc/spi@1/touchscreen@0/firmware-name "$2"
    [ -z "${6:-}" ] || str $R/dt/soc/spi@1/touchscreen@0/status "$6"
    touch "$R/fw/$2"
    if [ "$3" = 1 ]; then str $R/plat/210800000.aop/of_node/compatible "apple,s8000-aop"; touch $R/fw/apple/aop-s8000.bin $R/fw/apple/aop-s8000-ext.bin; fi
    if [ -n "$4" ]; then
        mkdir -p $R/plat/$4
        str $R/dt/$4/simple-audio-card,name "$5"; str $R/dt/$4/label "$5"
    fi
    ( export R; modprobe() { echo "$*" >>$R/mods; }; dmesg() { :; }; . /tmp/init-dt.sh; wait ) >/dev/null 2>&1
    sort $R/mods 2>/dev/null | tr "\n" " " | sed "s/ $//"
}
m=$(board n66 apple/n66-fwstart.z2fw 1 sound-speaker "iPhone Speaker")
[ "$m" = "apple_aop apple_aop_accel apple_sio apple_sio_dma apple_z2 clk_apple_nco snd_soc_apple_s8000_mca snd_soc_cs35l19 snd_soc_simple_card" ] ||
    fail "init on the N66 loads: $m"
echo "N66: $m"
grep -q "touchscreen0 = /soc/spi@1/touchscreen@0, status okay, firmware apple/n66-fwstart.z2fw" /tmp/dt-n66/run/touch.log || fail "N66 touch.log"
m=$(board j71 apple/j72-fwstart.z2fw 0 sound "iPad Air Speakers"); [ "$m" = apple_z2 ] || fail "init on the J71 loads: $m"; echo "J71: $m"
m=$(board j85 apple/j85-fwstart.z2fw 0 sound-speakers "iPad mini Speakers"); [ "$m" = apple_z2 ] || fail "init on the J85 loads: $m"; echo "J85: $m"
m=$(board j81 apple/j81-fwstart.z2fw 0 sound-speakers "J81 Speakers"); [ "$m" = apple_z2 ] || fail "init on the J81 loads: $m"; echo "J81: $m"
grep -q "no enabled apple,s8000-aop" /tmp/dt-j81/run/motion.log || fail "J81 motion.log"
grep -q "silent" /tmp/dt-j81/run/sound.log || fail "J81 sound.log"
m=$(board odd apple/x.z2fw 0 sound-speaker "iPad mini Speakers" disabled); [ -z "$m" ] || fail "disabled touchscreen, other speaker card: $m"
grep -q "disabled in the device tree" /tmp/dt-odd/run/touch.log || fail "disabled touchscreen log"
echo "a disabled touchscreen and a /sound-speaker of another name: nothing loaded"
R=/tmp/dt-none; rm -rf $R; mkdir -p $R/dt $R/plat $R/run $R/input
( export R; modprobe() { echo "$*" >>$R/mods; }; . /tmp/init-dt.sh; wait ) >/dev/null 2>&1
[ "$(cat $R/mods)" = "$(printf -- "-q gpio_keys\n-q evdev")" ] || fail "a bare tree loads: $(cat $R/mods)"
grep -q "no touchscreen0 alias" $R/run/touch.log || fail "no-touch log"
echo "a tree without any of them and no gpio-keys device: only the button fallback (gpio_keys, evdev)"

cd /out && ls && tar cf - *.png >&3
echo "ALL TESTS PASSED"
' | tar -xf - -C "$frames"
ls "$frames"/ipad-*.png
