/*
 * glint.c -- GLINT, a tilt maze for the iPhone 6s Plus (N66) under Linux.
 *
 * Tilt the phone: gravity, read from the accelerometer, rolls a glass marble
 * over a wooden board, between the walls, past the holes, into the brass cup.
 * Six hand-made boards, a timer, best times kept in RAM (/run/glint.best).
 *
 *   Home            start / pause / resume / next board
 *   Power           screen off (backlight.h): Home or Power lights it again,
 *                   the board paused. Without volume keys: Power held 0.8 s.
 *   hold Home 2 s   back to the title (on the title, with --from-picker: exit)
 *   Volume keys     title: choose the board; paused: Vol+ recalibrate,
 *                   Vol- restart the board; both together: sound on/off.
 *                   Without a motion sensor: Vol+ / Vol- turn gravity
 *                   counter-clockwise / clockwise while you play.
 *   Touchscreen (touch.h; found by its capabilities):
 *     tap           title: calibrate and start; the top bar in play: pause;
 *                   paused: resume; in the cup: next board
 *     swipe < >     title: choose the board
 *     finger down   without a motion sensor: gravity pulls towards it
 *     top left corner, held: the Home button (2 s: title; on the title
 *                   with --from-picker: back to the arcade)
 *
 * Motion sensor: the IIO device named "apple-aop-accel" (the MPU6800 behind
 * the AOP, driver apple_aop_accel). Only read: in_accel_{x,y,z}_raw are
 * pread() once per frame, in_accel_scale and in_accel_mount_matrix once.
 * Nothing is written unless --rate HZ is given (then, once, HZ goes into the
 * device's standard IIO sampling_frequency attribute; the driver's default
 * is 10 Hz, its range 1..100 Hz). The raw vector is gravity on the device's
 * axes (x right, y up, z out of the screen; measured on the N66: upright
 * y = -1 g, screen up z = -1 g; the mount matrix diag(1,1,-1) only flips z
 * for iio-sensor-proxy's convention, and this program flips it back).
 * Pressing Home on the title takes the pose of that moment as "level".
 * No sensor, a sensor without samples, frozen values or read errors: the
 * game says "NO MOTION SENSOR" and the volume keys steer; the reason goes to
 * the log, and the sensor is picked up again when it (re)appears.
 *
 * Screen: /dev/fb0 (simpledrm's fbdev), geometry and bitfields read at run
 * time, 32 and 16 bpp; tty1 in KD_GRAPHICS while drawing and back in KD_TEXT
 * on every way out (normal exit, SIGTERM/SIGINT, fatal signals, atexit;
 * `glint --restore-tty /dev/tty1` after a SIGKILL). The board is rendered
 * once per level; per frame only the rectangles that changed are redrawn and
 * sent (the marble, the clock ten times a second), with pwrite(): see
 * fb_present() for why not through the mapping.
 *
 * Sound: optional, the same bare-ALSA code and rules as VOIDMOTHS
 * (game-n66/src/voidmoths.c): the "Speaker" card only, amp -20 dB (never
 * above -6 dB, read back), mix limited to -6 dBFS, silent on any problem.
 *
 * Physics: 120 Hz fixed step, units of one board cell; see the constants.
 *
 * usage: glint [--tty PATH] [--fb PATH] [--fb-mmap] [--iio DIR|none] [--rate HZ]
 *              [--axes a,b,c] [--hi PATH|none] [--notice PATH|none]
 *              [--from-picker] [--swap] [--no-grab] [--mute] [--vol DB]
 *              [--level N] [--fps 60|30|20]
 *   --fps: how often the screen is redrawn (the physics stay at 120 Hz).
 *        glint --restore-tty PATH
 *        glint --selftest                       (physics and sensor maths)
 *        glint --bot FRAMES [--fakefb WxHxBPP[:bgr]] [--dump DIR]
 *              [--bot-lose FRAME] [--no-pilot] ... (headless self-test;
 *              --no-pilot: only $FAKE_TOUCH plays, -DTOUCH_TEST builds)
 * Exit status: 0 normal (signal, picker exit), 1 errors, 4 no usable
 * framebuffer.
 *
 * Build: aarch64-linux-gnu-gcc -std=gnu11 -static -O2 -o glint glint.c -lm
 */
#define _GNU_SOURCE
#include <dirent.h>
#include <errno.h>
#include <fcntl.h>
#include <limits.h>
#include <linux/fb.h>
#include <linux/input.h>
#include <linux/kd.h>
#include <math.h>
#include <signal.h>
#include <stdarg.h>
#include <stdint.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <sys/ioctl.h>
#include <sys/mman.h>
#include <sys/resource.h>
#include <sys/stat.h>
#include <sys/types.h>
#include <time.h>
#include <unistd.h>
#include <sound/asound.h>
#include <sound/tlv.h>
#include "touch.h"
#include "backlight.h"

#define HZ      60              /* frames per second */
#define SUB     2               /* physics steps per frame: 120 Hz */
#define PDT     (1.0f / (float)(HZ * SUB))
#define COLS    9
#define ROWS    14
#define MAXHOLE 40
#define NLEV    6

/* Physics, in board cells and seconds. */
#define BALL_R   0.232f         /* marble radius */
#define WALL_H   0.0625f        /* half the wall thickness */
#define HOLE_R   0.30f          /* hole radius as drawn */
#define HOLE_IN  0.26f          /* the marble falls when its centre is this close */
#define GOAL_IN  0.22f
#define ACC      15.0f          /* acceleration at full tilt (tilt 1 = TILT_FULL degrees) */
#define DRAG     0.8f           /* linear drag, 1/s */
#define ROLL_MU  0.5f           /* rolling resistance: also the tilt dead zone */
#define REST     0.38f          /* bounce off walls */
#define VMAX     11.0f          /* speed cap: 0.092 cells a step, wall + marble is 0.59 */
#define TILT_FULL 22.0f         /* degrees of tilt for full acceleration */

#define HOLD_SHOW (HZ / 2)
#define HOLD_QUIT (2 * HZ)

static long tick;               /* frames since start */
static int bot;                 /* headless self-test */
static int64_t t_start;

static int64_t now_ns(void)
{
	struct timespec ts;
	clock_gettime(CLOCK_MONOTONIC, &ts);
	return (int64_t)ts.tv_sec * 1000000000LL + ts.tv_nsec;
}

/* The program's clock in ms (touch.h, backlight.h): real time, or the
 * self-test's frames. */
static int64_t gl_clock_ms(void) { return bot ? (int64_t)tick * 1000 / HZ : now_ns() / 1000000; }

/* One line to stderr (/run/glint.log on the phone), with seconds since start. */
__attribute__((format(printf, 1, 2)))
static void logmsg(const char *fmt, ...)
{
	va_list ap;
	double t = bot ? (double)tick / HZ : (double)(now_ns() - t_start) / 1e9;
	fprintf(stderr, "glint %7.1f: ", t);
	va_start(ap, fmt);
	vfprintf(stderr, fmt, ap);
	va_end(ap);
	fputc('\n', stderr);
	fflush(stderr);
}

static float clampf(float v, float lo, float hi) { return v < lo ? lo : v > hi ? hi : v; }

static uint32_t rs = 2463534242u;
static uint32_t rnd(void) { rs ^= rs << 13; rs ^= rs >> 17; rs ^= rs << 5; return rs; }
static float frand(void) { return (float)(rnd() & 0xffffff) / 16777216.0f; }

/* ------------------------------------------------------------------ levels */
/*
 * A board is COLS x ROWS cells with thin walls on cell edges. The text form:
 *   Sx,y  start cell          Gx,y  goal cell          Ox,y  a hole
 *   Hy:a-b  a wall along the top edge of row y, over columns a..b
 *   Vx:a-b  a wall along the left edge of column x, over rows a..b
 * The outer frame is always there. A hole fills its cell: a walled corridor
 * with a hole in it is closed, so holes sit in halls and beside the route.
 */
typedef struct {
	const char *name, *def;
} LevelDef;

static const LevelDef level_defs[NLEV] = {
	{ "FIRST ROLL",
	  "S0,0 G8,13 H4:0-6 H9:2-8 O2,2 O4,6 O6,11" },
	{ "DOGLEG",
	  "S1,13 G7,0 V3:3-13 V6:0-10 O0,0 O1,6 O4,6 O5,13 O7,6 O8,13" },
	{ "SWITCHBACK",
	  "S0,0 G8,13 H2:0-6 H4:2-8 H6:0-6 H8:2-8 H10:0-6 H12:2-8 "
	  "O4,0 O8,0 O5,3 O0,2 O3,4 O6,5 O4,7 O2,6 O3,9 O5,8 O6,10 O3,11 O4,12 O6,13" },
	{ "PEPPER",
	  "S4,13 G4,0 H4:3-5 H10:3-5 O3,1 O5,1 O1,2 O7,2 O2,5 O6,5 O0,6 O8,6 O4,7 "
	  "O1,9 O7,9 O3,11 O5,11" },
	{ "THE SIEVE",
	  "S0,0 G8,13 H4:0-6 H8:2-8 H12:0-6 "
	  "O1,1 O3,1 O5,1 O7,1 O1,3 O3,3 O5,3 O7,3 O1,5 O3,5 O5,5 O7,5 "
	  "O1,7 O3,7 O5,7 O7,7 O1,9 O3,9 O5,9 O7,9 O1,11 O3,11 O5,11 O7,11" },
	{ "NARROWS",
	  "S0,0 G0,12 V1:0-2 V2:1-3 V3:0-2 V4:1-3 V5:0-2 V6:1-3 V7:0-2 V8:1-3 H4:0-7 "
	  "O6,4 O7,6 O5,6 O3,5 O4,7 O2,7 O1,5 O6,8 O3,8 H9:1-8 "
	  "H10:0-7 H11:1-8 H12:0-7 O6,13 O4,12 O2,13 O0,13" },
};
static const LevelDef tray_def = { "TRAY", "S4,11" };

typedef struct {
	const char *name;
	uint8_t h[ROWS + 1][COLS];      /* wall on the top edge of cell (x, y) */
	uint8_t v[ROWS][COLS + 1];      /* wall on the left edge of cell (x, y) */
	uint8_t hole_at[ROWS][COLS];
	int nh, has_goal;
	float hx[MAXHOLE], hy[MAXHOLE];
	int sx, sy, gx, gy;
} Level;

static Level levels[NLEV], tray;

static int level_parse(const LevelDef *d, Level *l)
{
	const char *p = d->def;
	int have_s = 0;
	memset(l, 0, sizeof *l);
	l->name = d->name;
	for (int x = 0; x < COLS; x++) l->h[0][x] = l->h[ROWS][x] = 1;
	for (int y = 0; y < ROWS; y++) l->v[y][0] = l->v[y][COLS] = 1;
	while (*p) {
		int a, b, c, n = 0;
		char k;
		while (*p == ' ') p++;
		if (!*p) break;
		k = *p++;
		if (k == 'S' || k == 'G' || k == 'O') {
			if (sscanf(p, "%d,%d%n", &a, &b, &n) != 2 || a < 0 || a >= COLS || b < 0 || b >= ROWS) return -1;
			if (k == 'S') { l->sx = a; l->sy = b; have_s++; }
			else if (k == 'G') { l->gx = a; l->gy = b; l->has_goal++; }
			else {
				if (l->nh >= MAXHOLE || l->hole_at[b][a]) return -1;
				l->hole_at[b][a] = 1;
				l->hx[l->nh] = (float)a + 0.5f; l->hy[l->nh] = (float)b + 0.5f;
				l->nh++;
			}
		} else if (k == 'H' || k == 'V') {
			if (sscanf(p, "%d:%d-%d%n", &a, &b, &c, &n) != 3 || b > c || b < 0) return -1;
			if (k == 'H') {
				if (a < 0 || a > ROWS || c >= COLS) return -1;
				for (int x = b; x <= c; x++) l->h[a][x] = 1;
			} else {
				if (a < 0 || a > COLS || c >= ROWS) return -1;
				for (int y = b; y <= c; y++) l->v[y][a] = 1;
			}
		} else return -1;
		p += n;
	}
	if (have_s != 1 || l->has_goal > 1) return -1;
	if (l->hole_at[l->sy][l->sx] || (l->has_goal && l->hole_at[l->gy][l->gx])) return -1;
	return 0;
}

/* May the marble roll from cell (x, y) one cell on in direction d (0 right, 1 down, 2 left, 3 up)? */
static int cell_open(const Level *l, int x, int y, int d)
{
	switch (d) {
	case 0: return x + 1 < COLS && !l->v[y][x + 1];
	case 1: return y + 1 < ROWS && !l->h[y + 1][x];
	case 2: return x > 0 && !l->v[y][x];
	default: return y > 0 && !l->h[y][x];
	}
}

static const int dir_x[4] = { 1, 0, -1, 0 }, dir_y[4] = { 0, 1, 0, -1 };

/* Shortest way over hole-free cells from (sx, sy) to (tx, ty): the cells in
 * order in path[], their number as the result (0: no way). */
static int level_path(const Level *l, int sx, int sy, int tx, int ty, int path[][2])
{
	int prev[ROWS][COLS], q[ROWS * COLS], qh = 0, qt = 0, n = 0, c;
	for (int y = 0; y < ROWS; y++) for (int x = 0; x < COLS; x++) prev[y][x] = -2;
	prev[sy][sx] = -1;
	q[qt++] = sy * COLS + sx;
	while (qh < qt) {
		int cur = q[qh++], x = cur % COLS, y = cur / COLS;
		if (x == tx && y == ty) break;
		for (int d = 0; d < 4; d++) {
			int nx = x + dir_x[d], ny = y + dir_y[d];
			if (!cell_open(l, x, y, d) || prev[ny][nx] != -2 || l->hole_at[ny][nx]) continue;
			prev[ny][nx] = cur;
			q[qt++] = ny * COLS + nx;
		}
	}
	if (prev[ty][tx] == -2) return 0;
	for (c = ty * COLS + tx; c >= 0; c = prev[c / COLS][c % COLS]) n++;
	c = ty * COLS + tx;
	for (int i = n - 1; i >= 0; i--) {
		path[i][0] = c % COLS; path[i][1] = c / COLS;
		c = prev[c / COLS][c % COLS];
	}
	return n;
}

static int levels_init(void)
{
	int path[ROWS * COLS][2];
	if (level_parse(&tray_def, &tray) < 0) return -1;
	for (int i = 0; i < NLEV; i++) {
		if (level_parse(&level_defs[i], &levels[i]) < 0 || !levels[i].has_goal) {
			logmsg("level %d (%s): bad definition", i + 1, level_defs[i].name);
			return -1;
		}
		if (!level_path(&levels[i], levels[i].sx, levels[i].sy, levels[i].gx, levels[i].gy, path)) {
			logmsg("level %d (%s): no way from start to goal", i + 1, level_defs[i].name);
			return -1;
		}
	}
	return 0;
}

/* ----------------------------------------------------------------- physics */

typedef struct { float x, y, vx, vy; } Ball;
enum { EV_NONE, EV_HOLE, EV_GOAL };

static void ball_reset(const Level *l, Ball *b)
{
	b->x = (float)l->sx + 0.5f; b->y = (float)l->sy + 0.5f;
	b->vx = b->vy = 0.0f;
}

/* The marble against the wall segment (ax, ay)-(bx, by); (ox, oy) is where it
 * was before this step. Returns the speed into the wall, 0 if no contact. */
static float collide_seg(Ball *b, float ax, float ay, float bx, float by, float ox, float oy)
{
	const float lim = BALL_R + WALL_H;
	float ex = bx - ax, ey = by - ay, t, px, py, dx, dy, d, vn;
	t = ((b->x - ax) * ex + (b->y - ay) * ey) / (ex * ex + ey * ey);
	t = clampf(t, 0.0f, 1.0f);
	px = ax + t * ex; py = ay + t * ey;
	dx = b->x - px; dy = b->y - py;
	d = sqrtf(dx * dx + dy * dy);
	if (d >= lim) return 0.0f;
	if (d < 1e-5f) {
		/* dead centre on the wall: back out the way it came */
		dx = ox - px; dy = oy - py;
		d = sqrtf(dx * dx + dy * dy);
		if (d < 1e-5f) { dx = -ey; dy = ex; d = sqrtf(dx * dx + dy * dy); }
	}
	dx /= d; dy /= d;
	b->x = px + dx * lim; b->y = py + dy * lim;
	vn = b->vx * dx + b->vy * dy;
	if (vn >= 0.0f) return 0.0f;
	b->vx -= (1.0f + REST) * vn * dx;
	b->vy -= (1.0f + REST) * vn * dy;
	return -vn;
}

/* One 120 Hz step under the acceleration (ax, ay). hazards: holes and goal
 * count. *impact: the hardest wall hit of the step; *which: the hole. */
static int phys_step(const Level *l, Ball *b, float ax, float ay, int hazards, float *impact, int *which)
{
	float sp, ox = b->x, oy = b->y, drag = 1.0f - DRAG * PDT, amag = sqrtf(ax * ax + ay * ay);
	*impact = 0.0f;
	sp = sqrtf(b->vx * b->vx + b->vy * b->vy);
	if (amag <= ROLL_MU && sp <= 2.0f * ROLL_MU * PDT) {
		b->vx = b->vy = 0.0f;                   /* at rest and not enough tilt to start */
	} else {
		b->vx += ax * PDT; b->vy += ay * PDT;
		sp = sqrtf(b->vx * b->vx + b->vy * b->vy);
		if (sp > 1e-6f) {
			float f = ROLL_MU * PDT;            /* rolling resistance against the motion */
			float k = sp > f ? (sp - f) / sp : 0.0f;
			b->vx *= k * drag; b->vy *= k * drag;
		}
		sp = sqrtf(b->vx * b->vx + b->vy * b->vy);
		if (sp > VMAX) { b->vx *= VMAX / sp; b->vy *= VMAX / sp; }
	}
	b->x += b->vx * PDT; b->y += b->vy * PDT;
	for (int pass = 0; pass < 3; pass++) {
		int cx = (int)floorf(b->x), cy = (int)floorf(b->y), hit = 0;
		for (int y = cy - 1; y <= cy + 2; y++)
			for (int x = cx - 1; x <= cx + 1; x++) {
				float s;
				if (x < 0 || x >= COLS || y < 0 || y > ROWS || !l->h[y][x]) continue;
				s = collide_seg(b, (float)x, (float)y, (float)x + 1.0f, (float)y, ox, oy);
				if (s > *impact) *impact = s;
				hit |= s > 0.0f;
			}
		for (int y = cy - 1; y <= cy + 1; y++)
			for (int x = cx - 1; x <= cx + 2; x++) {
				float s;
				if (x < 0 || x > COLS || y < 0 || y >= ROWS || !l->v[y][x]) continue;
				s = collide_seg(b, (float)x, (float)y, (float)x, (float)y + 1.0f, ox, oy);
				if (s > *impact) *impact = s;
				hit |= s > 0.0f;
			}
		/* never outside the frame, whatever happened */
		b->x = clampf(b->x, BALL_R + WALL_H, (float)COLS - BALL_R - WALL_H);
		b->y = clampf(b->y, BALL_R + WALL_H, (float)ROWS - BALL_R - WALL_H);
		if (!hit) break;
	}
	if (!hazards) return EV_NONE;
	for (int i = 0; i < l->nh; i++) {
		float dx = b->x - l->hx[i], dy = b->y - l->hy[i];
		if (dx * dx + dy * dy < HOLE_IN * HOLE_IN) { *which = i; return EV_HOLE; }
	}
	if (l->has_goal) {
		float dx = b->x - ((float)l->gx + 0.5f), dy = b->y - ((float)l->gy + 0.5f);
		if (dx * dx + dy * dy < GOAL_IN * GOAL_IN) return EV_GOAL;
	}
	return EV_NONE;
}

/* The marble's orientation (object to view, row-major): rolled by (dx, dy). */
static float rot[9] = { 1, 0, 0, 0, 1, 0, 0, 0, 1 };

static void rot_roll(float dx, float dy)
{
	float d = sqrtf(dx * dx + dy * dy), a, c, s, kx, ky, q[9], r[9];
	if (d < 1e-7f) return;
	a = d / BALL_R;
	c = cosf(a); s = sinf(a);
	kx = -dy / d; ky = dx / d;              /* axis in the board's plane, across the motion */
	q[0] = c + kx * kx * (1 - c); q[1] = kx * ky * (1 - c); q[2] = ky * s;
	q[3] = kx * ky * (1 - c); q[4] = c + ky * ky * (1 - c); q[5] = -kx * s;
	q[6] = -ky * s; q[7] = kx * s; q[8] = c;
	for (int i = 0; i < 3; i++)
		for (int j = 0; j < 3; j++)
			r[3 * i + j] = q[3 * i] * rot[j] + q[3 * i + 1] * rot[3 + j] + q[3 * i + 2] * rot[6 + j];
	memcpy(rot, r, sizeof rot);
}

static void rot_fix(void)   /* keep it a rotation: Gram-Schmidt on the rows */
{
	float *a = rot, *b = rot + 3, *c = rot + 6, n, d;
	n = sqrtf(a[0] * a[0] + a[1] * a[1] + a[2] * a[2]);
	if (n < 1e-6f) { static const float id[9] = { 1, 0, 0, 0, 1, 0, 0, 0, 1 }; memcpy(rot, id, sizeof id); return; }
	for (int i = 0; i < 3; i++) a[i] /= n;
	d = a[0] * b[0] + a[1] * b[1] + a[2] * b[2];
	for (int i = 0; i < 3; i++) b[i] -= d * a[i];
	n = sqrtf(b[0] * b[0] + b[1] * b[1] + b[2] * b[2]);
	if (n < 1e-6f) { static const float id[9] = { 1, 0, 0, 0, 1, 0, 0, 0, 1 }; memcpy(rot, id, sizeof id); return; }
	for (int i = 0; i < 3; i++) b[i] /= n;
	c[0] = a[1] * b[2] - a[2] * b[1];
	c[1] = a[2] * b[0] - a[0] * b[2];
	c[2] = a[0] * b[1] - a[1] * b[0];
}

/* ------------------------------------------------------------ motion sensor */

enum { SN_OFF, SN_SEARCH, SN_WAIT, SN_OK, SN_STALL };
static struct {
	int state, fd[3], logged_none, logged_wait, nofilter;
	char dir[300];
	float scale, M[9];
	long next_scan, since, last_change, losses, bad;
	int last_raw[3], have;
	float G[3];                 /* filtered gravity, in g, device axes (x right, y up, z out) */
	float hist[32][3];
	int hn, hi;
	int changes;
	long rate_t0;
	float rate_hz;
	float n[3], ex[3], ey[3];   /* the calibrated "level" pose */
	int calibrated;
} sn = { .state = SN_SEARCH, .fd = { -1, -1, -1 } };

static const char *iio_root = "/sys/bus/iio/devices";
static const char *iio_name = "apple-aop-accel";
static int sn_rate;             /* --rate: Hz to ask for, 0: leave it */
static int sn_axes[3];          /* --axes: 0 = use the mount matrix; else +-1..3 */

static float dot3(const float *a, const float *b) { return a[0] * b[0] + a[1] * b[1] + a[2] * b[2]; }
static int norm3(float *a)
{
	float n = sqrtf(dot3(a, a));
	if (n < 1e-6f) return -1;
	a[0] /= n; a[1] /= n; a[2] /= n;
	return 0;
}

/* The pose with gravity along n (device axes) becomes "level": ex and ey are
 * the device's x and y axes laid into the plane across n. */
static void sn_set_level(const float *nin)
{
	static const float X[3] = { 1, 0, 0 }, Y[3] = { 0, 1, 0 }, Z[3] = { 0, 0, -1 };
	float n[3] = { nin[0], nin[1], nin[2] }, ex[3], ey[3], d;
	if (norm3(n) < 0) { n[0] = 0; n[1] = 0; n[2] = -1; }
	d = dot3(X, n);
	for (int i = 0; i < 3; i++) ex[i] = X[i] - d * n[i];
	if (sqrtf(dot3(ex, ex)) < 0.05f) {           /* held on its side: x is along gravity */
		d = dot3(Z, n);
		for (int i = 0; i < 3; i++) ex[i] = Z[i] - d * n[i];
	}
	norm3(ex);
	d = dot3(Y, n);
	for (int i = 0; i < 3; i++) ey[i] = Y[i] - d * n[i] - dot3(Y, ex) * ex[i];
	if (sqrtf(dot3(ey, ey)) < 0.05f) {           /* held upright: lean back = roll up the screen */
		d = dot3(Z, n);
		for (int i = 0; i < 3; i++) ey[i] = Z[i] - d * n[i] - dot3(Z, ex) * ex[i];
	}
	norm3(ey);
	memcpy(sn.n, n, sizeof n); memcpy(sn.ex, ex, sizeof ex); memcpy(sn.ey, ey, sizeof ey);
}

/* One raw sample (the three in_accel_*_raw values) into the filter. */
static void sn_feed(const int raw[3])
{
	float v[3], g[3], m;
	if (sn_axes[0]) {
		for (int i = 0; i < 3; i++) {
			int a = sn_axes[i];
			g[i] = (float)raw[(a < 0 ? -a : a) - 1] * (a < 0 ? -1.0f : 1.0f) * sn.scale / 9.80665f;
		}
	} else {
		for (int i = 0; i < 3; i++)
			v[i] = (sn.M[3 * i] * (float)raw[0] + sn.M[3 * i + 1] * (float)raw[1] + sn.M[3 * i + 2] * (float)raw[2]) *
			       sn.scale / 9.80665f;
		/* after the mount matrix: upright y = -1, screen up z = +1; gravity's z is the other way */
		g[0] = v[0]; g[1] = v[1]; g[2] = -v[2];
	}
	m = sqrtf(dot3(g, g));
	if (!(m > 0.35f && m < 3.0f)) { sn.bad++; return; }   /* free fall, a knock, or nonsense */
	if (!sn.have || sn.nofilter) memcpy(sn.G, g, sizeof g);
	else {
		const float k = 1.0f - expf(-1.0f / ((float)HZ * 0.07f));   /* 70 ms low-pass */
		for (int i = 0; i < 3; i++) sn.G[i] += (g[i] - sn.G[i]) * k;
	}
	sn.have = 1;
	memcpy(sn.hist[sn.hi], g, sizeof g);
	sn.hi = (sn.hi + 1) % 32;
	if (sn.hn < 32) sn.hn++;
}

static int sn_usable(void) { return sn.state == SN_OK && sn.have; }

/* Tilt against the calibrated pose: (tx, ty) on the screen (x right, y down),
 * length at most 1 (= TILT_FULL degrees or more). */
static void sn_tilt(float *tx, float *ty)
{
	float g[3] = { sn.G[0], sn.G[1], sn.G[2] }, x, y, m;
	const float gain = 1.0f / sinf(TILT_FULL * (float)M_PI / 180.0f);
	*tx = *ty = 0.0f;
	if (norm3(g) < 0) return;
	x = dot3(g, sn.ex) * gain; y = -dot3(g, sn.ey) * gain;
	m = sqrtf(x * x + y * y);
	if (m > 1.0f) { x /= m; y /= m; }
	*tx = x; *ty = y;
}

/* The pose of the last half second becomes "level". */
static void sn_calibrate(void)
{
	float n[3] = { 0, 0, 0 };
	if (!sn_usable() || sn.hn < 1) return;
	for (int i = 0; i < sn.hn; i++) for (int k = 0; k < 3; k++) n[k] += sn.hist[i][k];
	sn_set_level(n);
	sn.calibrated = 1;
	logmsg("sensor: calibrated, level is gravity (%.2f, %.2f, %.2f) g, %.0f degrees from flat",
	       (double)(n[0] / (float)sn.hn), (double)(n[1] / (float)sn.hn), (double)(n[2] / (float)sn.hn),
	       (double)(acosf(clampf(-sn.n[2], -1.0f, 1.0f)) * 180.0f / (float)M_PI));
}

static int read_small(const char *dir, const char *name, char *buf, size_t n)
{
	char p[420];
	ssize_t r;
	int fd;
	snprintf(p, sizeof p, "%s/%s", dir, name);
	fd = open(p, O_RDONLY | O_CLOEXEC | O_NONBLOCK);
	if (fd < 0) return -1;
	r = read(fd, buf, n - 1);
	close(fd);
	if (r < 0) return -1;
	buf[r] = 0;
	while (r > 0 && (buf[r - 1] == '\n' || buf[r - 1] == ' ')) buf[--r] = 0;
	return 0;
}

static void sn_close(void)
{
	for (int i = 0; i < 3; i++) { if (sn.fd[i] >= 0) close(sn.fd[i]); sn.fd[i] = -1; }
	sn.have = 0; sn.hn = sn.hi = 0;
}

/* Look for the accelerometer among the IIO devices. */
static int sn_scan(void)
{
	static const char *ax[3] = { "in_accel_x_raw", "in_accel_y_raw", "in_accel_z_raw" };
	DIR *d = opendir(iio_root);
	struct dirent *e;
	char dir[300] = "", buf[128];
	if (!d) return -1;
	while (!dir[0] && (e = readdir(d))) {
		char p[300];
		if (strncmp(e->d_name, "iio:device", 10)) continue;
		snprintf(p, sizeof p, "%.200s/%.64s", iio_root, e->d_name);
		if (read_small(p, "name", buf, sizeof buf) == 0 && !strcmp(buf, iio_name)) snprintf(dir, sizeof dir, "%s", p);
	}
	closedir(d);
	if (!dir[0]) return -1;
	sn_close();
	for (int i = 0; i < 3; i++) {
		char p[420];
		snprintf(p, sizeof p, "%s/%s", dir, ax[i]);
		sn.fd[i] = open(p, O_RDONLY | O_CLOEXEC | O_NONBLOCK);
		if (sn.fd[i] < 0) {
			logmsg("sensor: %s: %s", p, strerror(errno));
			sn_close();
			return -1;
		}
	}
	snprintf(sn.dir, sizeof sn.dir, "%s", dir);
	sn.scale = 0.000149637f;                 /* 9.80665 / 65536, the driver's */
	if (read_small(dir, "in_accel_scale", buf, sizeof buf) == 0 && atof(buf) > 0.0) sn.scale = (float)atof(buf);
	else logmsg("sensor: no in_accel_scale, taking %.9f", (double)sn.scale);
	{
		static const float def[9] = { 1, 0, 0, 0, 1, 0, 0, 0, -1 };
		float m[9];
		memcpy(sn.M, def, sizeof def);
		if (read_small(dir, "in_accel_mount_matrix", buf, sizeof buf) == 0 &&
		    sscanf(buf, "%f, %f, %f; %f, %f, %f; %f, %f, %f", &m[0], &m[1], &m[2], &m[3], &m[4], &m[5], &m[6], &m[7], &m[8]) == 9) {
			memcpy(sn.M, m, sizeof m);
			if (memcmp(m, def, sizeof def))
				logmsg("sensor: mount matrix '%s' is not the measured diag(1,1,-1); using it as given (--axes overrides)", buf);
		} else logmsg("sensor: no in_accel_mount_matrix, taking diag(1,1,-1)");
	}
	if (sn_rate > 0) {
		char p[420], v[16];
		int fd, n = snprintf(v, sizeof v, "%d\n", sn_rate);
		snprintf(p, sizeof p, "%s/sampling_frequency", dir);
		fd = open(p, O_WRONLY | O_CLOEXEC);
		if (fd < 0 || write(fd, v, (size_t)n) != n) logmsg("sensor: --rate %d: %s: %s", sn_rate, p, strerror(errno));
		else logmsg("sensor: asked for %d Hz (sampling_frequency)", sn_rate);
		if (fd >= 0) close(fd);
	}
	if (read_small(dir, "sampling_frequency", buf, sizeof buf) < 0) snprintf(buf, sizeof buf, "?");
	logmsg("sensor: %s is '%s', scale %.9f, matrix %g %g %g; %g %g %g; %g %g %g, sampling_frequency %s",
	       dir, iio_name, (double)sn.scale, (double)sn.M[0], (double)sn.M[1], (double)sn.M[2], (double)sn.M[3],
	       (double)sn.M[4], (double)sn.M[5], (double)sn.M[6], (double)sn.M[7], (double)sn.M[8], buf);
	sn.state = SN_WAIT;
	sn.since = sn.last_change = sn.rate_t0 = tick;
	sn.changes = 0; sn.rate_hz = 0.0f; sn.logged_wait = 0;
	return 0;
}

static int read_int(int fd, int *v)
{
	char b[32], *end;
	ssize_t r = pread(fd, b, sizeof b - 1, 0);
	long x;
	if (r < 0) return -1;
	if (r == 0) { errno = ENODATA; return -1; }
	b[r] = 0;
	errno = 0;
	x = strtol(b, &end, 10);
	if (end == b || errno || x > INT_MAX || x < INT_MIN) { errno = EINVAL; return -1; }
	*v = (int)x;
	return 0;
}

static void sn_lost(const char *why)
{
	logmsg("sensor: lost (%s); volume keys steer until it is back", why);
	sn_close();
	sn.losses++;
	sn.state = SN_SEARCH;
	sn.next_scan = tick + HZ;
}

/* Once per frame. */
static void sn_poll(void)
{
	int raw[3];
	if (sn.state == SN_OFF) {
		if (!sn.logged_none) { sn.logged_none = 1; logmsg("sensor: off (--iio none); volume keys steer"); }
		return;
	}
	if (sn.state == SN_SEARCH) {
		if (tick < sn.next_scan) return;
		sn.next_scan = tick + (tick < 60 * HZ ? HZ : 5 * HZ);
		if (sn_scan() < 0) {
			if (!sn.logged_none && tick >= 20 * HZ) {
				sn.logged_none = 1;
				logmsg("sensor: no IIO device named '%s' under %s (AOP not started? modules, firmware?); "
				       "volume keys steer, still looking", iio_name, iio_root);
			}
			return;
		}
	}
	for (int i = 0; i < 3; i++)
		if (read_int(sn.fd[i], &raw[i]) < 0) {
			if (errno == EAGAIN && sn.state == SN_WAIT) {
				if (!sn.logged_wait && tick - sn.since > 10 * HZ) {
					sn.logged_wait = 1;
					logmsg("sensor: no sample after 10 s (the AOP sends no reports: calibration missing in the DT?)");
				}
				return;
			}
			sn_lost(strerror(errno));
			return;
		}
	if (sn.state == SN_WAIT) {
		sn.state = SN_OK;
		sn.last_change = tick;
		logmsg("sensor: first sample raw %d %d %d", raw[0], raw[1], raw[2]);
	}
	if (memcmp(raw, sn.last_raw, sizeof raw)) {
		memcpy(sn.last_raw, raw, sizeof raw);
		sn.last_change = tick;
		sn.changes++;
		if (sn.state == SN_STALL) { sn.state = SN_OK; logmsg("sensor: values move again"); }
	} else if (sn.state == SN_OK && tick - sn.last_change > 3 * HZ) {
		sn.state = SN_STALL;
		sn.losses++;
		logmsg("sensor: values frozen for 3 s (raw %d %d %d); volume keys steer until they move", raw[0], raw[1], raw[2]);
		/* the device may have been replaced: look again as well */
		sn.next_scan = tick + HZ;
	}
	if (sn.state == SN_STALL && tick >= sn.next_scan) {
		char buf[64];
		sn.next_scan = tick + 2 * HZ;
		if (read_small(sn.dir, "name", buf, sizeof buf) < 0) { sn_lost("device gone"); return; }
	}
	if (tick - sn.rate_t0 >= 2 * HZ) {
		sn.rate_hz = (float)sn.changes * (float)HZ / (float)(tick - sn.rate_t0);
		sn.changes = 0; sn.rate_t0 = tick;
	}
	if (sn.state == SN_OK) sn_feed(raw);
}

/* ------------------------------------------------------------- framebuffer */

typedef struct { int x0, y0, x1, y1; } Rect;

static int fb_fd = -1, fb_fake;
static uint8_t *fb_map, *fb_base;
static size_t fb_maplen;
static unsigned fb_line, fb_Bpp;
static struct fb_bitfield fb_r, fb_g, fb_b, fb_t;
static int fb_direct;           /* 32 bpp XRGB: rows are copied as they are */
static int fb_use_write;        /* real framebuffer: pwrite() rows (see fb_present) */
static uint8_t *fb_rowbuf;
static long long fb_bytes;
static int W, H;                /* the screen */
static uint32_t *cv, *bg;       /* what is shown; the board without the moving things */
static uint8_t *wood, *mask;
static float U;                 /* scale against 1080x1920 */
static int cell, bx0, by0;      /* board: cell size and top left corner, pixels */

static volatile sig_atomic_t kd_on;
static int kd_fd = -1;

static void say(int fd, const char *s)   /* async-signal-safe */
{
	size_t n = strlen(s);
	while (n > 0) {
		ssize_t w = write(fd, s, n);
		if (w <= 0) { if (w < 0 && errno == EINTR) continue; return; }
		s += w; n -= (size_t)w;
	}
}

static void kd_text(void)                 /* async-signal-safe */
{
	if (!kd_on) return;
	kd_on = 0;
	if (fb_fake) say(2, "kd: KD_TEXT\n");    /* the self-test has no console */
	else {
		ioctl(kd_fd, KDSETMODE, KD_TEXT);
		say(kd_fd, "\033[0m\033[?25h\033[H\033[2J");
	}
}

static void kd_graphics(int tty)
{
	if (fb_fake) {
		kd_on = 1;
		say(2, "kd: KD_GRAPHICS\n");
		return;
	}
	if (tty < 0) return;
	kd_fd = tty;
	/* cursor off, no console blanking or powerdown */
	say(tty, "\033[?25l\033[9;0]\033[14;0]");
	kd_on = 1;
	if (ioctl(tty, KDSETMODE, KD_GRAPHICS) < 0) {
		kd_on = 0;
		logmsg("graphics: KDSETMODE KD_GRAPHICS failed: %s (the console may draw over the game)", strerror(errno));
	}
}

static uint32_t fb_pack(uint32_t c)
{
	uint32_t v = ((c >> 16 & 0xff) >> (8 - fb_r.length)) << fb_r.offset |
	             ((c >> 8 & 0xff) >> (8 - fb_g.length)) << fb_g.offset |
	             ((c & 0xff) >> (8 - fb_b.length)) << fb_b.offset;
	if (fb_t.length) v |= ((1u << fb_t.length) - 1u) << fb_t.offset;   /* opaque */
	return v;
}

static void fb_unpack(uint32_t v, uint8_t rgb[3])
{
	const struct fb_bitfield *f[3] = { &fb_r, &fb_g, &fb_b };
	for (int i = 0; i < 3; i++) {
		unsigned len = f[i]->length, x = (v >> f[i]->offset) & ((1u << len) - 1u);
		rgb[i] = (uint8_t)(len >= 8 ? x : (x << (8 - len)) | (2 * len > 8 ? x >> (2 * len - 8) : 0));
	}
}

static int fb_field_ok(const struct fb_bitfield *f, unsigned bpp, int need)
{
	if (!need && f->length == 0) return 1;
	return f->length >= 1 && f->length <= 8 && f->offset + f->length <= bpp && f->msb_right == 0;
}

/* Take a framebuffer described by v and f, mapped at mem (len bytes). */
static int fb_setup(const struct fb_var_screeninfo *v, const struct fb_fix_screeninfo *f, uint8_t *mem, size_t len)
{
	unsigned bpp = v->bits_per_pixel;
	size_t need;
	if (bpp != 16 && bpp != 32) { logmsg("graphics: %u bits per pixel is not supported", bpp); return -1; }
	if (!fb_field_ok(&v->red, bpp, 1) || !fb_field_ok(&v->green, bpp, 1) || !fb_field_ok(&v->blue, bpp, 1) ||
	    !fb_field_ok(&v->transp, bpp, 0) || v->grayscale || v->nonstd) {
		logmsg("graphics: unusual pixel layout (r %u/%u g %u/%u b %u/%u)",
		       v->red.offset, v->red.length, v->green.offset, v->green.length, v->blue.offset, v->blue.length);
		return -1;
	}
	if (v->xres < 160 || v->yres < 160 || v->xres > 8192 || v->yres > 8192 || f->line_length < v->xres * (bpp / 8)) {
		logmsg("graphics: odd size %ux%u, %u bytes a line", v->xres, v->yres, f->line_length);
		return -1;
	}
	need = (size_t)(v->yoffset + v->yres - 1) * f->line_length + (size_t)(v->xoffset + v->xres) * (bpp / 8);
	if (need > len) { logmsg("graphics: mapping too small (%zu < %zu bytes)", len, need); return -1; }
	W = (int)v->xres; H = (int)v->yres;
	U = (float)W / 1080.0f;
	if ((float)H / 1920.0f < U) U = (float)H / 1920.0f;
	cell = (int)(112.0f * U);
	if (cell < 10) { logmsg("graphics: %dx%d is too small for the board", W, H); return -1; }
	bx0 = (W - cell * COLS) / 2;
	by0 = H - cell * ROWS - (int)(36.0f * U) - 1;
	fb_line = f->line_length; fb_Bpp = bpp / 8;
	fb_r = v->red; fb_g = v->green; fb_b = v->blue; fb_t = v->transp;
	fb_direct = bpp == 32 && fb_r.offset == 16 && fb_g.offset == 8 && fb_b.offset == 0 &&
	            fb_r.length == 8 && fb_g.length == 8 && fb_b.length == 8 && fb_t.length == 0;
	fb_map = mem; fb_maplen = len;
	fb_base = mem + (size_t)v->yoffset * fb_line + (size_t)v->xoffset * fb_Bpp;
	cv = calloc((size_t)W * (size_t)H, sizeof *cv);
	bg = calloc((size_t)W * (size_t)H, sizeof *bg);
	wood = malloc((size_t)W * (size_t)H);
	mask = malloc((size_t)W * (size_t)H);
	fb_rowbuf = malloc((size_t)W * 4);
	if (!cv || !bg || !wood || !mask || !fb_rowbuf) { logmsg("graphics: out of memory"); return -1; }
	logmsg("graphics: %dx%d, %u bpp (r %u/%u g %u/%u b %u/%u), %u bytes a line%s; cell %d px, board at %d,%d",
	       W, H, bpp, fb_r.offset, fb_r.length, fb_g.offset, fb_g.length, fb_b.offset, fb_b.length, fb_line,
	       fb_direct ? ", direct" : "", cell, bx0, by0);
	return 0;
}

static int fb_force_mmap;

static int fb_open(const char *path)
{
	struct fb_var_screeninfo v;
	struct fb_fix_screeninfo f;
	size_t len;
	void *m;
	int fd = open(path, O_RDWR | O_CLOEXEC);
	if (fd < 0) { logmsg("graphics: %s: %s", path, strerror(errno)); return -1; }
	memset(&v, 0, sizeof v);
	memset(&f, 0, sizeof f);
	if (ioctl(fd, FBIOGET_VSCREENINFO, &v) < 0 || ioctl(fd, FBIOGET_FSCREENINFO, &f) < 0) {
		logmsg("graphics: %s: screen info: %s", path, strerror(errno));
		close(fd);
		return -1;
	}
	if (f.type != FB_TYPE_PACKED_PIXELS || f.visual != FB_VISUAL_TRUECOLOR) {
		logmsg("graphics: %s: not packed true colour (type %u, visual %u)", path, f.type, f.visual);
		close(fd);
		return -1;
	}
	len = f.smem_len ? f.smem_len : (size_t)f.line_length * v.yres_virtual;
	m = mmap(NULL, len, PROT_READ | PROT_WRITE, MAP_SHARED, fd, 0);
	if (m == MAP_FAILED) {
		logmsg("graphics: %s: mmap of %zu bytes: %s", path, len, strerror(errno));
		close(fd);
		return -1;
	}
	fb_fd = fd;
	fb_use_write = !fb_force_mmap;
	if (fb_setup(&v, &f, m, len) < 0) {
		munmap(m, len);
		close(fd);
		fb_fd = -1; fb_map = fb_base = NULL;
		return -1;
	}
	return 0;
}

/* The self-test's framebuffer: memory, described like the real one. Each line
 * has 64 bytes of padding past the picture, filled with 0xA5: nothing may
 * ever touch it. */
static const char *fake_file;    /* --fakefb-file: keep the fake screen in this file and pwrite() to it */

static int fb_open_fake(const char *spec)
{
	struct fb_var_screeninfo v;
	struct fb_fix_screeninfo f;
	unsigned w, h, bpp;
	char order[8] = "rgb";
	uint8_t *m;
	if (sscanf(spec, "%ux%ux%u:%7s", &w, &h, &bpp, order) < 3 || w > 8192 || h > 8192 || w < 1 || h < 1) return -1;
	memset(&v, 0, sizeof v);
	memset(&f, 0, sizeof f);
	v.xres = v.xres_virtual = w; v.yres = v.yres_virtual = h; v.bits_per_pixel = bpp;
	if (bpp == 16) {
		v.red = (struct fb_bitfield){ 11, 5, 0 };
		v.green = (struct fb_bitfield){ 5, 6, 0 };
		v.blue = (struct fb_bitfield){ 0, 5, 0 };
	} else {
		int bgr = !strcmp(order, "bgr");
		v.red = (struct fb_bitfield){ bgr ? 0u : 16u, 8, 0 };
		v.green = (struct fb_bitfield){ 8, 8, 0 };
		v.blue = (struct fb_bitfield){ bgr ? 16u : 0u, 8, 0 };
	}
	f.type = FB_TYPE_PACKED_PIXELS; f.visual = FB_VISUAL_TRUECOLOR;
	f.line_length = w * (bpp / 8) + 64;
	f.smem_len = f.line_length * h;
	if (fake_file) {
		int fd = open(fake_file, O_RDWR | O_CREAT | O_TRUNC | O_CLOEXEC, 0600);
		if (fd < 0 || ftruncate(fd, (off_t)f.smem_len) < 0) { perror(fake_file); return -1; }
		m = mmap(NULL, f.smem_len, PROT_READ | PROT_WRITE, MAP_SHARED, fd, 0);
		if (m == MAP_FAILED) { perror("mmap"); return -1; }
		fb_fd = fd;
		fb_use_write = 1;
	} else m = malloc(f.smem_len);
	if (!m) return -1;
	memset(m, 0xa5, f.smem_len);
	fb_fake = 1;
	if (fb_setup(&v, &f, m, f.smem_len) < 0) {
		if (!fake_file) free(m);
		fb_fake = 0;
		return -1;
	}
	return 0;
}

/*
 * Copy a rectangle of cv to the screen. The real framebuffer is simpledrm's
 * fbdev emulation, a shadow buffer: what is stored through mmap() only
 * reaches the panel when the deferred-I/O timer runs (every 50 ms, 20 times
 * a second), while write() marks the damage and starts the copy at once
 * (drm_fb_helper_damage_range). So rows go out with pwrite(), one call per
 * row of the rectangle; mmap is the fallback (and --fb-mmap forces it).
 */
static void fb_present(Rect r)
{
	if (r.x0 < 0) r.x0 = 0;
	if (r.y0 < 0) r.y0 = 0;
	if (r.x1 > W) r.x1 = W;
	if (r.y1 > H) r.y1 = H;
	if (r.x1 <= r.x0 || r.y1 <= r.y0) return;
	for (int y = r.y0; y < r.y1; y++) {
		const uint32_t *s = cv + (size_t)y * (size_t)W + r.x0;
		uint8_t *d = fb_base + (size_t)y * fb_line + (size_t)r.x0 * fb_Bpp;
		int n = r.x1 - r.x0;
		if (fb_use_write) {
			const uint8_t *src = (const uint8_t *)s;
			size_t len = (size_t)n * fb_Bpp, done = 0;
			if (!fb_direct) {
				src = fb_rowbuf;
				if (fb_Bpp == 4) for (int i = 0; i < n; i++) { uint32_t p = fb_pack(s[i]); memcpy(fb_rowbuf + 4 * i, &p, 4); }
				else for (int i = 0; i < n; i++) { uint16_t p = (uint16_t)fb_pack(s[i]); memcpy(fb_rowbuf + 2 * i, &p, 2); }
			}
			while (done < len) {
				ssize_t w = pwrite(fb_fd, src + done, len - done, (off_t)(d - fb_map) + (off_t)done);
				if (w < 0 && errno == EINTR) continue;
				if (w <= 0) {
					logmsg("graphics: pwrite to the framebuffer: %s; using the mapping from here on",
					       w < 0 ? strerror(errno) : "short write");
					fb_use_write = 0;
					break;
				}
				done += (size_t)w;
			}
			if (fb_use_write) continue;
		}
		if (fb_direct) memcpy(d, s, (size_t)n * 4);
		else if (fb_Bpp == 4) for (int i = 0; i < n; i++) { uint32_t p = fb_pack(s[i]); memcpy(d + 4 * i, &p, 4); }
		else for (int i = 0; i < n; i++) { uint16_t p = (uint16_t)fb_pack(s[i]); memcpy(d + 2 * i, &p, 2); }
	}
	fb_bytes += (long long)(r.x1 - r.x0) * (r.y1 - r.y0) * (long long)fb_Bpp;
}

/* The self-test: the screen, read back through its bitfields, as PPM. */
static void fb_dump(const char *dir, const char *name)
{
	char p[400];
	uint8_t *row = malloc((size_t)W * 3);
	FILE *f;
	snprintf(p, sizeof p, "%s/%s.ppm", dir, name);
	f = fopen(p, "wb");
	if (!f || !row) { perror(p); free(row); if (f) fclose(f); return; }
	fprintf(f, "P6\n%d %d\n255\n", W, H);
	for (int y = 0; y < H; y++) {
		const uint8_t *src = fb_base + (size_t)y * fb_line;
		for (int x = 0; x < W; x++) {
			uint32_t v = 0;
			if (fb_Bpp == 4) memcpy(&v, src + 4 * x, 4);
			else { uint16_t s; memcpy(&s, src + 2 * x, 2); v = s; }
			fb_unpack(v, row + 3 * x);
		}
		fwrite(row, 3, (size_t)W, f);
	}
	fclose(f);
	free(row);
	logmsg("dump: %s", p);
}

static int fb_padding_ok(void)
{
	for (int y = 0; y < H; y++)
		for (unsigned x = (unsigned)W * fb_Bpp; x < fb_line; x++)
			if (fb_base[(size_t)y * fb_line + x] != 0xa5) return 0;
	return 1;
}

/* ----------------------------------------------------------------- drawing */

static inline uint32_t mix(uint32_t d, uint32_t s, int a)   /* a: 0..256 of s over d */
{
	uint32_t rb = (((d & 0xff00ffu) * (uint32_t)(256 - a) + (s & 0xff00ffu) * (uint32_t)a) >> 8) & 0xff00ffu;
	uint32_t g = (((d & 0x00ff00u) * (uint32_t)(256 - a) + (s & 0x00ff00u) * (uint32_t)a) >> 8) & 0x00ff00u;
	return rb | g;
}

static inline uint32_t rgbf(float r, float g, float b)
{
	return (uint32_t)clampf(r, 0.0f, 255.0f) << 16 | (uint32_t)clampf(g, 0.0f, 255.0f) << 8 | (uint32_t)clampf(b, 0.0f, 255.0f);
}

static inline uint32_t shade(uint32_t c, float k)
{
	return rgbf((float)(c >> 16 & 0xff) * k, (float)(c >> 8 & 0xff) * k, (float)(c & 0xff) * k);
}

static int sc(float k) { int v = (int)(k * U + 0.5f); return v < 1 ? 1 : v; }
static int px(float k) { return (int)(k * U + 0.5f); }

/* The rectangles drawn on cv since layer_begin(). */
typedef struct {
	Rect r[12], old[12];
	int n, nold, valid;
	char key[240];
} Layer;
static Layer *cur_layer;
static int full_redraw = 1;

static int mark_hold;            /* > 0: the caller has marked the whole item already */

static void mark(int x0, int y0, int x1, int y1)
{
	Layer *l = cur_layer;
	if (mark_hold) return;
	if (x0 < 0) x0 = 0;
	if (y0 < 0) y0 = 0;
	if (x1 > W) x1 = W;
	if (y1 > H) y1 = H;
	if (!l || x1 <= x0 || y1 <= y0) return;
	if (l->n == (int)(sizeof l->r / sizeof l->r[0])) {
		Rect *r = &l->r[l->n - 1];              /* full: grow the last one */
		if (x0 < r->x0) r->x0 = x0;
		if (y0 < r->y0) r->y0 = y0;
		if (x1 > r->x1) r->x1 = x1;
		if (y1 > r->y1) r->y1 = y1;
		return;
	}
	l->r[l->n++] = (Rect){ x0, y0, x1, y1 };
}

static void fill(uint32_t *t, int x0, int y0, int x1, int y1, uint32_t c, int a)
{
	if (x0 < 0) x0 = 0;
	if (y0 < 0) y0 = 0;
	if (x1 > W) x1 = W;
	if (y1 > H) y1 = H;
	if (x1 <= x0 || y1 <= y0) return;
	if (t == cv) mark(x0, y0, x1, y1);
	for (int y = y0; y < y1; y++) {
		uint32_t *p = t + (size_t)y * (size_t)W;
		if (a >= 256) for (int x = x0; x < x1; x++) p[x] = c;
		else for (int x = x0; x < x1; x++) p[x] = mix(p[x], c, a);
	}
}

/* A disc or ring (r0 > 0), anti-aliased. */
static void disc(uint32_t *t, float cx, float cy, float r0, float r1, uint32_t c, int a)
{
	int x0 = (int)floorf(cx - r1 - 1), x1 = (int)ceilf(cx + r1 + 1), y0 = (int)floorf(cy - r1 - 1), y1 = (int)ceilf(cy + r1 + 1);
	if (x0 < 0) x0 = 0;
	if (y0 < 0) y0 = 0;
	if (x1 > W) x1 = W;
	if (y1 > H) y1 = H;
	if (x1 <= x0 || y1 <= y0) return;
	if (t == cv) mark(x0, y0, x1, y1);
	for (int y = y0; y < y1; y++)
		for (int x = x0; x < x1; x++) {
			float dx = (float)x + 0.5f - cx, dy = (float)y + 0.5f - cy, d = sqrtf(dx * dx + dy * dy);
			float cov = clampf(r1 - d + 0.5f, 0.0f, 1.0f);
			if (r0 > 0.0f) cov *= clampf(d - r0 + 0.5f, 0.0f, 1.0f);
			if (cov > 0.0f) {
				uint32_t *p = t + (size_t)y * (size_t)W + x;
				*p = mix(*p, c, (int)(cov * (float)a));
			}
		}
}

/* A rounded panel with a border. */
static void panel(uint32_t *t, int x0, int y0, int x1, int y1, float rad, uint32_t c, int a, uint32_t border)
{
	float hx = (float)(x1 - x0) / 2.0f, hy = (float)(y1 - y0) / 2.0f, cx = (float)x0 + hx, cy = (float)y0 + hy;
	float bw = clampf(3.0f * U, 1.0f, 8.0f);
	if (t == cv) mark(x0, y0, x1, y1);
	for (int y = y0 < 0 ? 0 : y0; y < y1 && y < H; y++)
		for (int x = x0 < 0 ? 0 : x0; x < x1 && x < W; x++) {
			float qx = fabsf((float)x + 0.5f - cx) - hx + rad, qy = fabsf((float)y + 0.5f - cy) - hy + rad, d;
			uint32_t *p = t + (size_t)y * (size_t)W + x;
			if (qx <= 0.0f || qy <= 0.0f) d = (qx > qy ? qx : qy) - rad;
			else d = sqrtf(qx * qx + qy * qy) - rad;
			if (d > 0.5f) continue;
			if (d > -bw) *p = mix(*p, border, (int)(clampf(0.5f - d, 0.0f, 1.0f) * 256.0f));
			else *p = mix(*p, c, a);
		}
}

/* 5x7 glyphs, rows top to bottom, bit 4 = left column. ASCII 32..126 (lower
 * case is drawn as upper case), and 1..5: arrows left, right, up, down, a
 * middle dot. */
static const uint8_t font57[95][7] = {
	{ 0x00,0x00,0x00,0x00,0x00,0x00,0x00 }, { 0x04,0x04,0x04,0x04,0x00,0x00,0x04 },
	{ 0x0A,0x0A,0x0A,0x00,0x00,0x00,0x00 }, { 0x0A,0x0A,0x1F,0x0A,0x1F,0x0A,0x0A },
	{ 0x04,0x0F,0x14,0x0E,0x05,0x1E,0x04 }, { 0x18,0x19,0x02,0x04,0x08,0x13,0x03 },
	{ 0x0C,0x12,0x14,0x08,0x15,0x12,0x0D }, { 0x0C,0x04,0x08,0x00,0x00,0x00,0x00 },
	{ 0x02,0x04,0x08,0x08,0x08,0x04,0x02 }, { 0x08,0x04,0x02,0x02,0x02,0x04,0x08 },
	{ 0x00,0x04,0x15,0x0E,0x15,0x04,0x00 }, { 0x00,0x04,0x04,0x1F,0x04,0x04,0x00 },
	{ 0x00,0x00,0x00,0x00,0x0C,0x04,0x08 }, { 0x00,0x00,0x00,0x1F,0x00,0x00,0x00 },
	{ 0x00,0x00,0x00,0x00,0x00,0x0C,0x0C }, { 0x00,0x01,0x02,0x04,0x08,0x10,0x00 },
	{ 0x0E,0x11,0x13,0x15,0x19,0x11,0x0E }, { 0x04,0x0C,0x04,0x04,0x04,0x04,0x0E },
	{ 0x0E,0x11,0x01,0x02,0x04,0x08,0x1F }, { 0x1F,0x02,0x04,0x02,0x01,0x11,0x0E },
	{ 0x02,0x06,0x0A,0x12,0x1F,0x02,0x02 }, { 0x1F,0x10,0x1E,0x01,0x01,0x11,0x0E },
	{ 0x06,0x08,0x10,0x1E,0x11,0x11,0x0E }, { 0x1F,0x01,0x02,0x04,0x08,0x08,0x08 },
	{ 0x0E,0x11,0x11,0x0E,0x11,0x11,0x0E }, { 0x0E,0x11,0x11,0x0F,0x01,0x02,0x0C },
	{ 0x00,0x0C,0x0C,0x00,0x0C,0x0C,0x00 }, { 0x00,0x0C,0x0C,0x00,0x0C,0x04,0x08 },
	{ 0x02,0x04,0x08,0x10,0x08,0x04,0x02 }, { 0x00,0x00,0x1F,0x00,0x1F,0x00,0x00 },
	{ 0x08,0x04,0x02,0x01,0x02,0x04,0x08 }, { 0x0E,0x11,0x01,0x02,0x04,0x00,0x04 },
	{ 0x0E,0x11,0x01,0x0D,0x15,0x15,0x0E }, { 0x0E,0x11,0x11,0x11,0x1F,0x11,0x11 },
	{ 0x1E,0x11,0x11,0x1E,0x11,0x11,0x1E }, { 0x0E,0x11,0x10,0x10,0x10,0x11,0x0E },
	{ 0x1C,0x12,0x11,0x11,0x11,0x12,0x1C }, { 0x1F,0x10,0x10,0x1E,0x10,0x10,0x1F },
	{ 0x1F,0x10,0x10,0x1E,0x10,0x10,0x10 }, { 0x0E,0x11,0x10,0x17,0x11,0x11,0x0F },
	{ 0x11,0x11,0x11,0x1F,0x11,0x11,0x11 }, { 0x0E,0x04,0x04,0x04,0x04,0x04,0x0E },
	{ 0x07,0x02,0x02,0x02,0x02,0x12,0x0C }, { 0x11,0x12,0x14,0x18,0x14,0x12,0x11 },
	{ 0x10,0x10,0x10,0x10,0x10,0x10,0x1F }, { 0x11,0x1B,0x15,0x15,0x11,0x11,0x11 },
	{ 0x11,0x11,0x19,0x15,0x13,0x11,0x11 }, { 0x0E,0x11,0x11,0x11,0x11,0x11,0x0E },
	{ 0x1E,0x11,0x11,0x1E,0x10,0x10,0x10 }, { 0x0E,0x11,0x11,0x11,0x15,0x12,0x0D },
	{ 0x1E,0x11,0x11,0x1E,0x14,0x12,0x11 }, { 0x0F,0x10,0x10,0x0E,0x01,0x01,0x1E },
	{ 0x1F,0x04,0x04,0x04,0x04,0x04,0x04 }, { 0x11,0x11,0x11,0x11,0x11,0x11,0x0E },
	{ 0x11,0x11,0x11,0x11,0x11,0x0A,0x04 }, { 0x11,0x11,0x11,0x15,0x15,0x15,0x0A },
	{ 0x11,0x11,0x0A,0x04,0x0A,0x11,0x11 }, { 0x11,0x11,0x11,0x0A,0x04,0x04,0x04 },
	{ 0x1F,0x01,0x02,0x04,0x08,0x10,0x1F }, { 0x0E,0x08,0x08,0x08,0x08,0x08,0x0E },
	{ 0x00,0x10,0x08,0x04,0x02,0x01,0x00 }, { 0x0E,0x02,0x02,0x02,0x02,0x02,0x0E },
	{ 0x04,0x0A,0x11,0x00,0x00,0x00,0x00 }, { 0x00,0x00,0x00,0x00,0x00,0x00,0x1F },
	{ 0x08,0x04,0x02,0x00,0x00,0x00,0x00 },
	{ 0 }, { 0 }, { 0 }, { 0 }, { 0 }, { 0 }, { 0 }, { 0 }, { 0 }, { 0 }, { 0 }, { 0 }, { 0 },
	{ 0 }, { 0 }, { 0 }, { 0 }, { 0 }, { 0 }, { 0 }, { 0 }, { 0 }, { 0 }, { 0 }, { 0 }, { 0 },
	{ 0x02,0x04,0x04,0x08,0x04,0x04,0x02 }, { 0x04,0x04,0x04,0x04,0x04,0x04,0x04 },
	{ 0x08,0x04,0x04,0x02,0x04,0x04,0x08 }, { 0x00,0x00,0x08,0x15,0x02,0x00,0x00 },
};
static const uint8_t font_extra[5][7] = {
	{ 0x00,0x04,0x08,0x1F,0x08,0x04,0x00 }, { 0x00,0x04,0x02,0x1F,0x02,0x04,0x00 },
	{ 0x04,0x0E,0x15,0x04,0x04,0x04,0x00 }, { 0x00,0x04,0x04,0x04,0x15,0x0E,0x04 },
	{ 0x00,0x00,0x0C,0x0C,0x00,0x00,0x00 },
};

static const uint8_t *glyph(unsigned char ch)
{
	static const uint8_t box[7] = { 0x1F,0x11,0x11,0x11,0x11,0x11,0x1F };
	if (ch >= 1 && ch <= 5) return font_extra[ch - 1];
	if (ch >= 'a' && ch <= 'z') ch = (unsigned char)(ch - 32);
	if (ch >= 32 && ch < 127) return font57[ch - 32];
	return box;
}

static int tw(const char *s, int z) { int n = (int)strlen(s); return n ? (n * 6 - 1) * z : 0; }

/* Text at (x, y), z times the 5x7 size, colour c with alpha a. */
static void text(uint32_t *t, int x, int y, const char *s, int z, uint32_t c, int a)
{
	if (t == cv) mark(x, y, x + tw(s, z), y + 7 * z);
	for (; *s; s++, x += 6 * z) {
		const uint8_t *g = glyph((unsigned char)*s);
		mark_hold++;                            /* marked as a whole above */
		for (int j = 0; j < 7; j++)
			for (int i = 0; i < 5; i++)
				if (g[j] & (0x10 >> i)) fill(t, x + i * z, y + j * z, x + i * z + z, y + j * z + z, c, a);
		mark_hold--;
	}
}

/* Light text with a dark shadow, centred on cx (or from x with ctr 0). */
static void label(uint32_t *t, int x, int y, const char *s, int z, uint32_t c, int ctr)
{
	int o = z >= 3 ? (z + 1) / 3 : 1;
	if (ctr == 1) x -= tw(s, z) / 2;
	if (ctr == 2) x -= tw(s, z);
	if (t == cv) mark(x, y, x + tw(s, z) + o, y + 7 * z + o);
	mark_hold++;
	text(t, x + o, y + o, s, z, 0x120b06, 200);
	text(t, x, y, s, z, c, 256);
	mark_hold--;
}

/* Text cut into the wood: dark, with a light lower-right edge. */
static void engrave(uint32_t *t, int cx, int y, const char *s, int z)
{
	int x = cx - tw(s, z) / 2, o = z >= 4 ? z / 4 : 1;
	text(t, x + o, y + o, s, z, 0xfff0d0, 90);
	text(t, x, y, s, z, 0x2a180c, 185);
}

static uint32_t hash32(uint32_t x)
{
	x ^= x >> 16; x *= 0x7feb352du; x ^= x >> 15; x *= 0x846ca68bu; x ^= x >> 16;
	return x;
}
static float hnoise(int ix, int iy)
{
	return (float)(hash32((uint32_t)ix * 0x9e3779b1u ^ hash32((uint32_t)iy + 0x7f4a7c15u)) & 0xffffffu) / 16777216.0f;
}
static float vnoise(float x, float y)
{
	int ix = (int)floorf(x), iy = (int)floorf(y);
	float fx = x - (float)ix, fy = y - (float)iy, a, b;
	fx = fx * fx * (3.0f - 2.0f * fx); fy = fy * fy * (3.0f - 2.0f * fy);
	a = hnoise(ix, iy) + (hnoise(ix + 1, iy) - hnoise(ix, iy)) * fx;
	b = hnoise(ix, iy + 1) + (hnoise(ix + 1, iy + 1) - hnoise(ix, iy + 1)) * fx;
	return a + (b - a) * fy;
}

/* The wood: planks running down the screen, 0..255 from dark to light grain. */
static void wood_init(void)
{
	float inv = 1.0f / U;
	for (int y = 0; y < H; y++)
		for (int x = 0; x < W; x++) {
			float fx = (float)x * inv, fy = (float)y * inv;
			int plank = (int)(fx / 216.0f);
			float px_in = fx - (float)plank * 216.0f, off = (float)plank * 37.0f;
			float warp = vnoise(fx * 0.006f + off, fy * 0.0022f) * 60.0f;
			float v = 0.5f + 0.20f * (hnoise(plank, 77) - 0.5f);
			v += 0.16f * sinf((fx + warp) * 0.19f + off);
			v += 0.22f * (vnoise(fx * 0.045f + off, fy * 0.0035f) - 0.5f);
			v += 0.16f * (vnoise(fx * 0.31f + off, fy * 0.012f) - 0.5f);
			v += 0.05f * (hnoise(x, y) - 0.5f);
			if (px_in < 2.0f || px_in > 214.5f) v -= 0.30f;        /* the seam between planks */
			wood[(size_t)y * (size_t)W + x] = (uint8_t)(clampf(v, 0.0f, 1.0f) * 255.0f);
		}
}

static uint32_t ramp(uint32_t c0, uint32_t c1, int v)   /* v: 0..255 */
{
	return mix(c0, c1, v + (v >> 7));
}

/* Distance from (x, y) to the segment a-b; the nearest point's direction in (*nx, *ny). */
static float seg_dist(float x, float y, float ax, float ay, float bx, float by, float *nx, float *ny)
{
	float ex = bx - ax, ey = by - ay, t = ((x - ax) * ex + (y - ay) * ey) / (ex * ex + ey * ey), dx, dy, d;
	t = clampf(t, 0.0f, 1.0f);
	dx = x - (ax + t * ex); dy = y - (ay + t * ey);
	d = sqrtf(dx * dx + dy * dy);
	if (d > 1e-4f) { *nx = dx / d; *ny = dy / d; } else { *nx = *ny = 0.0f; }
	return d;
}

/* A wall's shadow into mask (the strongest wins: no double dark where walls meet). */
static void wall_shadow(float ax, float ay, float bx, float by, float ht)
{
	float ox = 0.050f * (float)cell, oy = 0.070f * (float)cell, soft = 0.045f * (float)cell + 1.0f, nx, ny;
	int x0 = (int)floorf((ax < bx ? ax : bx) + ox - ht - soft), x1 = (int)ceilf((ax > bx ? ax : bx) + ox + ht + soft);
	int y0 = (int)floorf((ay < by ? ay : by) + oy - ht - soft), y1 = (int)ceilf((ay > by ? ay : by) + oy + ht + soft);
	for (int y = y0 < 0 ? 0 : y0; y < y1 && y < H; y++)
		for (int x = x0 < 0 ? 0 : x0; x < x1 && x < W; x++) {
			float d = seg_dist((float)x + 0.5f - ox, (float)y + 0.5f - oy, ax, ay, bx, by, &nx, &ny) - ht;
			int m = (int)(clampf(0.5f - d / (2.0f * soft), 0.0f, 1.0f) * 255.0f);
			uint8_t *p = mask + (size_t)y * (size_t)W + x;
			if (m > *p) *p = (uint8_t)m;
		}
}

/* A wall: a rounded strip of dark wood, bevelled, lit from the top left. */
static void wall_draw(float ax, float ay, float bx, float by, float ht)
{
	float nx, ny;
	int x0 = (int)floorf((ax < bx ? ax : bx) - ht - 1), x1 = (int)ceilf((ax > bx ? ax : bx) + ht + 1);
	int y0 = (int)floorf((ay < by ? ay : by) - ht - 1), y1 = (int)ceilf((ay > by ? ay : by) + ht + 1);
	for (int y = y0 < 0 ? 0 : y0; y < y1 && y < H; y++)
		for (int x = x0 < 0 ? 0 : x0; x < x1 && x < W; x++) {
			float d = seg_dist((float)x + 0.5f, (float)y + 0.5f, ax, ay, bx, by, &nx, &ny) - ht;
			float e, lit, k;
			uint32_t c, *p;
			if (d > 0.5f) continue;
			e = clampf(1.0f + d / (ht * 0.6f), 0.0f, 1.0f);       /* 0 on the flat top, 1 at the edge */
			lit = -(nx * 0.6f + ny * 0.8f);
			k = 1.0f + e * (lit > 0.0f ? 0.55f * lit : 0.50f * lit);
			c = ramp(0x5a2f1b, 0x8a5230, wood[(size_t)((y * 7 + 13) % H) * (size_t)W + (size_t)((x * 3 + 5) % W)]);
			p = bg + (size_t)y * (size_t)W + x;
			*p = mix(*p, shade(c, k), (int)(clampf(0.5f - d, 0.0f, 1.0f) * 256.0f));
		}
}

/* A hole (goal 0) or the brass cup (goal 1) at (cx, cy), radius r pixels. */
static void hole_draw(float cx, float cy, float r, int goal)
{
	float ro = r * (goal ? 1.22f : 1.10f);
	int x0 = (int)floorf(cx - ro - 1), x1 = (int)ceilf(cx + ro + 1), y0 = (int)floorf(cy - ro - 1), y1 = (int)ceilf(cy + ro + 1);
	for (int y = y0 < 0 ? 0 : y0; y < y1 && y < H; y++)
		for (int x = x0 < 0 ? 0 : x0; x < x1 && x < W; x++) {
			float dx = ((float)x + 0.5f - cx) / r, dy = ((float)y + 0.5f - cy) / r, rr = sqrtf(dx * dx + dy * dy);
			float lit = rr > 1e-3f ? (dx * 0.6f + dy * 0.8f) / rr : 0.0f;   /* +1 on the lower right */
			uint32_t *p = bg + (size_t)y * (size_t)W + x, c;
			float cov = clampf((1.0f - rr) * r + 0.5f, 0.0f, 1.0f);
			if (goal) {
				/* the brass ring around the cup */
				float ring = clampf((1.22f - rr) * r + 0.5f, 0.0f, 1.0f);
				float t = (rr - 1.0f) / 0.22f, bev = t < 0.5f ? -1.0f : 1.0f;
				if (ring > 0.0f && rr >= 0.98f)
					*p = mix(*p, shade(0xd9b24c, 1.0f - 0.38f * lit * bev), (int)(ring * 256.0f));
			} else if (rr >= 1.0f && rr < 1.10f) {
				*p = mix(*p, 0x000000, (int)((1.10f - rr) / 0.10f * 70.0f));   /* worn rim */
			}
			if (cov <= 0.0f) continue;
			if (goal) {
				float k = 0.55f + 0.45f * clampf(0.5f + 0.5f * lit * rr, 0.0f, 1.0f);
				c = shade(0x2c9a54, k * (rr > 0.8f ? 1.0f - (rr - 0.8f) * 1.6f : 1.0f));
				if (rr < 0.20f) c = mix(c, 0xf0d070, (int)(clampf((0.20f - rr) * r, 0.0f, 1.0f) * 256.0f));
			} else {
				/* the far inner wall catches the light; the near one is in the dark */
				float w = clampf((rr - 0.50f) / 0.50f, 0.0f, 1.0f) * clampf(lit, 0.0f, 1.0f);
				c = mix(0x0c0705, 0x6b4526, (int)(w * w * 230.0f));
			}
			*p = mix(*p, c, (int)(cov * 256.0f));
		}
}

static int from_picker;

static float bpx(float x) { return (float)bx0 + x * (float)cell; }
static float bpy(float y) { return (float)by0 + y * (float)cell; }

static const char *best_str(long cs, char *buf, size_t n);

/* Render the board (and the title's lettering) into bg. */
static void build_bg(const Level *l, int title)
{
	float ht = WALL_H * (float)cell;
	int bw = cell * COLS, bh = cell * ROWS, f = (int)ht + px(4.0f);
	/* the frame all around, the lighter playing surface inside */
	for (int y = 0; y < H; y++)
		for (int x = 0; x < W; x++) {
			int v = wood[(size_t)y * (size_t)W + x];
			int in = x >= bx0 && x < bx0 + bw && y >= by0 && y < by0 + bh;
			bg[(size_t)y * (size_t)W + x] = in ? ramp(0xc59a5c, 0xe6c58e, v) : ramp(0x24150c, 0x4a2e1a, v);
		}
	/* a darker rebate around the board */
	fill(bg, bx0 - f, by0 - f, bx0 + bw + f, by0, 0x000000, 60);
	fill(bg, bx0 - f, by0 + bh, bx0 + bw + f, by0 + bh + f, 0x000000, 60);
	fill(bg, bx0 - f, by0, bx0, by0 + bh, 0x000000, 60);
	fill(bg, bx0 + bw, by0, bx0 + bw + f, by0 + bh, 0x000000, 60);
	if (title) {
		int cx = W / 2, y = by0 + px(70.0f);
		engrave(bg, cx, y, "GLINT", sc(26.0f));
		y += px(26.0f * 7.0f + 34.0f);
		engrave(bg, cx, y, "A TILT MAZE", sc(8.0f));
		y += px(8.0f * 7.0f + 90.0f);
		int t = touch_present();
		engrave(bg, cx, y, "HOLD THE PHONE THE WAY", sc(6.0f)); y += px(62.0f);
		engrave(bg, cx, y, "YOU LIKE TO PLAY,", sc(6.0f)); y += px(62.0f);
		engrave(bg, cx, y, t ? "THEN TAP THE SCREEN" : "THEN PRESS HOME", sc(6.0f)); y += px(120.0f);
		engrave(bg, cx, y, "THAT POSE COUNTS AS LEVEL", sc(4.0f)); y += px(110.0f);
		engrave(bg, cx, y, t ? "SWIPE \1 \2      CHOOSE THE BOARD" : "VOL+ / VOL-    CHOOSE THE BOARD", sc(4.0f)); y += px(50.0f);
		engrave(bg, cx, y, "BOTH VOL KEYS  SOUND ON / OFF  ", sc(4.0f)); y += px(50.0f);
		engrave(bg, cx, y, t ? "HOME, TAP TOP  PAUSE           " : "HOME           PAUSE           ", sc(4.0f)); y += px(50.0f);
		engrave(bg, cx, y, t ? "HOLD \1 CORNER  BACK TO HERE    " : "HOLD HOME 2 S  BACK TO HERE    ", sc(4.0f)); y += px(50.0f);
		if (from_picker) engrave(bg, cx, y, "  AND HERE     BACK TO THE GAMES", sc(4.0f));
	}
	/* shadows of the walls */
	memset(mask, 0, (size_t)W * (size_t)H);
	for (int pass = 0; pass < 2; pass++) {
		for (int y = 0; y <= ROWS; y++)
			for (int x = 0; x < COLS; x++) {
				int x1 = x;
				if (!l->h[y][x]) continue;
				while (x1 + 1 < COLS && l->h[y][x1 + 1]) x1++;
				if (pass == 0) wall_shadow(bpx((float)x), bpy((float)y), bpx((float)x1 + 1.0f), bpy((float)y), ht);
				else wall_draw(bpx((float)x), bpy((float)y), bpx((float)x1 + 1.0f), bpy((float)y), ht);
				x = x1;
			}
		for (int x = 0; x <= COLS; x++)
			for (int y = 0; y < ROWS; y++) {
				int y1 = y;
				if (!l->v[y][x]) continue;
				while (y1 + 1 < ROWS && l->v[y1 + 1][x]) y1++;
				if (pass == 0) wall_shadow(bpx((float)x), bpy((float)y), bpx((float)x), bpy((float)y1 + 1.0f), ht);
				else wall_draw(bpx((float)x), bpy((float)y), bpx((float)x), bpy((float)y1 + 1.0f), ht);
				y = y1;
			}
		if (pass == 0) {
			for (int y = by0 - f; y < by0 + bh + f && y < H; y++)
				for (int x = bx0 - f; x < bx0 + bw + f && x < W; x++) {
					size_t i;
					if (x < 0 || y < 0) continue;
					i = (size_t)y * (size_t)W + (size_t)x;
					if (mask[i]) bg[i] = mix(bg[i], 0x1a0e06, mask[i] * 120 / 255);
				}
			/* start mark, holes and the cup lie under the walls' edges */
			if (!title) {
				float sx = bpx((float)l->sx + 0.5f), sy = bpy((float)l->sy + 0.5f), r = HOLE_R * (float)cell;
				disc(bg, sx, sy, r * 0.92f, r * 1.0f, 0x3a2412, 110);
				disc(bg, sx, sy, 0.0f, r * 0.14f, 0x3a2412, 110);
			}
			for (int i = 0; i < l->nh; i++) hole_draw(bpx(l->hx[i]), bpy(l->hy[i]), HOLE_R * (float)cell, 0);
			if (l->has_goal) hole_draw(bpx((float)l->gx + 0.5f), bpy((float)l->gy + 0.5f), HOLE_R * (float)cell, 1);
		}
	}
	full_redraw = 1;
}

/* The marble at (cx, cy) pixels, radius r pixels, with its shadow; dim < 1
 * darkens it (falling into a hole). */
static void marble_draw(float cx, float cy, float r, float dim)
{
	const float Lx = -0.42f, Ly = -0.58f, Lz = 0.697f;          /* the light: top left, towards the eye */
	const float Hx = -0.228f, Hy = -0.315f, Hz = 0.921f;        /* halfway to the eye */
	float sx = cx + 0.20f * r, sy = cy + 0.30f * r, sr = 1.12f * r;
	int x0 = (int)floorf(cx - r - 2), y0 = (int)floorf(cy - r - 2);
	int x1 = (int)ceilf(sx + sr + 1), y1 = (int)ceilf(sy + sr + 1);
	if (x0 < 0) x0 = 0;
	if (y0 < 0) y0 = 0;
	if (x1 > W) x1 = W;
	if (y1 > H) y1 = H;
	if (x1 <= x0 || y1 <= y0) return;
	mark(x0, y0, x1, y1);
	for (int y = y0; y < y1; y++)
		for (int x = x0; x < x1; x++) {
			uint32_t *p = cv + (size_t)y * (size_t)W + x;
			float fx = (float)x + 0.5f, fy = (float)y + 0.5f;
			float dx = (fx - cx) / r, dy = (fy - cy) / r, rr = dx * dx + dy * dy, d, cov;
			if (rr < 1.0f + 3.0f / r) {
				d = sqrtf(rr);
				cov = clampf((1.0f - d) * r + 0.5f, 0.0f, 1.0f);
			} else { d = 2.0f; cov = 0.0f; }
			if (cov < 1.0f) {
				/* the shadow on the board */
				float qx = fx - sx, qy = fy - sy, q = sqrtf(qx * qx + qy * qy);
				float a = clampf((sr - q) / (0.45f * r), 0.0f, 1.0f);
				if (a > 0.0f) *p = mix(*p, 0x140a04, (int)(a * a * (3.0f - 2.0f * a) * 125.0f * dim));
			}
			if (cov > 0.0f) {
				float nz = sqrtf(rr < 1.0f ? 1.0f - rr : 0.0f);
				/* the point on the glass, in the marble's own axes */
				float ox = rot[0] * dx + rot[3] * dy + rot[6] * nz;
				float oy = rot[1] * dx + rot[4] * dy + rot[7] * nz;
				float oz = rot[2] * dx + rot[5] * dy + rot[8] * nz;
				float s1 = fabsf(0.20f * ox + 0.90f * oy + 0.39f * oz);     /* a cream ribbon */
				float s2 = fabsf(0.80f * ox - 0.10f * oy - 0.59f * oz);     /* an amber one across it */
				float w1 = clampf((0.21f - s1) * 14.0f, 0.0f, 1.0f), w2 = clampf((0.11f - s2) * 14.0f, 0.0f, 1.0f);
				float br = 22.0f + 30.0f * oz, bgc = 74.0f + 40.0f * oz, bb = 168.0f + 40.0f * oz;
				float diff = dx * Lx + dy * Ly + nz * Lz, h = dx * Hx + dy * Hy + nz * Hz, spec, glow, k;
				br += (240.0f - br) * w1; bgc += (226.0f - bgc) * w1; bb += (190.0f - bb) * w1;
				br += (232.0f - br) * w2 * (1.0f - w1); bgc += (140.0f - bgc) * w2 * (1.0f - w1); bb += (40.0f - bb) * w2 * (1.0f - w1);
				if (diff < 0.0f) diff = 0.0f;
				if (h < 0.0f) h = 0.0f;
				spec = h * h; spec *= spec; spec *= spec; spec *= spec; spec *= spec; spec *= spec;   /* h^64 */
				glow = clampf(dx * 0.50f + dy * 0.62f - 0.22f, 0.0f, 1.0f) * nz * 1.5f;   /* light through the glass */
				k = (0.34f + 0.78f * diff) * (0.62f + 0.38f * nz) + 0.75f * glow;
				k *= dim;
				*p = mix(*p, rgbf(br * k + 255.0f * spec * dim, bgc * k + 255.0f * spec * dim, bb * k + 250.0f * spec * dim),
				         (int)(cov * 256.0f));
			}
		}
}

/* ------------------------------------------------------------------- input */

#define MAXDEV 16
#define NLONG ((KEY_CNT + 8 * (int)sizeof(long) - 1) / (8 * (int)sizeof(long)))
static int dev_fd[MAXDEV], ndev;
static uint8_t keydown[KEY_CNT], keytap[KEY_CNT];
static int have_home, have_power, have_vol, grab = 1, swap_lr, rescan;
static long bot_home_until, bot_up_until, bot_dn_until;   /* the self-test's fingers */

static int test_bit(const unsigned long *b, int n)
{
	return (int)((b[n / (8 * (int)sizeof(long))] >> (n % (8 * (int)sizeof(long)))) & 1UL);
}

static void sync_keys(void)
{
	memset(keydown, 0, sizeof keydown);
	for (int i = 0; i < ndev; i++) {
		unsigned long st[NLONG];
		memset(st, 0, sizeof st);
		if (ioctl(dev_fd[i], EVIOCGKEY(sizeof st), st) < 0) continue;
		for (int c = 0; c < KEY_CNT; c++)
			if (test_bit(st, c)) keydown[c] = 1;
	}
	bl_mask(keydown);
}

static void scan_devs(void)
{
	char names[160] = "";
	for (int i = 0; i < ndev; i++) close(dev_fd[i]);
	ndev = 0;
	have_home = have_power = have_vol = 0;
	for (int i = 0; i < 32 && ndev < MAXDEV; i++) {
		char p[32], name[48] = "?";
		unsigned long bits[NLONG];
		int fd, h, pw, v, kb;
		snprintf(p, sizeof p, "/dev/input/event%d", i);
		fd = open(p, O_RDONLY | O_NONBLOCK | O_CLOEXEC);
		if (fd < 0) continue;
		memset(bits, 0, sizeof bits);
		if (ioctl(fd, EVIOCGBIT(EV_KEY, sizeof bits), bits) < 0) { close(fd); continue; }
		h  = test_bit(bits, KEY_HOMEPAGE) || test_bit(bits, KEY_HOME);
		pw = test_bit(bits, KEY_POWER);
		v  = test_bit(bits, KEY_VOLUMEUP) || test_bit(bits, KEY_VOLUMEDOWN);
		kb = test_bit(bits, KEY_SPACE) && test_bit(bits, KEY_LEFT);
		if (!h && !pw && !v && !kb) { close(fd); continue; }
		have_home |= h; have_power |= pw; have_vol |= v;
		/* Exclusive: keeps the VT keyboard handler from turning the
		 * buttons into console input. */
		if (grab) ioctl(fd, EVIOCGRAB, 1);
		ioctl(fd, EVIOCGNAME(sizeof name - 1), name);
		name[sizeof name - 1] = 0;
		snprintf(names + strlen(names), sizeof names - strlen(names), " %.40s (event%d)", name, i);
		dev_fd[ndev++] = fd;
	}
	{
		static char last[160] = "?";
		if (strcmp(last, names)) logmsg("input:%s", ndev ? names : " no buttons found, still looking");
		snprintf(last, sizeof last, "%s", names);
	}
	sync_keys();
	rescan = 0;
	fakekeys_have(&have_home, &have_power, &have_vol);      /* test builds: $FAKE_KEYS */
	bl_set_keys(have_home, have_power, have_vol);
}

/* One key event (value 0 or 1): the screen-off keys first (backlight.h). */
static void key_event(int code, int value)
{
	if (code < 0 || code >= KEY_CNT || value < 0 || value > 1) return;
	switch (bl_key(code, value)) {
	case BL_PASS:
		if (value) keydown[code] = keytap[code] = 1;
		else keydown[code] = 0;
		break;
	case BL_STATE: keydown[code] = 0; break;
	case BL_TAP: keytap[code] = 1; keydown[code] = 0; break;
	default: break;
	}
}

static void read_events(void)
{
	struct input_event ev[64];
	int dropped = 0;
	memset(keytap, 0, sizeof keytap);
	for (int i = 0; i < ndev; i++) {
		for (;;) {
			ssize_t n = read(dev_fd[i], ev, sizeof ev);
			if (n < 0) {
				if (errno == ENODEV) rescan = 1;
				break;
			}
			if (n == 0) break;
			for (size_t k = 0; k < (size_t)n / sizeof ev[0]; k++) {
				const struct input_event *e = &ev[k];
				if (e->type == EV_SYN && e->code == SYN_DROPPED) { dropped = 1; continue; }
				if (e->type != EV_KEY || e->code >= KEY_CNT) continue;
				key_event(e->code, e->value);
			}
		}
	}
	if (dropped) sync_keys();
}

static int kdn(int c) { return keydown[c] || keytap[c]; }
/* Home; Power when there is no Home key; Enter or Space on a keyboard. */
static int held_home(void)
{
	return tick < bot_home_until || touch_corner_down() || kdn(KEY_HOMEPAGE) || kdn(KEY_HOME) ||
	       kdn(KEY_ENTER) || kdn(KEY_SPACE) || (!have_home && kdn(KEY_POWER));
}
static int held_up(void)
{
	return tick < bot_up_until || kdn(swap_lr ? KEY_VOLUMEDOWN : KEY_VOLUMEUP) || kdn(KEY_LEFT) || kdn(KEY_UP);
}
static int held_dn(void)
{
	return tick < bot_dn_until || kdn(swap_lr ? KEY_VOLUMEUP : KEY_VOLUMEDOWN) || kdn(KEY_RIGHT) || kdn(KEY_DOWN);
}

/* ------------------------------------------------------------------- sound */
/*
 * The loudspeaker, exactly as VOIDMOTHS drives it (game-n66/src/voidmoths.c,
 * from which this code is copied): the ALSA card "iPhone Speaker" (id
 * "Speaker"), MCA2 in TDM mode, CS35L19/21 amplifier, 48 kHz S16_LE mono,
 * through the bare kernel interface (HW_PARAMS, SW_PARAMS, PREPARE,
 * WRITEI_FRAMES on pcmC<n>D0p; the control ioctls on controlC<n> for
 * "Speaker Volume" only).
 *
 * Safety: no other card is opened (found by its id, checked again with
 * CARD_INFO before anything is written); the only control written is the
 * amp's "Speaker Volume", once per open, -20 dB by default and never above
 * -6 dB, read back; the mix is hard-limited to PEAK of full scale.
 *
 * Never blocking: the PCM is opened O_NONBLOCK and gets what fits each
 * frame. Underruns are recovered with PREPARE; any other failure closes the
 * device and the game runs silent.
 */
#define SR          48000
#define NVOICE      12
#define SND_CHUNK   256
#define SND_WAIT    (15 * HZ)
#define VOL_DB      -20.0f
#define VOL_MAX_DB  -6.0f
#define PEAK        0.5f

#ifdef GL_FAKE_ALSA
static int fake_ioctl(int fd, unsigned long req, void *arg);
static int fake_open(const char *path, int flags);
#define xioctl(fd, req, arg) fake_ioctl((fd), (req), (arg))
#define xopen(path, flags) fake_open((path), (flags))
#else
#define xioctl(fd, req, arg) ioctl((fd), (req), (arg))
#define xopen(path, flags) open((path), (flags))
#endif

enum { W_SQUARE, W_NOISE, W_TRI };
typedef struct {
	int on, wave, delay, len, pos;
	float freq, fmul, phase, duty, vol, nval;
	int curve;
	uint16_t lfsr;
} Voice;

enum { SND_OFF, SND_SEARCH, SND_ON, SND_NONE, SND_FAIL };
static Voice vc[NVOICE];
static int snd_state = SND_SEARCH, snd_card = -1, snd_pcm = -1;
static long snd_deadline, snd_next_try, snd_underruns;
static unsigned long snd_period, snd_bufsize;
static float snd_vol_db = VOL_DB;
static int16_t snd_buf[SND_CHUNK];
static int snd_len, snd_off;
static float hp_x, hp_y;
static const char *snd_root = "";   /* path prefix, for the test build only */
static float roll_speed;            /* cells/s: the rumble follows it */
static struct { float amp, phase, nval, lp; uint16_t lfsr; } rum = { .lfsr = 0x3b5du };

static int snd_live(void) { return snd_state == SND_ON || bot; }

static void voice(int wf, float f0, float f1, float ms, float vol, float duty, int curve, float delay_ms)
{
	Voice *v = NULL;
	int len = (int)(ms * (float)SR / 1000.0f);
	if (!snd_live()) return;
	for (int i = 0; i < NVOICE && !v; i++)
		if (!vc[i].on) v = &vc[i];
	if (!v) {
		int best = INT_MAX;
		for (int i = 0; i < NVOICE; i++) {
			int left = vc[i].delay + vc[i].len - vc[i].pos;
			if (left < best) { best = left; v = &vc[i]; }
		}
	}
	if (len < 1) len = 1;
	*v = (Voice){ 0 };
	v->on = 1; v->wave = wf; v->len = len; v->curve = curve;
	v->delay = (int)(delay_ms * (float)SR / 1000.0f);
	v->freq = f0; v->fmul = powf(f1 / f0, 1.0f / (float)len);
	v->duty = duty; v->vol = vol; v->lfsr = 0x4ac1u; v->nval = 1.0f;
}

/* The effects. Frequencies stay above ~300 Hz: the phone speaker is tiny. */
static void sfx_notice(void)
{
	voice(W_TRI, 1175.0f, 1175.0f, 150.0f, 0.16f, 0.0f, 1, 0.0f);
	voice(W_TRI, 1568.0f, 1568.0f, 340.0f, 0.16f, 0.0f, 1, 140.0f);
}
static void sfx_click(float hard)       /* a wall hit, hard: 0..1 */
{
	voice(W_NOISE, 7000.0f, 2500.0f, 22.0f, 0.06f + 0.20f * hard, 0.0f, 2, 0.0f);
	voice(W_TRI, 1500.0f - 500.0f * hard, 700.0f, 30.0f + 30.0f * hard, 0.05f + 0.14f * hard, 0.0f, 2, 0.0f);
}
static void sfx_fall(void)
{
	voice(W_TRI, 900.0f, 320.0f, 420.0f, 0.22f, 0.0f, 1, 0.0f);
	voice(W_NOISE, 3000.0f, 500.0f, 180.0f, 0.12f, 0.0f, 2, 380.0f);
}
static void sfx_start(void)
{
	voice(W_TRI, 784.0f, 784.0f, 110.0f, 0.16f, 0.0f, 1, 0.0f);
	voice(W_TRI, 1175.0f, 1175.0f, 200.0f, 0.16f, 0.0f, 1, 100.0f);
}
static void sfx_select(void) { voice(W_TRI, 1320.0f, 1320.0f, 50.0f, 0.12f, 0.0f, 1, 0.0f); }
static void sfx_win(int best)
{
	static const float f[6] = { 784.0f, 988.0f, 1175.0f, 1568.0f, 1976.0f, 2349.0f };
	for (int i = 0; i < (best ? 6 : 4); i++) voice(W_TRI, f[i], f[i], 220.0f, 0.17f, 0.0f, 1, 95.0f * (float)i);
	voice(W_NOISE, 5000.0f, 5000.0f, 30.0f, 0.10f, 0.0f, 2, 0.0f);   /* the marble drops into the cup */
}

static void synth(int16_t *out, int n)
{
	float target = clampf(roll_speed / 7.0f, 0.0f, 1.0f) * 0.085f;
	float clock = (520.0f + 300.0f * clampf(roll_speed, 0.0f, VMAX)) / (float)SR;
	for (int k = 0; k < n; k++) {
		float mixv = 0.0f, y;
		for (int i = 0; i < NVOICE; i++) {
			Voice *v = &vc[i];
			float env, s;
			if (!v->on) continue;
			if (v->delay > 0) { v->delay--; continue; }
			env = 1.0f - (float)v->pos / (float)v->len;
			if (v->curve == 2) env *= env;
			if (v->pos < 96) env *= (float)v->pos / 96.0f;   /* 2 ms attack: no click */
			v->phase += v->freq / (float)SR;
			if (v->wave == W_NOISE) {
				while (v->phase >= 1.0f) {
					unsigned bit = (v->lfsr ^ (v->lfsr >> 1)) & 1u;
					v->phase -= 1.0f;
					v->lfsr = (uint16_t)((v->lfsr >> 1) | (bit << 14));
					v->nval = (v->lfsr & 1u) ? 1.0f : -1.0f;
				}
				s = v->nval;
			} else {
				if (v->phase >= 1.0f) v->phase -= floorf(v->phase);
				if (v->wave == W_TRI) s = 4.0f * fabsf(v->phase - 0.5f) - 1.0f;
				else s = 2.0f * ((v->phase < v->duty ? 1.0f : 0.0f) - v->duty);
			}
			mixv += s * env * v->vol;
			v->freq *= v->fmul;
			if (++v->pos >= v->len) v->on = 0;
		}
		/* the rolling rumble: held noise clocked by the speed, softened */
		rum.amp += (target - rum.amp) * 0.0008f;
		if (rum.amp > 0.0005f) {
			rum.phase += clock;
			while (rum.phase >= 1.0f) {
				unsigned bit = (rum.lfsr ^ (rum.lfsr >> 1)) & 1u;
				rum.phase -= 1.0f;
				rum.lfsr = (uint16_t)((rum.lfsr >> 1) | (bit << 14));
				rum.nval = (rum.lfsr & 1u) ? 1.0f : -1.0f;
			}
			rum.lp += (rum.nval - rum.lp) * 0.18f;
			mixv += rum.lp * rum.amp;
		}
		/* DC blocker (~20 Hz high-pass), then the hard limit */
		y = mixv - hp_x + 0.9974f * hp_y;
		hp_x = mixv; hp_y = y;
		if (y > PEAK) y = PEAK;
		if (y < -PEAK) y = -PEAK;
		out[k] = (int16_t)lrintf(y * 32767.0f);
	}
}

/* The card number of the speaker in /proc/asound/cards, or -1. Lines read
 * " 0 [Speaker        ]: simple-card - iPhone Speaker". */
static int snd_find_card(void)
{
	char p[160], line[256];
	int found = -1;
	FILE *f;
	if (bot && !snd_root[0]) return -1;   /* the self-test never touches real devices */
	snprintf(p, sizeof p, "%s/proc/asound/cards", snd_root);
	f = fopen(p, "re");
	if (!f) return -1;
	while (found < 0 && fgets(line, sizeof line, f)) {
		char id[32];
		const char *sname;
		int n, l;
		if (sscanf(line, " %d [%31[^]]]", &n, id) != 2 || n < 0 || n > 31) continue;
		for (l = (int)strlen(id); l > 0 && id[l - 1] == ' '; l--) id[l - 1] = 0;
		sname = strstr(line, "]: ");
		sname = sname ? strstr(sname, " - ") : NULL;
		if (!strcmp(id, "Speaker") || (sname && !strncmp(sname + 3, "iPhone Speaker", 14))) found = n;
	}
	fclose(f);
	return found;
}

static void snd_close(void)
{
	if (snd_pcm >= 0) {
		close(snd_pcm);
		logmsg("sound: closed (%ld underruns)", snd_underruns);
	}
	snd_pcm = -1;
	snd_len = snd_off = 0;
	memset(vc, 0, sizeof vc);
	rum.amp = 0.0f;
}

static void snd_fail(const char *what, int err)
{
	logmsg("sound: %s failed: %s; running silent", what, err ? strerror(err) : "unexpected answer");
	snd_close();
	snd_state = SND_FAIL;
}

static int64_t tlv_cdb(const unsigned *tlv, long vmin, long vmax, long v)
{
	if (tlv[0] == SNDRV_CTL_TLVT_DB_SCALE)
		return (int64_t)(int)tlv[2] + (int64_t)(v - vmin) * (int64_t)(tlv[3] & 0xffffu);
	return (int64_t)(int)tlv[2] +
	       ((int64_t)(int)tlv[3] - (int64_t)(int)tlv[2]) * (v - vmin) / (vmax > vmin ? vmax - vmin : 1);
}

/* "Speaker Volume" to snd_vol_db (never above VOL_MAX_DB), checked by reading it back. */
static int snd_set_volume(int ctl)
{
	static const char *names[] = { "Speaker Volume", "Speaker Playback Volume" };
	struct snd_ctl_elem_info info;
	struct snd_ctl_elem_value val;
	unsigned buf[2 + 32], *tlv = buf + 2;
	struct snd_ctl_tlv *t = (struct snd_ctl_tlv *)buf;
	long vmin, vmax, v = -1;
	int64_t target = (int64_t)lrintf(snd_vol_db * 100.0f);
	int ok = 0;

	if (target > (int64_t)lrintf(VOL_MAX_DB * 100.0f)) target = (int64_t)lrintf(VOL_MAX_DB * 100.0f);
	for (size_t i = 0; i < sizeof names / sizeof names[0] && !ok; i++) {
		memset(&info, 0, sizeof info);
		info.id.iface = SNDRV_CTL_ELEM_IFACE_MIXER;
		snprintf((char *)info.id.name, sizeof info.id.name, "%s", names[i]);
		ok = xioctl(ctl, SNDRV_CTL_IOCTL_ELEM_INFO, &info) == 0;
	}
	if (!ok) { snd_fail("finding the 'Speaker Volume' control", errno); return -1; }
	vmin = info.value.integer.min; vmax = info.value.integer.max;
	if (info.type != SNDRV_CTL_ELEM_TYPE_INTEGER || info.count < 1 || info.count > 8 ||
	    !(info.access & SNDRV_CTL_ELEM_ACCESS_WRITE) || !(info.access & SNDRV_CTL_ELEM_ACCESS_TLV_READ) ||
	    vmax <= vmin || vmax - vmin > 4096) {
		snd_fail("checking 'Speaker Volume' (type, range or no dB scale)", 0);
		return -1;
	}
	memset(buf, 0, sizeof buf);
	t->numid = info.id.numid;
	t->length = (unsigned)(sizeof buf - 2 * sizeof buf[0]);
	if (xioctl(ctl, SNDRV_CTL_IOCTL_TLV_READ, t) < 0) { snd_fail("reading the dB scale", errno); return -1; }
	if (!((tlv[0] == SNDRV_CTL_TLVT_DB_SCALE || tlv[0] == SNDRV_CTL_TLVT_DB_MINMAX ||
	       tlv[0] == SNDRV_CTL_TLVT_DB_MINMAX_MUTE) && tlv[1] >= 2 * sizeof(unsigned))) {
		snd_fail("reading the dB scale (unknown TLV type)", 0);
		return -1;
	}
	/* the loudest setting that is not above the target */
	for (long x = vmax; x >= vmin && v < 0; x--)
		if (tlv_cdb(tlv, vmin, vmax, x) <= target) v = x;
	if (v < 0 || v == vmin) { snd_fail("choosing a volume (scale out of range)", 0); return -1; }
	memset(&val, 0, sizeof val);
	val.id.numid = info.id.numid;
	for (unsigned i = 0; i < info.count; i++) val.value.integer.value[i] = v;
	if (xioctl(ctl, SNDRV_CTL_IOCTL_ELEM_WRITE, &val) < 0) { snd_fail("setting 'Speaker Volume'", errno); return -1; }
	memset(&val, 0, sizeof val);
	val.id.numid = info.id.numid;
	if (xioctl(ctl, SNDRV_CTL_IOCTL_ELEM_READ, &val) < 0) { snd_fail("reading 'Speaker Volume' back", errno); return -1; }
	for (unsigned i = 0; i < info.count; i++)
		if (val.value.integer.value[i] != v) {
			snd_fail("reading 'Speaker Volume' back (value differs)", 0);
			return -1;
		}
	logmsg("sound: '%s' = %ld of %ld..%ld (%.1f dB)", (const char *)info.id.name, v, vmin, vmax,
	       (double)tlv_cdb(tlv, vmin, vmax, v) / 100.0);
	return 0;
}

static struct snd_mask *hw_mask(struct snd_pcm_hw_params *p, int n)
{
	return &p->masks[n - SNDRV_PCM_HW_PARAM_FIRST_MASK];
}
static struct snd_interval *hw_iv(struct snd_pcm_hw_params *p, int n)
{
	return &p->intervals[n - SNDRV_PCM_HW_PARAM_FIRST_INTERVAL];
}
static void hw_set_mask(struct snd_pcm_hw_params *p, int n, unsigned bit)
{
	struct snd_mask *m = hw_mask(p, n);
	memset(m, 0, sizeof *m);
	m->bits[bit >> 5] = 1u << (bit & 31);
}
static void hw_set(struct snd_pcm_hw_params *p, int n, unsigned v)
{
	struct snd_interval *i = hw_iv(p, n);
	i->min = i->max = v;
	i->integer = 1;
}

static int snd_hw_params(unsigned period, unsigned periods)
{
	struct snd_pcm_hw_params p;
	memset(&p, 0, sizeof p);
	for (int n = SNDRV_PCM_HW_PARAM_FIRST_MASK; n <= SNDRV_PCM_HW_PARAM_LAST_MASK; n++)
		memset(hw_mask(&p, n), 0xff, sizeof(struct snd_mask));
	for (int n = SNDRV_PCM_HW_PARAM_FIRST_INTERVAL; n <= SNDRV_PCM_HW_PARAM_LAST_INTERVAL; n++)
		hw_iv(&p, n)->max = ~0u;
	p.rmask = ~0u;
	p.info = ~0u;
	hw_set_mask(&p, SNDRV_PCM_HW_PARAM_ACCESS, SNDRV_PCM_ACCESS_RW_INTERLEAVED);
	hw_set_mask(&p, SNDRV_PCM_HW_PARAM_FORMAT, (unsigned)SNDRV_PCM_FORMAT_S16_LE);
	hw_set_mask(&p, SNDRV_PCM_HW_PARAM_SUBFORMAT, (unsigned)SNDRV_PCM_SUBFORMAT_STD);
	hw_set(&p, SNDRV_PCM_HW_PARAM_SAMPLE_BITS, 16);
	hw_set(&p, SNDRV_PCM_HW_PARAM_FRAME_BITS, 16);
	hw_set(&p, SNDRV_PCM_HW_PARAM_CHANNELS, 1);
	hw_set(&p, SNDRV_PCM_HW_PARAM_RATE, SR);
	hw_set(&p, SNDRV_PCM_HW_PARAM_PERIOD_SIZE, period);
	hw_set(&p, SNDRV_PCM_HW_PARAM_PERIODS, periods);
	if (xioctl(snd_pcm, SNDRV_PCM_IOCTL_HW_PARAMS, &p) < 0) return -1;
	if (hw_iv(&p, SNDRV_PCM_HW_PARAM_RATE)->min != SR || hw_iv(&p, SNDRV_PCM_HW_PARAM_CHANNELS)->min != 1) {
		errno = EINVAL;
		return -1;
	}
	snd_period = hw_iv(&p, SNDRV_PCM_HW_PARAM_PERIOD_SIZE)->min;
	snd_bufsize = hw_iv(&p, SNDRV_PCM_HW_PARAM_BUFFER_SIZE)->min;
	if (snd_period < 1 || snd_bufsize < 2 * snd_period || snd_bufsize > SR) {
		errno = EINVAL;
		return -1;
	}
	return 0;
}

/* Open the speaker card found in /proc/asound/cards. 0: sound is on. */
static int snd_open(void)
{
	/* 43 ms of buffer: two and a half frames of headroom. Fallbacks in order. */
	static const unsigned cfg[][2] = { { 512, 4 }, { 256, 8 }, { 1024, 4 }, { 480, 6 } };
	struct snd_ctl_card_info ci;
	struct snd_pcm_info pi;
	struct snd_pcm_sw_params sw;
	char p[160];
	int ctl, ok = 0;

	snprintf(p, sizeof p, "%s/dev/snd/controlC%d", snd_root, snd_card);
	ctl = xopen(p, O_RDWR | O_NONBLOCK | O_CLOEXEC);
	if (ctl < 0) { snd_fail("opening the control device", errno); return -1; }
	memset(&ci, 0, sizeof ci);
	if (xioctl(ctl, SNDRV_CTL_IOCTL_CARD_INFO, &ci) < 0) {
		close(ctl);
		snd_fail("CARD_INFO", errno);
		return -1;
	}
	ci.id[sizeof ci.id - 1] = 0;
	ci.name[sizeof ci.name - 1] = 0;
	if (ci.card != snd_card || (strcmp((const char *)ci.id, "Speaker") && strcmp((const char *)ci.name, "iPhone Speaker"))) {
		logmsg("sound: card %d is '%s' ('%s'), not the speaker", snd_card, (const char *)ci.id, (const char *)ci.name);
		close(ctl);
		snd_fail("checking the card", 0);
		return -1;
	}
	ok = snd_set_volume(ctl) == 0;
	close(ctl);
	if (!ok) return -1;

	snprintf(p, sizeof p, "%s/dev/snd/pcmC%dD0p", snd_root, snd_card);
	snd_pcm = xopen(p, O_RDWR | O_NONBLOCK | O_CLOEXEC);
	if (snd_pcm < 0) { snd_fail("opening the PCM", errno); return -1; }
	memset(&pi, 0, sizeof pi);
	if (xioctl(snd_pcm, SNDRV_PCM_IOCTL_INFO, &pi) < 0) { snd_fail("PCM INFO", errno); return -1; }
	if ((int)pi.card != snd_card || pi.stream != SNDRV_PCM_STREAM_PLAYBACK) { snd_fail("checking the PCM", 0); return -1; }
	ok = 0;
	for (size_t i = 0; i < sizeof cfg / sizeof cfg[0] && !ok; i++)
		ok = snd_hw_params(cfg[i][0], cfg[i][1]) == 0;
	if (!ok) { snd_fail("HW_PARAMS (48 kHz, S16_LE, mono)", errno); return -1; }
	memset(&sw, 0, sizeof sw);
	sw.tstamp_mode = SNDRV_PCM_TSTAMP_NONE;
	sw.period_step = 1;
	sw.avail_min = snd_period;
	sw.start_threshold = 2 * snd_period;
	sw.stop_threshold = snd_bufsize;
	sw.boundary = snd_bufsize;
	while (sw.boundary <= (unsigned long)LONG_MAX / 4) sw.boundary *= 2;
	sw.proto = SNDRV_PCM_VERSION;
	if (xioctl(snd_pcm, SNDRV_PCM_IOCTL_SW_PARAMS, &sw) < 0) { snd_fail("SW_PARAMS", errno); return -1; }
	if (xioctl(snd_pcm, SNDRV_PCM_IOCTL_PREPARE, NULL) < 0) { snd_fail("PREPARE", errno); return -1; }
	snd_state = SND_ON;
	snd_len = snd_off = 0;
	snd_underruns = 0;
	logmsg("sound: on, card %d, %lu-frame periods, %lu-frame buffer (%lu ms)",
	       snd_card, snd_period, snd_bufsize, snd_bufsize * 1000 / SR);
	return 0;
}

/* Write what fits; never waits. */
static void snd_pump(void)
{
	int xruns = 0;
	if (snd_state != SND_ON) return;
	for (int guard = 0; guard < 64; guard++) {
		struct snd_xferi x;
		if (snd_len == 0) {
			synth(snd_buf, SND_CHUNK);
			snd_len = SND_CHUNK;
			snd_off = 0;
		}
		memset(&x, 0, sizeof x);
		x.buf = snd_buf + snd_off;
		x.frames = (snd_pcm_uframes_t)snd_len;
		if (xioctl(snd_pcm, SNDRV_PCM_IOCTL_WRITEI_FRAMES, &x) < 0) {
			int err = errno;
			if (err == EAGAIN) break;
			if (err == EINTR) continue;
			if ((err == EPIPE || err == ESTRPIPE) && ++xruns <= 3) {
				snd_underruns++;
				if (err == ESTRPIPE && xioctl(snd_pcm, SNDRV_PCM_IOCTL_RESUME, NULL) == 0) continue;
				if (err == ESTRPIPE && errno == EAGAIN) break;
				if (xioctl(snd_pcm, SNDRV_PCM_IOCTL_PREPARE, NULL) == 0) continue;
				err = errno;
			}
			snd_fail("writing to the PCM", err);
			return;
		}
		if (x.result <= 0 || x.result > snd_len) break;
		snd_off += (int)x.result;
		snd_len -= (int)x.result;
	}
}

static void snd_start_search(void)
{
	snd_state = SND_SEARCH;
	snd_deadline = tick + SND_WAIT;
	snd_next_try = tick;
}

/* Once per frame: look for the card, then keep the buffer full. */
static void snd_tick(void)
{
	if (snd_state == SND_SEARCH && tick >= snd_next_try) {
		snd_next_try = tick + HZ;
		snd_card = snd_find_card();
		if (snd_card >= 0) {
			if (snd_open() == 0) sfx_start();
		} else if (tick >= snd_deadline) {
			snd_state = SND_NONE;
			logmsg("sound: no 'Speaker' card in %s/proc/asound/cards after %d s; running silent",
			       snd_root, SND_WAIT / HZ);
		}
	}
	if (snd_state == SND_ON) snd_pump();
	else if (bot) {
		/* the self-test exercises the mixer even without a device */
		int16_t scratch[SR / HZ];
		synth(scratch, SR / HZ);
	}
}

static long st_toggles;
static void snd_toggle(void)
{
	st_toggles++;
	switch (snd_state) {
	case SND_ON:
		snd_close();
		snd_state = SND_OFF;
		logmsg("sound: off");
		break;
	case SND_SEARCH:
		snd_state = SND_OFF;
		break;
	case SND_OFF:
	case SND_NONE:
		logmsg("sound: on requested");
		snd_start_search();
		break;
	default:
		break;   /* SND_FAIL: silent until the game restarts */
	}
}

/* The screen goes dark: no sound, the card closed; looked for again when
 * the screen is back. */
static int snd_was_on;
static void snd_suspend(void)
{
	snd_was_on = snd_state == SND_ON || snd_state == SND_SEARCH;
	snd_close();
	if (snd_was_on) {
		snd_state = SND_OFF;
		logmsg("sound: paused while the screen is off");
	}
}

static void snd_resume(void)
{
	if (!snd_was_on) return;
	snd_was_on = 0;
	snd_start_search();
}

static const char *snd_status(void)
{
	switch (snd_state) {
	case SND_ON:     return "SOUND ON";
	case SND_OFF:    return "SOUND OFF";
	case SND_SEARCH: return "SOUND...";
	case SND_NONE:   return "NO SPEAKER";
	default:         return "SOUND ERROR";
	}
}

/* ----------------------------------------------------------------- notices */
/*
 * `notice TEXT` on the phone (or `orchard exec -- notice TEXT` from the Mac)
 * writes /run/notice. Twice a second the game stat()s it; when its mtime,
 * inode or size changes, the first line, cleaned to printable ASCII and cut
 * to 60 characters, is shown for 6 s at the top, with a soft chime. A newer
 * notice replaces the one on screen. A file that is already there when the
 * game starts is shown only if it is less than 30 s old.
 */
#define NOTE_LEN   60
#define NOTE_TICKS (6 * HZ)
static const char *notice_path = "/run/notice";
static char note_cur[NOTE_LEN + 1];
static long note_t, st_notices;
static struct { int seen; long long sec, nsec, size; unsigned long long ino; } note_st;

static void notice_poll(int first)
{
	struct stat sb;
	char raw[512];
	ssize_t n;
	size_t o = 0;
	int fd;
	if (!notice_path) return;
	if (stat(notice_path, &sb) < 0 || !S_ISREG(sb.st_mode)) {
		note_st.seen = 1; note_st.sec = -1; note_st.ino = 0;
		return;
	}
	if (note_st.seen && note_st.sec == (long long)sb.st_mtim.tv_sec && note_st.nsec == (long long)sb.st_mtim.tv_nsec &&
	    note_st.ino == (unsigned long long)sb.st_ino && note_st.size == (long long)sb.st_size)
		return;
	note_st.seen = 1;
	note_st.sec = (long long)sb.st_mtim.tv_sec; note_st.nsec = (long long)sb.st_mtim.tv_nsec;
	note_st.ino = (unsigned long long)sb.st_ino; note_st.size = (long long)sb.st_size;
	if (first) {
		struct timespec now;
		clock_gettime(CLOCK_REALTIME, &now);
		if (now.tv_sec - sb.st_mtim.tv_sec > 30 || sb.st_mtim.tv_sec - now.tv_sec > 30) return;
	}
	fd = open(notice_path, O_RDONLY | O_NONBLOCK | O_NOCTTY | O_CLOEXEC);
	if (fd < 0) return;
	if (fstat(fd, &sb) < 0 || !S_ISREG(sb.st_mode)) { close(fd); return; }
	n = read(fd, raw, sizeof raw);
	close(fd);
	if (n <= 0) return;
	/* first line, printable ASCII, runs of blanks as one */
	for (ssize_t i = 0; i < n && raw[i] != '\n' && raw[i] != '\r' && raw[i] && o < NOTE_LEN; i++) {
		unsigned char c = (unsigned char)raw[i];
		if (c == '\t') c = ' ';
		if (c >= 0x80) { if (c >= 0xc0) note_cur[o++] = '?'; continue; }
		if (c < 0x20 || c == 0x7f) continue;
		if (c == ' ' && (o == 0 || note_cur[o - 1] == ' ')) continue;
		note_cur[o++] = (char)c;
	}
	while (o > 0 && note_cur[o - 1] == ' ') o--;
	note_cur[o] = 0;
	if (!o) return;
	note_t = NOTE_TICKS;
	st_notices++;
	sfx_notice();
	logmsg("notice: '%s'", note_cur);
}

/* ------------------------------------------------------------------- state */

enum { S_TITLE, S_PLAY, S_PAUSE, S_FALL, S_WIN, S_DONE };
static int state = S_TITLE, state_t, level, sel;
static const Level *lv = &tray;
static Ball ball;
static long steps, falls;       /* physics steps on the clock; falls this board */
static int clock_on, fall_hole, new_best;
static float fall_x, fall_y;
static long best[NLEV];         /* centiseconds, 0: none yet */
static const char *best_path = "/run/glint.best";
static float tilt_x, tilt_y;    /* what moves the marble now, each -1..1 */
static float fb_angle = (float)M_PI / 2.0f;   /* no sensor: where gravity points (down the screen) */
static int home_t, home_latch, up_arm, dn_arm, both_latch, was_home, was_up, was_dn;
static int pilot = 1;           /* the self-test's autopilot */
static int t_seen;              /* the touchscreen was there when the title was drawn */
static long st_ttaps, st_tswipes, st_tsteer;
static int click_cd;
static volatile sig_atomic_t quit_flag;
static long st_wins, st_falls, st_pauses, st_quits, st_calib, st_picker_exit;
static int st_cleared;          /* bit per board solved */
static Layer ly_board, ly_hud, ly_bub;

static long centis(long s) { return s * 100 / (HZ * SUB); }

static const char *best_str(long cs, char *buf, size_t n)
{
	if (cs <= 0) snprintf(buf, n, "-:--.--");
	else {
		if (cs > 599999) cs = 599999;
		snprintf(buf, n, "%ld:%02ld.%02ld", cs / 6000, cs / 100 % 60, cs % 100);
	}
	return buf;
}

/* The best-times file: "<boards cleared>\n" then one line of centiseconds
 * per board (the first number is what a game picker may show as a score). */
static void best_load(void)
{
	FILE *f;
	long n;
	if (!best_path) return;
	f = fopen(best_path, "re");
	if (!f) return;
	if (fscanf(f, "%ld", &n) == 1)
		for (int i = 0; i < NLEV; i++)
			if (fscanf(f, "%ld", &best[i]) != 1 || best[i] < 0 || best[i] > 599999) best[i] = 0;
	fclose(f);
}

static void best_save(void)
{
	char tmp[300];
	FILE *f;
	int n = 0;
	if (!best_path) return;
	snprintf(tmp, sizeof tmp, "%.280s.tmp", best_path);
	f = fopen(tmp, "we");
	if (!f) { logmsg("best times: %s: %s", tmp, strerror(errno)); return; }
	for (int i = 0; i < NLEV; i++) n += best[i] > 0;
	fprintf(f, "%d\n", n);
	for (int i = 0; i < NLEV; i++) fprintf(f, "%ld\n", best[i]);
	if (fclose(f) != 0 || rename(tmp, best_path) != 0) logmsg("best times: %s: %s", best_path, strerror(errno));
}

static void bot_new_board(void);

static void set_state(int s) { state = s; state_t = 0; }

static void to_title(void)
{
	lv = &tray;
	ball_reset(lv, &ball);
	set_state(S_TITLE);
	if (cv) build_bg(lv, 1);
}

static void start_level(int n)
{
	level = sel = n;
	lv = &levels[n];
	ball_reset(lv, &ball);
	steps = falls = 0;
	clock_on = 0;
	fb_angle = (float)M_PI / 2.0f;
	set_state(S_PLAY);
	if (cv) build_bg(lv, 0);
	sfx_start();
	logmsg("board %d (%s)%s", n + 1, lv->name, sn_usable() ? "" : ", no motion sensor: volume keys");
	bot_new_board();
}

static void win(void)
{
	long cs = centis(steps);
	if (cs < 1) cs = 1;
	new_best = best[level] == 0 || cs < best[level];
	if (new_best) { best[level] = cs; best_save(); }
	ball.x = (float)lv->gx + 0.5f; ball.y = (float)lv->gy + 0.5f;
	ball.vx = ball.vy = 0.0f;
	st_wins++;
	st_cleared |= 1 << level;
	sfx_win(new_best);
	logmsg("board %d (%s) cleared in %ld.%02ld s, %ld falls%s", level + 1, lv->name, cs / 100, cs % 100, falls,
	       new_best ? ", a best time" : "");
	set_state(S_WIN);
}

static void home_short(void)
{
	switch (state) {
	case S_TITLE:
		sn_calibrate();
		st_calib++;
		start_level(sel);
		break;
	case S_PLAY: st_pauses++; set_state(S_PAUSE); break;
	case S_PAUSE: set_state(S_PLAY); break;
	case S_WIN:
		if (level + 1 < NLEV) start_level(level + 1);
		else set_state(S_DONE);
		break;
	case S_DONE: sel = 0; to_title(); break;
	default: break;
	}
}

static void home_long(void)
{
	if (state == S_TITLE) {
		if (from_picker) { st_picker_exit++; logmsg("back to the picker"); quit_flag = 1; }
		return;
	}
	st_quits++;
	sel = level;
	to_title();
}

static void vol_tap(int up)
{
	if (state == S_TITLE) {
		sel = (sel + (up ? 1 : NLEV - 1)) % NLEV;
		sfx_select();
	} else if (state == S_PAUSE) {
		if (up) { sn_calibrate(); st_calib++; sfx_select(); set_state(S_PLAY); }
		else start_level(level);
	}
}

/*
 * The touchscreen, once a frame: a tap is a short Home press (title:
 * calibrate and start; paused: resume; in the cup: next board), except in
 * play, where only a tap on the top bar pauses (a finger on the board may
 * be steering); a swipe left or right on the title chooses the board. The
 * corner is the Home button itself (held_home()).
 */
static void touch_tick(void)
{
	TGesture g;
	touch_view(W, H, 0, 0, 1, W, H, W / 6, H / 12);
	touch_poll(bot ? (int64_t)tick * 1000 / HZ : now_ns() / 1000000, grab);
	if (touch_present() != t_seen && state == S_TITLE && cv) {
		t_seen = touch_present();
		build_bg(lv, 1);    /* the title's hints follow what is there */
		full_redraw = 1;
	}
	while (touch_next(&g)) {
		if (g.kind == TG_TAP) {
			st_ttaps++;
			if (state != S_PLAY || g.y < by0) home_short();
		} else if (g.kind == TG_SWIPE && state == S_TITLE && (g.dir == TD_LEFT || g.dir == TD_RIGHT)) {
			st_tswipes++;
			vol_tap(g.dir == TD_LEFT);
		}
	}
}

static void keys_tick(void)
{
	int h = held_home(), u = held_up(), d = held_dn();
	if (h && !was_home) { home_t = 0; home_latch = 0; }
	if (h) {
		home_t++;
		if (home_t >= HOLD_QUIT && !home_latch) { home_latch = 1; home_long(); }
	} else if (was_home) {
		if (!home_latch) home_short();
		home_t = 0;
	}
	if (u && !was_up) up_arm = 1;
	if (d && !was_dn) dn_arm = 1;
	if (u && d) {
		if (!both_latch) { both_latch = 1; snd_toggle(); }
		up_arm = dn_arm = 0;
	} else if (!u && !d) both_latch = 0;
	if (!u && was_up && up_arm) { up_arm = 0; vol_tap(1); }
	if (!d && was_dn && dn_arm) { dn_arm = 0; vol_tap(0); }
	was_home = h; was_up = u; was_dn = d;
}

/* Where gravity pulls the marble this frame. */
static void tilt_tick(void)
{
	if (sn_usable()) { sn_tilt(&tilt_x, &tilt_y); return; }
	if (state == S_PLAY && touch_down()) {
		/* no sensor, a finger down: gravity pulls towards it */
		float dx = (float)tp.x - bpx(ball.x), dy = (float)tp.y - bpy(ball.y);
		if (dx * dx + dy * dy > 1.0f) fb_angle = atan2f(dy, dx);
		st_tsteer++;
	}
	if (state == S_PLAY) {
		/* the volume keys turn gravity; it always pulls */
		if (held_up() && !held_dn()) fb_angle -= 2.6f / (float)HZ;
		if (held_dn() && !held_up()) fb_angle += 2.6f / (float)HZ;
		if (fb_angle > (float)M_PI) fb_angle -= 2.0f * (float)M_PI;
		if (fb_angle < -(float)M_PI) fb_angle += 2.0f * (float)M_PI;
		tilt_x = 0.45f * cosf(fb_angle); tilt_y = 0.45f * sinf(fb_angle);
	} else { tilt_x = 0.0f; tilt_y = 0.30f; }
}

static void game_tick(void)
{
	touch_tick();
	if (quit_flag) return;
	keys_tick();
	if (quit_flag) return;
	tilt_tick();
	state_t++;
	if (click_cd > 0) click_cd--;
	roll_speed = 0.0f;
	if (state == S_TITLE || state == S_PLAY) {
		float hardest = 0.0f;
		for (int i = 0; i < SUB; i++) {
			float imp, ox = ball.x, oy = ball.y;
			int which = 0, ev = phys_step(lv, &ball, ACC * tilt_x, ACC * tilt_y, state == S_PLAY, &imp, &which);
			rot_roll(ball.x - ox, ball.y - oy);
			if (imp > hardest) hardest = imp;
			if (state != S_PLAY) continue;
			if (!clock_on && (fabsf(ball.x - ((float)lv->sx + 0.5f)) > 0.04f || fabsf(ball.y - ((float)lv->sy + 0.5f)) > 0.04f))
				clock_on = 1;
			if (clock_on && steps < 599999L * (HZ * SUB) / 100) steps++;
			if (ev == EV_HOLE) {
				fall_hole = which; fall_x = ball.x; fall_y = ball.y;
				falls++; st_falls++;
				sfx_fall();
				set_state(S_FALL);
				break;
			}
			if (ev == EV_GOAL) { win(); break; }
		}
		if (tick % 600 == 0) rot_fix();
		roll_speed = sqrtf(ball.vx * ball.vx + ball.vy * ball.vy);
		if (hardest > 0.7f && click_cd == 0) { sfx_click(clampf(hardest / 7.0f, 0.0f, 1.0f)); click_cd = 4; }
	} else if (state == S_FALL) {
		if (clock_on) steps += SUB;         /* the clock runs on while it drops */
		if (state_t >= HZ * 7 / 10) {
			ball_reset(lv, &ball);
			set_state(S_PLAY);
		}
	}
	if (note_t > 0) note_t--;
	if (tick % (HZ / 2) == 0) notice_poll(0);
}

/* ------------------------------------------------------------------ render */

static int drew;
static int layer_begin(Layer *l, const char *key)
{
	if (!full_redraw && l->valid && !strcmp(l->key, key)) return 0;
	if (!full_redraw)
		for (int i = 0; i < l->nold; i++) {
			Rect r = l->old[i];
			for (int y = r.y0; y < r.y1; y++)
				memcpy(cv + (size_t)y * (size_t)W + r.x0, bg + (size_t)y * (size_t)W + r.x0, (size_t)(r.x1 - r.x0) * 4);
		}
	l->n = 0;
	drew = 1;
	snprintf(l->key, sizeof l->key, "%s", key);
	l->valid = 1;
	cur_layer = l;
	return 1;
}

static void layer_end(Layer *l)
{
	cur_layer = NULL;
	if (!full_redraw) {
		Rect u;
		int joined = 0;
		if (l->nold == 1 && l->n == 1) {
			/* the marble's old and new place overlap: one rectangle for both */
			Rect a = l->old[0], b = l->r[0];
			u = (Rect){ a.x0 < b.x0 ? a.x0 : b.x0, a.y0 < b.y0 ? a.y0 : b.y0, a.x1 > b.x1 ? a.x1 : b.x1, a.y1 > b.y1 ? a.y1 : b.y1 };
			joined = (long)(u.x1 - u.x0) * (u.y1 - u.y0) <=
			         (long)(a.x1 - a.x0) * (a.y1 - a.y0) + (long)(b.x1 - b.x0) * (b.y1 - b.y0);
		}
		if (joined) fb_present(u);
		else {
			for (int i = 0; i < l->nold; i++) {
				int same = 0;
				for (int k = 0; k < l->n; k++) same |= !memcmp(&l->old[i], &l->r[k], sizeof(Rect));
				if (!same) fb_present(l->old[i]);
			}
			for (int i = 0; i < l->n; i++) fb_present(l->r[i]);
		}
	}
	memcpy(l->old, l->r, sizeof l->r);
	l->nold = l->n;
}

static const char *sensor_line(char *buf, size_t n)
{
	if (sn_usable() && sn.rate_hz >= 0.5f) snprintf(buf, n, "MOTION SENSOR OK  %d HZ", (int)(sn.rate_hz + 0.5f));
	else if (sn_usable()) snprintf(buf, n, "MOTION SENSOR OK");
	else if (sn.state == SN_OFF) snprintf(buf, n, "NO MOTION SENSOR (OFF)");
	else if ((sn.state == SN_SEARCH || sn.state == SN_WAIT) && !sn.logged_none && !sn.logged_wait && !sn.losses && tick < 20 * HZ)
		snprintf(buf, n, "LOOKING FOR THE MOTION SENSOR...");
	else snprintf(buf, n, "NO MOTION SENSOR");
	return buf;
}

static void draw_hud(void)
{
	char key[240], a[160], b[24], s[64];
	const uint32_t cream = 0xf2e6c8, brass = 0xe0b955, red = 0xff9a7a;
	int holding = home_t >= HOLD_SHOW && !home_latch && (state != S_TITLE || from_picker);
	int nosens = !sn_usable();
	long cs = centis(steps) / 10;
	sensor_line(s, sizeof s);
	snprintf(key, sizeof key, "%d %d %d %ld %ld %ld %d %s %s %d %d%d %s", state, level, sel, state == S_TITLE ? 0 : cs,
	         falls, best[state == S_TITLE ? sel : level], holding ? home_t : 0, s, snd_status(), note_t > 0,
	         touch_present(), touch_corner_down(), note_t > 0 ? note_cur : "");
	if (!layer_begin(&ly_hud, key)) return;
	if (state == S_TITLE) {
		snprintf(a, sizeof a, "\1 VOL-   BOARD %d OF %d   VOL+ \2", sel + 1, NLEV);
		label(cv, W / 2, px(34.0f), a, sc(4.0f), cream, 1);
		label(cv, W / 2, px(88.0f), levels[sel].name, sc(8.0f), brass, 1);
		snprintf(a, sizeof a, "BEST %s", best_str(best[sel], b, sizeof b));
		label(cv, W / 2, px(168.0f), a, sc(4.0f), cream, 1);
	} else {
		snprintf(a, sizeof a, "BOARD %d/%d", level + 1, NLEV);
		label(cv, bx0, px(34.0f), a, sc(4.0f), cream, 0);
		label(cv, W - bx0, px(34.0f), lv->name, sc(4.0f), brass, 2);
		snprintf(a, sizeof a, "%ld:%02ld.%ld", cs / 600, cs / 10 % 60, cs % 10);
		label(cv, bx0, px(84.0f), a, sc(14.0f), cream, 0);
		snprintf(a, sizeof a, "BEST %s", best_str(best[level], b, sizeof b));
		label(cv, W - bx0 - px(150.0f), px(96.0f), a, sc(4.0f), cream, 2);
		snprintf(a, sizeof a, "FALLS %ld", falls);
		label(cv, W - bx0 - px(150.0f), px(146.0f), a, sc(4.0f), cream, 2);
	}
	if (touch_present() && (state == S_TITLE || state == S_PAUSE)) {
		/* the touch corner: a small brass wedge */
		int n = px(44.0f);
		mark(0, 0, n, n);
		mark_hold++;
		for (int y = 0; y < n; y++) fill(cv, 0, y, n - y, y + 1, touch_corner_down() ? 0xfff0a0 : brass, 220);
		mark_hold--;
	}
	if (holding) {
		int x0 = W / 2 - px(300.0f), x1 = W / 2 + px(300.0f), y0 = px(218.0f), y1 = px(250.0f);
		int n = (x1 - x0 - 8) * (home_t - HOLD_SHOW) / (HOLD_QUIT - HOLD_SHOW);
		fill(cv, x0, y0, x1, y1, 0x120b06, 200);
		fill(cv, x0 + 4, y0 + 4, x0 + 4 + n, y1 - 4, brass, 256);
		label(cv, W / 2, y0 + (y1 - y0 - 7 * sc(2.0f)) / 2, state == S_TITLE ? "HOLD: BACK TO THE PICKER" : "HOLD: BACK TO THE TITLE",
		      sc(2.0f), cream, 1);
	} else if (state == S_TITLE) {
		snprintf(a, sizeof a, "%s  \5  %s", s, snd_status());
		label(cv, W / 2, px(222.0f), a, sc(3.0f), nosens ? red : cream, 1);
		if (nosens) label(cv, W / 2, px(254.0f), touch_present() ? "A FINGER OR VOL+ / VOL- WILL STEER" : "VOL+ / VOL- WILL TURN GRAVITY",
				  sc(3.0f), red, 1);
	} else if (nosens) {
		label(cv, bx0, px(222.0f), "NO MOTION SENSOR", sc(3.0f), red, 0);
		label(cv, bx0, px(254.0f), touch_present() ? "THE MARBLE ROLLS TO YOUR FINGER" : "VOL+ / VOL- TURN GRAVITY", sc(3.0f), red, 0);
	}
	if (note_t > 0) {
		/* the notice: a panel over the top of the HUD, one or two lines */
		char l1[NOTE_LEN + 1], l2[NOTE_LEN + 1] = "";
		int z = sc(4.0f), maxc = (W - 2 * px(30.0f)) / (6 * z), len = (int)strlen(note_cur), cut = len, lines, ph;
		if (maxc < 4) maxc = 4;
		if (len > maxc) {
			cut = maxc;
			for (int i = maxc; i > maxc / 2; i--) if (note_cur[i] == ' ') { cut = i; break; }
			snprintf(l2, sizeof l2, "%.*s", maxc, note_cur + cut + (note_cur[cut] == ' '));
		}
		snprintf(l1, sizeof l1, "%.*s", cut, note_cur);
		lines = l2[0] ? 2 : 1;
		ph = px(24.0f) + lines * 9 * z;
		panel(cv, px(10.0f), px(8.0f), W - px(10.0f), px(8.0f) + ph, 16.0f * U, 0x10243a, 240, 0x9ac4ee);
		label(cv, W / 2, px(8.0f) + px(12.0f) + z, l1, z, 0xffffff, 1);
		if (l2[0]) label(cv, W / 2, px(8.0f) + px(12.0f) + z + 9 * z, l2, z, 0xffffff, 1);
	}
	layer_end(&ly_hud);
}

/* The bubble: where gravity pulls, in a small dial at the right of the HUD. */
static void draw_bubble(void)
{
	char key[64];
	float r = 52.0f * U, cx = (float)(W - bx0) - r - 2.0f, cy = 150.0f * U;
	int qx = (int)(tilt_x * r * 0.72f), qy = (int)(tilt_y * r * 0.72f), show = state != S_TITLE;
	if (tick % 3 && ly_bub.valid && !full_redraw) return;     /* 20 times a second is plenty */
	snprintf(key, sizeof key, "%d %d %d %d", show, qx, qy, sn_usable());
	if (!layer_begin(&ly_bub, key)) return;
	if (show) {
		mark((int)(cx - r) - 1, (int)(cy - r) - 1, (int)(cx + r) + 2, (int)(cy + r) + 2);
		mark_hold++;
		disc(cv, cx, cy, 0.0f, r, 0x120b06, 150);
		disc(cv, cx, cy, r - clampf(3.0f * U, 1.0f, 6.0f), r, 0xe0b955, 256);
		disc(cv, cx, cy, 0.0f, clampf(3.0f * U, 1.0f, 6.0f), 0xe0b955, 160);
		disc(cv, cx + (float)qx, cy + (float)qy, 0.0f, r * 0.24f, sn_usable() ? 0x7fd0ff : 0xff9a7a, 256);
		mark_hold--;
	}
	layer_end(&ly_bub);
}

static void draw_board(void)
{
	char key[240], a[160], b[24];
	const uint32_t cream = 0xf2e6c8, brass = 0xe0b955;
	float r = BALL_R * (float)cell, mx = bpx(ball.x), my = bpy(ball.y), dim = 1.0f;
	int show = 1, cx = W / 2, pw = px(430.0f), y;
	if (state == S_FALL) {
		float t = clampf((float)state_t / ((float)HZ * 0.45f), 0.0f, 1.0f);
		mx = bpx(fall_x + (lv->hx[fall_hole] - fall_x) * t);
		my = bpy(fall_y + (lv->hy[fall_hole] - fall_y) * t);
		r *= 1.0f - 0.55f * t;
		dim = 1.0f - 0.85f * t;
		show = state_t < HZ * 45 / 100;
	}
	if (state == S_WIN || state == S_DONE) { r *= 0.86f; dim = 0.9f; }
	snprintf(key, sizeof key, "%d %d %d %d %d %d %d %ld", state, level, (int)(mx * 4.0f), (int)(my * 4.0f), (int)(r * 4.0f),
	         (int)(dim * 64.0f), show, state == S_WIN ? centis(steps) : 0);
	if (!layer_begin(&ly_board, key)) return;
	if (show) marble_draw(mx, my, r, dim);
	if (state == S_PAUSE) {
		y = by0 + cell * ROWS / 2 - px(210.0f);
		panel(cv, cx - pw, y, cx + pw, y + px(420.0f), 28.0f * U, 0x1a100a, 225, brass);
		label(cv, cx, y + px(40.0f), "PAUSED", sc(10.0f), brass, 1);
		label(cv, cx, y + px(150.0f), touch_present() ? "HOME, TAP   ROLL ON              " : "HOME        ROLL ON              ", sc(4.0f), cream, 1);
		label(cv, cx, y + px(205.0f), "VOL+        THIS POSE IS LEVEL   ", sc(4.0f), cream, 1);
		label(cv, cx, y + px(260.0f), "VOL-        START THE BOARD AGAIN", sc(4.0f), cream, 1);
		label(cv, cx, y + px(315.0f), touch_present() ? "HOLD HOME   TITLE (OR THE CORNER)" : "HOLD HOME   TITLE                ", sc(4.0f), cream, 1);
	} else if (state == S_WIN) {
		/* the panel keeps clear of the cup */
		y = lv->gy >= ROWS / 2 ? by0 + px(180.0f) : by0 + cell * ROWS - px(180.0f) - px(520.0f);
		panel(cv, cx - pw, y, cx + pw, y + px(520.0f), 28.0f * U, 0x1a100a, 225, brass);
		label(cv, cx, y + px(40.0f), "IN THE CUP!", sc(9.0f), brass, 1);
		label(cv, cx, y + px(140.0f), best_str(centis(steps), a, sizeof a), sc(14.0f), cream, 1);
		if (new_best) label(cv, cx, y + px(270.0f), "A NEW BEST TIME", sc(5.0f), 0x9be28a, 1);
		else {
			snprintf(a, sizeof a, "BEST %s", best_str(best[level], b, sizeof b));
			label(cv, cx, y + px(270.0f), a, sc(5.0f), cream, 1);
		}
		snprintf(a, sizeof a, "FALLS %ld", falls);
		label(cv, cx, y + px(335.0f), a, sc(5.0f), cream, 1);
		label(cv, cx, y + px(430.0f), level + 1 < NLEV ? (touch_present() ? "TAP: NEXT BOARD" : "HOME: NEXT BOARD")
						     : (touch_present() ? "TAP: ON" : "HOME: ON"), sc(5.0f), brass, 1);
	} else if (state == S_DONE) {
		long tot = 0;
		y = by0 + cell * ROWS / 2 - px(260.0f);
		for (int i = 0; i < NLEV; i++) tot += best[i];
		panel(cv, cx - pw, y, cx + pw, y + px(520.0f), 28.0f * U, 0x1a100a, 235, brass);
		label(cv, cx, y + px(40.0f), "ALL SIX", sc(10.0f), brass, 1);
		label(cv, cx, y + px(130.0f), "BOARDS", sc(10.0f), brass, 1);
		label(cv, cx, y + px(250.0f), "BEST TIMES TOGETHER", sc(4.0f), cream, 1);
		label(cv, cx, y + px(300.0f), best_str(tot, a, sizeof a), sc(10.0f), cream, 1);
		label(cv, cx, y + px(430.0f), touch_present() ? "TAP: TITLE" : "HOME: TITLE", sc(5.0f), brass, 1);
	}
	layer_end(&ly_board);
}

static long frames_drawn;

static void render(void)
{
	int was_full = full_redraw;
	drew = 0;
	if (full_redraw) {
		memcpy(cv, bg, (size_t)W * (size_t)H * 4);
		ly_board.nold = ly_hud.nold = ly_bub.nold = 0;
		ly_board.valid = ly_hud.valid = ly_bub.valid = 0;
	}
	draw_board();
	draw_hud();
	draw_bubble();
	if (was_full) {
		fb_present((Rect){ 0, 0, W, H });
		full_redraw = 0;
	}
	frames_drawn += drew;
}

/* ------------------------------------------------------------ the self-test */

static const char *dump_dir;
static char dumped[32][24];
static int ndumped;
static long bot_lose_at = -1;
static int bot_rt;                  /* the self-test plays in real time (for the CPU share) */
static struct {
	int path[ROWS * COLS][2], np, wp, mode, paused, held_quit, title_n;
	long falls_seen, board_t;
	float n0[3], ex[3], ey[3];
	char dev[320], dev_saved[320];
} bt;

static void dump_once(const char *name)
{
	if (!dump_dir) return;
	for (int i = 0; i < ndumped; i++) if (!strcmp(dumped[i], name)) return;
	if (ndumped == 32) return;
	snprintf(dumped[ndumped++], sizeof dumped[0], "%s", name);
	fb_dump(dump_dir, name);
}

/* The self-test tilts the "phone": it writes the raw values a real one would
 * report into the fake IIO tree, around a pose leaning back 30 degrees. */
static void bot_set_tilt(float tx, float ty)
{
	static const char *ax[3] = { "in_accel_x_raw", "in_accel_y_raw", "in_accel_z_raw" };
	float s = sinf(TILT_FULL * (float)M_PI / 180.0f), sx = tx * s, sy = ty * s, sz, g[3];
	if (!bt.dev[0]) return;
	sz = sqrtf(clampf(1.0f - sx * sx - sy * sy, 0.0f, 1.0f));
	for (int i = 0; i < 3; i++) g[i] = sx * bt.ex[i] - sy * bt.ey[i] + sz * bt.n0[i];
	for (int i = 0; i < 3; i++) {
		char p[400], v[24];
		int fd, n = snprintf(v, sizeof v, "%d\n", (int)lrintf(g[i] * 65536.0f) + (int)(rnd() % 121) - 60);
		snprintf(p, sizeof p, "%s/%s", bt.dev, ax[i]);
		fd = open(p, O_WRONLY | O_TRUNC | O_CLOEXEC);
		if (fd < 0) continue;
		if (write(fd, v, (size_t)n) != n) perror(p);
		close(fd);
	}
}

static void bot_plan_goal(void)
{
	bt.np = level_path(lv, lv->sx, lv->sy, lv->gx, lv->gy, bt.path);
	bt.wp = 0;
}

/* A way to the hole that is nearest by the path: for the falling test. */
static int bot_plan_hole(void)
{
	int bestn = 0, p[ROWS * COLS][2];
	for (int i = 0; i < lv->nh; i++) {
		int hx = (int)lv->hx[i], hy = (int)lv->hy[i];
		for (int d = 0; d < 4; d++) {
			int ax = hx - dir_x[d], ay = hy - dir_y[d], n;
			if (ax < 0 || ax >= COLS || ay < 0 || ay >= ROWS || lv->hole_at[ay][ax] || !cell_open(lv, ax, ay, d)) continue;
			n = level_path(lv, lv->sx, lv->sy, ax, ay, p);
			if (n && (!bestn || n + 1 < bestn)) {
				memcpy(bt.path, p, sizeof p);
				bt.path[n][0] = hx; bt.path[n][1] = hy;
				bestn = n + 1;
			}
		}
	}
	bt.np = bestn; bt.wp = 0;
	return bestn;
}

static void bot_new_board(void)
{
	if (!bot) return;
	bt.board_t = 0;
	bt.falls_seen = 0;
	bt.held_quit = 0;
	bt.mode = 0;
	if (level == 1 && !(st_falls > 0) && bot_plan_hole()) bt.mode = 1;   /* once: go and fall into a hole */
	else bot_plan_goal();
}

static void bot_think(void)
{
	float tx = 0.0f, ty = 0.0f;
	if (bot_lose_at >= 0 && tick == bot_lose_at && bt.dev[0]) {
		/* the sensor goes away: its values freeze, then the device is gone */
		char p[400];
		logmsg("bot: the fake sensor goes away");
		snprintf(p, sizeof p, "%s/name", bt.dev);
		unlink(p);
		snprintf(bt.dev_saved, sizeof bt.dev_saved, "%s", bt.dev);
		bt.dev[0] = 0;
	}
	if (bot_lose_at >= 0 && tick == bot_lose_at + 12 * HZ && bt.dev_saved[0]) {
		char p[400];
		FILE *f;
		logmsg("bot: the fake sensor is back");
		snprintf(p, sizeof p, "%s/name", bt.dev_saved);
		f = fopen(p, "we");
		if (f) { fprintf(f, "%s\n", iio_name); fclose(f); }
		snprintf(bt.dev, sizeof bt.dev, "%s", bt.dev_saved);
	}
	switch (state) {
	case S_TITLE:
		if (state_t == 1) bt.title_n++;
		if (state_t == 40) bot_dn_until = tick + 5;
		if (state_t == 60) bot_up_until = tick + 5;
		if (state_t == 80 || state_t == 110) bot_up_until = bot_dn_until = tick + 6;   /* sound off, on */
		if (state_t == 100) dump_once(sn_usable() ? "title" : "title-nosensor");
		if (bt.title_n == 2 && state_t == 140) bot_home_until = tick + HOLD_QUIT + 20;   /* hold: picker exit, or nothing */
		if (state_t == 140 + (bt.title_n == 2 ? HOLD_QUIT + 60 : 0)) bot_home_until = tick + 5;
		if (state_t < 100) { tx = 0.25f * sinf((float)state_t * 0.05f); ty = 0.25f * cosf((float)state_t * 0.031f); }
		break;                                  /* then still: Home takes this pose as level */
	case S_PLAY: {
		float gx, gy, ex, ey;
		bt.board_t++;
		if (falls != bt.falls_seen) {
			bt.falls_seen = falls;
			bt.mode = 0;
			bot_plan_goal();
		}
		if (bt.mode == 1 && bt.board_t > 25 * HZ) { bt.mode = 0; bot_plan_goal(); }
		if (level == 0 && !bt.paused && bt.board_t == 90) { bt.paused = 1; bot_home_until = tick + 5; }
		if (state_t == 100) { char n[24]; snprintf(n, sizeof n, "level-%d", level + 1); dump_once(n); }
		if (bt.board_t > 240 * HZ && !bt.held_quit) {         /* stuck: give up, back to the title */
			bt.held_quit = 1;
			logmsg("bot: gives up on board %d", level + 1);
			bot_home_until = tick + HOLD_QUIT + 10;
		}
		if (!sn_usable()) {
			/* no sensor: just work the keys */
			if (bt.board_t % 90 < 30) bot_up_until = tick + 1;
			else if (bt.board_t % 90 > 60) bot_dn_until = tick + 1;
			if (bt.board_t == 300) dump_once("level-nosensor");
			break;
		}
		if (bt.np < 1) break;
		while (bt.wp < bt.np - 1 && fabsf(ball.x - ((float)bt.path[bt.wp][0] + 0.5f)) < 0.34f &&
		       fabsf(ball.y - ((float)bt.path[bt.wp][1] + 0.5f)) < 0.34f)
			bt.wp++;
		gx = (float)bt.path[bt.wp][0] + 0.5f; gy = (float)bt.path[bt.wp][1] + 0.5f;
		ex = gx - ball.x; ey = gy - ball.y;
		tx = clampf((13.0f * ex - 7.0f * ball.vx) / ACC, -1.0f, 1.0f);
		ty = clampf((13.0f * ey - 7.0f * ball.vy) / ACC, -1.0f, 1.0f);
		break;
	}
	case S_PAUSE:
		if (state_t == 10) dump_once("pause");
		if (state_t == 30) bot_up_until = tick + 5;           /* recalibrate: resumes */
		if (state_t == 90) bot_home_until = tick + 5;         /* (if it did not) */
		break;
	case S_FALL:
		if (state_t == 12) dump_once("fall");
		break;
	case S_WIN:
		if (state_t == 40) { char n[24]; snprintf(n, sizeof n, "win-%d", level + 1); dump_once(n); }
		if (state_t == 70) bot_home_until = tick + 5;
		break;
	case S_DONE:
		if (state_t == 40) dump_once("done");
		if (state_t == 70) bot_home_until = tick + 5;
		break;
	}
	bot_set_tilt(tx, ty);
}

static void bot_init(void)
{
	static const float n0[3] = { 0.0f, -0.5f, -0.8660254f };   /* leaning back 30 degrees from flat */
	DIR *d;
	struct dirent *e;
	memcpy(bt.n0, n0, sizeof n0);
	bt.ex[0] = 1.0f; bt.ex[1] = 0.0f; bt.ex[2] = 0.0f;
	bt.ey[0] = 0.0f; bt.ey[1] = 0.8660254f; bt.ey[2] = -0.5f;
	if (sn.state == SN_OFF || !(d = opendir(iio_root))) return;
	while ((e = readdir(d))) {
		char p[300], buf[64];
		snprintf(p, sizeof p, "%.200s/%.64s", iio_root, e->d_name);
		if (!strncmp(e->d_name, "iio:device", 10) && read_small(p, "name", buf, sizeof buf) == 0 && !strcmp(buf, iio_name))
			snprintf(bt.dev, sizeof bt.dev, "%s", p);
	}
	closedir(d);
	bot_set_tilt(0.0f, 0.0f);
}

/* Without the autopilot (--no-pilot, touch tests): pictures of the title,
 * a board and the pause panel as the recorded touches reach them. */
static void touch_dumps(void)
{
	if (state == S_TITLE && state_t == 100) dump_once("touch-title");
	if (state == S_PLAY && state_t == 100) dump_once("touch-play");
	if (state == S_PAUSE && state_t == 10) dump_once("touch-pause");
}

/* --selftest: the physics and the sensor arithmetic, without any device. */
#define CHECK(c, ...) do { if (!(c)) { printf("FAIL: " __VA_ARGS__); printf("\n"); return 1; } } while (0)

static int wall_between(const Level *l, int x0, int y0, int x1, int y1)
{
	if (x0 == x1 && y0 == y1) return 0;
	if (y0 == y1 && x1 == x0 + 1) return !cell_open(l, x0, y0, 0);
	if (y0 == y1 && x1 == x0 - 1) return !cell_open(l, x0, y0, 2);
	if (x0 == x1 && y1 == y0 + 1) return !cell_open(l, x0, y0, 1);
	if (x0 == x1 && y1 == y0 - 1) return !cell_open(l, x0, y0, 3);
	if (abs(x1 - x0) == 1 && abs(y1 - y0) == 1)     /* round a corner: one of the two ways must be open */
		return (wall_between(l, x0, y0, x1, y0) || wall_between(l, x1, y0, x1, y1)) &&
		       (wall_between(l, x0, y0, x0, y1) || wall_between(l, x0, y1, x1, y1));
	return 1;
}

/* How far the marble's centre is from the nearest wall of l. */
static float wall_clearance(const Level *l, const Ball *b)
{
	float best_d = 99.0f, nx, ny, d;
	for (int y = 0; y <= ROWS; y++) for (int x = 0; x < COLS; x++)
		if (l->h[y][x] && (d = seg_dist(b->x, b->y, (float)x, (float)y, (float)x + 1.0f, (float)y, &nx, &ny)) < best_d) best_d = d;
	for (int y = 0; y < ROWS; y++) for (int x = 0; x <= COLS; x++)
		if (l->v[y][x] && (d = seg_dist(b->x, b->y, (float)x, (float)y, (float)x, (float)y + 1.0f, &nx, &ny)) < best_d) best_d = d;
	return best_d;
}

static int selftest(void)
{
	Ball b;
	float imp, tx, ty;
	int which = 0, ev;
	long nsteps = 0;
	float worst = 99.0f, vtop = 0.0f;

	/* 1. no tunnelling: every board and the tray, full tilt and beyond, in
	 * fixed, turning and random directions; holes do not count here */
	for (int li = 0; li <= NLEV; li++) {
		const Level *l = li < NLEV ? &levels[li] : &tray;
		for (int run = 0; run < 40; run++) {
			float gain = run < 24 ? 1.0f : 6.0f;     /* 6x: far past any real tilt; the speed cap must hold */
			float ang = (float)run * 0.2618f, spin = run % 3 == 1 ? 3.1f : run % 3 == 2 ? -0.7f : 0.0f;
			ball_reset(l, &b);
			if (run >= 12) { b.x = 0.5f + (float)(rnd() % COLS); b.y = 0.5f + (float)(rnd() % ROWS); }
			for (int s = 0; s < 20 * HZ * SUB; s++) {
				int cx0 = (int)floorf(b.x), cy0 = (int)floorf(b.y), cx1, cy1;
				float c, sp;
				if (run % 4 == 3 && s % 37 == 0) ang = frand() * 6.2832f;
				ang += spin * PDT;
				phys_step(l, &b, ACC * gain * cosf(ang), ACC * gain * sinf(ang), 0, &imp, &which);
				nsteps++;
				cx1 = (int)floorf(b.x); cy1 = (int)floorf(b.y);
				CHECK(cx1 >= 0 && cx1 < COLS && cy1 >= 0 && cy1 < ROWS, "board %d run %d: marble left the board (%.3f, %.3f)", li + 1, run, (double)b.x, (double)b.y);
				CHECK(!wall_between(l, cx0, cy0, cx1, cy1), "board %d run %d step %d: through a wall, cell %d,%d to %d,%d",
				      li + 1, run, s, cx0, cy0, cx1, cy1);
				c = wall_clearance(l, &b);
				if (c < worst) worst = c;
				sp = sqrtf(b.vx * b.vx + b.vy * b.vy);
				if (sp > vtop) vtop = sp;
				CHECK(c > BALL_R + WALL_H - 0.02f, "board %d run %d step %d: marble inside a wall (clearance %.4f)", li + 1, run, s, (double)c);
				CHECK(isfinite(b.x) && isfinite(b.y) && sp <= VMAX * 1.5f, "board %d: speed %.2f", li + 1, (double)sp);
			}
		}
	}
	printf("ok: no tunnelling in %ld steps on %d boards (closest %.4f cells to a wall line, limit %.4f; top speed %.2f of %.1f)\n",
	       nsteps, NLEV + 1, (double)worst, (double)(BALL_R + WALL_H), (double)vtop, (double)VMAX);

	/* 2. the marble settles on a flat board, and stays under the dead zone */
	ball_reset(&tray, &b);
	b.vx = 6.0f; b.vy = -3.5f;
	for (int s = 0; s < 30 * HZ * SUB; s++) phys_step(&tray, &b, 0.0f, 0.0f, 0, &imp, &which);
	CHECK(b.vx == 0.0f && b.vy == 0.0f, "flat board: still moving after 30 s (%.5f, %.5f)", (double)b.vx, (double)b.vy);
	{
		float x = b.x, y = b.y;
		for (int s = 0; s < 10 * HZ * SUB; s++) phys_step(&tray, &b, ROLL_MU * 0.9f, 0.0f, 0, &imp, &which);
		CHECK(b.x == x && b.y == y, "a tilt under the dead zone moved the marble");
		for (int s = 0; s < HZ * SUB; s++) phys_step(&tray, &b, ROLL_MU * 3.0f, 0.0f, 0, &imp, &which);
		CHECK(b.x > x + 0.2f, "a tilt over the dead zone did not move the marble");
	}
	printf("ok: settles on a flat board; dead zone %.1f%% of full tilt\n", (double)(100.0f * ROLL_MU / ACC));

	/* 3. holes take the marble and the goal ends the board */
	{
		const Level *l = &levels[0];
		int path[ROWS * COLS][2];
		ball_reset(l, &b);
		b.x = l->hx[0] - 0.9f; b.y = l->hy[0];
		ev = EV_NONE;
		for (int s = 0; s < 5 * HZ * SUB && ev == EV_NONE; s++) ev = phys_step(l, &b, ACC * 0.5f, 0.0f, 1, &imp, &which);
		CHECK(ev == EV_HOLE && which == 0, "rolling at a hole: event %d, hole %d", ev, which);
		ball_reset(l, &b);
		CHECK(b.x == (float)l->sx + 0.5f && b.y == (float)l->sy + 0.5f && b.vx == 0.0f && b.vy == 0.0f, "reset to the start");
		b.x = (float)l->gx + 0.5f; b.y = (float)l->gy - 0.5f;
		ev = EV_NONE;
		for (int s = 0; s < 5 * HZ * SUB && ev == EV_NONE; s++) ev = phys_step(l, &b, 0.0f, ACC * 0.5f, 1, &imp, &which);
		CHECK(ev == EV_GOAL, "rolling at the goal: event %d", ev);
		for (int i = 0; i < NLEV; i++) {
			int n = level_path(&levels[i], levels[i].sx, levels[i].sy, levels[i].gx, levels[i].gy, path);
			CHECK(n > 1, "board %d has no way to the goal", i + 1);
			printf("ok: board %d %-11s %2d holes, shortest way %2d cells\n", i + 1, levels[i].name, levels[i].nh, n);
		}
		/* a hole in a walled corridor closes it: nothing slips past */
		CHECK(0.5f - WALL_H - BALL_R < HOLE_IN, "a marble could pass a hole in a corridor");
	}
	printf("ok: holes take the marble, the cup ends the board\n");

	/* 4. the sensor arithmetic, with the six poses measured on the N66
	 * (raw means of hwcheck-n66-aop's run, sensor frame) */
	{
		static const struct { const char *name; int raw[3]; float tx, ty; } pose[] = {
			{ "screen up", { 2146, 392, -62784 }, 0, 0 },
			{ "upright, home down", { 105, -60977, -20731 }, 0, 1 },
			{ "upside down, home up", { -69, 63802, -11808 }, 0, -1 },
			{ "home right", { -61441, 2076, -20147 }, -1, 0 },
			{ "home left", { 61567, -231, -21322 }, 1, 0 },
		};
		static const float flat[3] = { 0, 0, -1 }, id[9] = { 1, 0, 0, 0, 1, 0, 0, 0, -1 };
		int raw[3];
		sn.scale = 0.000149637f; sn.nofilter = 1; sn.state = SN_OK;
		memcpy(sn.M, id, sizeof id);
		sn_set_level(flat);
		for (size_t i = 0; i < sizeof pose / sizeof pose[0]; i++) {
			sn.have = 0;
			sn_feed(pose[i].raw);
			sn_tilt(&tx, &ty);
			CHECK(sn.have && fabsf(tx - pose[i].tx) < 0.12f && fabsf(ty - pose[i].ty) < 0.12f,
			      "pose '%s': tilt (%.2f, %.2f), expected (%.0f, %.0f)", pose[i].name, (double)tx, (double)ty, (double)pose[i].tx, (double)pose[i].ty);
			printf("ok: pose %-22s rolls the marble (%+.2f, %+.2f) on the screen\n", pose[i].name, (double)tx, (double)ty);
		}
		/* calibrated leaning back 40 degrees: that pose is level, 10 degrees more rolls up the screen */
		raw[0] = 0; raw[1] = (int)(-65536.0f * sinf(0.698f)); raw[2] = (int)(-65536.0f * cosf(0.698f));
		sn.hn = sn.hi = 0; sn.have = 0;
		sn_feed(raw);
		sn_calibrate();
		sn_tilt(&tx, &ty);
		CHECK(fabsf(tx) < 0.01f && fabsf(ty) < 0.01f, "calibrated pose is not level (%.3f, %.3f)", (double)tx, (double)ty);
		raw[1] = (int)(-65536.0f * sinf(0.698f - 0.1745f)); raw[2] = (int)(-65536.0f * cosf(0.698f - 0.1745f));
		sn_feed(raw); sn_tilt(&tx, &ty);
		CHECK(fabsf(tx) < 0.01f && fabsf(ty - -sinf(0.1745f) / sinf(TILT_FULL * (float)M_PI / 180.0f)) < 0.01f,
		      "10 degrees flatter than calibrated: (%.3f, %.3f)", (double)tx, (double)ty);
		raw[0] = (int)(65536.0f * sinf(0.1745f)); raw[1] = (int)(-65536.0f * sinf(0.698f) * cosf(0.1745f));
		raw[2] = (int)(-65536.0f * cosf(0.698f) * cosf(0.1745f));
		sn_feed(raw); sn_tilt(&tx, &ty);
		CHECK(tx > 0.4f && fabsf(ty) < 0.02f, "10 degrees to the right of calibrated: (%.3f, %.3f)", (double)tx, (double)ty);
		/* calibrated bolt upright and overhead: still sane axes */
		raw[0] = 0; raw[1] = -65536; raw[2] = 0;
		sn.hn = sn.hi = 0; sn_feed(raw); sn_calibrate();
		raw[1] = (int)(-65536.0f * cosf(0.1745f)); raw[2] = (int)(-65536.0f * sinf(0.1745f));   /* top leans away */
		sn_feed(raw); sn_tilt(&tx, &ty);
		CHECK(fabsf(tx) < 0.01f && ty < -0.4f, "upright, leaning back: (%.3f, %.3f)", (double)tx, (double)ty);
		raw[0] = 0; raw[1] = 0; raw[2] = 65536;
		sn.hn = sn.hi = 0; sn_feed(raw); sn_calibrate();
		raw[1] = (int)(65536.0f * sinf(0.1745f)); raw[2] = (int)(65536.0f * cosf(0.1745f));   /* overhead, top edge down */
		sn_feed(raw); sn_tilt(&tx, &ty);
		CHECK(fabsf(tx) < 0.01f && ty < -0.4f, "overhead, top edge down: (%.3f, %.3f)", (double)tx, (double)ty);
		/* nonsense is ignored */
		raw[0] = raw[1] = raw[2] = 0;
		sn.bad = 0; sn_feed(raw);
		CHECK(sn.bad == 1, "a zero vector was taken as gravity");
		printf("ok: calibration (leaning back, upright, overhead) and junk rejection\n");
	}
	printf("SELFTEST PASSED\n");
	return 0;
}

/* ------------------------------------------------------------- screen off */
/*
 * Power darkens the screen (backlight.h). While it is dark nothing moves:
 * no physics, no clock, no drawing, no sound (the card is closed), the
 * motion sensor is not read, the touchscreen is read and dropped; the
 * buttons are slept on and /run/notice is looked at twice a second (a new
 * notice lights the screen). When it comes back a board in play is paused
 * (S_PAUSE: Home or a tap plays on, Vol+ recalibrates, Vol- restarts).
 */
static int was_dark;
static int64_t dark_next_notice, dark_next_scan;

static void screen_edges(void)
{
	if (bl_dark() == was_dark) return;
	was_dark = bl_dark();
	touch_discard(gl_clock_ms());
	memset(keytap, 0, sizeof keytap);
	if (was_dark) {
		snd_suspend();
		dark_next_notice = 0;
		logmsg("screen dark: state %d, board %d, clock %ld cs, falls %ld", state, level + 1, centis(steps), falls);
		return;
	}
	if (state == S_FALL) { ball_reset(lv, &ball); set_state(S_PAUSE); }
	else if (state == S_PLAY) set_state(S_PAUSE);
	was_home = home_latch = held_home();
	home_t = 0;
	was_up = held_up(); was_dn = held_dn();
	up_arm = dn_arm = 0;
	sn.last_change = tick;
	full_redraw = 1;
	snd_resume();
	logmsg("screen lit: state %d, board %d, clock %ld cs, falls %ld%s", state, level + 1, centis(steps), falls,
	       state == S_PAUSE ? ", paused" : "");
}

static void dark_notice(void)
{
	long before = st_notices;
	notice_poll(0);
	if (st_notices != before) bl_wake("a notice");
}

/* Dark, on the device: sleep on the buttons. */
static void dark_wait(void)
{
	struct pollfd pk[MAXDEV];
	int64_t now;
	for (int i = 0; i < ndev; i++) pk[i] = (struct pollfd){ dev_fd[i], POLLIN, 0 };
	poll(ndev ? pk : NULL, (nfds_t)ndev, BL_IDLE_MS);
	read_events();
	fakekeys_pump(key_event);
	now = now_ns();
	if (now >= dark_next_notice) {
		dark_notice();
		touch_discard(gl_clock_ms());
		dark_next_notice = now + BL_IDLE_MS * 1000000LL;
	}
	if (rescan || (ndev == 0 && now >= dark_next_scan)) { scan_devs(); dark_next_scan = now + 2000000000LL; }
	screen_edges();
}

/* Dark, in the self-test: one frame passes. */
static void dark_bot(void)
{
	fakekeys_pump(key_event);
	if (tick % (HZ / 2) == 0) dark_notice();
	touch_discard(gl_clock_ms());
	screen_edges();
}

/* ------------------------------------------------------------------- main */

static void on_signal(int sig) { (void)sig; quit_flag = 1; }

/* Any other signal that would end the game: console back to text, then the
 * signal goes on to do what it does. Only async-signal-safe calls here. */
static void on_fatal(int sig)
{
	kd_text();
	bl_on_sigsafe();
	signal(sig, SIG_DFL);
	raise(sig);
}

static void at_exit(void) { kd_text(); bl_on_sigsafe(); }

static void catch_signals(void)
{
	static const int fatal[] = {
		SIGQUIT, SIGILL, SIGTRAP, SIGABRT, SIGBUS, SIGFPE, SIGSEGV, SIGUSR1, SIGUSR2, SIGALRM,
		SIGXCPU, SIGXFSZ, SIGVTALRM, SIGPROF, SIGIO, SIGPWR, SIGSYS,
#ifdef SIGSTKFLT
		SIGSTKFLT,
#endif
	};
	struct sigaction sa;
	signal(SIGPIPE, SIG_IGN);
	signal(SIGTTOU, SIG_IGN);
	signal(SIGHUP, SIG_IGN);
	signal(SIGINT, on_signal);
	signal(SIGTERM, on_signal);
	memset(&sa, 0, sizeof sa);
	sa.sa_handler = on_fatal;
	sa.sa_flags = (int)(SA_RESETHAND | SA_NODEFER);
	sigemptyset(&sa.sa_mask);
	for (size_t i = 0; i < sizeof fatal / sizeof fatal[0]; i++) sigaction(fatal[i], &sa, NULL);
	atexit(at_exit);
}

/* --restore-tty: a console left in graphics mode (the game was killed with
 * SIGKILL) back to text mode, cursor on. */
static int restore_tty(const char *path)
{
	int r, fd;
	bl_restore_only();      /* and a backlight left dark: on */
	fd = open(path, O_RDWR | O_NOCTTY | O_CLOEXEC);
	if (fd < 0) { perror(path); return 1; }
	r = ioctl(fd, KDSETMODE, KD_TEXT);
	if (r < 0) perror("KDSETMODE KD_TEXT");
	say(fd, "\033[0m\033[?7h\033[?25h");
	close(fd);
	return r < 0;
}

static void usage(void)
{
	fputs("usage: glint [--tty PATH] [--fb PATH] [--fb-mmap] [--iio DIR|none] [--rate HZ] [--axes a,b,c]\n"
	      "             [--hi PATH|none] [--notice PATH|none] [--from-picker] [--swap] [--no-grab]\n"
	      "             [--mute] [--vol DB] [--level N] [--fps 60|30|20]\n"
	      "       glint --restore-tty PATH | --selftest\n"
	      "       glint --bot FRAMES [--fakefb WxHxBPP[:bgr]] [--dump DIR] [--bot-lose FRAME] [--no-pilot]\n", stderr);
}

int main(int argc, char **argv)
{
	const char *tty_path = "/dev/tty1", *fb_path = "/dev/fb0", *fake = NULL;
	long bot_frames = 0, late = 0;
	int frame_div = 1;
	int tty = -1, do_selftest = 0, start = 0;
	int64_t next, cost_sum = 0, cost_max = 0;

	t_start = now_ns();
	for (int i = 1; i < argc; i++) {
		const char *a = argv[i], *v = i + 1 < argc ? argv[i + 1] : NULL;
		if (!strcmp(a, "--restore-tty") && v) return restore_tty(v);
		else if (!strcmp(a, "--selftest")) do_selftest = 1;
		else if (!strcmp(a, "--tty") && v) { tty_path = v; i++; }
		else if (!strcmp(a, "--fb") && v) { fb_path = v; i++; }
		else if (!strcmp(a, "--fakefb") && v) { fake = v; i++; }
		else if (!strcmp(a, "--fb-mmap")) fb_force_mmap = 1;
		else if (!strcmp(a, "--fakefb-file") && v) { fake_file = v; i++; }
		else if (!strcmp(a, "--bot-rt")) bot_rt = 1;
		else if (!strcmp(a, "--iio") && v) { if (!strcmp(v, "none")) sn.state = SN_OFF; else iio_root = v; i++; }
		else if (!strcmp(a, "--rate") && v) { sn_rate = atoi(v); if (sn_rate < 0 || sn_rate > 100) sn_rate = 0; i++; }
		else if (!strcmp(a, "--axes") && v) {
			if (sscanf(v, "%d,%d,%d", &sn_axes[0], &sn_axes[1], &sn_axes[2]) != 3 || abs(sn_axes[0]) < 1 || abs(sn_axes[0]) > 3 ||
			    abs(sn_axes[1]) < 1 || abs(sn_axes[1]) > 3 || abs(sn_axes[2]) < 1 || abs(sn_axes[2]) > 3) { usage(); return 1; }
			i++;
		}
		else if (!strcmp(a, "--hi") && v) { best_path = strcmp(v, "none") ? v : NULL; i++; }
		else if (!strcmp(a, "--notice") && v) { notice_path = strcmp(v, "none") ? v : NULL; i++; }
		else if (!strcmp(a, "--from-picker")) from_picker = 1;
		else if (!strcmp(a, "--swap")) swap_lr = 1;
		else if (!strcmp(a, "--no-grab")) grab = 0;
		else if (!strcmp(a, "--mute")) snd_state = SND_OFF;
		else if (!strcmp(a, "--vol") && v) { snd_vol_db = (float)atof(v); if (!(snd_vol_db <= VOL_MAX_DB)) snd_vol_db = VOL_MAX_DB; i++; }
		else if (!strcmp(a, "--level") && v) { start = atoi(v); i++; }
		else if (!strcmp(a, "--fps") && v) {
			int f = atoi(v);
			if (f != 60 && f != 30 && f != 20) { usage(); return 1; }
			frame_div = HZ / f;
			i++;
		}
		else if (!strcmp(a, "--bot") && v) { bot = 1; bot_frames = atol(v); i++; }
		else if (!strcmp(a, "--dump") && v) { dump_dir = v; i++; }
		else if (!strcmp(a, "--bot-lose") && v) { bot_lose_at = atol(v); i++; }
		else if (!strcmp(a, "--no-pilot")) pilot = 0;
		else { usage(); return 1; }
	}
	if (levels_init() < 0) return 1;
	{
		static const float flat[3] = { 0, 0, -1 };
		sn_set_level(flat);
	}
	if (do_selftest) return selftest();
#ifdef GL_FAKE_ALSA
	if (getenv("FAKE_ALSA_ROOT")) snd_root = getenv("FAKE_ALSA_ROOT");
#endif
	if (bot && !fake) fake = "1080x1920x32";
	if (snd_state == SND_OFF) logmsg("sound off (--mute)");
	else snd_start_search();
	if (start >= 1 && start <= NLEV) sel = start - 1;
	best_load();
	catch_signals();
	bl_init(gl_clock_ms);

	if (fake) {
		if (fb_open_fake(fake) < 0) { logmsg("graphics: fake framebuffer '%s' not usable", fake); return 4; }
	} else {
		tty = open(tty_path, O_RDWR | O_NOCTTY | O_CLOEXEC);
		if (tty < 0) logmsg("%s: %s", tty_path, strerror(errno));
		if (fb_open(fb_path) < 0) {
			if (tty >= 0) say(tty, "\r\nglint: no usable framebuffer, see /run/glint.log\r\n");
			return 4;
		}
	}
	kd_graphics(tty);
	wood_init();
	if (bot) {
		bot_init();
		fakekeys_have(&have_home, &have_power, &have_vol);
		bl_set_keys(have_home, have_power, have_vol);
	} else scan_devs();
	tp.last_scan = bot ? 0 : now_ns() / 1000000;
	touch_open(grab);
	t_seen = touch_present();
	logmsg("%s", tp.desc);
	notice_poll(1);
	to_title();
	logmsg("GLINT: %d boards, physics %d Hz, screen %d Hz%s", NLEV, HZ * SUB, HZ / frame_div, from_picker ? ", from the picker" : "");

	next = now_ns();
	while (!quit_flag) {
		int64_t t0, c;
		if (bl_dark()) {
			if (bot) {
				dark_bot();
				if (++tick >= bot_frames) break;
				continue;
			}
			dark_wait();
			next = now_ns();
			continue;
		}
		t0 = now_ns();
		if (!bot) {
			if (rescan || (ndev == 0 && tick % (2 * HZ) == 0)) scan_devs();
			read_events();
		} else memset(keytap, 0, sizeof keytap);   /* as read_events() does */
		fakekeys_pump(key_event);
		bl_tick();
		screen_edges();
		if (bl_dark()) continue;
		if (bot && pilot) bot_think();
		if (bot && !pilot) touch_dumps();
		sn_poll();
		game_tick();
		if (quit_flag) break;
		if (tick % frame_div == 0) render();
		snd_tick();
		c = now_ns() - t0;
		cost_sum += c;
		if (c > cost_max && tick > 5) cost_max = c;
		tick++;
		if (bot) {
			if (tick >= bot_frames) break;
			if (!bot_rt) continue;
		}
		next += 1000000000LL / HZ;
		{
			int64_t now = now_ns();
			if (now > next + 100000000LL) { next = now; late++; }       /* far behind: start over */
			else if (now < next) {
				struct timespec ts = { (time_t)(next / 1000000000LL), (long)(next % 1000000000LL) };
				while (clock_nanosleep(CLOCK_MONOTONIC, TIMER_ABSTIME, &ts, NULL) == EINTR && !quit_flag) { }
			}
		}
	}

	if (fb_fake) logmsg("%s", fb_padding_ok() ? "graphics: line padding intact" : "graphics: WROTE PAST THE LINE LENGTH");
	fprintf(stderr, "frames: %ld, %.0f us each on average, longest %.0f us; %ld drawn, %.1f MB to the screen, %ld late\n",
	        tick, tick ? (double)cost_sum / 1e3 / (double)tick : 0.0, (double)cost_max / 1e3, frames_drawn,
	        (double)fb_bytes / 1e6, late);
	{
		struct rusage ru;
		double wall = (double)(now_ns() - t_start) / 1e9;
		if (getrusage(RUSAGE_SELF, &ru) == 0) {
			double cpu = (double)ru.ru_utime.tv_sec + (double)ru.ru_utime.tv_usec / 1e6 +
			             (double)ru.ru_stime.tv_sec + (double)ru.ru_stime.tv_usec / 1e6;
			fprintf(stderr, "cpu: %.2f s of CPU in %.1f s (%.1f %% of one core)\n", cpu, wall, wall > 0 ? 100.0 * cpu / wall : 0.0);
		}
	}
	fprintf(stderr, "stats: frames=%ld wins=%ld cleared=%d falls=%ld pauses=%ld quits=%ld calib=%ld toggles=%ld sound=%d "
	        "sensor=%d losses=%ld notices=%ld picker_exit=%ld state=%d level=%d\n",
	        tick, st_wins, st_cleared, st_falls, st_pauses, st_quits, st_calib, st_toggles, snd_state,
	        sn_usable(), sn.losses, st_notices, st_picker_exit, state, level + 1);
	fprintf(stderr, "touch=%d contacts=%ld taps=%ld swipes=%ld longs=%ld corner=%ld ttaps=%ld tswipes=%ld tsteer=%ld sel=%d tdropped=%ld\n",
	        touch_present(), tp.n_contact, tp.n_tap, tp.n_swipe, tp.n_long, tp.n_corner, st_ttaps, st_tswipes, st_tsteer, sel + 1,
	        tp.n_dropped);
	bl_exit();
	snd_close();
	kd_text();
	if (tty >= 0) close(tty);
	return 0;
}

#ifdef GL_FAKE_ALSA
#include "fake_alsa.c"
#endif
