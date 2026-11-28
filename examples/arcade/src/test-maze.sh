#!/bin/sh
# Headless tests of HEDGEWICK (src/hedgewick.c) and the ARCADE picker
# (src/arcade.c, three games: VOIDMOTHS, HEDGEWICK, GLINT) in the kernel
# toolchain image, under ASan and UBSan (native
# gcc there is aarch64 too). Nothing here touches a phone or a real device:
#  - the phone builds (static, -Werror) compile clean and carry no test code;
#  - the built-in gardens pass their checks (closed, connected, symmetric,
#    no dead ends);
#  - the autopilot steers Wick through the mazes for many ticks into a fake
#    framebuffer (a plain file, $FAKE_FB) at several sizes, at 16, 24 and
#    32 bpp, BGR order, a padded stride, smaller than the canvas, landscape,
#    through the mapping (the default) and through pwrite() (--fb-write); every frame is checked by the game's own
#    invariants, a few are decoded and checked for the right colours;
#  - the console mode: KD_GRAPHICS while drawing and KD_TEXT again after a
#    normal exit, SIGTERM, SIGINT, SIGHUP, SIGQUIT, SIGSEGV, SIGABRT and the
#    Home hold back to the arcade ($FAKE_KD logs the changes);
#  - sound against src/fake_alsa.c (the speaker card next to a headset card
#    that must never be opened): -20 dB, never above -6 dB, every failure
#    mode silent; the picker opens the card only for a sound and closes it
#    before a game starts;
#  - the picker driven by scripted button presses, with fake child games
#    (shell scripts) and with the real HEDGEWICK as its child; the exact
#    argv each of the three games gets; the real GLINT binary (built from
#    src/glint.c) accepts the argv the picker gives it. A fake IIO tree with
#    an "apple-aop-accel" device (--iio-root) makes GLINT's card live, as on
#    the N66; test-ipad.sh covers the boards without one;
#  - screen off (src/backlight.h) on a fake sysfs ($FAKE_BL_ROOT) with
#    scripted buttons ($FAKE_KEYS): Power darkens (bl_power 4), Home and
#    Power light it again (0), the press that lights it does nothing else;
#    HEDGEWICK frozen while dark (an unattended Wick dies, a dark one does
#    not) and paused after, a Home tap plays on; the touchscreen ignored while
#    dark; the speaker closed while dark and opened again; Power held 0.8 s
#    on a board without volume keys, a short press keeps its meaning; in
#    real time: nothing drawn while dark, near-idle CPU, a notice lights the
#    screen, SIGTERM, SIGSEGV and SIGABRT while dark light it again, after a
#    SIGKILL `arcade --restore-tty` and a restarted program do; a device with
#    brightness only (kept value written back); the picker lights a screen a
#    game left dark; max_brightness never touched;
#  - frame cost, measured in an -O2 build without sanitizers.
# Frame dumps (PNG) go to /private/tmp/claude-501/arcade-frames/maze/.
# Usage: src/test-maze.sh [FRAMES_DIR]
set -eu
set -o pipefail     # a FAIL inside the container fails the script
here=$(cd "$(dirname "$0")" && pwd)
frames=${1:-/private/tmp/claude-501/arcade-frames/maze}
mkdir -p "$frames"
rm -f "$frames"/*.png "$frames"/*.ppm
# Sources go in on stdin (OrbStack's bind mount can serve a stale copy of a
# file rewritten a moment earlier); frames come back as a tar on fd 3.
COPYFILE_DISABLE=1 tar -C "$here" -cf - hedgewick.c arcade.c arcade_common.h hedgewick_art.h touch.h backlight.h \
    fake_alsa.c glint.c touchrec.py |
docker run -i --rm localhost/orchard-hardware-kernel-toolchain:bookworm sh -c '
set -eu
exec 3>&1 1>&2
mkdir -p /w /out && cd /w && tar xf - 2>/dev/null
fail() { echo "FAIL: $*"; exit 1; }
CF="-std=gnu11 -Wall -Wextra -Wshadow -Werror -O1 -g -fsanitize=address,undefined -fno-sanitize-recover=all -fno-omit-frame-pointer"
PF="-std=gnu11 -Wall -Wextra -Wshadow -Werror -static -O2"

echo "== builds"
gcc $CF -DMZ_TEST -o hw hedgewick.c -lm
gcc $CF -DMZ_TEST -o ar arcade.c -lm
gcc $CF -DMZ_TEST -DMZ_FAKE_ALSA -o hw-snd hedgewick.c -lm
gcc $CF -DMZ_TEST -DMZ_FAKE_ALSA -o ar-snd arcade.c -lm
gcc -std=gnu11 -O2 -DMZ_TEST -o hw-fast hedgewick.c -lm
gcc -std=gnu11 -O2 -DMZ_TEST -o ar-fast arcade.c -lm
aarch64-linux-gnu-gcc $PF -o hw-phone hedgewick.c -lm
aarch64-linux-gnu-gcc $PF -o ar-phone arcade.c -lm
aarch64-linux-gnu-gcc $PF -o glint glint.c -lm
mkdir -p "/tmp/iio/iio:device2"
echo apple-aop-accel >"/tmp/iio/iio:device2/name"
IIO="--iio-root /tmp/iio"
for b in hw-phone ar-phone; do
    if nm $b | grep -q fake_; then fail "fake ALSA code in $b"; fi
    if grep -q FAKE_FB $b; then fail "fake framebuffer code in $b"; fi
    if grep -q FAKE_TOUCH $b; then fail "fake touchscreen code in $b"; fi
    if grep -q "FAKE_BL_ROOT\|FAKE_KEYS" $b; then fail "fake backlight or key code in $b"; fi
done
grep -q -- --from-picker hw-phone || fail "hedgewick does not carry --from-picker"
echo "phone builds clean, no test code in them"

cat >/w/ppm.py <<"EOF"
import sys, zlib, struct
def readppm(p):
    d = open(p, "rb").read(); parts = []; i = 0
    while len(parts) < 4:
        while d[i:i+1].isspace(): i += 1
        j = i
        while not d[j:j+1].isspace(): j += 1
        parts.append(d[i:j]); i = j
    i += 1; w, h = int(parts[1]), int(parts[2])
    return w, h, d[i:i + w * h * 3]
def png(path, w, h, rgb):
    raw = b"".join(b"\0" + rgb[y*w*3:(y+1)*w*3] for y in range(h))
    ch = lambda t, b: struct.pack(">I", len(b)) + t + b + struct.pack(">I", zlib.crc32(t + b) & 0xffffffff)
    open(path, "wb").write(b"\x89PNG\r\n\x1a\n" + ch(b"IHDR", struct.pack(">IIBBBBB", w, h, 8, 2, 0, 0, 0)) + ch(b"IDAT", zlib.compress(raw, 6)) + ch(b"IEND", b""))
def near(px, rgb, tol):
    return all(abs(px[k] - rgb[k]) <= tol for k in range(3))
if sys.argv[1] == "png":
    w, h, rgb = readppm(sys.argv[2]); png(sys.argv[3], w, h, rgb)
elif sys.argv[1] == "check":
    # check FILE NAME=RRGGBB:MINPERMILLE ...  (colour shares, tolerance for 565)
    w, h, rgb = readppm(sys.argv[2]); n = w * h; step = 7
    for spec in sys.argv[3:]:
        name, rest = spec.split("="); col, minpm = rest.split(":")
        c = (int(col[0:2], 16), int(col[2:4], 16), int(col[4:6], 16))
        hits = sum(1 for i in range(0, n, step) if near(rgb[i*3:i*3+3], c, 10))
        pm = hits * 1000 * step // n
        print("  %-8s %4d permille" % (name, pm))
        if pm < int(minpm): sys.exit("colour %s too rare: %d < %s" % (name, pm, minpm))
EOF

echo "== gardens and autopilot, no framebuffer"
./hw --bot 100000 --seed 7 >/dev/null 2>log || { tail -20 log; fail "bot 100000"; }
grep -E "^(ticks|quits)=" log
grep -q "bad=0" log || fail "invariants"
grep -q "clears=[1-9]" log || fail "the autopilot never cleared a garden in 100000 ticks"
grep -q "quits=[1-9]" log || fail "no Home hold back to the title"
grep -q "pauses=[1-9]" log || fail "no pause"

echo "== autopilot into fake framebuffers"
k=0
for spec in 1080x1920x32 1080x1920x16 1080x1920x32:bgr 1080x1920x32:4352 750x1334x32 720x1280x24 540x960x16 320x480x16 160x200x32 1920x1080x32; do
    rm -f /tmp/fb
    k=$((k + 1)); how=; want=mmap
    [ $((k % 2)) = 0 ] && { how=--fb-write; want=pwrite; }
    FAKE_FB=/tmp/fb:$spec ./hw --bot 20000 --seed 3 $how --shot play:/tmp/s.ppm >/dev/null 2>log || { tail -20 log; fail "bot $spec"; }
    grep -q "fb: .*, $want;" log || { grep "fb:" log; fail "$spec: not drawn through $want"; }
    printf "%-18s %-6s %s | %s\n" "$spec" $want "$(grep -o "canvas.*" log | head -1)" "$(grep -o "ticks=[0-9]* .*score=[0-9]*" log)"
    grep -q "bad=0" log || fail "$spec: invariants"
    case $spec in 160x200*) ;; *) python3 /w/ppm.py check /tmp/s.ppm hedge=1d5a2b:50 path=0a1020:50 ember=ff8a1f:1 >/dev/null || fail "$spec: colours";; esac
done
python3 /w/ppm.py check /tmp/s.ppm hedge=1d5a2b:0 >/dev/null
rm -f /tmp/fb
FAKE_FB=/tmp/fb:1080x1920x32 ./hw --bot 8000 --fb-write --shot play:/tmp/m.ppm >/dev/null 2>log || fail "pwrite path"
python3 /w/ppm.py check /tmp/m.ppm hedge=1d5a2b:150 path=0a1020:150 || fail "pwrite path: colours"
cp /tmp/fb /tmp/fb-write
rm -f /tmp/fb
FAKE_FB=/tmp/fb:1080x1920x32 ./hw --bot 8000 --shot play:/tmp/m2.ppm >/dev/null 2>log || fail "mmap path"
cmp -s /tmp/fb /tmp/fb-write || fail "mmap and pwrite leave different pictures"
echo "mmap (default) and pwrite (--fb-write) paths: identical framebuffer contents"

echo "== frame dumps"
rm -f /tmp/fb
FAKE_FB=/tmp/fb:1080x1920x32 ./hw --bot 9000 --seed 5 --notice /tmp/notice --bot-notice "Mac: new kernel built, reboot when you like" \
    --shot title:/out/hedgewick-1-title.ppm --shot play:/out/hedgewick-2-knot-garden.ppm \
    --shot dazzle:/out/hedgewick-3-dazzle.ppm --shot toast:/out/hedgewick-notice.ppm \
    --shot pause:/out/hedgewick-pause.ppm --shot hold:/out/hedgewick-hold.ppm --shot dying:/out/hedgewick-dying.ppm >/dev/null 2>log || fail "shots"
grep "shot " log | cut -c20-
grep -q "notice: Mac: new kernel" log || fail "the notice was not shown"
rm -f /tmp/fb
FAKE_FB=/tmp/fb:1080x1920x32 ./hw --bot 3000 --level 2 --shot play:/out/hedgewick-4-yew-walk.ppm >/dev/null 2>log || fail "level 2"
rm -f /tmp/fb
FAKE_FB=/tmp/fb:1080x1920x16 ./hw --bot 3000 --level 3 --shot play:/out/hedgewick-5-moon-cloister-rgb565.ppm >/dev/null 2>log || fail "level 3"
rm -f /tmp/fb
FAKE_FB=/tmp/fb:1080x1920x32 ./hw --bot 30000 --seed 11 --shot over:/out/hedgewick-over.ppm >/dev/null 2>log || fail "over"

echo "== console mode (KD_GRAPHICS while drawing, KD_TEXT on every way out)"
touch /tmp/tty     # stands in for /dev/tty1 (escape codes land here)
kdcheck() {   # LOG PROG what
    first=$(head -1 "$1"); last=$(tail -1 "$1")
    [ "$first" = "$2 graphics" ] || fail "$3: first mode change is \"$first\""
    [ "$last" = "$2 text" ] || fail "$3: console left as \"$last\""
}
for sig in TERM INT HUP QUIT; do
    rm -f /tmp/kd /tmp/fb
    FAKE_FB=/tmp/fb:1080x1920x32 FAKE_KD=/tmp/kd ./hw --tty /tmp/tty --hi /tmp/hw.hi >/dev/null 2>log &
    p=$!; sleep 3; kill -$sig $p; wait $p || fail "SIG$sig: exit status $?"
    kdcheck /tmp/kd hedgewick "SIG$sig"
    printf "%-8s %s | %s\n" "SIG$sig" "$(tr "\n" " " </tmp/kd)" "$(grep -o "cpu:.*" log)"
done
for sig in SEGV ABRT; do
    rm -f /tmp/kd /tmp/fb
    FAKE_FB=/tmp/fb:1080x1920x32 FAKE_KD=/tmp/kd ./hw-fast --tty /tmp/tty --hi /tmp/hw.hi >/dev/null 2>log &
    p=$!; sleep 2; kill -$sig $p; st=0; wait $p || st=$?
    [ $st -gt 128 ] || fail "SIG$sig: status $st"
    kdcheck /tmp/kd hedgewick "SIG$sig"
    printf "%-8s %s (status %d)\n" "SIG$sig" "$(tr "\n" " " </tmp/kd)" $st
done
rm -f /tmp/kd /tmp/fb
FAKE_FB=/tmp/fb:1080x1920x32 FAKE_KD=/tmp/kd ./hw --bot 400000 --seed 3 --from-picker --tty /tmp/tty >/dev/null 2>log || { tail log; fail "from-picker bot"; }
grep -q "back to the arcade" log || fail "Home hold on the title did not leave for the arcade"
grep -q "bad=0" log || fail "from-picker invariants"
kdcheck /tmp/kd hedgewick "back to the arcade"
echo "Home hold on the title: $(grep -o "ticks=[0-9]*" log), exit 0, $(tr "\n" " " </tmp/kd)"

echo "== sound (fake speaker card 1; card 0 is a headset that must stay closed)"
R=/tmp/fakeroot
mkdir -p $R/proc/asound $R/dev/snd
printf " 0 [Headset        ]: cs42l71 - iPhone Headset\n                      iPhone Headset\n 1 [Speaker        ]: simple-card - iPhone Speaker\n                      iPhone Speaker\n" >$R/proc/asound/cards
touch $R/dev/snd/controlC0 $R/dev/snd/pcmC0D0p $R/dev/snd/controlC1 $R/dev/snd/pcmC1D0p
FAKE_ALSA_ROOT=$R FAKE_ALSA_DUMP=/tmp/snd.raw ./hw-snd --bot 30000 >/dev/null 2>log || { tail -20 log; fail "fake ok"; }
grep -E "fake: (Speaker|frames)|sound: on" log | sort | uniq -c
grep -q "Speaker Volume := 164 (-20.0 dB)" log || fail "volume not -20 dB"
grep -q "sound=2 " log || fail "sound not on at the end"
grep -q FORBIDDEN log && fail "opened another device"
peak=$(grep -o "peak=[0-9]*" log | tail -1 | cut -d= -f2)
[ "$peak" -le 16384 ] || fail "peak $peak above -6 dBFS"
for m in hwpick xrun; do
    FAKE_ALSA_ROOT=$R FAKE_ALSA_MODE=$m ./hw-snd --bot 6000 >/dev/null 2>log || { tail -20 log; fail "fake $m"; }
    grep -q "sound=2 " log || fail "$m: sound not on"
    printf "%-10s %s\n" $m "$(grep -o "underruns=[0-9]*" log)"
done
for m in hwfail notlv loud readback wrongcard dead; do
    FAKE_ALSA_ROOT=$R FAKE_ALSA_MODE=$m ./hw-snd --bot 3000 >/dev/null 2>log || { tail -20 log; fail "fake $m"; }
    printf "%-10s %s\n" $m "$(grep -m1 "running silent" log | cut -c21-)"
    grep -q "sound=4 " log || fail "$m: not silent"
done
FAKE_ALSA_ROOT=$R ./hw-snd --bot 300 --vol -3 >/dev/null 2>log || fail "vol -3"
grep -q "Speaker Volume := 192 (-6.0 dB)" log || fail "--vol -3 not capped at -6 dB"
echo "--vol -3   capped: $(grep -m1 "Speaker Volume :=" log)"
mkdir -p /tmp/nocard/proc/asound
printf " 0 [Headset        ]: cs42l71 - iPhone Headset\n                      iPhone Headset\n" >/tmp/nocard/proc/asound/cards
FAKE_ALSA_ROOT=/tmp/nocard ./hw-snd --bot 1200 >/dev/null 2>log || fail "no card"
grep -q "sound=3 " log || fail "no card: not silent"
echo "no card   silent"

echo "== picker, scripted, with fake games"
mkdir -p /tmp/bin /tmp/run
cat >/tmp/bin/voidmoths <<"EOS"
#!/bin/sh
# fake VOIDMOTHS without the option the picker looks for
echo "voidmoths $* | console $(tail -1 $FAKE_KD)" >>/tmp/children.log
while [ $# -gt 0 ]; do [ "$1" = --hi ] && echo 4321 >"$2"; shift; done
exit 0
EOS
cat >/tmp/bin/hedgewick <<"EOS"
#!/bin/sh
# fake HEDGEWICK; knows --from-picker
echo "hedgewick $* | console $(tail -1 $FAKE_KD)" >>/tmp/children.log
while [ $# -gt 0 ]; do [ "$1" = --hi ] && echo 98765 >"$2"; shift; done
exit ${FAKE_STATUS:-0}
EOS
cat >/tmp/bin/glint <<"EOS"
#!/bin/sh
# fake GLINT; knows --from-picker; takes the console like a game does
echo "glint $* | console $(tail -1 $FAKE_KD)" >>/tmp/children.log
echo "glint graphics" >>$FAKE_KD
while [ $# -gt 0 ]; do [ "$1" = --hi ] && printf "4\n1234\n2345\n0\n3456\n4567\n0\n" >"$2"; shift; done
echo "glint text" >>$FAKE_KD
exit 0
EOS
chmod +x /tmp/bin/voidmoths /tmp/bin/hedgewick /tmp/bin/glint
rm -f /tmp/kd /tmp/fb /tmp/children.log /tmp/run/*
printf "1\n0\n0\n0\n0\n0\n0\n" >/tmp/run/glint.best
# start on HEDGEWICK; up: VOIDMOTHS, play; down: HEDGEWICK, play with Home held
# (no second start); down: GLINT, play; down wraps to VOIDMOTHS, up wraps to
# GLINT, up: HEDGEWICK, play
FAKE_FB=/tmp/fb:1080x1920x32 FAKE_KD=/tmp/kd ./ar $IIO --tty /tmp/tty --bin-dir /tmp/bin --run-dir /tmp/run --notice /tmp/notice2 \
    --script "20:volup,40:home,80:voldown,100:home*40,160:voldown,180:home,220:voldown,230:volup,240:volup,250:home" --ticks 330 \
    --script-notice "290:Mac: the battery is at 80 %, charging stopped" \
    --shot 15:/out/arcade-1-start.ppm --shot 215:/out/arcade-2-back.ppm --shot toast:/out/arcade-notice.ppm >/dev/null 2>log || { tail -20 log; fail "picker"; }
grep -E "^ticks=|shot|start:|GLINT:" log | cut -c1-120
cat /tmp/children.log
[ $(wc -l </tmp/children.log) = 4 ] || fail "expected 4 launches"
grep -q "GLINT: installed, cleared 1" log || fail "GLINT boards cleared not read from glint.best"
sed -n 1p /tmp/children.log | grep -q "^voidmoths --tty /tmp/tty --hi /tmp/run/voidmoths.hi | console arcade text$" || fail "voidmoths launch"
sed -n 2p /tmp/children.log | grep -q "^hedgewick --tty /tmp/tty --hi /tmp/run/hedgewick.hi --from-picker | console arcade text$" || fail "hedgewick launch"
sed -n 3p /tmp/children.log | grep -q "^glint --tty /tmp/tty --hi /tmp/run/glint.best --from-picker --fb-mmap | console arcade text$" || fail "glint launch"
sed -n 4p /tmp/children.log | grep -q "^hedgewick " || fail "Vol+/Vol- do not wrap round the three games"
grep -q "launches=4 returns=4" log || fail "launch count (a held Home must not start a game again)"
grep -q "^ticks=.* sel=1 " log || fail "selection"
kdcheck /tmp/kd arcade "picker"
[ "$(tr "\n" " " </tmp/kd)" = "arcade graphics arcade text arcade graphics arcade text arcade graphics arcade text glint graphics glint text arcade graphics arcade text arcade graphics arcade text " ] ||
    fail "console hand-over: $(tr "\n" " " </tmp/kd)"
echo "console: $(tr "\n" " " </tmp/kd)"
python3 /w/ppm.py check /out/arcade-1-start.ppm gold=ffd24a:2 card=10182c:150 wood=8a5230:3 || fail "picker colours"
python3 /w/ppm.py check /out/arcade-2-back.ppm wood=8a5230:3 brass=e0b955:1 || fail "GLINT card colours"
# the high scores the games left behind, read again
FAKE_FB=/tmp/fb:1080x1920x32 ./ar $IIO --tty /tmp/tty --bin-dir /tmp/bin --run-dir /tmp/run --script "5:voldown" --ticks 10 >/dev/null 2>log || fail "picker, scores"
grep -q "GLINT: installed, cleared 4, board 1 1234 cs" log || { grep GLINT: log; fail "GLINT best times"; }
grep -q "HEDGEWICK: installed, hi 98765" log || fail "HEDGEWICK high score"
grep -q "VOIDMOTHS: installed (no --from-picker: it cannot come back here), hi 4321" log || fail "VOIDMOTHS high score"
echo "scores: $(grep -o "[A-Z]*: installed.*" log | tr "\n" ";")"

echo "== picker with --fb-write: the games are asked for the same path"
rm -f /tmp/kd /tmp/fb /tmp/children.log
FAKE_FB=/tmp/fb:1080x1920x32 FAKE_KD=/tmp/kd ./ar $IIO --fb-write --tty /tmp/tty --bin-dir /tmp/bin --run-dir /tmp/run \
    --script "10:home,30:voldown,40:home" --ticks 60 >/dev/null 2>log || { tail -20 log; fail "picker --fb-write"; }
grep -q "fb: .*, pwrite;" log || fail "picker: --fb-write not used"
sed -n 1p /tmp/children.log | grep -q "^hedgewick --tty /tmp/tty --hi /tmp/run/hedgewick.hi --from-picker --fb-write | " || fail "hedgewick --fb-write"
sed -n 2p /tmp/children.log | grep -q "^glint --tty /tmp/tty --hi /tmp/run/glint.best --from-picker | " || fail "glint without --fb-mmap"
cut -d"|" -f1 /tmp/children.log

echo "== the real GLINT binary takes the options of the picker"
for extra in --fb-mmap ""; do
    st=0; /w/glint --tty /tmp/tty --hi /tmp/run/glint.best --from-picker $extra --fb /nonexistent/fb0 >/dev/null 2>/tmp/glint.err || st=$?
    [ $st = 4 ] || { cat /tmp/glint.err; fail "glint with the picker argv ($extra): status $st, want 4 (no framebuffer)"; }
    echo "glint --tty --hi --from-picker $extra: status 4 (no framebuffer here), options accepted"
done
st=0; /w/glint --swap --no-grab --mute --vol -20.0 --fb /nonexistent/fb0 --tty /tmp/tty >/dev/null 2>&1 || st=$?
[ $st = 4 ] || fail "glint with --swap --no-grab --mute --vol: status $st"

rm -f /tmp/kd /tmp/fb /tmp/children.log
FAKE_STATUS=3 FAKE_FB=/tmp/fb:1080x1920x16 FAKE_KD=/tmp/kd ./ar --tty /tmp/tty --bin-dir /tmp/bin --run-dir /tmp/run \
    --script "10:home" --ticks 60 >/dev/null 2>log || fail "picker, failing game"
grep -q "msg=.HEDGEWICK ENDED WITH STATUS 3" log || fail "a failing game is not reported"
rm -f /tmp/fb
FAKE_FB=/tmp/fb:1080x1920x32 ./ar $IIO --bin-dir /nonexistent --run-dir /tmp/run --tty /tmp/tty --script "10:voldown,20:home" --ticks 40 >/dev/null 2>log || fail "picker, no games"
grep -q "msg=.GLINT IS NOT INSTALLED" log || fail "missing game not reported"
echo "failing game and missing game reported on screen"

echo "== picker with the real HEDGEWICK as its child, then GLINT (fake)"
mkdir -p /tmp/realbin && cp hw /tmp/realbin/hedgewick && cp /tmp/bin/voidmoths /tmp/bin/glint /tmp/realbin/
rm -f /tmp/kd /tmp/fb /tmp/run/hedgewick.log /tmp/children.log
ARCADE_CHILD_ARGS="--bot 400000 --seed 3" FAKE_FB=/tmp/fb:1080x1920x32 FAKE_KD=/tmp/kd ./ar $IIO --tty /tmp/tty --bin-dir /tmp/realbin --run-dir /tmp/run \
    --script "10:home,40:voldown,50:home" --ticks 70 >/dev/null 2>log || { tail log; fail "picker + hedgewick"; }
grep -q "HEDGEWICK returned" log || { tail log; tail /tmp/run/hedgewick.log; fail "hedgewick did not come back"; }
grep -q "GLINT returned" log || { tail log; fail "glint did not come back"; }
grep -q "back to the arcade" /tmp/run/hedgewick.log || fail "hedgewick log"
grep -q "bad=0" /tmp/run/hedgewick.log || fail "hedgewick invariants as a child"
grep -q "fb: .*, mmap;" /tmp/run/hedgewick.log || fail "hedgewick as a child: not through the mapping"
[ "$(tr "\n" " " </tmp/kd)" = "arcade graphics arcade text hedgewick graphics hedgewick text arcade graphics arcade text glint graphics glint text arcade graphics arcade text " ] ||
    fail "console hand-over: $(tr "\n" " " </tmp/kd)"
echo "hand-over: $(tr "\n" " " </tmp/kd)"
echo "child: $(grep -o "ticks=[0-9]* .*games=[0-9]*" /tmp/run/hedgewick.log)"

echo "== picker sound (on demand) and signals"
rm -f /tmp/fb
FAKE_ALSA_ROOT=$R FAKE_FB=/tmp/fb:1080x1920x32 ./ar-snd --bin-dir /tmp/bin --run-dir /tmp/run --tty /tmp/tty \
    --script "10:voldown,20:volup,200:home" --ticks 400 >/dev/null 2>log || { tail -20 log; fail "picker sound"; }
grep -E "sound: (on|closed)|Speaker Volume|start:" log | cut -c16-
grep -q FORBIDDEN log && fail "picker opened another device"
grep -q "Speaker Volume := 164" log || fail "picker volume"
awk "/sound: closed/{c=NR} /start:/{s=NR} END{exit !(c && s && c < s)}" log || fail "speaker not closed before the game"
for sig in TERM HUP; do
    rm -f /tmp/kd /tmp/fb
    FAKE_FB=/tmp/fb:1080x1920x32 FAKE_KD=/tmp/kd ./ar-fast --tty /tmp/tty --bin-dir /tmp/bin --run-dir /tmp/run >/dev/null 2>log &
    p=$!; sleep 5; kill -$sig $p; wait $p || fail "picker SIG$sig: status $?"
    kdcheck /tmp/kd arcade "picker SIG$sig"
    echo "SIG$sig idle picker: $(grep -o "cpu:.*" log); $(grep -o "frames.*" log)"
done
rm -f /tmp/kd
FAKE_KD=/tmp/kd ./ar --restore-console --tty /tmp/tty && [ "$(cat /tmp/kd)" = "arcade text" ] || fail "--restore-console"
rm -f /tmp/kd
FAKE_KD=/tmp/kd ./ar --restore-tty /tmp/tty && [ "$(cat /tmp/kd)" = "arcade text" ] || fail "--restore-tty"
st=0; ./ar --fb /nonexistent/fb0 --tty /tmp/tty >/dev/null 2>&1 || st=$?
[ $st = 4 ] || fail "no framebuffer: status $st, want 4"
echo "--restore-console and --restore-tty ok; no framebuffer: status 4"

echo "== screen off: Power darkens, Home or Power lights it again (fake sysfs)"
val() { grep -o "\b$2=[-0-9.]*" "$1" | tail -1 | cut -d= -f2; }
# scv LOG NAME: a counter from the "screen: offs=..." line at the exit
scv() { grep -o "screen: offs=.*" "$1" | grep -o "\b$2=[-0-9.]*" | cut -d= -f2; }
# blroot DIR [brightness]: a fake root with the N66 backlight, with bl_power
# or (brightness) with brightness only
# (and checks that the root it replaces still has max_brightness untouched)
blroot() {
    mb=$1/sys/class/backlight/20e200080.backlight/max_brightness
    if [ -e $mb ]; then
        [ "$(stat -c %Y $mb) $(cat $mb)" = "1000000000 100" ] || fail "max_brightness was touched: $(stat -c %Y $mb) $(cat $mb)"
    fi
    rm -rf $1; mkdir -p $1/run $1/sys/class/backlight/20e200080.backlight
    if [ "${2:-}" != brightness ]; then echo 0 >$1/sys/class/backlight/20e200080.backlight/bl_power; fi
    echo 37 >$1/sys/class/backlight/20e200080.backlight/brightness
    echo 100 >$mb; touch -d @1000000000 $mb
}
BL=/tmp/bl; BLD=$BL/sys/class/backlight/20e200080.backlight
bp() { cat $BLD/bl_power; }
lines() { grep -o "screen $2: .*" "$1"; }
blroot $BL
# an unattended Wick dies; one with the screen dark from 8 s to 40 s does not
FAKE_BL_ROOT=$BL FAKE_KEYS="home,power,vol/1000:home" ./hw --bot 1500 --no-pilot >/dev/null 2>log || { tail log; fail "hedgewick, control"; }
ctl=$(val log deaths)
[ "$ctl" -ge 1 ] || fail "control: an unattended Wick should die within 50 s"
FAKE_BL_ROOT=$BL FAKE_KEYS="home,power,vol/1000:home,8000:power,40000:home" ./hw --bot 1500 --no-pilot >/dev/null 2>log || { tail log; fail "hedgewick dark"; }
grep -q "screen off: a Power press" log || fail "hedgewick: mapping not logged"
grep -q "screen: off (Power): 20e200080.backlight bl_power=4" log || fail "Power did not write bl_power 4"
grep -q "screen: on (Home)" log || fail "Home did not light the screen"
d0=$(lines log dark); l1=$(lines log lit)
echo "$l1" | grep -q ", paused$" || fail "not paused after the dark screen: $l1"
[ "$(echo "$l1" | grep -o "deaths.*life_t [0-9]*")" = "$(echo "$d0" | grep -o "deaths.*life_t [0-9]*")" ] || fail "the game ran while dark: $d0 / $l1"
[ "$(val log deaths)" = 0 ] || fail "deaths while dark or paused: $(val log deaths)"
grep -q "paused=1" log || fail "not paused at the end (no press after the wake)"
[ "$(scv log offs)/$(scv log ons)/$(scv log eaten)" = 1/1/1 ] || fail "offs/ons/eaten: $(grep -o "offs=.*" log)"
[ "$(bp)" = 0 ] || fail "bl_power left at $(bp)"
echo "HEDGEWICK unattended: $ctl deaths in 50 s; dark 8-40 s: 0 deaths, still paused. $l1"
# Power lights it too; a Home tap plays on (the clock moves again)
FAKE_BL_ROOT=$BL FAKE_KEYS="home,power,vol/1000:home,8000:power,20000:power,22000:home,30000:power,31000:home" ./hw --bot 1200 --no-pilot >/dev/null 2>log || fail "hedgewick, Power wakes"
grep -q "screen: on (Power)" log || fail "Power did not light the screen"
[ "$(scv log offs)/$(scv log ons)/$(scv log eaten)" = 2/2/2 ] || fail "offs/ons/eaten: $(grep -o "offs=.*" log)"
t1=$(lines log lit | head -1 | grep -o "deaths.*life_t [0-9]*")
t2=$(lines log dark | tail -1 | grep -o "deaths.*life_t [0-9]*")
[ "$t2" != "$t1" ] || fail "a Home tap did not play on after the dark screen ($t1 / $t2)"
echo "Power lights it as well; a Home tap plays on: $t1 -> $t2"
# the touchscreen while dark: two taps that would have resumed are dropped
python3 -I touchrec.py /tmp/tt 0:1079:0:1919 1080x1920 "tap 10000 540 960; tap 12000 540 60"
FAKE_TOUCH=/tmp/tt:0:1079:0:1919 FAKE_BL_ROOT=$BL FAKE_KEYS="home,power,vol/1000:home,8000:power,15000:home" ./hw --bot 600 --no-pilot >/dev/null 2>log || fail "hedgewick, touch while dark"
[ "$(val log ttaps)" = 0 ] || fail "touch while dark was not ignored: ttaps=$(val log ttaps)"
[ "$(val log tdropped)" -gt 0 ] || fail "no touch events dropped"
grep -q "paused=1" log || fail "a tap while dark resumed the game"
echo "touch while dark: $(val log tdropped) events dropped, no tap seen, still paused"
# no volume keys: Power held 0.8 s darkens, a short Power press is a start as before
FAKE_BL_ROOT=$BL FAKE_KEYS="home,power/1000:power*200,5000:power*1200,9000:home" ./hw --bot 400 --no-pilot >/dev/null 2>log || fail "hedgewick, long press"
grep -q "screen off: Power held 0.8 s" log || fail "hedgewick: long-press mapping not logged"
grep -q "games=1 " log || fail "a short Power press no longer starts"
[ "$(scv log longs)/$(scv log taps)/$(scv log offs)/$(scv log ons)" = 1/1/1/1 ] || fail "long press: $(grep -o "offs=.*" log)"
grep -q "screen: off (Power held 0.8 s)" log || fail "long press did not darken"
echo "no volume keys: short Power = start, Power held 0.8 s = screen off"
# the speaker: closed while dark, opened again after
FAKE_ALSA_ROOT=$R FAKE_BL_ROOT=$BL FAKE_KEYS="home,power,vol/3000:power,6000:home" ./hw-snd --bot 400 >/dev/null 2>log || { tail log; fail "hedgewick, sound and dark"; }
seq=$(grep -oE "sound: (on|closed)|screen: (off|on)" log | tr "\n" ",")
case "$seq" in "sound: on,screen: off,sound: closed,screen: on,sound: on,"*) ;; *) fail "sound and dark: $seq";; esac
echo "sound: $seq"

echo "== screen off in real time (HEDGEWICK, ARCADE): nothing drawn, idle CPU, notices, signals"
blroot $BL
rm -f /tmp/fb /tmp/notice-rt
FAKE_BL_ROOT=$BL FAKE_KEYS="home,power,vol/1500:power" FAKE_FB=/tmp/fb:1080x1920x32 FAKE_KD=/tmp/kd ./hw --tty /tmp/tty --hi /tmp/hw.hi --notice /tmp/notice-rt >/dev/null 2>log &
p=$!; sleep 4
[ "$(bp)" = 4 ] || { kill $p; fail "real time: bl_power $(bp) after the Power press"; }
a=$(md5sum </tmp/fb); sleep 2; b=$(md5sum </tmp/fb)
[ "$a" = "$b" ] || { kill $p; fail "real time: HEDGEWICK drew while dark"; }
echo "Mac: build done" >/tmp/notice-rt; sleep 1.5
[ "$(bp)" = 0 ] || { kill $p; fail "a notice did not light the screen"; }
c=$(md5sum </tmp/fb)
[ "$c" != "$b" ] || { kill $p; fail "nothing drawn after the notice lit the screen"; }
kill -TERM $p; wait $p || fail "hedgewick: status $?"
cpu=$(grep -o "screen: on (a notice) after .*" log | grep -o "([0-9.]* %" | tr -d "(%")
awk "BEGIN{exit !($cpu < 3)}" || fail "HEDGEWICK used $cpu % of a core while dark"
echo "HEDGEWICK dark: frame unchanged for 2 s, $cpu % of one core (ASan); a notice lit it: $(grep -o "notice: .*" log)"
for sig in TERM SEGV ABRT; do
    bin=./hw; [ $sig = TERM ] || bin=./hw-fast
    FAKE_BL_ROOT=$BL FAKE_KEYS="home,power,vol/1000:power" FAKE_FB=/tmp/fb:1080x1920x32 FAKE_KD=/tmp/kd $bin --tty /tmp/tty --hi /tmp/hw.hi >/dev/null 2>log &
    p=$!; sleep 3
    [ "$(bp)" = 4 ] || { kill $p; fail "SIG$sig: not dark before the signal"; }
    kill -$sig $p; st=0; wait $p || st=$?
    [ "$(bp)" = 0 ] || fail "SIG$sig while dark: bl_power left at $(bp)"
    echo "SIG$sig while dark: bl_power 0 again (status $st)"
done
FAKE_BL_ROOT=$BL FAKE_KEYS="home,power,vol/1000:power" FAKE_FB=/tmp/fb:1080x1920x32 FAKE_KD=/tmp/kd ./hw --tty /tmp/tty --hi /tmp/hw.hi >/dev/null 2>log &
p=$!; sleep 3; kill -KILL $p; wait $p || true
[ "$(bp)" = 4 ] || fail "SIGKILL test: not dark"
rm -f /tmp/kd
FAKE_BL_ROOT=$BL FAKE_KD=/tmp/kd ./ar --restore-tty /tmp/tty 2>log || fail "--restore-tty"
[ "$(bp)" = 0 ] || fail "arcade --restore-tty did not light the screen after a SIGKILL"
grep -q "bl_power was 4, set to 0 (restore)" log || fail "--restore-tty log"
echo "SIGKILL while dark, then arcade --restore-tty (init does it after every run): bl_power 0"
echo 4 >$BLD/bl_power
FAKE_BL_ROOT=$BL ./hw --bot 10 >/dev/null 2>log || fail "restart"
[ "$(bp)" = 0 ] && grep -q "bl_power was 4, set to 0 (start)" log || fail "a restarted program did not light the screen"
echo "a restarted program (the respawn loop) lights a screen left dark"
# brightness only: the value is kept, written back on every way out
blroot $BL brightness
FAKE_BL_ROOT=$BL FAKE_KEYS="home,power,vol/1000:power" FAKE_FB=/tmp/fb:1080x1920x32 FAKE_KD=/tmp/kd ./hw --tty /tmp/tty --hi /tmp/hw.hi >/dev/null 2>log &
p=$!; sleep 3
[ "$(cat $BLD/brightness)" = 0 ] || { kill $p; fail "brightness only: not 0 while dark"; }
[ "$(cat $BL/run/arcade-backlight)" = "20e200080.backlight 37" ] || { kill $p; fail "brightness only: value not kept"; }
kill -TERM $p; wait $p
[ "$(cat $BLD/brightness)" = 37 ] || fail "brightness only: $(cat $BLD/brightness) after SIGTERM"
[ ! -e $BL/run/arcade-backlight ] || fail "state file left behind"
FAKE_BL_ROOT=$BL FAKE_KEYS="home,power,vol/1000:power" FAKE_FB=/tmp/fb:1080x1920x32 FAKE_KD=/tmp/kd ./hw --tty /tmp/tty --hi /tmp/hw.hi >/dev/null 2>log &
p=$!; sleep 3; kill -KILL $p; wait $p || true
[ "$(cat $BLD/brightness)" = 0 ] || fail "brightness only, SIGKILL: not dark"
FAKE_BL_ROOT=$BL ./hw --bot 10 >/dev/null 2>log || fail "restart, brightness"
[ "$(cat $BLD/brightness)" = 37 ] || fail "brightness only: a restarted program did not put 37 back"
echo "brightness only: 37 -> 0 while dark (kept in run/arcade-backlight) -> 37 after SIGTERM, and after SIGKILL + restart"
# the picker, scripted: dark at 10, Vol- eaten, Home lights it (no start), Vol- moves
blroot $BL
rm -f /tmp/fb /tmp/children.log
FAKE_BL_ROOT=$BL FAKE_FB=/tmp/fb:1080x1920x32 FAKE_KD=/tmp/kd ./ar $IIO --tty /tmp/tty --bin-dir /tmp/bin --run-dir /tmp/run --notice none \
    --script "10:power,40:voldown,60:home,100:voldown" --ticks 130 --shot 5:/out/arcade-screen-off-hint.ppm >/dev/null 2>log || { tail log; fail "picker dark"; }
grep -q "launches=0 " log || fail "the Home press that lit the screen started a game"
grep -q "^ticks=130 sel=2 " log || fail "selection: Vol- while dark must do nothing, after it one step: $(grep ^ticks log)"
[ "$(scv log offs)/$(scv log ons)/$(scv log eaten)" = 1/1/2 ] || fail "picker offs/ons/eaten: $(grep -o "offs=.*" log)"
[ "$(bp)" = 0 ] || fail "picker: bl_power $(bp)"
rm -f /tmp/children.log
FAKE_BL_ROOT=$BL FAKE_FB=/tmp/fb:1080x1920x32 FAKE_KD=/tmp/kd ./ar $IIO --tty /tmp/tty --bin-dir /tmp/bin --run-dir /tmp/run --notice none \
    --script "10:power,40:power,70:home" --ticks 100 >/dev/null 2>log || fail "picker, Power wakes"
grep -q "screen: on (Power)" log && grep -q "launches=1 " log || fail "picker: Power lights, then Home plays"
echo "ARCADE: Power off, Vol- and Home while dark do nothing but light it, Power lights it too"
# hand-over: a game that dies with the screen dark; the picker lights it
mkdir -p /tmp/blbin
printf "#!/bin/sh\n: --from-picker\necho 4 >\$FAKE_BL_ROOT/sys/class/backlight/20e200080.backlight/bl_power\nexit 0\n" >/tmp/blbin/hedgewick
cp /tmp/bin/voidmoths /tmp/bin/glint /tmp/blbin/; chmod +x /tmp/blbin/*
FAKE_BL_ROOT=$BL FAKE_FB=/tmp/fb:1080x1920x32 FAKE_KD=/tmp/kd ./ar $IIO --tty /tmp/tty --bin-dir /tmp/blbin --run-dir /tmp/run --notice none \
    --script "10:home" --ticks 40 >/dev/null 2>log || fail "picker, dark game"
[ "$(bp)" = 0 ] && grep -q "bl_power was 4, set to 0 (back from the game)" log || fail "the picker did not light what the game left dark"
blroot $BL brightness
printf "#!/bin/sh\n: --from-picker\necho \"20e200080.backlight 37\" >\$FAKE_BL_ROOT/run/arcade-backlight\necho 0 >\$FAKE_BL_ROOT/sys/class/backlight/20e200080.backlight/brightness\nexit 0\n" >/tmp/blbin/hedgewick
FAKE_BL_ROOT=$BL FAKE_FB=/tmp/fb:1080x1920x32 FAKE_KD=/tmp/kd ./ar $IIO --tty /tmp/tty --bin-dir /tmp/blbin --run-dir /tmp/run --notice none \
    --script "10:home" --ticks 40 >/dev/null 2>log || fail "picker, dark game, brightness"
[ "$(cat $BLD/brightness)" = 37 ] || fail "brightness only: the picker did not put 37 back after the game"
echo "hand-over: a game that leaves the screen dark (bl_power 4, or brightness 0) - the picker lights it on return"
# the picker in real time
blroot $BL
rm -f /tmp/fb
FAKE_BL_ROOT=$BL FAKE_KEYS="home,power,vol/1500:power" FAKE_FB=/tmp/fb:1080x1920x32 FAKE_KD=/tmp/kd ./ar --tty /tmp/tty --bin-dir /tmp/bin --run-dir /tmp/run --notice none >/dev/null 2>log &
p=$!; sleep 3.5
[ "$(bp)" = 4 ] || { kill $p; fail "picker, real time: not dark"; }
a=$(md5sum </tmp/fb); sleep 2; b=$(md5sum </tmp/fb)
[ "$a" = "$b" ] || { kill $p; fail "the picker drew while dark"; }
kill -TERM $p; wait $p || fail "picker: status $?"
[ "$(bp)" = 0 ] || fail "picker SIGTERM while dark: bl_power $(bp)"
cpu=$(scv log dark_cpu)
awk "BEGIN{exit !($cpu < 3)}" || fail "the picker used $cpu % of a core while dark"
echo "ARCADE dark: frame unchanged, $cpu % of one core (ASan); SIGTERM: bl_power 0"
blroot $BL
echo "max_brightness untouched in every run"

echo "== frame cost (-O2, no sanitizers, 1080x1920 XRGB8888)"
rm -f /tmp/fb
FAKE_FB=/tmp/fb:1080x1920x32 ./hw-fast --bot 20000 --seed 3 >/dev/null 2>log
grep -o "frames:.*" log
rm -f /tmp/fb
FAKE_FB=/tmp/fb:1080x1920x32 ./hw-fast --tty /tmp/tty --hi /tmp/hw.hi >/dev/null 2>log &
p=$!; sleep 5; kill -TERM $p; wait $p
echo "title screen, real time, 30 fps: $(grep -o "cpu:.*" log)"
rm -f /tmp/fb
FAKE_FB=/tmp/fb:1080x1920x32 ./ar-fast --bin-dir /tmp/bin --run-dir /tmp/run --script "10:voldown" --ticks 3000 >/dev/null 2>log
echo "picker: $(grep -o "frames [0-9]*, [0-9]* KB.*" log), $(grep -o "cpu:.*" log)"

echo "== PNG"
cd /out
for f in *.ppm; do python3 /w/ppm.py png $f ${f%.ppm}.png; rm $f; done
ls
echo "ALL TESTS PASSED"
tar cf - *.png >&3
' | tar -xf - -C "$frames"
ls -l "$frames"
