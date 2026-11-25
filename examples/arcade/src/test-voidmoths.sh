#!/bin/sh
# Headless tests of voidmoths in the kernel toolchain image, under ASan and
# UBSan (native gcc there is aarch64 too):
#  - the autopilot (which also holds Home to quit, lets go early, moves during
#    a hold and toggles the sound on the title) at several screen sizes, in
#    text mode and in pixel mode on a fake framebuffer (memory described like
#    /dev/fb0: 1080x1920 at 32 bpp XRGB and XBGR and 16 bpp RGB565, and odd
#    sizes); frames are dumped and must decode alike in every format;
#  - the console mode: KD_GRAPHICS while playing, KD_TEXT again on a normal
#    end, SIGTERM, SIGINT and fatal signals (SEGV, ABRT, QUIT, USR1, ALRM);
#    the real start-up path without /dev/fb0 falls back to text mode;
#  - the notice queue (order, queue limit, clean-up of odd text, the age
#    rule at start) and the notice helper script;
#  - the sound code against no device, and against a fake ALSA speaker card
#    (src/fake_alsa.c) in every failure mode it knows, dive swoops included;
#  - screen off (src/backlight.h) on a fake sysfs ($FAKE_BL_ROOT) with
#    scripted buttons ($FAKE_KEYS): Power darkens, Home and Power light it
#    again, the game frozen while dark (an unattended ship dies, a dark one
#    does not) and paused after until a press; touch ignored while dark;
#    Power held 0.8 s without volume keys (a short press still fires); the
#    speaker closed while dark; in real time: nothing drawn, near-idle CPU,
#    a notice lights it, SIGTERM and SIGSEGV while dark light it again, and
#    so do --restore-tty and a restart after a SIGKILL;
#  - the phone build (static, -Werror), to check it still compiles clean;
#  - the frame cost, in a build without sanitizers (-O2, as on the phone).
# Usage: src/test-voidmoths.sh [OUT.raw [FRAMES_DIR]]
#   OUT.raw receives 60 s of the game's sound (S16_LE, mono, 48 kHz) from the
#   fake card; FRAMES_DIR (default /private/tmp/claude-501/arcade-frames/voidmoths)
#   receives the dumped frames as PNG.
# The sources go in on stdin (see build.sh for why); the results come back as
# a tar stream on stdout.
set -eu
here=$(cd "$(dirname "$0")" && pwd)
raw=${1:-/dev/null}
frames=${2:-/private/tmp/claude-501/arcade-frames/voidmoths}
res=$(mktemp -d "${TMPDIR:-/tmp}/vmtest.XXXXXX")
trap 'rm -rf "$res"' EXIT
COPYFILE_DISABLE=1 tar -C "$here" -cf - voidmoths.c touch.h backlight.h fake_alsa.c ppm2png.py touchrec.py -C "$here/../rootfs/usr/bin" notice |
docker run -i --rm localhost/orchard-hardware-kernel-toolchain:bookworm sh -c '
set -eu
exec 3>&1 1>&2
mkdir -p /w /out/frames && cd /w && tar xf - 2>/dev/null
CF="-std=gnu11 -Wall -Wextra -Wshadow -Werror -O1 -g -fsanitize=address,undefined -fno-sanitize-recover=all -fno-omit-frame-pointer"
gcc $CF -o vm-asan voidmoths.c -lm
gcc $CF -DVM_FAKE_ALSA -o vm-fake voidmoths.c -lm
gcc $CF -DTOUCH_TEST -o vm-bl voidmoths.c -lm
gcc $CF -DVM_FAKE_ALSA -DBL_TEST -o vm-bl-snd voidmoths.c -lm
gcc -std=gnu11 -O2 -DBL_TEST -o vm-bl-fast voidmoths.c -lm
gcc -std=gnu11 -Wall -Wextra -Wshadow -Werror -O2 -o vm-bench voidmoths.c -lm
aarch64-linux-gnu-gcc -std=gnu11 -Wall -Wextra -Wshadow -Werror -static -O2 -o vm-phone voidmoths.c -lm
if nm vm-phone | grep -q fake_; then echo "FAIL: fake ALSA code in the phone build"; exit 1; fi
if grep -q "FAKE_BL_ROOT\|FAKE_KEYS\|FAKE_TOUCH" vm-phone; then echo "FAIL: test hooks in the phone build"; exit 1; fi
fail() { echo "FAIL: $*"; exit 1; }
stat() { grep -E "^(ticks|quits)=" "$1" | tr "\n" " "; echo; }
kd_ok() {   # $1 log: KD_GRAPHICS once, and the last word is KD_TEXT
    [ "$(grep -c "^kd: KD_GRAPHICS" "$1")" = 1 ] || fail "$2: KD_GRAPHICS not set once"
    [ "$(grep "^kd:" "$1" | tail -1)" = "kd: KD_TEXT" ] || fail "$2: console not back in KD_TEXT"
}

echo "== autopilot, text mode, no sound device"
for sz in 45x40 30x24 120x100 15x10; do
    ./vm-asan --bot 60000 --size $sz >/dev/null 2>log || fail "bot $sz"
    printf "%-8s " $sz; stat log
done
./vm-asan --bot 200000 >frame.txt 2>log || fail "bot 200000"
printf "%-8s " 200k; stat log
grep -q "quits=[1-9]" log || fail "no hold-to-quit in 200k ticks"
grep -q "no .Speaker. card" log || fail "no silent fallback logged"
grep -q "swoops=[1-9]" log || fail "no dive swoops"
grep -q "^kd:" log && fail "text mode touched the console mode"
./vm-asan --bot 3000 --mute >/dev/null 2>log || fail "bot --mute"
grep -q "sound off (--mute)" log || fail "--mute"
# --from-picker: the title starts on a tap, a 2 s Home hold on the title exits 0 (back to arcade)
./vm-asan --bot 200000 --from-picker >/dev/null 2>log || { tail -5 log; fail "bot --from-picker exit status"; }
grep -q "title: Home held, back to the arcade" log || fail "from-picker: no exit on a title hold"
grep -qE "games=[2-9]" log || fail "from-picker: the bot did not play two games first"

echo "== autopilot, pixel mode on a fake framebuffer"
DUMPS=30,110,745,1040,1462,1468,1560,2030
mkdir -p /tmp/nt
for spec in 1080x1920x32 1080x1920x32:bgr 1080x1920x16; do
    d=/tmp/f-$(echo $spec | tr ":" "-"); mkdir -p $d; rm -f /tmp/nt/notice
    ./vm-asan --bot 40000 --fakefb $spec --notice /tmp/nt/notice --dump $d --dump-at $DUMPS >/dev/null 2>log ||
        { tail -20 log; fail "fakefb $spec"; }
    printf "%-16s " $spec; stat log
    grep -m1 "^voidmoths.*graphics:" log | cut -c19-
    grep -q "line padding intact" log || fail "$spec: wrote past line_length"
    grep -q "quits=[1-9]" log || fail "$spec: no hold-to-quit"
    kd_ok log $spec
    [ "$(ls $d | wc -l)" = 8 ] || fail "$spec: frames missing"
    grep "notice:" log | cut -c19- >notices-$spec
done
cp log fakefb.log
for f in $(ls /tmp/f-1080x1920x32); do
    cmp -s /tmp/f-1080x1920x32/$f /tmp/f-1080x1920x32-bgr/$f || fail "XBGR frame $f differs from XRGB"
    dmax=$(python3 -I ppm2png.py diff /tmp/f-1080x1920x32/$f /tmp/f-1080x1920x16/$f)
    [ "$dmax" -le 7 ] || fail "RGB565 frame $f differs by $dmax"
done
echo "frames: XRGB = XBGR exactly; RGB565 within $dmax (5/6-bit rounding)"
for f in /tmp/f-1080x1920x32/*.ppm; do
    b=$(basename $f .ppm)
    python3 -I ppm2png.py png $f /out/frames/$b.png
done
python3 -I ppm2png.py png /tmp/f-1080x1920x16/frame-000745.ppm /out/frames/frame-000745-rgb565.png

for spec in 720x1280x32 640x480x16 1242x2208x32 200x200x32; do
    ./vm-asan --bot 20000 --fakefb $spec >/dev/null 2>log || { tail -20 log; fail "fakefb $spec"; }
    printf "%-16s " $spec; grep -m1 "graphics:" log | sed "s/.*graphics: //"
    grep -q "line padding intact" log || fail "$spec: wrote past line_length"
    kd_ok log $spec
done
./vm-asan --bot 300 --fakefb 1080x1920x24 >/dev/null 2>log || fail "fakefb 24 bpp"
grep -q "24 bits per pixel is not supported; text mode" log || fail "24 bpp: no text fallback"
grep -q "^kd:" log && fail "24 bpp: console mode touched"
echo "1080x1920x24     refused, text mode"
./vm-asan --bot 300 --fakefb 100x100x16 >/dev/null 2>log || fail "fakefb 100x100"
grep -q "too small for the field; text mode" log || fail "100x100: no text fallback"
echo "100x100x16       refused (too small), text mode"

echo "== notice queue"
cat notices-1080x1920x16
want="'"'"'first: build started'"'"'
queued '"'"'second'"'"' (1 waiting)
queued '"'"'third'"'"' (2 waiting)
queued '"'"'fourth'"'"' (3 waiting)
queue full, dropped '"'"'second'"'"'
queued '"'"'fifth'"'"' (3 waiting)
'"'"'third'"'"'
'"'"'fourth'"'"'
'"'"'fifth'"'"'
'"'"'caf? - step 7 ok done[31m and this line goes on well past...'"'"'
'"'"'caf? - step 7 ok done[31m and this line goes on well past...'"'"'
'"'"'short again'"'"'"
[ "$(sed "s/^ *notice: //" notices-1080x1920x16)" = "$want" ] || fail "notice sequence"
grep -q "notices=7$" fakefb.log || fail "notice count"
for spec in 1080x1920x32 1080x1920x32:bgr; do cmp -s notices-1080x1920x16 notices-$spec || fail "notices $spec"; done
rm -f /tmp/nt/notice
./vm-asan --bot 6000 --notice /tmp/nt/notice >/dev/null 2>log || fail "text-mode notices"
grep -q "notices=7$" log || fail "text-mode notice count"
echo "  hello before the start" >/tmp/nt/fresh
./vm-asan --bot 30 --notice /tmp/nt/fresh >/dev/null 2>log || fail "fresh notice"
grep -q "notice: .hello before the start." log || fail "a fresh notice at start was not shown"
touch -d @1000000000 /tmp/nt/fresh
./vm-asan --bot 30 --notice /tmp/nt/fresh >/dev/null 2>log || fail "old notice"
grep -q "notice:" log && fail "an old notice at start was shown"
echo "at start: a fresh notice is shown, an old one is not"
mkfifo /tmp/nt/fifo
./vm-asan --bot 60 --notice /tmp/nt/fifo >/dev/null 2>log || fail "notice fifo"
echo "a FIFO as the notice file: ignored, no hang"
NOTICE_FILE=/tmp/nt/h sh ./notice "it'"'"'s \"done\" \$HOME \`id\`;  two  spaces" || fail "notice helper"
[ "$(cat /tmp/nt/h)" = "it'"'"'s \"done\" \$HOME \`id\`;  two  spaces" ] || fail "notice helper text"
[ -z "$(ls /tmp/nt | grep tmp)" ] || fail "notice helper left a temporary file"
NOTICE_FILE=/tmp/nt/h sh ./notice 2>/dev/null && fail "notice without text"
echo "notice helper: text kept as is, written by rename, no leftovers"

echo "== console mode on signals"
for sig in TERM INT SEGV ABRT QUIT USR1 ALRM; do
    ./vm-asan --bot 1000000000 --fakefb 1080x1920x32 >/dev/null 2>log &
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

echo "== the real start-up path (no /dev/fb0, no buttons, a tty that is /dev/null)"
for opt in "" "--text" "--fb /w/voidmoths.c"; do
    st=0; timeout --preserve-status -s TERM 3 ./vm-asan --tty /dev/null --hi none --mute --notice none $opt 2>log >/dev/null || st=$?
    printf "%-22s exit %s: %s\n" "${opt:-(default)}" $st "$(grep -m1 "graphics:" log | sed "s/.*graphics: //")"
    [ $st = 0 ] || fail "real path $opt: exit $st"
    grep -q "text mode" log || fail "real path $opt: no text fallback"
done
st=0; ./vm-asan --restore-tty /dev/null 2>/dev/null || st=$?
[ $st = 1 ] || fail "--restore-tty on a non-console: exit $st"
echo "--restore-tty /dev/null: refused cleanly (not a console)"

R=/tmp/fakeroot
mkdir -p $R/proc/asound $R/dev/snd
printf " 0 [Headset        ]: cs42l71 - iPhone Headset\n                      iPhone Headset\n 1 [Speaker        ]: simple-card - iPhone Speaker\n                      iPhone Speaker\n" >$R/proc/asound/cards
touch $R/dev/snd/controlC0 $R/dev/snd/pcmC0D0p $R/dev/snd/controlC1 $R/dev/snd/pcmC1D0p

echo "== fake speaker card (card 1; card 0 is a headset that must stay closed)"
FAKE_ALSA_ROOT=$R FAKE_ALSA_DUMP=/tmp/snd.raw ./vm-fake --bot 30000 --fakefb 1080x1920x32 >/dev/null 2>log || { tail -20 log; fail "fake ok"; }
grep -E "sound:|fake: (Speaker|HW_PARAMS|frames)" log | sort | uniq -c | sort -rn | head -12
stat log
grep -q "Speaker Volume := 164 (-20.0 dB)" log || fail "volume not -20 dB"
grep -q "sound=2 " log || fail "sound not on at the end"
grep -q "swoops=[1-9]" log || fail "no dive swoops with sound on"
grep -q "FORBIDDEN" log && fail "opened another device"
pk=$(sed -n "s/^fake: frames=.* peak=\([0-9]*\) .*/\1/p" log)
[ -n "$pk" ] && [ "$pk" -le 16384 ] || fail "mix above -6 dBFS (peak $pk)"
head -c $((48000 * 2 * 60)) /tmp/snd.raw >/tmp/snd60.raw

for m in hwpick xrun; do
    FAKE_ALSA_ROOT=$R FAKE_ALSA_MODE=$m ./vm-fake --bot 6000 >/dev/null 2>log || { tail -20 log; fail "fake $m"; }
    printf "%-10s " $m; grep -E "HW_PARAMS|sound: on" log | tail -2 | tr "\n" " "; stat log
    grep -q "sound=2 " log || fail "$m: sound not on"
done
grep -q "underruns=[1-9]" log || fail "xrun: no underruns seen"

for m in hwfail notlv loud readback wrongcard dead; do
    FAKE_ALSA_ROOT=$R FAKE_ALSA_MODE=$m ./vm-fake --bot 3000 >/dev/null 2>log || { tail -20 log; fail "fake $m"; }
    printf "%-10s " $m; grep -m1 "running silent" log | cut -c19-
    grep -q "sound=4 " log || fail "$m: not silent"
done

for v in -3 -40; do
    FAKE_ALSA_ROOT=$R ./vm-fake --bot 300 --vol $v >/dev/null 2>log || fail "vol $v"
    printf "%-10s " "--vol $v"; grep -m1 "Speaker Volume :=" log
done

mkdir -p /tmp/nocard/proc/asound
printf " 0 [Headset        ]: cs42l71 - iPhone Headset\n                      iPhone Headset\n" >/tmp/nocard/proc/asound/cards
for r in /tmp/nocard /tmp/empty; do
    FAKE_ALSA_ROOT=$r ./vm-fake --bot 1200 >/dev/null 2>log || fail "no card $r"
    printf "%-10s " "${r#/tmp/}"; grep -m1 "running silent" log | cut -c19-
    grep -q "sound=3 " log || fail "$r: not silent"
done

echo "== screen off: Power darkens, Home or Power lights it again (fake sysfs)"
val() { grep -o "\b$2=[-0-9.]*" "$1" | tail -1 | cut -d= -f2; }
scv() { grep -o "screen: offs=.*" "$1" | grep -o "\b$2=[-0-9.]*" | cut -d= -f2; }
BL=/tmp/bl; BLD=$BL/sys/class/backlight/20e200080.backlight
mkdir -p $BL/run $BLD; echo 0 >$BLD/bl_power; echo 37 >$BLD/brightness; echo 100 >$BLD/max_brightness
touch -d @1000000000 $BLD/max_brightness
bp() { cat $BLD/bl_power; }
lines() { grep -o "screen $2: .*" "$1"; }
FAKE_BL_ROOT=$BL FAKE_KEYS="home,power,vol/1000:home" ./vm-bl --bot 1800 --no-pilot >/dev/null 2>log || fail "control"
ctl=$(val log deaths)
[ "$ctl" -ge 1 ] || fail "control: an unattended ship should die within 60 s"
FAKE_BL_ROOT=$BL FAKE_KEYS="home,power,vol/1000:home,5000:power,40000:home" ./vm-bl --bot 1800 --no-pilot >/dev/null 2>log || { tail log; fail "dark"; }
grep -q "screen: off (Power): 20e200080.backlight bl_power=4" log || fail "Power did not write bl_power 4"
grep -q "screen: on (Home)" log || fail "Home did not light the screen"
d0=$(lines log dark); l1=$(lines log lit)
echo "$l1" | grep -q ", paused$" || fail "not paused after the dark screen: $l1"
[ "$(echo "$l1" | grep -o "deaths.*" | sed "s/, paused$//")" = "$(echo "$d0" | grep -o "deaths.*")" ] || fail "the game ran while dark: $d0 / $l1"
[ "$(val log deaths)" = 0 ] && grep -q "paused=1" log || fail "deaths while dark or paused, or not paused"
[ "$(scv log offs)/$(scv log ons)/$(scv log eaten)" = 1/1/1 ] || fail "offs/ons/eaten: $(grep -o "offs=.*" log)"
echo "VOIDMOTHS unattended: $ctl deaths in 60 s; dark 5-40 s: 0 deaths, still paused. $l1"
FAKE_BL_ROOT=$BL FAKE_KEYS="home,power,vol/1000:home,5000:power,15000:power,17000:home" ./vm-bl --bot 1500 --no-pilot >/dev/null 2>log || fail "Power wakes"
grep -q "screen: on (Power)" log || fail "Power did not light the screen"
grep -q "play on after the dark screen" log && grep -q "paused=0" log || fail "a Home press did not play on"
echo "Power lights it as well; a Home press plays on"
python3 -I touchrec.py /tmp/tt 0:1079:0:1919 1080x1920 "tap 8000 540 960; tap 10000 900 1800"
FAKE_TOUCH=/tmp/tt:0:1079:0:1919 FAKE_BL_ROOT=$BL FAKE_KEYS="home,power,vol/1000:home,5000:power,15000:home" ./vm-bl --bot 600 --no-pilot --fakefb 1080x1920x32 >/dev/null 2>log || { tail log; fail "touch while dark"; }
[ "$(val log ttaps)" = 0 ] && [ "$(val log tdropped)" -gt 0 ] && grep -q "paused=1" log || fail "touch while dark: $(grep "^touch=" log)"
echo "touch while dark: $(val log tdropped) events dropped, no tap seen, still paused"
FAKE_BL_ROOT=$BL FAKE_KEYS="home,power/1000:power*200,5000:power*1200,9000:home" ./vm-bl --bot 400 --no-pilot >/dev/null 2>log || fail "long press"
grep -q "screen off: Power held 0.8 s" log || fail "long-press mapping not logged"
grep -q "games=1 " log || fail "a short Power press no longer starts"
[ "$(scv log longs)/$(scv log taps)/$(scv log offs)/$(scv log ons)" = 1/1/1/1 ] || fail "long press: $(grep -o "offs=.*" log)"
echo "no volume keys: short Power = fire/start, Power held 0.8 s = screen off"
FAKE_BL_ROOT=$BL FAKE_KEYS="home/1000:power" ./vm-bl --bot 100 --no-pilot >/dev/null 2>log || fail "no Power"
grep -q "screen off: none (no Power key)" log && [ "$(scv log offs)" = 0 ] || fail "without a Power key: $(grep "screen off" log)"
FAKE_ALSA_ROOT=$R FAKE_BL_ROOT=$BL FAKE_KEYS="home,power,vol/3000:power,6000:home" ./vm-bl-snd --bot 400 >/dev/null 2>log || { tail log; fail "sound and dark"; }
seq=$(grep -oE "sound: (on|closed)|screen: (off|on) " log | tr "\n" ",")
[ "$seq" = "sound: on,screen: off ,sound: closed,screen: on ,sound: on,sound: closed," ] || fail "sound and dark: $seq"
echo "sound: $seq"
touch /tmp/vt; rm -f /tmp/nt/rt
FAKE_BL_ROOT=$BL FAKE_KEYS="home,power,vol/1500:power" ./vm-bl --tty /tmp/vt --hi none --mute --notice /tmp/nt/rt --text >/dev/null 2>log &
p=$!; sleep 3
[ "$(bp)" = 4 ] || { kill $p; fail "real time: not dark"; }
a=$(wc -c </tmp/vt); sleep 2; b=$(wc -c </tmp/vt)
[ "$a" = "$b" ] || { kill $p; fail "real time: drawn while dark ($a -> $b bytes)"; }
echo "Mac: build done" >/tmp/nt/rt; sleep 1.5
[ "$(bp)" = 0 ] || { kill $p; fail "a notice did not light the screen"; }
[ "$(wc -c </tmp/vt)" -gt "$b" ] || { kill $p; fail "nothing drawn after the notice"; }
kill -TERM $p; wait $p || fail "exit status $?"
cpu=$(grep -o "screen: on (a notice) after .*" log | grep -o "([0-9.]* %" | tr -d "(%")
awk "BEGIN{exit !($cpu < 3)}" || fail "$cpu % of a core while dark"
echo "real time: nothing drawn for 2 s while dark, $cpu % of one core (ASan); a notice lit it"
for sig in TERM SEGV; do
    FAKE_BL_ROOT=$BL FAKE_KEYS="home,power,vol/1000:power" ./vm-bl-fast --tty /tmp/vt --hi none --mute --notice none --text >/dev/null 2>log &
    p=$!; sleep 2.5
    [ "$(bp)" = 4 ] || { kill $p; fail "SIG$sig: not dark"; }
    kill -$sig $p; st=0; wait $p || st=$?
    [ "$(bp)" = 0 ] || fail "SIG$sig while dark: bl_power $(bp)"
    echo "SIG$sig while dark: bl_power 0 again (exit $st)"
done
FAKE_BL_ROOT=$BL FAKE_KEYS="home,power,vol/1000:power" ./vm-bl-fast --tty /tmp/vt --hi none --mute --notice none --text >/dev/null 2>log &
p=$!; sleep 2.5; kill -KILL $p; wait $p || true
[ "$(bp)" = 4 ] || fail "SIGKILL: not dark"
FAKE_BL_ROOT=$BL ./vm-bl --restore-tty /tmp/vt 2>log || true
[ "$(bp)" = 0 ] || fail "--restore-tty did not light the screen"
echo 4 >$BLD/bl_power
FAKE_BL_ROOT=$BL ./vm-bl --bot 10 >/dev/null 2>log || fail "restart"
[ "$(bp)" = 0 ] || fail "a restart did not light the screen"
echo "SIGKILL while dark: --restore-tty lights it, and so does a restart"
[ "$(command stat -c %Y $BLD/max_brightness) $(cat $BLD/max_brightness)" = "1000000000 100" ] || fail "max_brightness touched"

echo "== frame cost (-O2, no sanitizers; this host, not the phone)"
for spec in 1080x1920x32 1080x1920x16; do
    ./vm-bench --bot 20000 --fakefb $spec >/dev/null 2>log || fail "bench $spec"
    printf "%-14s " $spec; grep "^frames:" log
done

echo "== last frame of the 200k-tick text-mode run"
cat frame.txt >&2
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
