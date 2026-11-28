/*
 * touch.h -- the touchscreen for the ARCADE programs (arcade, hedgewick,
 * voidmoths, glint). One header with no dependencies beyond libc and the
 * kernel's input UAPI; each program includes it after its own headers.
 *
 * The device is found by what it can do, never by its name: an evdev node
 * with EV_ABS, ABS_MT_POSITION_X, ABS_MT_POSITION_Y and INPUT_PROP_DIRECT (a
 * screen, not a touchpad). On the Orchard boards that is apple_z2 (N66, J71,
 * J85, J81), which reports multitouch protocol B (slots, tracking ids) and
 * applies touchscreen-inverted-y itself. Its axis ranges come from
 * EVIOCGABS: J85, J81 and N66 report screen pixels, J71 its own units
 * (touchscreen-size 14621 x 19539); either way the range spans the panel.
 *
 * Mapping: raw -> screen pixel (range onto the screen size) -> the
 * program's canvas, through the same scale and centring offset the program
 * draws with (touch_view()). A touch in the letterbox gives canvas
 * coordinates outside 0..cw-1 / 0..ch-1; hit tests then simply miss.
 *
 * One finger is followed (the first down); others are ignored until it
 * lifts. What the programs get:
 *   - gestures, queued: TAP (down and up within TAP_MS, moved less than the
 *     slop), SWIPE (moved a swipe length from the anchor while down; one per
 *     length, direction by the larger axis), LONG (held still LONG_MS);
 *   - the contact: tp.down, tp.x, tp.y (canvas), for dragging;
 *   - the corner: a contact that starts in the top-left box (touch_corner(),
 *     the letterbox beyond it included) is a virtual Home button:
 *     touch_corner_down() while it stays there; it makes no gestures.
 *
 * Test builds (MZ_TEST or TOUCH_TEST): $FAKE_TOUCH=FILE:XMIN:XMAX:YMIN:YMAX
 * stands in for the device: FILE holds recorded struct input_event, each
 * delivered once the caller's clock reaches its timestamp (ms).
 */
#ifndef TOUCH_H
#define TOUCH_H

#include <errno.h>
#include <fcntl.h>
#include <linux/input.h>
#include <stdint.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <sys/ioctl.h>
#include <unistd.h>

#define TP_MAXSLOT 10
#define TP_NL(n) (((n) + 8 * (int)sizeof(long) - 1) / (8 * (int)sizeof(long)))
#define TP_QLEN 16
#define TAP_MS 450
#define LONG_MS 700

enum { TG_NONE, TG_TAP, TG_SWIPE, TG_LONG };
enum { TD_UP, TD_RIGHT, TD_DOWN, TD_LEFT };     /* swipe directions */
typedef struct { int kind, x, y, dir, x0, y0; } TGesture;

static struct {
	int fd, evnum, grabbed;
	char name[48], desc[96];
	int xmin, xmax, ymin, ymax;
	int sw, sh, ox, oy, scale, cw, ch;      /* the view */
	int slop, swipe, cnr_w, cnr_h;          /* canvas units */
	int slot, id[TP_MAXSLOT], rx[TP_MAXSLOT], ry[TP_MAXSLOT];
	int prim;                               /* followed slot, -1 none */
	int down, corner, cnr_live, x, y, x0, y0, ax, ay, moved, swiped, longed;
	int64_t t0, now;
	TGesture q[TP_QLEN];
	int qh, qn;
	long n_contact, n_tap, n_swipe, n_long, n_corner, n_events, n_dropped;
	int64_t last_scan;
#if defined(MZ_TEST) || defined(TOUCH_TEST)
	struct input_event *fake;
	size_t nfake, ifake;
#endif
} tp = { .fd = -1, .prim = -1, .scale = 1, .sw = 1, .sh = 1, .cw = 1, .ch = 1, .slop = 2, .swipe = 6, .last_scan = -100000 };

static int tp_bit(const unsigned long *b, int n)
{
	return (int)((b[n / (8 * (int)sizeof(long))] >> (n % (8 * (int)sizeof(long)))) & 1UL);
}

/* The rule, on the capability bitmaps (EVIOCGBIT(0), EVIOCGBIT(EV_ABS),
 * EVIOCGPROP): a direct multitouch screen. */
static __attribute__((unused)) int touch_caps_ok(const unsigned long *ev, const unsigned long *abs, const unsigned long *prop)
{
	return tp_bit(ev, EV_ABS) && tp_bit(abs, ABS_MT_POSITION_X) && tp_bit(abs, ABS_MT_POSITION_Y) &&
	       tp_bit(prop, INPUT_PROP_DIRECT);
}

static void tp_reset_slots(void)
{
	for (int i = 0; i < TP_MAXSLOT; i++) tp.id[i] = -1;
	tp.slot = 0;
	tp.prim = -1;
	tp.down = tp.corner = tp.cnr_live = 0;
}

/* Where the program draws: screen sw x sh pixels, canvas cw x ch units at
 * (ox, oy), each unit scale pixels. The corner box is cnr_w x cnr_h units. */
static __attribute__((unused)) void touch_view(int sw, int sh, int ox, int oy, int scale, int cw, int ch, int cnr_w, int cnr_h)
{
	int u;
	tp.sw = sw > 0 ? sw : 1; tp.sh = sh > 0 ? sh : 1;
	tp.ox = ox; tp.oy = oy;
	tp.scale = scale > 0 ? scale : 1;
	tp.cw = cw > 0 ? cw : 1; tp.ch = ch > 0 ? ch : 1;
	tp.cnr_w = cnr_w; tp.cnr_h = cnr_h;
	u = tp.cw < tp.ch ? tp.cw : tp.ch;
	tp.slop = u / 24 > 2 ? u / 24 : 2;
	tp.swipe = u / 9 > 2 * tp.slop ? u / 9 : 2 * tp.slop;
}

static int tp_floordiv(int a, int b) { return a >= 0 ? a / b : -((-a + b - 1) / b); }

/* Raw device coordinates to canvas units (may be outside the canvas). */
static __attribute__((unused)) void touch_map(int rx, int ry, int *cx, int *cy)
{
	long long wx = (long long)tp.xmax - tp.xmin + 1, wy = (long long)tp.ymax - tp.ymin + 1;
	int sx, sy;
	if (wx < 1) wx = 1;
	if (wy < 1) wy = 1;
	sx = (int)(((long long)rx - tp.xmin) * tp.sw / wx);
	sy = (int)(((long long)ry - tp.ymin) * tp.sh / wy);
	*cx = tp_floordiv(sx - tp.ox, tp.scale);
	*cy = tp_floordiv(sy - tp.oy, tp.scale);
}

static int tp_in_corner(int x, int y, int margin)
{
	return tp.cnr_w > 0 && x < tp.cnr_w + margin && y < tp.cnr_h + margin;
}

static void tp_push(int kind, int x, int y, int dir)
{
	TGesture *g;
	if (tp.qn >= TP_QLEN) return;
	g = &tp.q[(tp.qh + tp.qn++) % TP_QLEN];
	g->kind = kind; g->x = x; g->y = y; g->dir = dir; g->x0 = tp.x0; g->y0 = tp.y0;
	if (kind == TG_TAP) tp.n_tap++;
	else if (kind == TG_SWIPE) tp.n_swipe++;
	else if (kind == TG_LONG) tp.n_long++;
}

/* The next gesture; 0 when there is none. */
static __attribute__((unused)) int touch_next(TGesture *g)
{
	if (!tp.qn) return 0;
	*g = tp.q[tp.qh];
	tp.qh = (tp.qh + 1) % TP_QLEN;
	tp.qn--;
	return 1;
}

static __attribute__((unused)) void touch_flush(void) { tp.qn = 0; }

static void tp_begin(int x, int y)
{
	tp.down = 1;
	tp.x = tp.x0 = tp.ax = x;
	tp.y = tp.y0 = tp.ay = y;
	tp.moved = tp.swiped = tp.longed = 0;
	tp.t0 = tp.now;
	tp.corner = tp.cnr_live = tp_in_corner(x, y, 0);
	tp.n_contact++;
	if (tp.corner) tp.n_corner++;
}

static void tp_move(int x, int y)
{
	int dx, dy, adx, ady;
	tp.x = x; tp.y = y;
	if (abs(x - tp.x0) > tp.slop || abs(y - tp.y0) > tp.slop) tp.moved = 1;
	if (tp.corner) {
		/* a corner contact makes no gestures; dragged out of the box it is
		 * no longer the virtual button either */
		if (!tp_in_corner(x, y, tp.slop)) tp.cnr_live = 0;
		return;
	}
	dx = x - tp.ax; dy = y - tp.ay;
	adx = abs(dx); ady = abs(dy);
	if ((adx >= tp.swipe || ady >= tp.swipe) && !tp.longed) {
		int dir = adx > ady ? (dx > 0 ? TD_RIGHT : TD_LEFT) : (dy > 0 ? TD_DOWN : TD_UP);
		tp_push(TG_SWIPE, x, y, dir);
		tp.swiped = 1;
		tp.ax = x; tp.ay = y;
	}
}

static void tp_end(void)
{
	int was_corner = tp.corner;
	tp.down = tp.corner = tp.cnr_live = 0;
	if (was_corner || tp.swiped || tp.longed || tp.moved) return;
	if (tp.now - tp.t0 <= TAP_MS) tp_push(TG_TAP, tp.x, tp.y, 0);
}

/* One SYN_REPORT: the followed finger began, moved or lifted. */
static void tp_frame(void)
{
	int cx, cy;
	if (tp.prim >= 0 && tp.id[tp.prim] < 0) {
		tp.prim = -1;
		if (tp.down) tp_end();
	}
	if (tp.prim < 0) {
		for (int i = 0; i < TP_MAXSLOT; i++)
			if (tp.id[i] >= 0) { tp.prim = i; break; }
		if (tp.prim >= 0) {
			touch_map(tp.rx[tp.prim], tp.ry[tp.prim], &cx, &cy);
			tp_begin(cx, cy);
		}
		return;
	}
	touch_map(tp.rx[tp.prim], tp.ry[tp.prim], &cx, &cy);
	if (cx != tp.x || cy != tp.y) tp_move(cx, cy);
}

/* Feed one event from the device (or a recording). */
static __attribute__((unused)) void touch_feed(const struct input_event *e)
{
	tp.n_events++;
	if (e->type == EV_SYN) {
		if (e->code == SYN_REPORT) tp_frame();
		else if (e->code == SYN_DROPPED) {
			/* lost events: forget the contact; the next finger starts afresh */
			tp_reset_slots();
		}
		return;
	}
	if (e->type != EV_ABS) return;
	switch (e->code) {
	case ABS_MT_SLOT: tp.slot = e->value; break;
	case ABS_MT_TRACKING_ID:
		if (tp.slot >= 0 && tp.slot < TP_MAXSLOT) tp.id[tp.slot] = e->value < 0 ? -1 : e->value;
		break;
	case ABS_MT_POSITION_X:
		if (tp.slot >= 0 && tp.slot < TP_MAXSLOT) tp.rx[tp.slot] = e->value;
		break;
	case ABS_MT_POSITION_Y:
		if (tp.slot >= 0 && tp.slot < TP_MAXSLOT) tp.ry[tp.slot] = e->value;
		break;
	}
}

/* The clock moved on: a still finger becomes a long press. */
static void tp_clock(int64_t now_ms)
{
	tp.now = now_ms;
	if (tp.down && !tp.corner && !tp.moved && !tp.longed && tp.now - tp.t0 >= LONG_MS) {
		tp.longed = 1;
		tp_push(TG_LONG, tp.x, tp.y, 0);
	}
}

static __attribute__((unused)) void touch_close(void)
{
	if (tp.fd >= 0) close(tp.fd);   /* also ends the grab */
	tp.fd = -1;
	tp_reset_slots();
	tp.qn = 0;
#if defined(MZ_TEST) || defined(TOUCH_TEST)
	free(tp.fake);
	tp.fake = NULL;
	tp.nfake = tp.ifake = 0;
#endif
}

#if defined(MZ_TEST) || defined(TOUCH_TEST)
static int tp_open_fake(const char *spec)
{
	char path[256];
	const char *c = strchr(spec, ':');
	FILE *f;
	long sz;
	if (!c || (size_t)(c - spec) >= sizeof path) return -1;
	memcpy(path, spec, (size_t)(c - spec));
	path[c - spec] = 0;
	if (sscanf(c + 1, "%d:%d:%d:%d", &tp.xmin, &tp.xmax, &tp.ymin, &tp.ymax) != 4) return -1;
	f = fopen(path, "rb");
	if (!f) return -1;
	fseek(f, 0, SEEK_END);
	sz = ftell(f);
	fseek(f, 0, SEEK_SET);
	tp.nfake = sz > 0 ? (size_t)sz / sizeof(struct input_event) : 0;
	tp.fake = calloc(tp.nfake + 1, sizeof(struct input_event));
	if (!tp.fake || fread(tp.fake, sizeof(struct input_event), tp.nfake, f) != tp.nfake) { fclose(f); return -1; }
	fclose(f);
	tp.ifake = 0;
	tp.fd = open("/dev/null", O_RDONLY | O_CLOEXEC);
	tp.evnum = -1;
	snprintf(tp.name, sizeof tp.name, "recorded");
	return tp.fd >= 0 ? 0 : -1;
}
#endif

/* Find and open the touchscreen. 1 found, 0 none. */
static __attribute__((unused)) int touch_open(int excl)
{
	touch_close();
#if defined(MZ_TEST) || defined(TOUCH_TEST)
	{
		const char *fk = getenv("FAKE_TOUCH");
		if (fk) {
			if (tp_open_fake(fk) < 0) { touch_close(); snprintf(tp.desc, sizeof tp.desc, "bad FAKE_TOUCH"); return 0; }
			goto found;
		}
	}
#endif
	for (int i = 0; i < 32; i++) {
		char p[32];
		unsigned long ev[TP_NL(EV_CNT)], abs_[TP_NL(ABS_CNT)], prop[TP_NL(INPUT_PROP_CNT)];
		struct input_absinfo ax, ay;
		int fd;
		snprintf(p, sizeof p, "/dev/input/event%d", i);
		fd = open(p, O_RDONLY | O_NONBLOCK | O_CLOEXEC);
		if (fd < 0) continue;
		memset(ev, 0, sizeof ev); memset(abs_, 0, sizeof abs_); memset(prop, 0, sizeof prop);
		if (ioctl(fd, EVIOCGBIT(0, sizeof ev), ev) < 0 || ioctl(fd, EVIOCGBIT(EV_ABS, sizeof abs_), abs_) < 0 ||
		    ioctl(fd, EVIOCGPROP(sizeof prop), prop) < 0 || !touch_caps_ok(ev, abs_, prop) ||
		    ioctl(fd, EVIOCGABS(ABS_MT_POSITION_X), &ax) < 0 || ioctl(fd, EVIOCGABS(ABS_MT_POSITION_Y), &ay) < 0 ||
		    ax.maximum <= ax.minimum || ay.maximum <= ay.minimum) {
			close(fd);
			continue;
		}
		tp.fd = fd;
		tp.evnum = i;
		tp.xmin = ax.minimum; tp.xmax = ax.maximum;
		tp.ymin = ay.minimum; tp.ymax = ay.maximum;
		ioctl(fd, EVIOCGNAME(sizeof tp.name - 1), tp.name);
		tp.name[sizeof tp.name - 1] = 0;
		tp.grabbed = excl && ioctl(fd, EVIOCGRAB, 1) == 0;
		break;
	}
	if (tp.fd < 0) { snprintf(tp.desc, sizeof tp.desc, "no touchscreen"); return 0; }
#if defined(MZ_TEST) || defined(TOUCH_TEST)
found:
#endif
	tp_reset_slots();
	snprintf(tp.desc, sizeof tp.desc, "touch %.30s (event%d) x %d..%d y %d..%d",
		 tp.name, tp.evnum, tp.xmin, tp.xmax, tp.ymin, tp.ymax);
	return 1;
}

/* Read what arrived and let the clock run; now_ms is the program's clock
 * (in self-tests its tick count in ms). Looks for a touchscreen that
 * appears later (apple_z2 loads its firmware after boot) every 2 s. */
static __attribute__((unused)) void touch_poll(int64_t now_ms, int excl)
{
	if (tp.fd < 0) {
		if (now_ms - tp.last_scan >= 2000 || now_ms < tp.last_scan) {
			tp.last_scan = now_ms;
			touch_open(excl);
		}
		if (tp.fd < 0) { tp.now = now_ms; return; }
	}
#if defined(MZ_TEST) || defined(TOUCH_TEST)
	if (tp.fake) {
		while (tp.ifake < tp.nfake) {
			const struct input_event *e = &tp.fake[tp.ifake];
			int64_t t = (int64_t)e->input_event_sec * 1000 + (int64_t)e->input_event_usec / 1000;
			if (t > now_ms) break;
			tp.now = t > tp.now ? t : tp.now;
			tp_clock(tp.now);
			touch_feed(e);
			tp.ifake++;
		}
		tp_clock(now_ms);
		return;
	}
#endif
	tp.now = now_ms;
	for (;;) {
		struct input_event ev[64];
		ssize_t n = read(tp.fd, ev, sizeof ev);
		if (n <= 0) {
			if (n < 0 && errno == ENODEV) touch_close();
			break;
		}
		for (size_t k = 0; k < (size_t)n / sizeof ev[0]; k++) touch_feed(&ev[k]);
	}
	tp_clock(now_ms);
}

/* The screen is dark (backlight.h): read what arrived and drop it, forget
 * the contact and the queued gestures. A finger still down when the screen
 * comes back is ignored until it lifts (its slot has no tracking id). */
static __attribute__((unused)) void touch_discard(int64_t now_ms)
{
#if defined(MZ_TEST) || defined(TOUCH_TEST)
	if (tp.fake) {
		while (tp.ifake < tp.nfake) {
			const struct input_event *e = &tp.fake[tp.ifake];
			if ((int64_t)e->input_event_sec * 1000 + (int64_t)e->input_event_usec / 1000 > now_ms) break;
			tp.ifake++;
			tp.n_dropped++;
		}
		goto done;
	}
#endif
	while (tp.fd >= 0) {
		struct input_event ev[64];
		ssize_t n = read(tp.fd, ev, sizeof ev);
		if (n <= 0) {
			if (n < 0 && errno == ENODEV) touch_close();
			break;
		}
		tp.n_dropped += (long)((size_t)n / sizeof ev[0]);
	}
#if defined(MZ_TEST) || defined(TOUCH_TEST)
done:
#endif
	tp_reset_slots();
	tp.qn = 0;
	tp.now = now_ms;
}

static __attribute__((unused)) int touch_present(void) { return tp.fd >= 0; }
/* A recording stands in for the device (test builds). */
static __attribute__((unused)) int tp_fake_active(void)
{
#if defined(MZ_TEST) || defined(TOUCH_TEST)
	return tp.fake != NULL;
#else
	return 0;
#endif
}
/* The descriptor to poll() for touch events; -1 for none (or a recording). */
static __attribute__((unused)) int tp_pollfd(void) { return tp_fake_active() ? -1 : tp.fd; }
/* A finger is down that is not the corner. */
static __attribute__((unused)) int touch_down(void) { return tp.down && !tp.corner; }
static __attribute__((unused)) int touch_corner_down(void) { return tp.down && tp.corner && tp.cnr_live; }
static __attribute__((unused)) int touch_in(int x0, int y0, int x1, int y1, int x, int y)
{
	return x >= x0 && x < x1 && y >= y0 && y < y1;
}

#endif
