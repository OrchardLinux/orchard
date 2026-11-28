/*
 * arcade.c -- ARCADE, the game picker of the arcade payload (N66, J71, J85,
 * J81; examples/arcade).
 *
 * A framebuffer screen with the three games stacked (VOIDMOTHS, HEDGEWICK,
 * GLINT), each on a card with a small moving preview, its name and its high
 * score (read from the game's own high-score file; for GLINT the boards
 * cleared and board 1's best time, from /run/glint.best). Volume Up / Volume
 * Down move the selection, Home starts the selected game. With a
 * touchscreen: tap a card to choose it, tap it again to play.
 *
 * What the board has is looked at, never its name: GLINT needs a motion
 * sensor (an IIO device named "apple-aop-accel"); without one its card is
 * grey ("NEEDS A MOTION SENSOR") and cannot be chosen, and it comes alive
 * when the sensor appears. Buttons are whatever evdev devices offer Home,
 * Power or the volume keys (KEY_MUTE, the iPads' mute switch, is ignored);
 * the touchscreen is the evdev device with direct multitouch (touch.h). The game runs as a child process with the
 * console handed over (picker's buttons released, VT back in KD_TEXT and
 * cleared, speaker closed); when it exits, the picker takes the console back
 * and draws itself again. A game started from here gets --from-picker:
 * holding Home for 2 s on its title screen makes it exit, which brings you
 * back here.
 *
 *   Volume Up    previous game        Volume Down  next game (both wrap)
 *   Home         play                 (Power, if there is no Home key)
 *   No volume keys: Power (with Home) next game; one key only: tap it for
 *   the next game, hold it 0.6 s to play.
 *   Touch: tap a card to choose it, tap the chosen card to play.
 *   USB keyboard: Up/Down choose, Enter/Space play.
 *   Screen off (backlight.h, the same in every game): with volume keys a
 *   Power press; without them Power held 0.8 s (a short press stays "next
 *   game"). Home or Power lights it again; that press does nothing else.
 *   While dark the picker draws nothing, plays nothing, ignores the
 *   touchscreen and wakes twice a second for /run/notice (a new notice
 *   lights the screen). After every game the backlight is lit again, should
 *   the game have left it dark.
 *
 * Idle cost: the screen is redrawn at 10 fps only while something moves
 * (the selected card's preview, for 2 minutes after the last button press,
 * or a notice); otherwise it wakes twice a second to look at /run/notice.
 * Only changed pixels are written. While a game runs the picker sleeps in
 * waitpid().
 *
 * Notices: the first line of /run/notice, whenever the file changes, as a
 * toast for 6 s with a soft chime (the games do the same).
 *
 * usage: arcade [--tty PATH] [--fb PATH] [--bin-dir DIR] [--run-dir DIR]
 *               [--notice PATH|none] [--swap] [--no-grab] [--mute] [--vol DB]
 *               [--fb-write] [--iio-root DIR]
 *
 * The screen is written through the mmap()ed framebuffer, as VOIDMOTHS does;
 * the games are started so that they do the same (HEDGEWICK's default,
 * GLINT gets --fb-mmap). --fb-write switches the picker and both games to
 * pwrite() of the changed rows instead (HEDGEWICK gets --fb-write, GLINT its
 * own default).
 *        arcade --restore-console [--tty PATH]   (VT back to text mode;
 *        arcade --restore-tty PATH is the same, spelled as in voidmoths)
 *        arcade --script EVENTS --ticks N [--shot WHEN:FILE.ppm]... [--keys LIST] (self-test)
 *            EVENTS: TICK:KEY[*HOLD],...  KEY: volup voldown home power mute chord
 *            LIST: the keys the scripted board has (home,power,vol; none)
 *            Touch, in test builds: $FAKE_TOUCH (see touch.h).
 *
 * Exit status: 0 on a signal, 1 on errors, 4 when there is no usable
 * framebuffer (init then falls back to VOIDMOTHS in text mode).
 *
 * Build: aarch64-linux-gnu-gcc -static -O2 -o arcade arcade.c -lm
 */
#define _GNU_SOURCE
#include "arcade_common.h"
#include "hedgewick_art.h"
#include "touch.h"
#include <dirent.h>
#include <sys/prctl.h>
#include <sys/wait.h>

#define CW 270
#define CH 480
#define NGAME 3
#define IDLE_STOP (120LL * 1000000000LL)    /* stop animating after 2 min idle */
#define MAXSHOT 8
#define MAXSCRIPT 64
#define ONEKEY_MS 600                       /* one button only: held this long, play */

static const struct {
	const char *name, *bin, *tag1, *tag2;
	const char *hisuf;              /* high-score file: <run-dir>/<bin><hisuf> */
	const char *opt_mmap, *opt_write;   /* how to ask for each framebuffer path */
} games[NGAME] = {
	{ "VOIDMOTHS", "voidmoths", "FIXED SHOOTER", "HOLD OFF THE SWARM", ".hi", NULL, NULL },
	{ "HEDGEWICK", "hedgewick", "MAZE CHASE", "CARRY THE LANTERN", ".hi", NULL, "--fb-write" },
	{ "GLINT", "glint", "TILT MAZE", "TILT THE PHONE", ".best", "--fb-mmap", NULL },
};
#define GLINT 2

static struct {
	int installed, returns;
	long hi;
	long cleared, best1;            /* GLINT: boards cleared, board 1 in 1/100 s */
} gs[NGAME];

static const char *bin_dir = "/usr/bin", *run_dir = "/run", *tty_path = "/dev/tty1";
static const char *iio_root = "/sys/bus/iio/devices";
static int sel = 1, swap_lr, mute, vol_set, launch_req, home_latch, dirty = 1;
static int motion;                      /* an "apple-aop-accel" IIO device is there */
static int onekey_armed;                /* one button only: pressed, not yet decided */
static int64_t onekey_t0;
static long touch_starts, touch_picks;
static uint8_t remap_grey[256];
static long anim;                       /* 10 fps animation frames */
static int64_t last_input;
static char msg[64];                    /* last launch problem, shown in the footer */
static long launches, returns_ok;

/* ------------------------------------------------------------- the games */

static void game_path(int i, const char *dir, const char *suffix, char *out, size_t n)
{
	snprintf(out, n, "%s/%s%s", dir, games[i].bin, suffix);
}

/* Is there an IIO device named "apple-aop-accel" (GLINT's tilt)? */
static int find_motion(void)
{
	DIR *d = opendir(iio_root);
	struct dirent *e;
	int found = 0;
	if (!d) return 0;
	while (!found && (e = readdir(d))) {
		char p[512], name[64] = "";
		FILE *f;
		if (e->d_name[0] == '.') continue;
		snprintf(p, sizeof p, "%s/%s/name", iio_root, e->d_name);
		f = fopen(p, "re");
		if (!f) continue;
		if (fgets(name, sizeof name, f)) {
			name[strcspn(name, "\n")] = 0;
			found = !strcmp(name, "apple-aop-accel");
		}
		fclose(f);
	}
	closedir(d);
	return found;
}

/* GLINT needs the motion sensor; the others can always be chosen. */
static int selectable(int i) { return i != GLINT || motion; }

static void check_motion(void)
{
	int m = find_motion();
	if (m != motion) {
		logmsg("motion sensor (apple-aop-accel under %s): %s", iio_root, m ? "found" : "gone");
		motion = m;
		dirty = 1;
	}
	if (!selectable(sel)) sel = 1;
}

/* The program's clock in ms: real time, or the scripted ticks. */
static int64_t clock_ms(void)
{
	return bot ? (int64_t)tick * 1000 / HZ : now_ns() / 1000000;
}

/* The screen went dark or came back (backlight.h): no sound, no touch while
 * dark; everything drawn again when it is back. */
static int was_dark;
static void screen_edges(void)
{
	if (bl_dark() == was_dark) return;
	was_dark = bl_dark();
	touch_discard(clock_ms());
	if (was_dark) {
		snd_suspend();
		onekey_armed = 0;
	} else {
		fb.full = 1;
		dirty = 1;
		last_input = now_ns();
	}
}

/* Installed? Does it know --from-picker? Its high score. */
static void probe_games(void)
{
	for (int i = 0; i < NGAME; i++) {
		char p[256];
		FILE *f;
		game_path(i, bin_dir, "", p, sizeof p);
		gs[i].installed = access(p, X_OK) == 0;
		gs[i].returns = 0;
		f = gs[i].installed ? fopen(p, "re") : NULL;
		if (f) {
			/* look for the option's name in the binary */
			static char buf[65536];
			size_t keep = 0, n;
			while ((n = fread(buf + keep, 1, sizeof buf - keep, f)) > 0) {
				size_t len = keep + n;
				if (memmem(buf, len, "--from-picker", 13)) { gs[i].returns = 1; break; }
				keep = len < 12 ? len : 12;
				memmove(buf, buf + len - keep, keep);
			}
			fclose(f);
		}
		game_path(i, run_dir, games[i].hisuf, p, sizeof p);
		if (i == GLINT) {
			/* "<boards cleared>\n" then one line of centiseconds per board */
			gs[i].cleared = gs[i].best1 = 0;
			f = fopen(p, "re");
			if (f) {
				long n = 0, b = 0;
				if (fscanf(f, "%ld", &n) == 1 && n >= 0 && n <= 99) {
					gs[i].cleared = n;
					if (fscanf(f, "%ld", &b) == 1 && b > 0 && b <= 599999) gs[i].best1 = b;
				}
				fclose(f);
			}
		} else {
			gs[i].hi = read_number(p);
		}
	}
}

/* --------------------------------------------------------------- sound */

static void sfx_move(void)
{
	static const float f[NGAME] = { 1175.0f, 1319.0f, 1568.0f };
	tnote(f[sel], 0.0f, 45.0f, 0.1f);
}
static void sfx_go(void)
{
	tnote(988.0f, 0.0f, 80.0f, 0.13f);
	tnote(1319.0f, 70.0f, 80.0f, 0.13f);
	tnote(1976.0f, 140.0f, 200.0f, 0.13f);
}
static void sfx_no(void) { voice(W_SQUARE, 440.0f, 392.0f, 160.0f, 0.1f, 0.5f, 1, 0.0f); }

/* --------------------------------------------------------------- drawing */

/* VOIDMOTHS' cast, as small pixel icons for the preview */
static const char *const vm_moth[3][5] = {
	{ "M..M..M", "MM.W.MM", "MMMMMMM", ".MM.MM.", "M.....M" },   /* empress */
	{ ".Y...Y.", "YYY.YYY", "YyYOYyY", ".YY.YY.", "..Y.Y.." },   /* lantern */
	{ "E.....E", ".E.E.E.", "EEEWEEE", ".E.E.E.", "E.....E" },   /* dart */
};
static const char *const vm_ship[6] = {
	"....y....", "...YYY...", "..YyOyY..", ".wYYYYYw.", "ww.YYY.ww", "w...O...w",
};

/* A sprite drawn upside down: the moths' wing beat. */
static void sprite_vflip(int x, int y, const char *const *rows, int h)
{
	const char *r[8];
	for (int j = 0; j < h && j < 8; j++) r[j] = rows[h - 1 - j];
	cv_sprite(x, y, r, h, 0, NULL, 1);
}

static void preview_voidmoths(int x0, int y0, int w, int h, long f)
{
	int sway = (int)lround(6.0 * sin((double)f * 0.12)), sx;
	cv_rect(x0, y0, w, h, pc('K'));
	for (int i = 0; i < 30; i++) {
		uint32_t v = (uint32_t)i * 2654435761u;
		int yy = (int)((v >> 8) % (unsigned)h + (unsigned)(f / 3)) % h;
		cv_px(x0 + (int)(v % (unsigned)w), y0 + yy, pc(i % 4 ? 'S' : 'w'));
	}
	for (int r = 0; r < 3; r++)
		for (int c = 0; c < 4; c++) {
			int mx = x0 + 16 + c * 22 + sway, my = y0 + 12 + r * 14;
			int diving = r == 2 && c == (int)((f / 60) % 4) && f % 60 >= 20;
			if (diving) {
				double t = (double)(f % 60 - 20) / 40.0;
				mx += (int)lround(18.0 * sin(t * 6.28));
				my += (int)lround(t * (h - 40));
			}
			if ((f / 4 + r + c) % 2) cv_sprite(mx, my, vm_moth[r], 5, 0, NULL, 1);
			else sprite_vflip(mx, my, vm_moth[r], 5);
		}
	sx = x0 + w / 2 - 4 + (int)lround(30.0 * sin((double)f * 0.07));
	cv_sprite(sx, y0 + h - 10, vm_ship, 6, 0, NULL, 1);
	if (f % 12 < 9) {
		int shy = y0 + h - 14 - (int)(f % 12) * 9;
		cv_rect(sx + 4, shy, 1, 3, pc('y'));
	}
}

/* HEDGEWICK: Wick walks round a ring of path, gathering embers, the moth on
 * his heels; every other lap a bloom turns the tables. */
#define RW 12
#define RH 10
static void ring_pos(long p, int *x, int *y, int *d)
{
	const int lw = (RW - 3) * 9, lh = (RH - 3) * 9, per = 2 * (lw + lh);
	p %= per;
	if (p < 0) p += per;
	if (p < lw) { *x = 9 + (int)p; *y = 9; *d = 1; }
	else if (p < lw + lh) { *x = 9 + lw; *y = 9 + (int)p - lw; *d = 2; }
	else if (p < 2 * lw + lh) { *x = 9 + lw - ((int)p - lw - lh); *y = 9 + lh; *d = 3; }
	else { *x = 9; *y = 9 + lh - ((int)p - 2 * lw - lh); *d = 0; }
}

static void preview_hedgewick(int x0, int y0, int w, int h, long f)
{
	const int lw = (RW - 3) * 9, lh = (RH - 3) * 9, per = 2 * (lw + lh);
	long lap = (f * 3) / per, p = (f * 3) % per;
	int flee = lap % 2 == 1, hx, hy, hd, mx, my, md;
	cv_rect(x0, y0, w, h, pc('g'));
	for (int ty = 0; ty < RH; ty++)
		for (int tx = 0; tx < RW; tx++) {
			int ring = (tx == 1 || tx == RW - 2) ? ty >= 1 && ty <= RH - 2 : (ty == 1 || ty == RH - 2) && tx >= 1 && tx <= RW - 2;
			int inner = (tx == 4 || tx == 7) && ty >= 4 && ty <= RH - 5;
			int px = x0 + tx * 9, py = y0 + ty * 9;
			if (px + 9 > x0 + w || py + 9 > y0 + h) continue;
			if (ring || inner) {
				cv_rect(px, py, 9, 9, pc('N'));
			} else {
				cv_rect(px, py, 9, 9, pc('g'));
				for (int k = 0; k < 6; k++) {
					uint32_t v = (uint32_t)(tx * 31 + ty * 17 + k) * 2654435761u;
					cv_px(px + (int)(v % 9), py + (int)((v >> 8) % 9), pc(k % 2 ? 'G' : 'L'));
				}
			}
		}
	/* embers ahead of Wick on the ring; the bloom at a corner */
	for (long e = 0; e < per; e += 9) {
		int ex, ey, ed;
		if (!flee && e < p) continue;
		ring_pos(e, &ex, &ey, &ed);
		if (e == per / 2) {
			if (!flee) cv_sprite(x0 + ex + 1, y0 + ey + 1, art_bloom[(f / 3) % 2], 7, 0, NULL, 1);
			continue;
		}
		cv_px(x0 + ex + 4, y0 + ey + 4, pc('Y'));
		cv_px(x0 + ex + 3, y0 + ey + 4, pc('O')); cv_px(x0 + ex + 5, y0 + ey + 4, pc('O'));
		cv_px(x0 + ex + 4, y0 + ey + 3, pc('O')); cv_px(x0 + ex + 4, y0 + ey + 5, pc('O'));
	}
	ring_pos(p, &hx, &hy, &hd);
	ring_pos(flee ? p + 40 : p - 45, &mx, &my, &md);
	{
		const char *const *ms = art_crit[K_MOTH][(f / 2) % 2];
		if (!flee) cv_sprite(x0 + mx - 1, y0 + my - 1, ms, SPR, 0, NULL, 1);
		else    /* dazzled: pale */
			for (int j = 0; j < SPR; j++)
				for (int i = 0; i < SPR; i++)
					if (ms[j][i] != '.')
						cv_px(x0 + mx - 1 + i, y0 + my - 1 + j, pc(ms[j][i] == 'W' ? 'b' : ms[j][i] == 'v' ? 'c' : 'C'));
	}
	cv_sprite(x0 + hx - 1, y0 + hy - 1, art_wick[hd == 3 ? 1 : hd][(f / 2) % 2], SPR, hd == 3, NULL, 1);
}

/* GLINT: a glass marble rolls over a wooden board, round two walls and
 * past two holes, and drops into the brass cup; then it starts again. */
static void disc(int cx, int cy, int r, int col)
{
	for (int dy = -r; dy <= r; dy++)
		for (int dx = -r; dx <= r; dx++)
			if (dx * dx + dy * dy <= r * r + r / 2) cv_px(cx + dx, cy + dy, col);
}

static void preview_glint(int x0, int y0, int w, int h, long f)
{
	/* the marble's way: right, down, left, down, right into the cup */
	static const int wp[][2] = { { 14, 16 }, { 98, 16 }, { 98, 46 }, { 16, 46 }, { 16, 78 }, { 92, 78 } };
	const int nwp = (int)(sizeof wp / sizeof wp[0]);
	int len = 0, s, mx = wp[0][0], my = wp[0][1], in_cup;
	for (int k = 1; k < nwp; k++) len += abs(wp[k][0] - wp[k - 1][0]) + abs(wp[k][1] - wp[k - 1][1]);
	s = (int)((f * 3) % (len + 36));
	in_cup = s >= len;
	if (!in_cup) {
		int left = s;
		for (int k = 1; k < nwp; k++) {
			int dx = wp[k][0] - wp[k - 1][0], dy = wp[k][1] - wp[k - 1][1], seg = abs(dx) + abs(dy);
			if (left <= seg) {
				mx = wp[k - 1][0] + (dx > 0 ? left : dx < 0 ? -left : 0);
				my = wp[k - 1][1] + (dy > 0 ? left : dy < 0 ? -left : 0);
				break;
			}
			left -= seg;
		}
	}
	/* the board: wood with a running grain, a brass rim */
	for (int y = 0; y < h; y++)
		for (int x = 0; x < w; x++) {
			int g = (int)lround(3.0 * sin((double)x * 0.09 + (double)(y % 13) * 0.7)) + y;
			cv_px(x0 + x, y0 + y, pc(g % 9 == 0 ? 'u' : g % 9 == 4 ? 'k' : 'U'));
		}
	cv_frame(x0, y0, w, h, pc('I'));
	cv_frame(x0 + 1, y0 + 1, w - 2, h - 2, pc('a'));
	/* two walls with a lit top edge */
	cv_rect(x0 + 2, y0 + 29, 80, 4, pc('u'));
	cv_rect(x0 + 2, y0 + 29, 80, 1, pc('k'));
	cv_rect(x0 + 30, y0 + 61, w - 32, 4, pc('u'));
	cv_rect(x0 + 30, y0 + 61, w - 32, 1, pc('k'));
	/* holes, the cup */
	disc(x0 + 58, y0 + 25, 3, pc('u'));
	disc(x0 + 58, y0 + 25, 2, pc('i'));
	disc(x0 + 44, y0 + 56, 3, pc('u'));
	disc(x0 + 44, y0 + 56, 2, pc('i'));
	disc(x0 + 92, y0 + 78, 7, pc('I'));
	disc(x0 + 92, y0 + 78, 5, pc('a'));
	disc(x0 + 92, y0 + 78, 4, pc('J'));
	if (in_cup) {
		/* resting in the cup, a little smaller, with a glint */
		disc(x0 + 92, y0 + 78, 3, pc('x'));
		cv_px(x0 + 91, y0 + 77, pc('X'));
		if ((f / 2) % 2) cv_px(x0 + 90, y0 + 76, pc('W'));
	} else {
		double a = (double)s / 5.0;     /* rolls: the swirl turns with the distance */
		disc(x0 + mx + 1, y0 + my + 2, 5, pc('u'));         /* shadow */
		disc(x0 + mx, y0 + my, 5, pc('x'));
		disc(x0 + mx - 1, y0 + my - 1, 3, pc('X'));
		cv_px(x0 + mx + (int)lround(3.0 * cos(a)), y0 + my + (int)lround(3.0 * sin(a)), pc('O'));
		cv_px(x0 + mx + (int)lround(3.0 * cos(a + 3.14)), y0 + my + (int)lround(3.0 * sin(a + 3.14)), pc('o'));
		cv_px(x0 + mx - 2, y0 + my - 3, pc('W'));
	}
}

static void fmt_cs(long cs, char *b, size_t n)
{
	snprintf(b, n, "%ld:%02ld.%02ld", cs / 6000, (cs / 100) % 60, cs % 100);
}

#define CARD_H 110
static void draw_card(int i, int y)
{
	int on = i == sel, x = 8, w = CW - 16, h = CARD_H;
	long f = on ? anim : 0;
	char b[48], t[24];
	cv_box(x, y, w, h, pc('h'), pc(on ? 'Y' : 'Q'));
	if (on) cv_frame(x + 1, y + 1, w - 2, h - 2, pc('O'));
	cv_frame(17, y + 7, 114, 96, pc('s'));
	if (i == 0) preview_voidmoths(18, y + 8, 112, 94, f);
	else if (i == 1) preview_hedgewick(18, y + 8, 112, 94, f);
	else preview_glint(18, y + 8, 112, 94, f);
	txt(140, y + 9, games[i].name, pc(on ? 'W' : 'w'), 2, pc('K'));
	txt(140, y + 30, games[i].tag1, pc('B'), 1, -1);
	txt(140, y + 40, games[i].tag2, pc('H'), 1, -1);
	if (i == GLINT) {
		snprintf(b, sizeof b, "CLEARED %ld/6", gs[i].cleared);
		txt(140, y + 56, b, pc('Y'), 1, -1);
		if (gs[i].best1 > 0) {
			fmt_cs(gs[i].best1, t, sizeof t);
			snprintf(b, sizeof b, "BOARD 1 %s", t);
			txt(140, y + 66, b, pc('Y'), 1, -1);
		}
	} else {
		snprintf(b, sizeof b, "HI %06ld", gs[i].hi);
		txt(140, y + 56, b, pc('Y'), 1, -1);
	}
	if (!gs[i].installed) {
		txt(140, y + 78, "NOT INSTALLED", pc('R'), 1, -1);
	} else if (!gs[i].returns) {
		txt(140, y + 78, "NO WAY BACK TO", pc('P'), 1, -1);
		txt(140, y + 88, "THE ARCADE YET", pc('P'), 1, -1);
	}
	if (!selectable(i)) {
		/* no motion sensor: the card goes grey */
		for (int yy = y; yy < y + h; yy++)
			for (int xx = x; xx < x + w; xx++) fb.cv[yy * CW + xx] = remap_grey[fb.cv[yy * CW + xx]];
		cv_box(138, y + 74, CW - 8 - 138 - 4, 25, pc('K'), pc('R'));
		txt(142, y + 78, "NEEDS A", pc('R'), 1, -1);
		txt(142, y + 88, "MOTION SENSOR", pc('R'), 1, -1);
		return;
	}
	if (on && gs[i].installed) {
		const char *k = have_home || have_power || have_vol ? (have_home ? "HOME: PLAY" : "POWER: PLAY") : NULL;
		if (touch_present()) k = k ? (have_home ? "HOME/TAP: PLAY" : "TAP: PLAY") : "TAP AGAIN: PLAY";
		if (k) {
			txt(140, y + 96, k, pc('Y'), 1, -1);
			if ((anim / 5) % 2 == 0) txt(140 + txt_w(k, 1) + 4, y + 96, "\2", pc('Y'), 1, -1);
		}
	}
}

static void render(void)
{
	cv_clear(pc('K'));
	for (int i = 0; i < 40; i++) {
		uint32_t v = (uint32_t)(i + 7) * 2246822519u;
		cv_px((int)(v % CW), (int)((v >> 9) % 46), pc((anim / 3 + i) % 31 ? 'S' : 'W'));
	}
	txt_c(CW / 2, 5, "ARCADE", pc('Y'), 4, pc('t'));
	txt_c(CW / 2, 38, "CHOOSE A GAME", pc('w'), 1, -1);
	for (int i = 0; i < NGAME; i++) draw_card(i, 50 + i * (CARD_H + 6));
	{
		/* the controls this board has */
		const char *keys = have_vol ? (have_home || !have_power ? "VOL+ / VOL-  CHOOSE     HOME  PLAY"
							  : "VOL+ / VOL-  CHOOSE     POWER  PLAY")
			: have_home && have_power ? "POWER  NEXT GAME     HOME  PLAY"
			: have_home ? "HOME: TAP NEXT GAME, HOLD PLAY"
			: have_power ? "POWER: TAP NEXT GAME, HOLD PLAY" : NULL;
		int yk = 398;
		if (bl_mode() == BLK_PRESS) keys = "VOL+/- CHOOSE  HOME PLAY  POWER SCREEN OFF";
		else if (bl_mode() == BLK_LONG) keys = "POWER NEXT  HOME PLAY  HOLD POWER SCREEN OFF";
		if (keys) { txt_c(CW / 2, yk, keys, pc('W'), 1, -1); yk += 11; }
		if (touch_present()) txt_c(CW / 2, yk, "TOUCH: TAP A GAME, TAP AGAIN TO PLAY", pc('W'), 1, -1);
		else if (!keys) txt_c(CW / 2, yk, "NO BUTTONS OR TOUCHSCREEN YET", pc('R'), 1, -1);
		txt_c(CW / 2, 422, have_home ? "TO COME BACK: HOLD HOME 2 S ON A TITLE"
			: "TO COME BACK: HOLD POWER 2 S ON A TITLE", pc('B'), 1, -1);
		if (touch_present()) txt_c(CW / 2, 432, "OR HOLD ITS TOP LEFT CORNER 2 S", pc('B'), 1, -1);
	}
	if (msg[0]) txt_c(CW / 2, 446, msg, pc('R'), 1, -1);
	{
		char b[64];
		snprintf(b, sizeof b, "%.44s", dev_desc);
		txt_c(CW / 2, 458, b, pc('H'), 1, -1);
		if (touch_present()) {
			snprintf(b, sizeof b, "%.44s", tp.desc);
			txt_c(CW / 2, 468, b, pc('H'), 1, -1);
		}
	}
	if (ntc_t > 0) {
		char lines[5][64];
		int n = wrap_text(ntc_text, 42, lines, 5);
		cv_box(4, 396, CW - 8, 80, pc('n'), pc('B'));
		txt(10, 401, "NOTICE", pc('B'), 1, -1);
		for (int i = 0; i < n; i++) txt(10, 413 + 12 * i, lines[i], pc('W'), 1, -1);
	}
}

/* ------------------------------------------------------------------ input */

/* Move the selection by one, over the cards that can be chosen. */
static void step_sel(int d)
{
	int ns = sel;
	for (int k = 0; k < NGAME; k++) {
		ns = (ns + d + NGAME) % NGAME;
		if (selectable(ns)) break;
	}
	if (ns != sel) { sel = ns; sfx_move(); }
}

static void want_launch(void)
{
	if (!selectable(sel)) { snprintf(msg, sizeof msg, "%s NEEDS A MOTION SENSOR", games[sel].name); sfx_no(); return; }
	launch_req = 1;
}

/*
 * The keys, by what the board has: with volume keys they choose and Home
 * (Power without Home) plays, Power with Home plays too. Without volume
 * keys, Power with Home is "next game"; with one button only, a tap is
 * "next game" and a 0.6 s hold plays (onekey_tick()). KEY_MUTE (the iPads'
 * mute switch, held while the switch is on) and other keys do nothing.
 */
static void on_key(int code, int value)
{
	int up = swap_lr ? KEY_VOLUMEDOWN : KEY_VOLUMEUP, down = swap_lr ? KEY_VOLUMEUP : KEY_VOLUMEDOWN;
	int home = have_home ? code == KEY_HOMEPAGE || code == KEY_HOME : code == KEY_POWER;
	int onekey = !have_vol && (have_home ^ have_power);
	last_input = now_ns();
	dirty = 1;
	if (value == 0) {
		if (home && onekey && onekey_armed) { onekey_armed = 0; step_sel(1); }
		if (home) home_latch = 0;
		return;
	}
	if (code == up || code == KEY_UP || code == down || code == KEY_DOWN) {
		step_sel(code == up || code == KEY_UP ? -1 : 1);
	} else if (home && onekey) {
		if (!home_latch) { onekey_armed = 1; onekey_t0 = clock_ms(); }
	} else if (code == KEY_POWER && have_home && !have_vol) {
		step_sel(1);
	} else if ((home && !home_latch) || code == KEY_ENTER || code == KEY_SPACE || (code == KEY_POWER && have_home)) {
		want_launch();
	}
}

/* One button only: held long enough, it plays. */
static void onekey_tick(void)
{
	if (onekey_armed && clock_ms() - onekey_t0 >= ONEKEY_MS) {
		onekey_armed = 0;
		home_latch = 1;         /* its release is not "next game" */
		dirty = 1;
		want_launch();
	}
}

/* Touch: a tap on a card chooses it, a tap on the chosen card plays. */
static void touch_tick(void)
{
	TGesture g;
	touch_view(fb.w, fb.h, fb.ox, fb.oy, fb.scale, CW, CH, 0, 0);
	touch_poll(clock_ms(), grab);
	if (tp.down) { last_input = now_ns(); }
	while (touch_next(&g)) {
		last_input = now_ns();
		dirty = 1;
		if (g.kind != TG_TAP) continue;
		for (int i = 0; i < NGAME; i++) {
			int y = 50 + i * (CARD_H + 6);
			if (!touch_in(8, y, CW - 8, y + CARD_H, g.x, g.y)) continue;
			if (!selectable(i)) {
				snprintf(msg, sizeof msg, "%s NEEDS A MOTION SENSOR", games[i].name);
				sfx_no();
			} else if (i == sel) {
				touch_starts++;
				want_launch();
			} else {
				touch_picks++;
				sel = i;
				msg[0] = 0;
				sfx_move();
			}
		}
	}
}

/* ------------------------------------------------------------- run a game */

static int64_t run_t0;

static void wait_child(pid_t pid, int *st)
{
	int killed = 0;
	int64_t deadline = 0;
	for (;;) {
		pid_t r = waitpid(pid, st, killed ? WNOHANG : 0);
		if (r == pid) return;
		if (r < 0 && errno != EINTR) { *st = 0; return; }
		if (quit_sig && !killed) {
			logmsg("signal %d: stopping the game (pid %d)", (int)quit_sig, (int)pid);
			kill(pid, SIGTERM);
			killed = 1;
			deadline = now_ns() + 3000000000LL;
		}
		if (killed) {
			struct timespec ts = { 0, 50000000 };
			if (now_ns() > deadline) { kill(pid, SIGKILL); killed = 2; deadline = INT64_MAX; }
			nanosleep(&ts, NULL);
		}
	}
}

static void run_game(int i)
{
	char bin[256], hip[256], logp[256], volb[16];
	char *argv[32];
	int n = 0, st = 0, held;
	pid_t pid;

	probe_games();
	if (!gs[i].installed) {
		snprintf(msg, sizeof msg, "%s IS NOT INSTALLED", games[i].name);
		sfx_no();
		return;
	}
	game_path(i, bin_dir, "", bin, sizeof bin);
	game_path(i, run_dir, games[i].hisuf, hip, sizeof hip);
	game_path(i, run_dir, ".log", logp, sizeof logp);
	argv[n++] = (char *)games[i].bin;
	argv[n++] = (char *)"--tty";
	argv[n++] = (char *)tty_path;
	argv[n++] = (char *)"--hi";
	argv[n++] = hip;
	if (gs[i].returns) argv[n++] = (char *)"--from-picker";
	if (swap_lr) argv[n++] = (char *)"--swap";
	if (!grab) argv[n++] = (char *)"--no-grab";
	if (mute) argv[n++] = (char *)"--mute";
	if (vol_set) {
		snprintf(volb, sizeof volb, "%.1f", (double)snd_vol_db);
		argv[n++] = (char *)"--vol";
		argv[n++] = volb;
	}
	/* the same framebuffer path as the picker */
	if (fb_use_write ? games[i].opt_write != NULL : games[i].opt_mmap != NULL)
		argv[n++] = (char *)(fb_use_write ? games[i].opt_write : games[i].opt_mmap);
#ifdef MZ_TEST
	{
		/* test builds: extra arguments for the child, e.g. "--bot 30000" */
		static char extra[256];
		const char *e = getenv("ARCADE_CHILD_ARGS");
		if (e) {
			snprintf(extra, sizeof extra, "%s", e);
			for (char *t = strtok(extra, " "); t && n < 30; t = strtok(NULL, " ")) argv[n++] = t;
		}
	}
#endif
	argv[n] = NULL;

	sfx_go();
	/* let the start sound play out, then hand everything over */
	for (int k = 0; k < (bot ? 12 : 0); k++) { tick++; snd_tick(); }
	if (!bot) {
		int64_t until = now_ns() + 350000000LL;
		while (now_ns() < until && snd_state == SND_ON) {
			struct pollfd pf = { snd_pcm, POLLOUT, 0 };
			poll(&pf, 1, 20);
			snd_pump();
		}
	}
	snd_set_idle();
	close_devs();
	if (!tp_fake_active()) touch_close();   /* the game opens (and grabs) it itself */
	onekey_armed = 0;
	console_text(0);
	logmsg("start: %s%s (%s)", bin, gs[i].returns ? " --from-picker" : "", games[i].name);
	launches++;
	run_t0 = now_ns();
	fflush(stderr);
	pid = fork();
	if (pid == 0) {
		struct sigaction sa;
		int lf, nf;
		memset(&sa, 0, sizeof sa);
		sa.sa_handler = SIG_DFL;
		for (int s = 1; s < 32; s++) sigaction(s, &sa, NULL);
		prctl(PR_SET_PDEATHSIG, SIGTERM);   /* the picker killed: the game goes too */
		lf = open(logp, O_WRONLY | O_CREAT | O_APPEND | O_CLOEXEC, 0644);
		if (lf >= 0) dup2(lf, 2);
		nf = open("/dev/null", O_RDWR | O_CLOEXEC);
		if (nf >= 0) { dup2(nf, 0); dup2(nf, 1); }
		execv(bin, argv);
		dprintf(2, "arcade: exec %s: %s\n", bin, strerror(errno));
		_exit(127);
	}
	if (pid < 0) {
		logmsg("fork: %s", strerror(errno));
		snprintf(msg, sizeof msg, "COULD NOT START %s", games[i].name);
	} else {
		wait_child(pid, &st);
		msg[0] = 0;
		if (WIFEXITED(st) && WEXITSTATUS(st) == 0) {
			returns_ok++;
			logmsg("%s returned after %.0f s", games[i].name, (double)(now_ns() - run_t0) / 1e9);
		} else if (WIFEXITED(st) && WEXITSTATUS(st) == 127) {
			snprintf(msg, sizeof msg, "COULD NOT START %s", games[i].name);
			logmsg("%s: exec failed (see %s)", games[i].name, logp);
		} else if (WIFEXITED(st)) {
			snprintf(msg, sizeof msg, "%s ENDED WITH STATUS %d", games[i].name, WEXITSTATUS(st));
			logmsg("%s exited with status %d after %.0f s", games[i].name, WEXITSTATUS(st), (double)(now_ns() - run_t0) / 1e9);
		} else if (WIFSIGNALED(st)) {
			snprintf(msg, sizeof msg, "%s STOPPED BY SIGNAL %d", games[i].name, WTERMSIG(st));
			logmsg("%s killed by signal %d", games[i].name, WTERMSIG(st));
		}
	}
	/* take the console back, and the light: a game killed while its screen
	 * was dark (SIGKILL) could not light it itself */
	bl_force_on("back from the game");
	console_graphics();
	fb.full = 1;
	held = 0;
	if (!bot) {
		scan_devs();
		held = have_home ? keydown[KEY_HOMEPAGE] || keydown[KEY_HOME] : keydown[KEY_POWER];
	} else {
		held = keydown[KEY_HOMEPAGE];
	}
	home_latch = held;      /* the Home hold that left the game: not a new start */
	if (!tp_fake_active()) touch_open(grab);
	touch_flush();          /* nothing from the game's last touch */
	check_motion();
	notice_sync();          /* a notice the game already showed is not shown again */
	ntc_t = 0;
	probe_games();
	launch_req = 0;
	last_input = now_ns();
	dirty = 1;
}

/* ------------------------------------------------------- scripted self-test */

static struct { long t; int code, hold; } script[MAXSCRIPT];
static int nscript;
static struct { char when[16]; const char *path; int done; } shots[MAXSHOT];
static int nshots, returned_frames = -1;

static int parse_script(const char *s)
{
	char buf[1024], *save = NULL;
	snprintf(buf, sizeof buf, "%s", s);
	for (char *t = strtok_r(buf, ",", &save); t && nscript < MAXSCRIPT; t = strtok_r(NULL, ",", &save)) {
		char key[16] = "";
		long at;
		int hold = 2;
		if (sscanf(t, "%ld:%15[a-z]*%d", &at, key, &hold) < 2) return -1;
		script[nscript].t = at;
		script[nscript].hold = hold < 1 ? 1 : hold;
		if (!strcmp(key, "volup")) script[nscript].code = KEY_VOLUMEUP;
		else if (!strcmp(key, "voldown")) script[nscript].code = KEY_VOLUMEDOWN;
		else if (!strcmp(key, "home")) script[nscript].code = KEY_HOMEPAGE;
		else if (!strcmp(key, "power")) script[nscript].code = KEY_POWER;
		else if (!strcmp(key, "mute")) script[nscript].code = KEY_MUTE;     /* the iPads' mute switch */
		else if (!strcmp(key, "chord")) script[nscript].code = -1;
		else return -1;
		nscript++;
	}
	return 0;
}

static void script_tick(void)
{
	for (int i = 0; i < nscript; i++) {
		int c = script[i].code;
		if (tick == script[i].t) {
			if (c < 0) {
				key_event(KEY_VOLUMEUP, 1);
				key_event(KEY_VOLUMEDOWN, 1);
			} else {
				key_event(c, 1);
			}
		}
		if (tick == script[i].t + script[i].hold) {
			if (c < 0) {
				key_event(KEY_VOLUMEUP, 0);
				key_event(KEY_VOLUMEDOWN, 0);
			} else {
				key_event(c, 0);
			}
		}
	}
}

static void take_shots(void)
{
	for (int i = 0; i < nshots; i++) {
		const char *w = shots[i].when;
		int due = (w[0] >= '0' && w[0] <= '9') ? tick == atol(w)
			: !strcmp(w, "return") ? returned_frames == 4
			: !strcmp(w, "toast") ? ntc_t == NOTICE_T - 20 : 0;
		if (shots[i].done || !due) continue;
		shots[i].done = 1;
		if (fb_dump_ppm(shots[i].path) == 0) logmsg("shot %s: %s (tick %ld)", w, shots[i].path, tick);
	}
}

/* ------------------------------------------------------------------- main */

static void usage(void)
{
	fprintf(stderr,
		"usage: arcade [--tty PATH] [--fb PATH] [--bin-dir DIR] [--run-dir DIR] [--notice PATH|none]\n"
		"              [--swap] [--no-grab] [--mute] [--vol DB] [--fb-write] [--iio-root DIR]\n"
		"       arcade --restore-console [--tty PATH] | --restore-tty PATH\n"
		"       arcade --script EVENTS --ticks N [--shot WHEN:FILE.ppm]... [--script-notice TICK:TEXT]\n"
		"              [--keys home,power,vol|none]\n");
	exit(2);
}

int main(int argc, char **argv)
{
	const char *fb_path = "/dev/fb0", *script_s = NULL, *snotice = NULL, *keys_s = "home,power,vol";
	long ticks = 0, snotice_t = -1;
	int restore = 0;

	common_init("arcade");
	for (int i = 1; i < argc; i++) {
		const char *a = argv[i], *v = i + 1 < argc ? argv[i + 1] : NULL;
		if (!strcmp(a, "--tty") && v) { tty_path = v; i++; }
		else if (!strcmp(a, "--fb") && v) { fb_path = v; i++; }
		else if (!strcmp(a, "--bin-dir") && v) { bin_dir = v; i++; }
		else if (!strcmp(a, "--run-dir") && v) { run_dir = v; i++; }
		else if (!strcmp(a, "--notice") && v) { ntc_path = strcmp(v, "none") ? v : NULL; i++; }
		else if (!strcmp(a, "--swap")) swap_lr = 1;
		else if (!strcmp(a, "--no-grab")) grab = 0;
		else if (!strcmp(a, "--mute")) mute = 1;
		else if (!strcmp(a, "--fb-write")) fb_use_write = 1;
		else if (!strcmp(a, "--iio-root") && v) { iio_root = v; i++; }
		else if (!strcmp(a, "--keys") && v) { keys_s = v; i++; }
		else if (!strcmp(a, "--vol") && v) {
			snd_vol_db = strtof(v, NULL);
			if (!(snd_vol_db <= VOL_MAX_DB)) snd_vol_db = VOL_MAX_DB;
			if (snd_vol_db < -60.0f) snd_vol_db = -60.0f;
			vol_set = 1;
			i++;
		} else if (!strcmp(a, "--restore-console")) restore = 1;
		else if (!strcmp(a, "--restore-tty") && i + 1 < argc) { restore = 1; tty_path = argv[++i]; }
		else if (!strcmp(a, "--script") && v) { script_s = v; bot = 1; i++; }
		else if (!strcmp(a, "--ticks") && v) { ticks = atol(v); i++; }
		else if (!strcmp(a, "--script-notice") && v) {
			snotice_t = atol(v);
			snotice = strchr(v, ':');
			if (!snotice) usage();
			snotice++;
			i++;
		} else if (!strcmp(a, "--shot") && v && nshots < MAXSHOT) {
			const char *c = strchr(v, ':');
			if (!c || c == v || (size_t)(c - v) >= sizeof shots[0].when) usage();
			memcpy(shots[nshots].when, v, (size_t)(c - v));
			shots[nshots].when[c - v] = 0;
			shots[nshots++].path = c + 1;
			i++;
		} else usage();
	}

	if (restore) {
		/* after a SIGKILL: the console back to text, the screen lit */
		bl_restore_only();
		if (tty_open(tty_path) < 0) return 1;
		console_text(1);
		return 0;
	}
	if (bot && (parse_script(script_s) < 0 || ticks <= 0)) usage();

	install_signals();
	if (fb_open(fb_path) < 0) { logmsg("no usable framebuffer; exiting with status 4"); return 4; }
	if (fb_canvas(CW, CH) < 0) return 1;
	logmsg("fb: %s; canvas %dx%d scaled %dx at %d,%d", fb.desc, CW, CH, fb.scale, fb.ox, fb.oy);
	if (tty_open(tty_path) == 0) console_graphics();
	snd_ondemand = 1;
	snd_state = mute ? SND_OFF : SND_IDLE;
	for (int i = 0; i < 256; i++) remap_grey[i] = (uint8_t)i;
	for (int i = 0; i < NPAL; i++) {
		uint32_t rgb = palette[i].rgb;
		int lum = (int)((((rgb >> 16) & 255) * 3 + ((rgb >> 8) & 255) * 6 + (rgb & 255)) / 10);
		remap_grey[i] = (uint8_t)pc(lum > 150 ? 'S' : lum > 60 ? 's' : 'K');
	}
	motion = find_motion();
	logmsg("motion sensor (apple-aop-accel under %s): %s", iio_root, motion ? "found" : "none yet, GLINT is grey");
	if (!selectable(sel)) sel = 1;
	probe_games();
	for (int i = 0; i < NGAME; i++) {
		char sc[64];
		if (i == GLINT) snprintf(sc, sizeof sc, "cleared %ld, board 1 %ld cs", gs[i].cleared, gs[i].best1);
		else snprintf(sc, sizeof sc, "hi %ld", gs[i].hi);
		logmsg("%s: %s%s, %s", games[i].name, gs[i].installed ? "installed" : "not installed",
		       gs[i].installed && !gs[i].returns ? " (no --from-picker: it cannot come back here)" : "", sc);
	}
	bl_init(clock_ms);
	if (bot) {
		have_home = strstr(keys_s, "home") != NULL;
		have_power = strstr(keys_s, "power") != NULL;
		have_vol = strstr(keys_s, "vol") != NULL;
		snprintf(dev_desc, sizeof dev_desc, "scripted input (%s)", keys_s);
		bl_set_keys(have_home, have_power, have_vol);
	} else {
		scan_devs();
		home_latch = have_home ? keydown[KEY_HOMEPAGE] || keydown[KEY_HOME] : keydown[KEY_POWER];
	}
	touch_view(fb.w, fb.h, fb.ox, fb.oy, fb.scale, CW, CH, 0, 0);
	tp.last_scan = clock_ms();
	touch_open(grab);
	logmsg("%s", tp.desc);
	notice_sync();
	last_input = now_ns();

	if (bot) {
		/* scripted: one loop pass per 30 Hz tick, as fast as it goes */
		for (tick = 0; tick < ticks && !quit_sig; tick++) {
			if (tick == snotice_t && ntc_path) {
				FILE *f = fopen(ntc_path, "we");
				if (f) { fprintf(f, "%s\n", snotice); fclose(f); }
			}
			script_tick();
			fakekeys_pump(key_event);
			bl_tick();
			screen_edges();
			if (bl_dark()) {
				/* nothing moves; notices are still looked at */
				if (tick % (HZ / 2) == 0 && notice_check()) bl_wake("a notice");
				touch_discard(clock_ms());
				screen_edges();
				if (bl_dark()) continue;
			}
			onekey_tick();
			touch_tick();
			if (ntc_t > 0) ntc_t--;
			if (tick % (HZ / 2) == 0 && notice_check()) dirty = 1;
			if (tick % (2 * HZ) == 0 && !motion) check_motion();
			if (launch_req) {
				launch_req = 0;
				run_game(sel);
				returned_frames = 0;
			}
			snd_tick();
			if (tick % 3 == 0) {
				anim++;
				render();
				fb_present();
				if (returned_frames >= 0) returned_frames++;
			}
			take_shots();
		}
		fprintf(stderr, "ticks=%ld sel=%d launches=%ld returns=%ld frames=%ld bytes=%lld msg='%s' sound=%d\n",
			tick, sel, launches, returns_ok, fb.frames, fb.bytes, msg, snd_state);
		fprintf(stderr, "motion=%d touch=%d contacts=%ld taps=%ld swipes=%ld longs=%ld picks=%ld starts=%ld tdropped=%ld\n",
			motion, touch_present(), tp.n_contact, tp.n_tap, tp.n_swipe, tp.n_long, touch_picks, touch_starts, tp.n_dropped);
	} else {
		int64_t next_frame = now_ns(), next_notice = now_ns();
		long last_snd_tick = -1;
		int64_t next_scan = 0;
		while (!quit_sig) {
			int64_t now = now_ns(), wake;
			int animating = now - last_input < IDLE_STOP || ntc_t > 0;
			if (bl_dark()) {
				/* dark: sleep on the buttons only; twice a second the notice
				 * file (a new notice lights the screen) */
				struct pollfd pk[MAXDEV];
				int64_t left = (next_notice - now) / 1000000 + 1;
				for (int i = 0; i < ndev; i++) pk[i] = (struct pollfd){ dev_fd[i], POLLIN, 0 };
				poll(ndev ? pk : NULL, (nfds_t)ndev, left < 0 ? 0 : left > BL_IDLE_MS ? BL_IDLE_MS : (int)left);
				read_events();
				fakekeys_pump(key_event);
				now = now_ns();
				if (now >= next_notice) {
					if (notice_check()) bl_wake("a notice");
					touch_discard(clock_ms());
					next_notice = now + BL_IDLE_MS * 1000000LL;
				}
				if (rescan || (ndev == 0 && now >= next_scan)) { scan_devs(); next_scan = now + 2000000000LL; }
				screen_edges();
				if (!bl_dark()) next_frame = next_notice = now_ns();
				continue;
			}
			struct pollfd pf[MAXDEV + 2];
			nfds_t n = (nfds_t)(ndev > 0 && ndev <= MAXDEV ? ndev : 0);
			int si = -1, ms;
			wake = next_frame < next_notice ? next_frame : next_notice;
			ms = dirty ? 0 : (int)((wake - now + 999999) / 1000000);
			if (snd_state == SND_ON && ms > 30) ms = 30;
			if (ms < 0) ms = 0;
			for (int i = 0; i < ndev; i++) pf[i] = (struct pollfd){ dev_fd[i], POLLIN, 0 };
			if (snd_state == SND_ON && snd_poll_ok) {
				si = (int)n;
				pf[n++] = (struct pollfd){ snd_pcm, POLLOUT, 0 };
			}
			if (tp_pollfd() >= 0) pf[n++] = (struct pollfd){ tp_pollfd(), POLLIN, 0 };
			if ((onekey_armed || tp.down || bl_tick()) && ms > 50) ms = 50;    /* a hold or a long press ripens */
			poll(n ? pf : NULL, n, ms);
			read_events();
			fakekeys_pump(key_event);
			bl_tick();
			screen_edges();
			if (bl_dark()) { next_notice = now_ns(); continue; }
			onekey_tick();
			touch_tick();
			if (si >= 0 && pf[si].revents && !snd_pump()) snd_poll_ok = 0;
			now = now_ns();
			tick = (long)((now - t_start) / (1000000000LL / HZ));
			if (tick != last_snd_tick) { snd_tick(); last_snd_tick = tick; }
			if (rescan || (ndev == 0 && tick % (2 * HZ) == 0)) scan_devs();
			if (launch_req) {
				launch_req = 0;
				run_game(sel);
				next_frame = next_notice = now_ns();
				continue;
			}
			if (now >= next_notice) {
				if (ntc_t > 0) ntc_t -= HZ / 2;
				if (ntc_t < 0) ntc_t = 0;
				if (notice_check()) dirty = 1;
				if (!motion && tick % (2 * HZ) < HZ / 2) check_motion();
				next_notice = now + 500000000LL;
			}
			if (dirty || now >= next_frame) {
				if (now >= next_frame && animating) anim++;
				render();
				fb_present();
				dirty = 0;
				next_frame = now + (animating ? 100000000LL : 500000000LL);
			}
		}
		logmsg("stopped by signal %d", (int)quit_sig);
	}
	bl_exit();
	cpu_report();
	logmsg("frames %ld, %lld KB written", fb.frames, fb.bytes / 1024);
	snd_close();
	console_text(1);
	return 0;
}

#ifdef MZ_FAKE_ALSA
#include "fake_alsa.c"
#endif
