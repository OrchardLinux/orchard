#!/usr/bin/env python3
"""touchrec.py OUT XMIN:XMAX:YMIN:YMAX WxH SCRIPT -- write a recorded touch stream.

The file holds struct input_event (aarch64: 64-bit seconds and microseconds,
type, code, value) as apple_z2 sends them: multitouch protocol B, one slot per
finger, a tracking id per contact, a SYN_REPORT per frame. The programs read
it through $FAKE_TOUCH (touch.h) and deliver each event once their clock
reaches its timestamp.

Positions in SCRIPT are screen pixels of a WxH screen; they are turned into
the device's raw units (the range XMIN..XMAX, YMIN..YMAX spans the screen), as
a panel whose units are not pixels (J71) reports them. Times are ms.

SCRIPT: commands separated by ';'
  tap T X Y                     down at T, up 80 ms later
  long T X Y MS                 down at T, held still MS, up
  swipe T X0 Y0 X1 Y1 MS        down at T, moves to X1,Y1 in 8 frames over MS, up
  drag T X0 Y0 X1 Y1 MS         the same with a frame every 16 ms, finger kept down 200 ms at the end
  two T X Y X2 Y2 MS            a first finger at X,Y and a second at X2,Y2 for MS
  down T X Y | move T X Y | up T  single frames
"""
import struct
import sys

EV_SYN, EV_KEY, EV_ABS = 0, 1, 3
SYN_REPORT = 0
BTN_TOUCH = 0x14a
ABS_MT_SLOT, ABS_MT_POSITION_X, ABS_MT_POSITION_Y, ABS_MT_TRACKING_ID = 0x2f, 0x35, 0x36, 0x39


def main():
    out, rng, scr, script = sys.argv[1:5]
    xmin, xmax, ymin, ymax = (int(v) for v in rng.split(':'))
    w, h = (int(v) for v in scr.lower().split('x'))
    events = []
    tid = [100]

    def raw(px, py):
        rx = xmin + int((px + 0.5) * (xmax - xmin + 1) / w)
        ry = ymin + int((py + 0.5) * (ymax - ymin + 1) / h)
        return min(max(rx, xmin), xmax), min(max(ry, ymin), ymax)

    def ev(t, typ, code, val):
        events.append((t, typ, code, val))

    def frame(t, items):
        for typ, code, val in items:
            ev(t, typ, code, val)
        ev(t, EV_SYN, SYN_REPORT, 0)

    def down(t, x, y, slot=0, first=True):
        rx, ry = raw(x, y)
        tid[0] += 1
        items = [(EV_ABS, ABS_MT_SLOT, slot), (EV_ABS, ABS_MT_TRACKING_ID, tid[0]),
                 (EV_ABS, ABS_MT_POSITION_X, rx), (EV_ABS, ABS_MT_POSITION_Y, ry)]
        if first:
            items.append((EV_KEY, BTN_TOUCH, 1))
        frame(t, items)

    def move(t, x, y, slot=0):
        rx, ry = raw(x, y)
        frame(t, [(EV_ABS, ABS_MT_SLOT, slot), (EV_ABS, ABS_MT_POSITION_X, rx), (EV_ABS, ABS_MT_POSITION_Y, ry)])

    def up(t, slot=0, last=True):
        items = [(EV_ABS, ABS_MT_SLOT, slot), (EV_ABS, ABS_MT_TRACKING_ID, -1)]
        if last:
            items.append((EV_KEY, BTN_TOUCH, 0))
        frame(t, items)

    def path(t, x0, y0, x1, y1, ms, steps):
        down(t, x0, y0)
        for i in range(1, steps + 1):
            move(t + ms * i // steps, x0 + (x1 - x0) * i // steps, y0 + (y1 - y0) * i // steps)

    for cmd in filter(None, (c.strip() for c in script.split(';'))):
        a = cmd.split()
        op, n = a[0], [int(v) for v in a[1:]]
        if op == 'tap':
            down(n[0], n[1], n[2]); up(n[0] + 80)
        elif op == 'long':
            down(n[0], n[1], n[2]); up(n[0] + n[3])
        elif op == 'swipe':
            path(n[0], n[1], n[2], n[3], n[4], n[5], 8); up(n[0] + n[5] + 16)
        elif op == 'drag':
            steps = max(1, n[5] // 16)
            path(n[0], n[1], n[2], n[3], n[4], n[5], steps); up(n[0] + n[5] + 200)
        elif op == 'two':
            down(n[0], n[1], n[2]); down(n[0] + 30, n[3], n[4], slot=1, first=False)
            up(n[0] + n[5] - 30, slot=1, last=False); up(n[0] + n[5])
        elif op == 'down':
            down(n[0], n[1], n[2])
        elif op == 'move':
            move(n[0], n[1], n[2])
        elif op == 'up':
            up(n[0])
        else:
            sys.exit(f'touchrec.py: unknown command {op!r}')
    events.sort(key=lambda e: e[0])     # stable: a frame keeps its order
    with open(out, 'wb') as f:
        for t, typ, code, val in events:
            f.write(struct.pack('<qqHHi', t // 1000, (t % 1000) * 1000, typ, code, val))


if __name__ == '__main__':
    main()
