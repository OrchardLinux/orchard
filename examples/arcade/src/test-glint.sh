#!/bin/sh
# Headless tests of glint in the kernel toolchain image, under ASan and UBSan
# (native gcc there is aarch64 too):
#  - --selftest: the physics (no tunnelling at and far past full tilt on
#    every board, the marble settles on a flat board, holes and the cup) and
#    the sensor arithmetic (the six poses measured on the N66, calibration);
#  - the bot: it "tilts the phone" by writing raw values into a fake IIO
#    sysfs tree, calibrates on the title, pauses, falls into a hole once and
#    solves the boards, on a fake framebuffer (memory described like
#    /dev/fb0) at 1080x1920 in XRGB, XBGR and RGB565 and at odd sizes; frames
#    are dumped and must decode alike in every format; the pwrite() path is
#    run against a file and must give the same pictures;
#  - the sensor going wrong: none at all, frozen values, the device going
#    away and coming back, junk in the raw files; the IIO tree is never
#    written by the game (only with --rate, and then only sampling_frequency);
#  - the console mode: KD_GRAPHICS while drawing, KD_TEXT again on a normal
#    end, SIGTERM, SIGINT and fatal signals; --from-picker; best times;
#  - notices, in real time, through the notice helper script;
#  - the sound code against no device and against a fake ALSA speaker card
#    (src/fake_alsa.c) in every failure mode it knows;
#  - screen off (src/backlight.h) on a fake sysfs ($FAKE_BL_ROOT) with
#    scripted buttons ($FAKE_KEYS): Power darkens, Home and Power light it
#    again, the clock and the marble frozen while dark and the board paused
#    after (the autopilot plays on and clears it); Vol- and touch ignored
#    while dark; Power held 0.8 s without volume keys; the speaker closed
#    while dark; in real time: nothing drawn, near-idle CPU, a notice lights
#    it, SIGTERM and SIGSEGV while dark light it again, and so do
#    --restore-tty and a restart after a SIGKILL;
#  - the phone build (static, -Werror) and the frame cost and CPU share in a
#    build without sanitizers (-O2, as on the phone).
# Usage: src/test-glint.sh [OUT.raw [FRAMES_DIR]]
#   OUT.raw receives 60 s of the game's sound (S16_LE, mono, 48 kHz) from the
#   fake card; FRAMES_DIR (default /private/tmp/claude-501/arcade-frames/glint)
#   receives the dumped frames as PNG.
# The sources go in on stdin (see build.sh for why); the results come back as
# a tar stream on stdout.
set -eu
here=$(cd "$(dirname "$0")" && pwd)
raw=${1:-/dev/null}
frames=${2:-/private/tmp/claude-501/arcade-frames/glint}
res=$(mktemp -d "${TMPDIR:-/tmp}/gltest.XXXXXX")
trap 'rm -rf "$res"' EXIT
COPYFILE_DISABLE=1 tar -C "$here" -cf - glint.c touch.h backlight.h fake_alsa.c ppm2png.py touchrec.py -C "$here/../rootfs/usr/bin" notice |
docker run -i --rm localhost/orchard-hardware-kernel-toolchain:bookworm sh -c '
set -eu
exec 3>&1 1>&2
mkdir -p /w /out/frames && cd /w && tar xf - 2>/dev/null
CF="-std=gnu11 -Wall -Wextra -Wshadow -Werror -O1 -g -fsanitize=address,undefined -fno-sanitize-recover=all -fno-omit-frame-pointer"
gcc $CF -o gl-asan glint.c -lm
gcc $CF -DGL_FAKE_ALSA -o gl-fake glint.c -lm
gcc $CF -DTOUCH_TEST -o gl-bl glint.c -lm
gcc $CF -DGL_FAKE_ALSA -DBL_TEST -o gl-bl-snd glint.c -lm
gcc -std=gnu11 -Wall -Wextra -Wshadow -Werror -O2 -o gl-bench glint.c -lm
aarch64-linux-gnu-gcc -std=gnu11 -Wall -Wextra -Wshadow -Werror -static -O2 -o gl-phone glint.c -lm
if nm gl-phone | grep -q "fake_ioctl\|fake_open\|fk_"; then echo "FAIL: fake ALSA code in the phone build"; exit 1; fi
if grep -q "FAKE_BL_ROOT\|FAKE_KEYS\|FAKE_TOUCH" gl-phone; then echo "FAIL: test hooks in the phone build"; exit 1; fi
fail() { echo "FAIL: $*"; exit 1; }
stat() { grep "^stats:" "$1" | cut -c8-; }
val() { sed -n "s/^stats:.* $2=\([-0-9]*\).*/\1/p" "$1"; }
kd_ok() {   # $1 log: KD_GRAPHICS once, and the last word is KD_TEXT
    [ "$(grep -c "^kd: KD_GRAPHICS" "$1")" = 1 ] || fail "$2: KD_GRAPHICS not set once"
    [ "$(grep "^kd:" "$1" | tail -1)" = "kd: KD_TEXT" ] || fail "$2: console not back in KD_TEXT"
}
# A fake IIO tree as the N66 shows it: the charger ADC, the light sensor, the
# three AOP devices. $2: what is missing or wrong.
mkiio() {
    rm -rf $1
    dv=$1/iio:device2
    mkdir -p $1/iio:device0 $1/iio:device1 $dv $1/iio:device3 $1/trigger0
    echo apple-orion-vbus >$1/iio:device0/name
    echo ct821 >$1/iio:device1/name
    echo apple-aop-gyro >$1/iio:device3/name
    echo apple-aop-accel >$dv/name
    echo 0.000149637 >$dv/in_accel_scale
    echo "1, 0, 0; 0, 1, 0; 0, 0, -1" >$dv/in_accel_mount_matrix
    echo 10.000000 >$dv/sampling_frequency
    echo 0 >$dv/in_accel_x_raw; echo -32768 >$dv/in_accel_y_raw; echo -56756 >$dv/in_accel_z_raw
    case ${2:-} in
    nomatrix) rm $dv/in_accel_mount_matrix $dv/in_accel_scale ;;
    noraw) rm $dv/in_accel_z_raw ;;
    junk) echo "not a number" >$dv/in_accel_y_raw ;;
    esac
}
ro_sum() { cat $1/iio:device2/name $1/iio:device2/in_accel_scale $1/iio:device2/in_accel_mount_matrix $1/iio:device2/sampling_frequency | md5sum; }

echo "== self-test: physics and sensor arithmetic"
./gl-asan --selftest 2>/dev/null | tee selftest.txt
grep -q "^SELFTEST PASSED" selftest.txt || fail "selftest"

echo "== the bot on a fake framebuffer, tilting through a fake IIO tree"
for spec in 1080x1920x32 1080x1920x32:bgr 1080x1920x16; do
    d=/tmp/f-$(echo $spec | tr ":" "-"); mkdir -p $d; rm -f /tmp/best; mkiio /tmp/iio
    before=$(ro_sum /tmp/iio)
    ./gl-asan --bot 14500 --fakefb $spec --iio /tmp/iio --hi /tmp/best --notice none --dump $d >/dev/null 2>log ||
        { tail -20 log; fail "bot $spec"; }
    printf "%-17s %s\n" $spec "$(stat log)"
    grep -m1 "graphics:" log | cut -c16-
    grep -q "line padding intact" log || fail "$spec: wrote past line_length"
    kd_ok log $spec
    [ "$(ro_sum /tmp/iio)" = "$before" ] || fail "$spec: the game changed the IIO tree"
    [ "$(val log cleared)" = 63 ] || fail "$spec: the bot did not solve all six boards (cleared=$(val log cleared))"
    [ "$(val log falls)" -ge 1 ] || fail "$spec: no fall into a hole"
    [ "$(val log pauses)" -ge 1 ] || fail "$spec: no pause"
    [ "$(val log calib)" -ge 2 ] || fail "$spec: no calibration"
    [ "$(val log sensor)" = 1 ] || fail "$spec: sensor not in use at the end"
    grep -q "sensor: .*apple-aop-accel.*scale 0.000149637, matrix 1 0 0; 0 1 0; 0 0 -1, sampling_frequency 10.000000" log || fail "$spec: sensor not described"
    grep -q "calibrated, level is gravity (-\?0.00, -0.50, -0.87) g, 30 degrees from flat" log || fail "$spec: calibration pose"
    [ "$(head -1 /tmp/best)" = 6 ] || fail "$spec: best times not saved"
    [ "$(ls $d | wc -l)" -ge 16 ] || fail "$spec: frames missing ($(ls $d | tr "\n" " "))"
    grep "cleared in" log | head -6 | cut -c16- >wins-$spec
done
cat wins-1080x1920x16
cp log bot.log
cat /tmp/best | tr "\n" " "; echo "(best times file)"
for f in $(ls /tmp/f-1080x1920x32); do
    cmp -s /tmp/f-1080x1920x32/$f /tmp/f-1080x1920x32-bgr/$f || fail "XBGR frame $f differs from XRGB"
    dmax=$(python3 -I ppm2png.py diff /tmp/f-1080x1920x32/$f /tmp/f-1080x1920x16/$f)
    [ "$dmax" -le 7 ] || fail "RGB565 frame $f differs by $dmax"
done
echo "frames: XRGB = XBGR exactly; RGB565 within $dmax (5/6-bit rounding)"
for f in /tmp/f-1080x1920x32/*.ppm; do
    python3 -I ppm2png.py png $f /out/frames/$(basename $f .ppm).png
done
python3 -I ppm2png.py png /tmp/f-1080x1920x16/level-3.ppm /out/frames/level-3-rgb565.png

echo "== the pwrite path (the fake screen in a file), 32 and 16 bpp"
for spec in 1080x1920x32 1080x1920x16; do
    d=/tmp/w-$spec; mkdir -p $d; mkiio /tmp/iio
    ./gl-asan --bot 5200 --fakefb $spec --fakefb-file /tmp/fb.bin --iio /tmp/iio --hi none --notice none --dump $d >/dev/null 2>log ||
        { tail -20 log; fail "pwrite $spec"; }
    grep -q "line padding intact" log || fail "pwrite $spec: wrote past line_length"
    grep -q "pwrite to the framebuffer" log && fail "pwrite $spec: fell back to the mapping"
    n=0
    for f in $(ls $d); do cmp -s $d/$f /tmp/f-$spec/$f || fail "pwrite $spec: frame $f differs"; n=$((n + 1)); done
    [ $n -ge 6 ] || fail "pwrite $spec: only $n frames"
    echo "$spec: $n frames identical to the mapped run"
done

echo "== other screen sizes"
for spec in 720x1280x32 640x480x16 1242x2208x32 200x200x32 240x400x16; do
    mkiio /tmp/iio; d=/tmp/s-$spec; mkdir -p $d
    ./gl-asan --bot 5200 --fakefb $spec --iio /tmp/iio --hi none --notice none --dump $d >/dev/null 2>log || { tail -20 log; fail "fakefb $spec"; }
    printf "%-14s %s; wins=%s\n" $spec "$(grep -m1 "graphics:" log | sed "s/.*graphics: //; s/ (r .*line//")" "$(val log wins)"
    grep -q "line padding intact" log || fail "$spec: wrote past line_length"
    [ "$(val log wins)" -ge 2 ] || fail "$spec: the bot did not win (physics must not depend on the screen)"
    kd_ok log $spec
done
python3 -I ppm2png.py png /tmp/s-640x480x16/level-2.ppm /out/frames/small-640x480-level-2.png
st=0; ./gl-asan --bot 300 --fakefb 1080x1920x24 --iio none >/dev/null 2>log || st=$?
[ $st = 4 ] && grep -q "24 bits per pixel is not supported" log || fail "24 bpp not refused (exit $st)"
grep -q "^kd:" log && fail "24 bpp: console mode touched"
st=0; ./gl-asan --bot 300 --fakefb 100x100x16 --iio none >/dev/null 2>log || st=$?
[ $st = 4 ] || fail "100x100 not refused (exit $st)"
echo "1080x1920x24 and 100x100x16: refused, exit 4, console untouched"

echo "== no motion sensor: the game runs on the volume keys and says why"
for arg in none /nonexistent; do
    d=/tmp/n-$(basename $arg); mkdir -p $d
    ./gl-asan --bot 3000 --fakefb 1080x1920x32 --iio $arg --hi none --notice none --dump $d >/dev/null 2>log || { tail -20 log; fail "--iio $arg"; }
    printf "%-13s %s\n" "$arg" "$(grep -m1 "[0-9]: sensor:" log | cut -c16-)"
    grep -q "sensor: .*volume keys steer" log || fail "--iio $arg: reason not logged"
    grep -q "no motion sensor: volume keys" log || fail "--iio $arg: board start does not say so"
    [ "$(val log sensor)" = 0 ] || fail "--iio $arg: sensor in use?"
    [ "$(val log state)" = 1 ] || fail "--iio $arg: not playing at the end"
    [ -f $d/title-nosensor.ppm ] && [ -f $d/level-nosensor.ppm ] || fail "--iio $arg: frames missing"
done
python3 -I ppm2png.py png /tmp/n-none/title-nosensor.ppm /out/frames/title-nosensor.png
python3 -I ppm2png.py png /tmp/n-none/level-nosensor.ppm /out/frames/level-nosensor.png
mkiio /tmp/iio noraw
./gl-asan --bot 1500 --iio /tmp/iio --hi none --notice none >/dev/null 2>log || fail "noraw"
grep -q "sensor: .*in_accel_z_raw: No such file" log || fail "noraw: not logged"
[ "$(val log sensor)" = 0 ] || fail "noraw: sensor in use?"
echo "a raw file missing: $(grep -m1 "sensor:" log | cut -c16-)"
mkiio /tmp/iio junk
timeout --preserve-status -s TERM 3 ./gl-asan --fakefb 1080x1920x32 --iio /tmp/iio --hi none --notice none >/dev/null 2>log || fail "junk"
grep -q "sensor: lost (Invalid argument)" log || fail "junk: not logged"
echo "junk in a raw file: $(grep -m1 "sensor: lost" log | cut -c16-)"
mkiio /tmp/iio nomatrix
./gl-asan --bot 5200 --iio /tmp/iio --hi none --notice none >/dev/null 2>log || fail "nomatrix"
grep -q "no in_accel_mount_matrix, taking diag(1,1,-1)" log && grep -q "no in_accel_scale" log || fail "nomatrix: defaults not logged"
[ "$(val log wins)" -ge 2 ] || fail "nomatrix: the bot did not win"
echo "no scale, no mount matrix: defaults taken, the bot still wins"

echo "== the sensor freezes and goes away at frame 2500, comes back 12 s later"
mkiio /tmp/iio
./gl-asan --bot 14000 --iio /tmp/iio --hi none --notice none --bot-lose 2500 >/dev/null 2>log || { tail -20 log; fail "bot-lose"; }
grep "sensor:\|bot:" log | grep -v calibrated | cut -c7- | sed -n 2,12p
grep -q "values frozen for 3 s" log || fail "freeze not noticed"
grep -q "sensor: lost (device gone)" log || fail "loss not noticed"
[ "$(grep -c "sensor: first sample" log)" = 2 ] || fail "sensor not picked up again"
[ "$(val log sensor)" = 1 ] || fail "sensor not in use at the end"
[ "$(val log wins)" -ge 3 ] || fail "no wins after the sensor came back"
echo "$(stat log)"

echo "== --rate writes sampling_frequency, and only that; without it nothing is written"
mkiio /tmp/iio; before=$(ro_sum /tmp/iio)
./gl-asan --bot 300 --iio /tmp/iio --hi none --notice none --rate 50 >/dev/null 2>log || fail "--rate"
[ "$(head -1 /tmp/iio/iio:device2/sampling_frequency)" = 50 ] || fail "--rate 50 not written"
grep -q "asked for 50 Hz" log || fail "--rate not logged"
echo 10.000000 >/tmp/iio/iio:device2/sampling_frequency
[ "$(ro_sum /tmp/iio)" = "$before" ] || fail "--rate changed something else"
[ "$(ls /tmp/iio/iio:device2 | wc -l)" = 7 ] || fail "files appeared in the IIO tree"
echo "ok"

echo "== --from-picker: holding Home on the title ends the game with status 0"
mkiio /tmp/iio
st=0; ./gl-asan --bot 30000 --iio /tmp/iio --hi none --notice none --from-picker --dump /tmp >/dev/null 2>log || st=$?
[ $st = 0 ] && [ "$(val log picker_exit)" = 1 ] && [ "$(val log frames)" -lt 30000 ] || fail "--from-picker (exit $st)"
kd_ok log from-picker
echo "left at frame $(val log frames) of 30000, exit 0, console back in text mode"
./gl-asan --bot 30000 --iio /tmp/iio --hi none --notice none >/dev/null 2>log || fail "without --from-picker"
[ "$(val log picker_exit)" = 0 ] && [ "$(val log frames)" = 30000 ] || fail "left the title without --from-picker"
echo "without the flag the same hold does nothing"

echo "== best times survive a restart; a broken file is ignored"
mkiio /tmp/iio
./gl-asan --bot 200 --iio /tmp/iio --hi /tmp/best --notice none --dump /tmp/b >/dev/null 2>log || true
mkdir -p /tmp/b; ./gl-asan --bot 200 --iio /tmp/iio --hi /tmp/best --notice none --dump /tmp/b >/dev/null 2>log || fail "best reload"
python3 -I ppm2png.py png /tmp/b/title.ppm /out/frames/title-with-best.png
cmp -s /tmp/b/title.ppm /tmp/f-1080x1920x32/title.ppm && fail "the title does not show the saved best time"
printf "x\n-5\n99999999999999999999\n" >/tmp/bad; ./gl-asan --bot 200 --iio none --hi /tmp/bad --notice none >/dev/null 2>log || fail "bad best file"
echo "ok"

echo "== console mode on signals"
for sig in TERM INT SEGV ABRT QUIT USR1 ALRM; do
    mkiio /tmp/iio
    ./gl-asan --bot 1000000000 --fakefb 1080x1920x32 --iio /tmp/iio --hi none --notice none >/dev/null 2>log &
    pid=$!
    sleep 2
    kill -$sig $pid
    st=0; wait $pid || st=$?
    printf "%-5s exit %-3s %s\n" $sig $st "$(grep "^kd:" log | tr "\n" " ")"
    kd_ok log "SIG$sig"
    case $sig in
    TERM|INT) [ $st = 0 ] || fail "SIG$sig: exit $st" ;;
    SEGV) [ $st = 139 ] || fail "SIGSEGV: exit $st, not killed by the signal" ;;
    ABRT) [ $st = 134 ] || fail "SIGABRT: exit $st, not killed by the signal" ;;
    QUIT) [ $st = 131 ] || fail "SIGQUIT: exit $st, not killed by the signal" ;;
    USR1) [ $st = 138 ] || fail "SIGUSR1: exit $st, not killed by the signal" ;;
    ALRM) [ $st = 142 ] || fail "SIGALRM: exit $st, not killed by the signal" ;;
    esac
done

echo "== real time: the loop, no buttons, and notices through the helper"
mkdir -p /tmp/nt; rm -f /tmp/nt/notice
./gl-asan --fakefb 1080x1920x32 --iio none --hi none --notice /tmp/nt/notice 2>log >/dev/null &
pid=$!
sleep 2
NOTICE_FILE=/tmp/nt/notice sh ./notice "  step 3 done:   it'"'"'s \"fine\" \$HOME" || fail "notice helper"
sleep 2
NOTICE_FILE=/tmp/nt/notice sh ./notice "second notice, quite a long one, to see that it is cut off at sixty characters" || fail "notice helper"
sleep 2
kill -TERM $pid; st=0; wait $pid || st=$?
[ $st = 0 ] || fail "real-time run: exit $st"
grep "notice:\|input:\|^frames\|^cpu" log | sed "s/^glint *[0-9.]*: //"
grep -q "notice: .step 3 done: it.s \"fine\" \$HOME.$" log || fail "first notice"
grep -q "notice: .second notice, quite a long one, to see that it is cut off a.$" log || fail "second notice (60 characters)"
grep -q "notices=2 " log || fail "notice count"
kd_ok log real-time
[ -z "$(ls /tmp/nt | grep tmp)" ] || fail "notice helper left a temporary file"
echo "  hello before the start" >/tmp/nt/fresh
./gl-asan --bot 400 --iio none --hi none --notice /tmp/nt/fresh --dump /tmp/nt >/dev/null 2>log || fail "fresh notice"
grep -q "notice: .hello before the start." log || fail "a fresh notice at start was not shown"
python3 -I ppm2png.py png /tmp/nt/title-nosensor.ppm /out/frames/notice.png
touch -d @1000000000 /tmp/nt/fresh
./gl-asan --bot 60 --iio none --hi none --notice /tmp/nt/fresh >/dev/null 2>log || fail "old notice"
grep -q "notice:" log && fail "an old notice at start was shown"
mkfifo /tmp/nt/fifo
./gl-asan --bot 60 --iio none --hi none --notice /tmp/nt/fifo >/dev/null 2>log || fail "notice fifo"
echo "at start: a fresh notice is shown, an old one is not; a FIFO is ignored"

echo "== the real start-up path (no /dev/fb0, a tty that is /dev/null)"
st=0; timeout --preserve-status -s TERM 5 ./gl-asan --tty /dev/null --hi none --notice none 2>log >/dev/null || st=$?
[ $st = 4 ] || fail "no framebuffer: exit $st, not 4"
grep -q "^kd:" log && fail "no framebuffer: console mode touched"
echo "exit 4: $(grep -m1 graphics: log | cut -c16-)"
st=0; ./gl-asan --restore-tty /dev/null 2>/dev/null || st=$?
[ $st = 1 ] || fail "--restore-tty on a non-console: exit $st"
st=0; ./gl-asan --no-such-option 2>/dev/null || st=$?
[ $st = 1 ] || fail "unknown option: exit $st"
echo "--restore-tty /dev/null: refused cleanly (not a console)"

R=/tmp/fakeroot
mkdir -p $R/proc/asound $R/dev/snd
printf " 0 [Headset        ]: cs42l71 - iPhone Headset\n                      iPhone Headset\n 1 [Speaker        ]: simple-card - iPhone Speaker\n                      iPhone Speaker\n" >$R/proc/asound/cards
touch $R/dev/snd/controlC0 $R/dev/snd/pcmC0D0p $R/dev/snd/controlC1 $R/dev/snd/pcmC1D0p

echo "== fake speaker card (card 1; card 0 is a headset that must stay closed)"
mkiio /tmp/iio
FAKE_ALSA_ROOT=$R FAKE_ALSA_DUMP=/tmp/snd.raw ./gl-fake --bot 14500 --iio /tmp/iio --hi none --notice none >/dev/null 2>log || { tail -20 log; fail "fake ok"; }
grep -E "sound:|fake: (Speaker|HW_PARAMS|frames)" log | sed "s/^glint *[0-9.]*: //" | sort | uniq -c | sort -rn | head -10
grep -q "Speaker Volume := 164 (-20.0 dB)" log || fail "volume not -20 dB"
grep -q "sound=2 " log || fail "sound not on at the end"
grep -q "FORBIDDEN" log && fail "opened another device"
pk=$(sed -n "s/^fake: frames=.* peak=\([0-9]*\) .*/\1/p" log)
[ -n "$pk" ] && [ "$pk" -le 16384 ] && [ "$pk" -ge 1000 ] || fail "mix above -6 dBFS or silent (peak $pk)"
head -c $((48000 * 2 * 60)) /tmp/snd.raw >/tmp/snd60.raw

for m in hwpick xrun; do
    FAKE_ALSA_ROOT=$R FAKE_ALSA_MODE=$m ./gl-fake --bot 4000 --iio /tmp/iio --hi none --notice none >/dev/null 2>log || { tail -20 log; fail "fake $m"; }
    printf "%-10s %s\n" $m "$(grep -E "HW_PARAMS|sound: on" log | tail -2 | sed "s/^glint *[0-9.]*: //" | tr "\n" " ")"
    grep -q "sound=2 " log || fail "$m: sound not on"
done
grep -q "underruns" log || true

for m in hwfail notlv loud readback wrongcard dead; do
    FAKE_ALSA_ROOT=$R FAKE_ALSA_MODE=$m ./gl-fake --bot 2000 --iio /tmp/iio --hi none --notice none >/dev/null 2>log || { tail -20 log; fail "fake $m"; }
    printf "%-10s %s\n" $m "$(grep -m1 "running silent" log | cut -c16-)"
    grep -q "sound=4 " log || fail "$m: not silent"
done

for v in -3 -40; do
    FAKE_ALSA_ROOT=$R ./gl-fake --bot 300 --iio none --hi none --notice none --vol $v >/dev/null 2>log || fail "vol $v"
    printf "%-10s %s\n" "--vol $v" "$(grep -m1 "Speaker Volume :=" log)"
done
FAKE_ALSA_ROOT=$R ./gl-fake --bot 300 --iio none --hi none --notice none --vol -3 2>&1 >/dev/null | grep -q "Speaker Volume := 192 (-6.0 dB)" || fail "--vol -3 not held at -6 dB"
FAKE_ALSA_ROOT=$R ./gl-fake --bot 70 --iio none --hi none --notice none --mute >/dev/null 2>log || fail "--mute"
grep -q "fake: open" log && fail "--mute opened the card"

mkdir -p /tmp/nocard/proc/asound
printf " 0 [Headset        ]: cs42l71 - iPhone Headset\n                      iPhone Headset\n" >/tmp/nocard/proc/asound/cards
for r in /tmp/nocard /tmp/empty; do
    FAKE_ALSA_ROOT=$r ./gl-fake --bot 1200 --iio none --hi none --notice none >/dev/null 2>log || fail "no card $r"
    printf "%-10s %s\n" "${r#/tmp/}" "$(grep -m1 "running silent" log | cut -c16-)"
    grep -q "sound=3 " log || fail "$r: not silent"
done

echo "== screen off: Power darkens, Home or Power lights it again (fake sysfs)"
scv() { grep -o "screen: offs=.*" "$1" | grep -o "\b$2=[-0-9.]*" | cut -d= -f2; }
tv() { grep -o "^touch=.*" "$1" | grep -o "\b$2=[-0-9]*" | cut -d= -f2; }
BL=/tmp/bl; BLD=$BL/sys/class/backlight/20e200080.backlight
mkdir -p $BL/run $BLD; echo 0 >$BLD/bl_power; echo 37 >$BLD/brightness; echo 100 >$BLD/max_brightness
touch -d @1000000000 $BLD/max_brightness
bp() { cat $BLD/bl_power; }
lines() { grep -o "screen $2: .*" "$1"; }
mkiio /tmp/iio
FAKE_BL_ROOT=$BL FAKE_KEYS="home,power,vol/8000:power,15000:voldown,20000:home" ./gl-bl --bot 3000 --iio /tmp/iio --hi none --notice none >/dev/null 2>log || { tail log; fail "dark"; }
grep -q "screen: off (Power): 20e200080.backlight bl_power=4" log || fail "Power did not write bl_power 4"
grep -q "screen: on (Home)" log || fail "Home did not light the screen"
d0=$(lines log dark); l1=$(lines log lit)
echo "$d0" | grep -q "^screen dark: state 1," || fail "not in play when the screen went dark: $d0"
echo "$l1" | grep -q "^screen lit: state 2, .*, paused$" || fail "not paused after the dark screen: $l1"
[ "$(echo "$l1" | grep -o "board.*falls [0-9]*")" = "$(echo "$d0" | grep -o "board.*falls [0-9]*")" ] || fail "the board ran while dark: $d0 / $l1"
[ "$(scv log offs)/$(scv log ons)/$(scv log eaten)" = 1/1/2 ] || fail "offs/ons/eaten: $(grep -o "offs=.*" log)"
[ "$(val log cleared)" -ge 1 ] || fail "the autopilot did not play on after the pause"
[ "$(val log losses)" = 0 ] || fail "Vol- while dark restarted the board"
echo "GLINT: dark 8-20 s, $(echo "$d0" | grep -o "clock.*") before and after, then paused; Vol- while dark eaten; the autopilot plays on and clears $(val log cleared)"
FAKE_BL_ROOT=$BL FAKE_KEYS="home,power,vol/8000:power,12000:power" ./gl-bl --bot 1200 --iio /tmp/iio --hi none --notice none >/dev/null 2>log || fail "Power wakes"
grep -q "screen: on (Power)" log && [ "$(scv log ons)" = 1 ] || fail "Power did not light the screen"
echo "Power lights it as well"
python3 -I touchrec.py /tmp/tt 0:1079:0:1919 1080x1920 "tap 5000 540 960; tap 6000 540 960"
mkiio /tmp/iio
FAKE_TOUCH=/tmp/tt:0:1079:0:1919 FAKE_BL_ROOT=$BL FAKE_KEYS="home,power,vol/500:home,3000:power,9000:home" ./gl-bl --bot 700 --no-pilot --fakefb 1080x1920x32 --iio /tmp/iio --hi none --notice none >/dev/null 2>log || { tail log; fail "touch while dark"; }
[ "$(tv log ttaps)" = 0 ] && [ "$(tv log tdropped)" -gt 0 ] && [ "$(val log state)" = 2 ] || fail "touch while dark: $(grep "^touch=" log) state=$(val log state)"
echo "touch while dark: $(tv log tdropped) events dropped, no tap seen, still paused"
FAKE_BL_ROOT=$BL FAKE_KEYS="home,power/1000:power*200,5000:power*1200,9000:home" ./gl-bl --bot 700 --iio none --hi none --notice none >/dev/null 2>log || fail "long press"
grep -q "screen off: Power held 0.8 s" log || fail "long-press mapping not logged"
[ "$(scv log longs)/$(scv log taps)/$(scv log offs)/$(scv log ons)" = 1/1/1/1 ] || fail "long press: $(grep -o "offs=.*" log)"
echo "no volume keys: Power held 0.8 s = screen off, a short press passes through"
FAKE_ALSA_ROOT=$R FAKE_BL_ROOT=$BL FAKE_KEYS="home,power,vol/3000:power,6000:home" ./gl-bl-snd --bot 600 --iio /tmp/iio --hi none --notice none >/dev/null 2>log || { tail log; fail "sound and dark"; }
seq=$(grep -oE "sound: (on|closed)|screen: (off|on) " log | tr "\n" ",")
case "$seq" in *"sound: on,screen: off ,sound: closed,screen: on ,sound: on,"*) ;; *) fail "sound and dark: $seq";; esac   # the autopilot also toggles the sound
echo "sound: $seq"
mkiio /tmp/iio; rm -f /tmp/nt/rt /tmp/glfb
FAKE_BL_ROOT=$BL FAKE_KEYS="home,power,vol/500:home,3000:power" ./gl-bl --fakefb 1080x1920x32 --fakefb-file /tmp/glfb --iio /tmp/iio --hi none --notice /tmp/nt/rt >/dev/null 2>log &
p=$!; sleep 1.5; echo 20000 >/tmp/iio/iio:device2/in_accel_x_raw; sleep 0.5
a1=$(md5sum </tmp/glfb); sleep 0.3; a2=$(md5sum </tmp/glfb)
[ "$a1" != "$a2" ] || { kill $p; fail "real time: the marble does not move before the dark screen"; }
sleep 2
[ "$(bp)" = 4 ] || { kill $p; fail "real time: not dark"; }
b1=$(md5sum </tmp/glfb); sleep 2; b2=$(md5sum </tmp/glfb)
[ "$b1" = "$b2" ] || { kill $p; fail "real time: drawn while dark"; }
echo "Mac: build done" >/tmp/nt/rt; sleep 1.5
[ "$(bp)" = 0 ] || { kill $p; fail "a notice did not light the screen"; }
[ "$(md5sum </tmp/glfb)" != "$b2" ] || { kill $p; fail "nothing drawn after the notice"; }
kill -TERM $p; wait $p || fail "exit status $?"
lines log lit | grep -q ", paused$" || fail "real time: not paused after the notice"
cpu=$(grep -o "screen: on (a notice) after .*" log | grep -o "([0-9.]* %" | tr -d "(%")
awk "BEGIN{exit !($cpu < 3)}" || fail "$cpu % of a core while dark"
echo "real time: the marble rolls, then nothing drawn for 2 s while dark, $cpu % of one core (ASan); a notice lit it, paused"
for sig in TERM SEGV; do
    FAKE_BL_ROOT=$BL FAKE_KEYS="home,power,vol/1000:power" ./gl-bl --fakefb 1080x1920x32 --iio none --hi none --notice none >/dev/null 2>log &
    p=$!; sleep 2.5
    [ "$(bp)" = 4 ] || { kill $p; fail "SIG$sig: not dark"; }
    kill -$sig $p; st=0; wait $p || st=$?
    [ "$(bp)" = 0 ] || fail "SIG$sig while dark: bl_power $(bp)"
    echo "SIG$sig while dark: bl_power 0 again (exit $st)"
done
FAKE_BL_ROOT=$BL FAKE_KEYS="home,power,vol/1000:power" ./gl-bl --fakefb 1080x1920x32 --iio none --hi none --notice none >/dev/null 2>log &
p=$!; sleep 2.5; kill -KILL $p; wait $p || true
[ "$(bp)" = 4 ] || fail "SIGKILL: not dark"
FAKE_BL_ROOT=$BL ./gl-bl --restore-tty /dev/null 2>log || true
[ "$(bp)" = 0 ] || fail "--restore-tty did not light the screen"
echo 4 >$BLD/bl_power
FAKE_BL_ROOT=$BL ./gl-bl --bot 10 --iio none --hi none --notice none >/dev/null 2>log || fail "restart"
[ "$(bp)" = 0 ] || fail "a restart did not light the screen"
echo "SIGKILL while dark: --restore-tty lights it, and so does a restart"
[ "$(command stat -c %Y $BLD/max_brightness) $(cat $BLD/max_brightness)" = "1000000000 100" ] || fail "max_brightness touched"

echo "== frame cost (-O2, no sanitizers; this host, not the phone)"
for spec in 1080x1920x32 1080x1920x16; do
    mkiio /tmp/iio
    ./gl-bench --bot 14500 --fakefb $spec --iio /tmp/iio --hi none --notice none >/dev/null 2>log || fail "bench $spec"
    printf "%-14s %s\n" $spec "$(grep "^frames:" log)"
done
mkiio /tmp/iio
./gl-bench --bot 14500 --fakefb 1080x1920x32 --fakefb-file /tmp/fb.bin --iio /tmp/iio --hi none --notice none >/dev/null 2>log || fail "bench pwrite"
printf "%-14s %s\n" "pwrite, 32" "$(grep "^frames:" log)"
echo "== CPU share in real time (-O2): 20 s of the bot playing at 60 frames a second, pwrite to a file"
mkiio /tmp/iio
./gl-bench --bot 1200 --bot-rt --fakefb 1080x1920x32 --fakefb-file /tmp/fb.bin --iio /tmp/iio --hi none --notice none >/dev/null 2>log || fail "bench rt"
grep "^frames:\|^cpu:" log
mkiio /tmp/iio
./gl-bench --bot 1200 --bot-rt --fps 30 --fakefb 1080x1920x32 --fakefb-file /tmp/fb.bin --iio /tmp/iio --hi none --notice none >/dev/null 2>log || fail "bench rt 30"
echo "--fps 30: $(grep "^cpu:" log)"
echo "== idle in real time (-O2): 10 s on the title, nothing moves"
timeout --preserve-status -s TERM 10 ./gl-bench --fakefb 1080x1920x32 --fakefb-file /tmp/fb.bin --iio none --hi none --notice none >/dev/null 2>log || fail "bench idle"
grep "^frames:\|^cpu:" log
ls -l gl-phone | sed "s/^/unstripped phone build: /"

echo "ALL TESTS PASSED"
cp /tmp/snd60.raw /out/snd60.raw
tar -C /out -cf - . >&3
' | tar -C "$res" -xf -
[ -f "$res/snd60.raw" ] || { echo "test.sh: no results (a test failed)" >&2; exit 1; }
cat "$res/snd60.raw" >"$raw"
mkdir -p "$frames"
cp "$res"/frames/*.png "$frames"/
echo "frames: $frames"
ls "$frames"
