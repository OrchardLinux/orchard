/*
 * touch_test.c -- unit tests of touch.h, run by src/test.sh (native, with
 * ASan and UBSan): the capability rule, the mapping onto letterboxed
 * canvases (1536x2048, 1080x1920 and a landscape 2048x1536 screen, with
 * the J71's own units and pixel ranges), and the gestures from event
 * streams: tap, swipe, drag, long press, the corner, a second finger, lost
 * events. Prints "touch_test: N checks passed" or the first failure.
 *
 * Build: gcc -DTOUCH_TEST -o touch_test touch_test.c
 */
#define _GNU_SOURCE
#include "touch.h"

static int checks;
#define CHECK(c, ...) do { checks++; if (!(c)) { printf("FAIL %s:%d: ", __FILE__, __LINE__); printf(__VA_ARGS__); printf("\n"); exit(1); } } while (0)

static void setbit(unsigned long *b, int n) { b[n / (8 * (int)sizeof(long))] |= 1UL << (n % (8 * (int)sizeof(long))); }

/* The view a program sets up: fb_canvas()'s whole-number scale, centred. */
static void view(int sw, int sh, int cw, int ch, int cnr_w, int cnr_h)
{
	int s = sw / cw < sh / ch ? sw / cw : sh / ch;
	if (s < 1) s = 1;
	touch_view(sw, sh, (sw - cw * s) / 2, (sh - ch * s) / 2, s, cw, ch, cnr_w, cnr_h);
}

static void range(int xmax, int ymax) { tp.xmin = 0; tp.xmax = xmax; tp.ymin = 0; tp.ymax = ymax; }

/* Screen pixel to raw units, as touchrec.py does. */
static int rawx(int px) { return (int)(((double)px + 0.5) * (tp.xmax - tp.xmin + 1) / tp.sw) + tp.xmin; }
static int rawy(int py) { return (int)(((double)py + 0.5) * (tp.ymax - tp.ymin + 1) / tp.sh) + tp.ymin; }

static void at(int sx, int sy, int ex, int ey, const char *what)
{
	int cx, cy;
	touch_map(rawx(sx), rawy(sy), &cx, &cy);
	CHECK(cx == ex && cy == ey, "%s: screen %d,%d -> canvas %d,%d, want %d,%d", what, sx, sy, cx, cy, ex, ey);
}

static int64_t clk;
static int tid = 1;
static void ev(int type, int code, int value)
{
	struct input_event e;
	memset(&e, 0, sizeof e);
	e.type = (unsigned short)type; e.code = (unsigned short)code; e.value = value;
	touch_feed(&e);
}
static void syn(void) { ev(EV_SYN, SYN_REPORT, 0); }
static void wait_ms(int ms) { for (int i = 0; i < ms; i += 10) { clk += 10; tp_clock(clk); } }
static void fdown(int slot, int sx, int sy)
{
	ev(EV_ABS, ABS_MT_SLOT, slot); ev(EV_ABS, ABS_MT_TRACKING_ID, tid++);
	ev(EV_ABS, ABS_MT_POSITION_X, rawx(sx)); ev(EV_ABS, ABS_MT_POSITION_Y, rawy(sy)); syn();
}
static void fmove(int slot, int sx, int sy)
{
	ev(EV_ABS, ABS_MT_SLOT, slot); ev(EV_ABS, ABS_MT_POSITION_X, rawx(sx)); ev(EV_ABS, ABS_MT_POSITION_Y, rawy(sy)); syn();
}
static void fup(int slot) { ev(EV_ABS, ABS_MT_SLOT, slot); ev(EV_ABS, ABS_MT_TRACKING_ID, -1); syn(); }

static int ngest(int kind)
{
	int n = 0;
	for (int i = 0; i < tp.qn; i++) n += tp.q[(tp.qh + i) % TP_QLEN].kind == kind;
	return n;
}
static TGesture take(void)
{
	TGesture g = { 0, 0, 0, 0, 0, 0 };
	CHECK(touch_next(&g), "a gesture was expected");
	return g;
}

int main(void)
{
	/* ------------------------------------------------- the capability rule */
	{
		unsigned long ev_[TP_NL(EV_CNT)], abs_[TP_NL(ABS_CNT)], prop[TP_NL(INPUT_PROP_CNT)];
		memset(ev_, 0, sizeof ev_); memset(abs_, 0, sizeof abs_); memset(prop, 0, sizeof prop);
		setbit(ev_, EV_KEY);
		CHECK(!touch_caps_ok(ev_, abs_, prop), "a keyboard is not a touchscreen");
		setbit(ev_, EV_ABS); setbit(abs_, ABS_X); setbit(abs_, ABS_Y); setbit(prop, INPUT_PROP_DIRECT);
		CHECK(!touch_caps_ok(ev_, abs_, prop), "single-touch ABS_X/Y only: not taken");
		setbit(abs_, ABS_MT_POSITION_X);
		CHECK(!touch_caps_ok(ev_, abs_, prop), "MT X without MT Y: not taken");
		setbit(abs_, ABS_MT_POSITION_Y);
		CHECK(touch_caps_ok(ev_, abs_, prop), "direct multitouch: taken");
		memset(prop, 0, sizeof prop); setbit(prop, INPUT_PROP_POINTER);
		CHECK(!touch_caps_ok(ev_, abs_, prop), "a touchpad (no INPUT_PROP_DIRECT): not taken");
		setbit(prop, INPUT_PROP_DIRECT);
		memset(ev_, 0, sizeof ev_);
		CHECK(!touch_caps_ok(ev_, abs_, prop), "no EV_ABS: not taken");
	}

	/* ------------------------------------------------------ the mapping */
	/* iPad (J85, J81: pixels) 1536x2048, ARCADE's 270x480: x4 at 228,64 */
	range(1535, 2047);
	view(1536, 2048, 270, 480, 0, 0);
	CHECK(tp.scale == 4 && tp.ox == 228 && tp.oy == 64, "1536x2048 arcade view: x%d at %d,%d", tp.scale, tp.ox, tp.oy);
	at(228, 64, 0, 0, "arcade top left");
	at(228 + 1079, 64 + 1919, 269, 479, "arcade bottom right");
	at(768, 1024, 135, 240, "arcade centre");
	at(100, 30, -32, -9, "letterbox left/top");
	at(1500, 2040, 318, 494, "letterbox right/bottom");
	/* the J71's units: 14621 x 19539 over the same panel */
	range(14620, 19538);
	at(228, 64, 0, 0, "J71 units, top left");
	at(768, 1024, 135, 240, "J71 units, centre");
	at(228 + 1079, 64 + 1919, 269, 479, "J71 units, bottom right");
	/* HEDGEWICK's 180x320 on the iPad: x6 at 228,64 */
	range(1535, 2047);
	view(1536, 2048, 180, 320, 30, 26);
	CHECK(tp.scale == 6 && tp.ox == 228 && tp.oy == 64, "1536x2048 hedgewick view: x%d at %d,%d", tp.scale, tp.ox, tp.oy);
	at(228 + 6 * 90 + 3, 64 + 6 * 160 + 5, 90, 160, "hedgewick centre");
	at(0, 0, -38, -11, "hedgewick screen corner (letterbox)");
	/* N66 1080x1920: both canvases fill the screen */
	range(1079, 1919);
	view(1080, 1920, 270, 480, 0, 0);
	CHECK(tp.scale == 4 && tp.ox == 0 && tp.oy == 0, "1080x1920 arcade view");
	at(0, 0, 0, 0, "N66 top left");
	at(1079, 1919, 269, 479, "N66 bottom right");
	at(540, 960, 135, 240, "N66 centre");
	view(1080, 1920, 180, 320, 30, 26);
	at(1079, 1919, 179, 319, "N66 hedgewick bottom right");
	/* landscape 2048x1536 (a touch range in pixels too): x3 at 619,48 */
	range(2047, 1535);
	view(2048, 1536, 270, 480, 0, 0);
	CHECK(tp.scale == 3 && tp.ox == 619 && tp.oy == 48, "landscape arcade view: x%d at %d,%d", tp.scale, tp.ox, tp.oy);
	at(619, 48, 0, 0, "landscape top left");
	at(619 + 809, 48 + 1439, 269, 479, "landscape bottom right");
	at(10, 10, -203, -13, "landscape letterbox");
	/* a range that does not start at 0 */
	tp.xmin = 100; tp.xmax = 1635; tp.ymin = 50; tp.ymax = 2097;
	view(1536, 2048, 270, 480, 0, 0);
	at(228, 64, 0, 0, "offset range, top left");
	at(1307, 1983, 269, 479, "offset range, bottom right");

	/* ------------------------------------------------------- gestures */
	range(1535, 2047);
	view(1536, 2048, 270, 480, 45, 40);
	tp_reset_slots();
	/* tap */
	fdown(0, 228 + 4 * 100, 64 + 4 * 200); wait_ms(80); fup(0);
	{ TGesture g = take(); CHECK(g.kind == TG_TAP && g.x == 100 && g.y == 200, "tap: kind %d at %d,%d", g.kind, g.x, g.y); }
	CHECK(!touch_next(&(TGesture){ 0, 0, 0, 0, 0, 0 }), "one gesture for a tap");
	/* a slow press (over TAP_MS, under LONG_MS): nothing */
	fdown(0, 768, 1024); wait_ms(600); fup(0);
	CHECK(tp.qn == 0, "a 600 ms press is neither a tap nor a long press (%d queued)", tp.qn);
	/* a small wobble stays a tap */
	fdown(0, 768, 1024); wait_ms(20); fmove(0, 768 + 4 * 3, 1024 - 4 * 2); wait_ms(30); fup(0);
	CHECK(take().kind == TG_TAP, "a 3-unit wobble is still a tap");
	/* swipes in four directions */
	{
		static const int d[4][2] = { { 0, -1 }, { 1, 0 }, { 0, 1 }, { -1, 0 } };
		for (int k = 0; k < 4; k++) {
			int x = 768, y = 1024;
			fdown(0, x, y);
			for (int i = 1; i <= 8; i++) { wait_ms(10); fmove(0, x + d[k][0] * i * 4 * 5, y + d[k][1] * i * 4 * 5); }
			fup(0);
			{
				TGesture g = take();
				CHECK(g.kind == TG_SWIPE && g.dir == k, "swipe %d: kind %d dir %d", k, g.kind, g.dir);
				CHECK(g.x0 == 135 && g.y0 == 240, "swipe %d starts at %d,%d", k, g.x0, g.y0);
			}
			CHECK(tp.qn == 0, "swipe %d: one gesture, no tap (%d queued)", k, tp.qn);
		}
	}
	/* a diagonal goes by the larger axis */
	fdown(0, 768, 1024); wait_ms(10); fmove(0, 768 + 4 * 40, 1024 + 4 * 25); fup(0);
	{ TGesture g = take(); CHECK(g.kind == TG_SWIPE && g.dir == TD_RIGHT, "diagonal: dir %d", g.dir); }
	/* a long drag: one swipe per swipe length, and the contact follows */
	fdown(0, 300, 1024);
	for (int i = 1; i <= 50; i++) { wait_ms(16); fmove(0, 300 + i * 20, 1024); CHECK(touch_down(), "down while dragging"); }
	CHECK(tp.x == (300 + 50 * 20 - 228) / 4 && tp.y == 240, "drag follows: %d,%d", tp.x, tp.y);
	CHECK(ngest(TG_SWIPE) == (1000 / 4) / tp.swipe, "a 250-unit drag: %d swipes of %d", ngest(TG_SWIPE), tp.swipe);
	fup(0);
	CHECK(!touch_down() && ngest(TG_TAP) == 0, "a drag ends without a tap");
	touch_flush();
	/* long press: once, while held, at 700 ms; no tap after it */
	fdown(0, 900, 900); wait_ms(690);
	CHECK(tp.qn == 0, "no long press before %d ms", LONG_MS);
	wait_ms(20);
	{ TGesture g = take(); CHECK(g.kind == TG_LONG && g.x == (900 - 228) / 4, "long press: kind %d x %d", g.kind, g.x); }
	wait_ms(1500); fup(0);
	CHECK(tp.qn == 0, "one long press, no tap after it");
	/* a press that moves first is not a long press */
	fdown(0, 900, 900); wait_ms(100); fmove(0, 900 + 4 * 30, 900); wait_ms(1000); fup(0);
	CHECK(ngest(TG_LONG) == 0, "a moved press is not a long press");
	touch_flush();
	/* the corner: 45x40 units, the letterbox beyond it included */
	fdown(0, 5, 5);
	CHECK(touch_corner_down() && !touch_down(), "the screen corner (letterbox) is the corner button");
	wait_ms(2500);
	CHECK(touch_corner_down() && tp.qn == 0, "a held corner makes no gestures");
	fup(0);
	CHECK(!touch_corner_down() && tp.qn == 0, "corner let go: no tap");
	fdown(0, 228 + 4 * 40, 64 + 4 * 35);
	CHECK(touch_corner_down(), "inside the corner box on the canvas");
	fmove(0, 228 + 4 * 120, 64 + 4 * 35);
	CHECK(!touch_corner_down() && !touch_down() && tp.qn == 0, "dragged out of the corner: neither button nor gestures");
	fup(0);
	fdown(0, 228 + 4 * 50, 64 + 4 * 45); wait_ms(50); fup(0);
	CHECK(take().kind == TG_TAP, "just outside the corner: a tap");
	/* two fingers: the first is followed, the second ignored */
	fdown(0, 768, 1024); wait_ms(30); fdown(1, 300, 300); wait_ms(30); fmove(1, 300, 900); wait_ms(10); fup(1); wait_ms(10); fup(0);
	{ TGesture g = take(); CHECK(g.kind == TG_TAP && g.x == 135, "two fingers: the first one's tap (kind %d x %d)", g.kind, g.x); }
	CHECK(tp.qn == 0, "the second finger made nothing");
	/* the first lifts, the second stays: it becomes a new contact */
	fdown(0, 768, 1024); wait_ms(30); fdown(1, 400, 400); wait_ms(30); fup(0);
	CHECK(touch_down() && tp.x == (400 - 228) / 4, "the second finger takes over at %d,%d", tp.x, tp.y);
	wait_ms(30); fup(1);
	touch_flush();
	/* lost events: the contact is dropped, the next finger starts afresh */
	fdown(0, 768, 1024);
	ev(EV_SYN, SYN_DROPPED, 0);
	CHECK(!touch_down(), "SYN_DROPPED drops the contact");
	fup(0);
	CHECK(tp.qn == 0, "nothing from a dropped contact");
	fdown(0, 768, 1024); wait_ms(50); fup(0);
	CHECK(take().kind == TG_TAP, "a tap after SYN_DROPPED");
	/* a finger already down when the device was opened is ignored until it lifts */
	tp_reset_slots();
	ev(EV_ABS, ABS_MT_SLOT, 0); ev(EV_ABS, ABS_MT_POSITION_X, rawx(700)); syn();
	CHECK(!tp.down, "position updates without a tracking id: no contact");
	/* the queue holds TP_QLEN and drops the rest */
	for (int i = 0; i < TP_QLEN + 5; i++) { fdown(0, 768, 1024); wait_ms(20); fup(0); }
	CHECK(tp.qn == TP_QLEN, "queue full at %d", tp.qn);
	touch_flush();

	printf("touch_test: %d checks passed\n", checks);
	return 0;
}
