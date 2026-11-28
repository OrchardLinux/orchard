/*
 * hedgewick.c -- HEDGEWICK, a maze-chase game for a phone or tablet framebuffer
 * (the arcade payload: N66, J71, J85, J81).
 *
 * Wick carries a lantern through a night garden of hedges, gathering the
 * embers that lie on the paths to light the garden again. Four night
 * creatures hunt the light, each in its own way:
 *
 *   MOTH  flies straight at the lantern, but flutters (a quarter of its
 *         choices are random);
 *   TOAD  leaps ahead, to the next crossing on Wick's way, in hops;
 *   BAT   follows Wick's trail: it heads for where Wick was 2.5 s ago;
 *   OWL   guards the richest corner of the garden (the quarter with the
 *         most embers left) and swoops, faster, when Wick comes within six
 *         steps.
 *
 * A glow-bloom makes the lantern blaze: for a few seconds the creatures are
 * dazzled (pale, slow, fleeing) and Wick can catch them for 200, 400, 600,
 * 800 points; a caught creature bursts into sparks and sits in its den for
 * 2 s. All embers gathered: the garden is lit, next garden, everything a
 * little faster. Three gardens (KNOT GARDEN, YEW WALK, MOON CLOISTER) take
 * turns.
 *
 * Controls (three buttons on a portrait phone; see README-maze.md):
 *   Volume Up    turn left  (Wick's left), at the next opening
 *   Volume Down  turn right (Wick's right), at the next opening
 *   Home         tap: U-turn at once; on the title / game over: start (tap)
 *                hold 2 s: back to the title; on the title, when started by
 *                the arcade (--from-picker): back to the arcade
 *   Volume Up + Volume Down together: pause (title: sound on / off)
 *   Power        screen off (Home or Power: on again, the game paused); on
 *                a board without volume keys Power held 0.8 s, and a short
 *                press is a U-turn / start as before (backlight.h)
 *   USB keyboard: Left/Right turn, Space/Down/Enter U-turn or start, P pause,
 *                 M sound, Esc title (on the title, with --from-picker: arcade)
 *   Touchscreen (touch.h; found by its capabilities):
 *     swipe        go that way (up, down, left, right on the screen): at the
 *                  next opening; a swipe back is a U-turn at once
 *     tap          title / game over: start; paused: resume; the top bar
 *                  (score line) in play: pause
 *     top left corner, held: the Home button (2 s: title; on the title with
 *                  --from-picker: back to the arcade)
 *
 * Wick follows bends by himself and only stops at a T where the path ahead
 * ends; a turn pressed early waits (up to 1.5 s) for the next opening on
 * that side, and an arrow on the maze shows where it will be taken.
 *
 * Picture: /dev/fb0, drawn into a 180x320 canvas scaled by a whole number
 * and centred (6x: 1080x1920 on the N66, letterboxed on a 1536x2048 iPad), the
 * console VT in KD_GRAPHICS while the game runs. Sound: the speaker card
 * only, like VOIDMOTHS. Notices: the first line of /run/notice.
 *
 * usage: hedgewick [--tty PATH] [--fb PATH] [--hi PATH|none] [--from-picker]
 *                  [--swap] [--no-grab] [--mute] [--vol DB] [--fps 30|15|10]
 *                  [--fb-write | --fb-mmap]   (rows by pwrite(); default: mmap)
 *                  [--notice PATH|none]
 *        hedgewick --bot TICKS [--level N] [--seed N] [--shot WHEN:FILE.ppm]...
 *                  [--bot-notice TEXT] [--no-pilot] [other options]
 *            headless self-test: an autopilot plays TICKS ticks as fast as
 *            it can (picture into $FAKE_FB in test builds, or nowhere);
 *            --no-pilot: no autopilot, only $FAKE_TOUCH (test builds) plays.
 *
 * Build: aarch64-linux-gnu-gcc -static -O2 -o hedgewick hedgewick.c -lm
 */
#define _GNU_SOURCE
#include "arcade_common.h"
#include "hedgewick_art.h"
#include "touch.h"

#define CW 180              /* canvas, logical pixels */
#define CH 320
#define TS 9                /* tile size */
#define MW 19               /* maze, tiles */
#define MH 27
#define MX0 4               /* maze origin on the canvas */
#define MY0 23
#define SUB 16              /* sub-pixel steps per pixel */
#define TU (TS * SUB)       /* one tile in sub-pixels */
#define TC (4 * SUB)        /* tile centre: pixel 4 */
#define NCRIT 4
#define TRAIL 128
#define NMAZE 3
#define HOLD_SHOW (1 * HZ)
#define HOLD_QUIT (2 * HZ)
#define TURN_KEEP (3 * HZ / 2)   /* a queued turn waits this long */
#define MAXSHOT 12
#define CNR_W 30            /* the touch corner (virtual Home), canvas pixels */
#define CNR_H 26

static const int DX[4] = { 0, 1, 0, -1 }, DY[4] = { -1, 0, 1, 0 };   /* up right down left */
#define LEFT_OF(d)  (((d) + 3) & 3)
#define RIGHT_OF(d) (((d) + 1) & 3)
#define BACK_OF(d)  (((d) + 2) & 3)

/*
 * The gardens: '#' hedge, '.' ember, 'o' glow-bloom, '=' den gate, 'D' den,
 * 'P' Wick's start, 'M' the moth's start (above the gate). Each one is left-
 * right symmetric, closed, connected, with no dead ends and no 2x2 open
 * squares (checked at start; the self-test fails otherwise).
 */
static const struct { const char *name; const char *rows[MH]; } mazes[NMAZE] = {
	{ "KNOT GARDEN", {
		"###################",
		"#........#........#",
		"#.##.###.#.###.##.#",
		"#o##.###.#.###.##o#",
		"#.................#",
		"#.##.#.#####.#.##.#",
		"#....#...#...#....#",
		"####.###.#.###.####",
		"####.#...M...#.####",
		"####.#.##=##.#.####",
		"#......#DDD#......#",
		"#.##.#.#####.#.##.#",
		"#.##.#.......#.##.#",
		"#....#.#####.#....#",
		"###.##.#####.##.###",
		"#.................#",
		"#.###.##.#.##.###.#",
		"#..##....P....##..#",
		"##.##.##.#.##.##.##",
		"#...#.#.....#.#...#",
		"#.#.#.#.###.#.#.#.#",
		"#.#...#.....#...#.#",
		"#.#.#####.#####.#.#",
		"#o..#.........#..o#",
		"#.#.#.##.#.##.#.#.#",
		"#........#........#",
		"###################",
	} },
	{ "YEW WALK", {
		"###################",
		"#....#.......#....#",
		"#.##.#.##.##.#.##.#",
		"#.##...##.##...##.#",
		"#o##.#.##.##.#.##o#",
		"#.................#",
		"###.##.#.#.#.##.###",
		"#...##.#...#.##...#",
		"#.#....#.#.#....#.#",
		"#.#.##.#.#.#.##.#.#",
		"#.#.##...M...##.#.#",
		"#...##.##=##.##...#",
		"###.##.#DDD#.##.###",
		"#......#####......#",
		"#.##.#.......#.##.#",
		"#.##.#.#####.#.##.#",
		"#....#.#####.#....#",
		"##.#.....P.....#.##",
		"#..#.###.#.###.#..#",
		"#.##...#.#.#...##.#",
		"#.##.#.#...#.#.##.#",
		"#....#.#.#.#.#....#",
		"#o##.#...#...#.##o#",
		"#.##.#.#.#.#.#.##.#",
		"#.##.#.#.#.#.#.##.#",
		"#.................#",
		"###################",
	} },
	{ "MOON CLOISTER", {
		"###################",
		"#.................#",
		"#.###.#######.###.#",
		"#o###.#######.###o#",
		"#.###.#######.###.#",
		"#.................#",
		"#.#######.#######.#",
		"#.#######.#######.#",
		"#.#######.#######.#",
		"#........M........#",
		"#.###.###=###.###.#",
		"#.###.##DDD##.###.#",
		"#.###.#######.###.#",
		"#.................#",
		"#####.###.###.#####",
		"#####.###.###.#####",
		"#####.###.###.#####",
		"#........P........#",
		"#.###.###.###.###.#",
		"#.###.###.###.###.#",
		"#.###.###.###.###.#",
		"#.###.........###.#",
		"#.###.#######.###.#",
		"#o###.#######.###o#",
		"#.###.#######.###.#",
		"#.................#",
		"###################",
	} },
};

enum { S_TITLE, S_READY, S_PLAY, S_DYING, S_CLEAR, S_OVER };
enum { CS_DEN, CS_LEAVE, CS_OUT, CS_POOF };

typedef struct { int st, x, y, dir, dz, t, rel, anim, moved; } Crit;
typedef struct { int on, t, x, y; char txt[24]; } Popup;

static struct {
	int x, y, dir, q, q_t, waiting, alive, anim, streak, streak_t;
} hero;
static Crit cr[NCRIT];
static Popup pops[6];
static char grid[MH][MW];
static uint8_t item[MH][MW];
static uint8_t bg[CH][CW];
static int embers_left, maze_i, start_tx, start_ty, gate_x, gate_y;
static int state, state_t, paused, life_t, dz_t, caught_n, level;
static long score, hi, next_extra;
static int lives, hoard_x, hoard_y, hoard_dirty = 1, new_hi;
static int trail_x[TRAIL], trail_y[TRAIL], trail_n;
static const char *hi_path = "/run/hedgewick.hi";
static int from_picker, swap_lr, fps = HZ, exit_now, start_level = 1;
static uint8_t remap_dazzle[256], remap_out[256], remap_grey[256];

/* input of one tick */
static int in_turn[3], in_chord, in_home_press, in_home_release, in_esc, in_pause_kb, in_sound_kb, in_alt;
static long vol_t[2] = { -100, -100 };
static int q_prev;
static long q_prev_tick = -100;
static int qk_t, qk_latch, bot_home, sound_toggles;
static int pilot = 1;                  /* the self-test's autopilot */
static int t_want = -1, t_want_t;      /* touch: the way swiped to, absolute, and how long it waits */
static int t_corner, tg_start, tg_pause; /* touch: corner held last tick; a tap to start/resume; to pause */
static long st_tswipes, st_ttaps;

/* statistics (log, self-test) */
static long st_games, st_maxlevel, st_embers, st_caught, st_deaths, st_quits, st_bars;
static long st_pauses, st_uturns, st_turns, st_blooms, st_clears, st_bad, st_notices;
static int64_t ft_render, ft_present, ft_rmax, ft_pmax;
static long ft_n, ft_bytes;

static uint32_t rs = 2463534242u;
static uint32_t rnd(void) { rs ^= rs << 13; rs ^= rs >> 17; rs ^= rs << 5; return rs; }

/* The program's clock in ms: real time, or the self-test's ticks. */
static int64_t clock_ms(void) { return bot ? (int64_t)tick * 1000 / HZ : now_ns() / 1000000; }

static void bad(const char *fmt, ...) __attribute__((format(printf, 1, 2)));
static void bad(const char *fmt, ...)
{
	va_list ap;
	fprintf(stderr, "BAD (tick %ld): ", tick);
	va_start(ap, fmt);
	vfprintf(stderr, fmt, ap);
	va_end(ap);
	fputc('\n', stderr);
	st_bad++;
}

/* ------------------------------------------------------------------ sound */
/* All original. Everything stays above ~300 Hz: the speaker is tiny. */

static void sfx_ember(int streak)
{
	/* a pentatonic run that climbs while Wick keeps gathering */
	static const float sc[10] = { 1047, 1175, 1319, 1568, 1760, 2093, 2349, 2637, 3136, 3520 };
	tnote(sc[streak % 10], 0.0f, 45.0f, 0.10f);
}
static void sfx_bloom(void)
{
	static const float f[5] = { 1319, 1568, 1976, 2349, 2637 };
	for (int i = 0; i < 5; i++) tnote(f[i], 45.0f * (float)i, 160.0f, 0.13f);
	voice(W_NOISE, 12000.0f, 6000.0f, 260.0f, 0.04f, 0.0f, 2, 0.0f);
}
static void sfx_catch(int n)
{
	voice(W_NOISE, 9000.0f, 2500.0f, 140.0f, 0.12f, 0.0f, 2, 0.0f);
	voice(W_SQUARE, 600.0f + 200.0f * (float)n, 1800.0f + 400.0f * (float)n, 160.0f, 0.12f, 0.25f, 1, 20.0f);
}
static void sfx_death(void)
{
	/* the lantern gutters out */
	voice(W_NOISE, 5000.0f, 400.0f, 1000.0f, 0.2f, 0.0f, 2, 0.0f);
	voice(W_TRI, 988.0f, 247.0f, 1100.0f, 0.2f, 0.5f, 1, 0.0f);
	voice(W_SQUARE, 740.0f, 330.0f, 600.0f, 0.07f, 0.125f, 1, 250.0f);
}
static void sfx_start(void)
{
	static const struct { float f, at, ms; } m[] = {
		{ 784, 0, 110 }, { 988, 130, 110 }, { 1175, 260, 110 }, { 988, 390, 110 },
		{ 1319, 520, 200 }, { 1175, 780, 380 },
	}, b[] = { { 392, 0, 500 }, { 330, 520, 640 } };
	for (size_t i = 0; i < sizeof m / sizeof m[0]; i++) note(m[i].f, m[i].at, m[i].ms, 0.13f, 0.25f);
	for (size_t i = 0; i < sizeof b / sizeof b[0]; i++) tnote(b[i].f, b[i].at, b[i].ms, 0.16f);
}
static void sfx_clear(void)
{
	static const float f[8] = { 1047, 1175, 1319, 1568, 1760, 2093, 2349, 2637 };
	for (int i = 0; i < 8; i++) tnote(f[i], 55.0f * (float)i, i == 7 ? 420.0f : 90.0f, 0.13f);
}
static void sfx_extra(void)
{
	tnote(1568, 0, 120, 0.14f); tnote(2093, 120, 120, 0.14f); tnote(2637, 240, 360, 0.14f);
}
static void sfx_gameover(void)
{
	static const float f[5] = { 880, 740, 659, 554, 440 };
	for (int i = 0; i < 5; i++) tnote(f[i], 210.0f * (float)i, i == 4 ? 700.0f : 200.0f, 0.15f);
}
static void sfx_title(void)
{
	static const struct { float f, at, ms; } m[] = {
		{ 659, 0, 150 }, { 784, 160, 150 }, { 988, 320, 300 }, { 880, 640, 150 },
		{ 784, 800, 150 }, { 1175, 960, 460 },
	};
	for (size_t i = 0; i < sizeof m / sizeof m[0]; i++) tnote(m[i].f, m[i].at, m[i].ms, 0.15f);
	tnote(330, 0, 620, 0.1f); tnote(392, 640, 780, 0.1f);
}
static void sfx_queue(void) { tnote(1397.0f, 0.0f, 25.0f, 0.07f); }
static void sfx_uturn(void) { voice(W_NOISE, 4000.0f, 1800.0f, 70.0f, 0.06f, 0.0f, 2, 0.0f); }
static void sfx_glow(int k) { tnote(k & 1 ? 1568.0f : 1760.0f, 0.0f, 60.0f, 0.045f); }
static void sfx_pause(int on) { tnote(on ? 1175.0f : 880.0f, 0, 70, 0.1f); tnote(on ? 880.0f : 1175.0f, 80, 110, 0.1f); }

static void snd_toggle(void)
{
	sound_toggles++;
	switch (snd_state) {
	case SND_ON: snd_close(); snd_state = SND_OFF; logmsg("sound: off"); break;
	case SND_SEARCH: snd_state = SND_OFF; break;
	case SND_OFF: case SND_NONE: logmsg("sound: on requested"); snd_start_search(); break;
	default: break;
	}
}

static const char *snd_status(void)
{
	switch (snd_state) {
	case SND_ON:     return "SOUND ON (VOL+ & VOL-: OFF)";
	case SND_OFF:    return "SOUND OFF (VOL+ & VOL-: ON)";
	case SND_SEARCH: return "LOOKING FOR THE SPEAKER";
	case SND_NONE:   return "NO SPEAKER (VOL+&VOL-: RETRY)";
	default:         return "SOUND ERROR (SEE THE LOG)";
	}
}

/* ------------------------------------------------------------------- maze */

static int centre(int t) { return t * TU + TC; }
static int open_t(int x, int y) { return x >= 0 && y >= 0 && x < MW && y < MH && grid[y][x] == ' '; }
static int open_dir(int x, int y, int d) { return open_t(x + DX[d], y + DY[d]); }
static int exits(int x, int y)
{
	int n = 0;
	for (int d = 0; d < 4; d++) n += open_dir(x, y, d);
	return n;
}

/* Check every garden: closed, connected, no dead ends, no 2x2 open squares,
 * a den with its gate and the moth's start. Returns the number of faults. */
static int check_mazes(void)
{
	int faults = 0;
	for (int m = 0; m < NMAZE; m++) {
		static uint8_t seen[MH][MW];
		int n = 0, reach = 0, sx = -1, sy = -1, stk[MH * MW], sp = 0, gx = -1, gy = -1;
#define ISP(x, y) ((x) >= 0 && (y) >= 0 && (x) < MW && (y) < MH && strchr(".o PM", mazes[m].rows[y][x]))
		for (int y = 0; y < MH; y++) {
			if ((int)strlen(mazes[m].rows[y]) != MW) { logmsg("maze %d row %d: wrong width", m, y); faults++; continue; }
			for (int x = 0; x < MW; x++) {
				char c = mazes[m].rows[y][x];
				if (c == '=') { gx = x; gy = y; }
				if ((x == 0 || y == 0 || x == MW - 1 || y == MH - 1) && c != '#') { logmsg("maze %d: open border at %d,%d", m, x, y); faults++; }
				if (!ISP(x, y)) continue;
				n++;
				if (sx < 0) { sx = x; sy = y; }
				if (ISP(x + 1, y) + ISP(x - 1, y) + ISP(x, y + 1) + ISP(x, y - 1) < 2) { logmsg("maze %d: dead end at %d,%d", m, x, y); faults++; }
				if (ISP(x + 1, y) && ISP(x, y + 1) && ISP(x + 1, y + 1)) { logmsg("maze %d: open square at %d,%d", m, x, y); faults++; }
				if (c != mazes[m].rows[y][MW - 1 - x] && !(c == 'o' || mazes[m].rows[y][MW - 1 - x] == 'o')) {
					logmsg("maze %d: not symmetric at %d,%d", m, x, y);
					faults++;
				}
			}
		}
		if (gx < 0 || mazes[m].rows[gy - 1][gx] != 'M' || mazes[m].rows[gy + 1][gx] != 'D' ||
		    mazes[m].rows[gy + 1][gx - 1] != 'D' || mazes[m].rows[gy + 1][gx + 1] != 'D') {
			logmsg("maze %d: den shape", m);
			faults++;
		}
		memset(seen, 0, sizeof seen);
		if (sx >= 0) { seen[sy][sx] = 1; stk[sp++] = sy * MW + sx; }
		while (sp) {
			int p = stk[--sp], x = p % MW, y = p / MW;
			reach++;
			for (int d = 0; d < 4; d++) {
				int nx = x + DX[d], ny = y + DY[d];
				if (ISP(nx, ny) && !seen[ny][nx]) { seen[ny][nx] = 1; stk[sp++] = ny * MW + nx; }
			}
		}
#undef ISP
		if (reach != n) { logmsg("maze %d: %d of %d path tiles unreachable", m, n - reach, n); faults++; }
	}
	return faults;
}

/* --------------------------------------------------------------- drawing */

static uint32_t hash3(int a, int b, int c)
{
	uint32_t h = (uint32_t)a * 73856093u ^ (uint32_t)b * 19349663u ^ (uint32_t)c * 83492791u;
	h ^= h >> 13; h *= 0x5bd1e995u; h ^= h >> 15;
	return h;
}

static void bg_px(int x, int y, int c)
{
	if ((unsigned)x < CW && (unsigned)y < CH) bg[y][x] = (uint8_t)c;
}

static void ember_at(uint8_t (*dst)[CW], int px, int py, int bright)
{
	dst[py + 4][px + 4] = (uint8_t)pc(bright ? 'y' : 'Y');
	dst[py + 3][px + 4] = dst[py + 5][px + 4] = (uint8_t)pc(bright ? 'Y' : 'O');
	dst[py + 4][px + 3] = dst[py + 4][px + 5] = (uint8_t)pc(bright ? 'Y' : 'O');
}

static int hedge(int x, int y)
{
	return x < 0 || y < 0 || x >= MW || y >= MH || grid[y][x] == '#';
}

static void draw_tile_bg(int tx, int ty)
{
	int px0 = MX0 + tx * TS, py0 = MY0 + ty * TS;
	char g = grid[ty][tx];
	for (int j = 0; j < TS; j++)
		for (int i = 0; i < TS; i++) {
			uint32_t h = hash3(tx * TS + i, ty * TS + j, maze_i);
			int c;
			if (g == '#') {
				int up = !hedge(tx, ty - 1), dn = !hedge(tx, ty + 1), lf = !hedge(tx - 1, ty), rt = !hedge(tx + 1, ty);
				c = pc(h % 6 == 0 ? 'G' : h % 13 == 0 ? 'L' : 'g');
				if (up && j == 0) c = pc(h % 3 ? 'L' : 'l');
				else if (up && j == 1 && h % 4 == 0) c = pc('L');
				if (lf && i == 0) c = pc('L');
				if (rt && i == TS - 1) c = pc('G');
				if (dn && j == TS - 1) c = pc('G');
				/* rounded corners where two path sides meet */
				if ((up && lf && i == 0 && j == 0) || (up && rt && i == TS - 1 && j == 0) ||
				    (dn && lf && i == 0 && j == TS - 1) || (dn && rt && i == TS - 1 && j == TS - 1))
					c = pc('N');
			} else if (g == 'D') {
				c = pc(h % 9 == 0 ? 'a' : 'D');
			} else {
				c = pc('N');
				if (g == '=' && (j == 3 || j == 5)) c = pc('d');
				if (g == '=' && (i == 0 || i == TS - 1) && j >= 2 && j <= 6) c = pc('a');
			}
			bg_px(px0 + i, py0 + j, c);
		}
	if (item[ty][tx] == 1) ember_at(bg, px0, py0, 0);
}

static void draw_bg(void)
{
	memset(bg, pc('K'), sizeof bg);
	for (int y = 0; y < MH; y++)
		for (int x = 0; x < MW; x++) draw_tile_bg(x, y);
}

static void arrow(int cx, int cy, int d, int c)
{
	static const char *up[5] = { "..#..", ".###.", "#.#.#", "..#..", "..#.." };
	for (int j = 0; j < 5; j++)
		for (int i = 0; i < 5; i++) {
			int u = i - 2, v = j - 2, ru, rv;
			if (up[j][i] != '#') continue;
			switch (d) {
			case 0: ru = u; rv = v; break;
			case 1: ru = -v; rv = u; break;
			case 2: ru = -u; rv = -v; break;
			default: ru = v; rv = -u; break;
			}
			cv_px(cx + ru, cy + rv, c);
		}
}

static const char arrow_ch[4] = { '\3', '\2', '\4', '\1' };

/* ----------------------------------------------------------------- state */

static int dazzle_len(void) { int t = 8 * HZ - (level - 1) * HZ * 4 / 5; return t < 5 * HZ / 2 ? 5 * HZ / 2 : t; }
static int hero_speed(void) { return 24 + (level - 1 < 6 ? level - 1 : 6) + (dz_t ? 2 : 0); }
static int crit_base(void) { int s = 19 + (level - 1) * 3 / 2; return s > 31 ? 31 : s; }

static int lull(void)
{
	int len = 5 * HZ - (level - 1) * HZ / 2;
	if (life_t < 4 * HZ) return 1;
	if (level >= 9 || len < HZ) return 0;
	return life_t > 20 * HZ && life_t % (20 * HZ) < len;
}

static int tile_of(int v) { return v / TU; }

static int owl_swoop(void)
{
	const Crit *c = &cr[K_OWL];
	return c->st == CS_OUT && !c->dz &&
	       abs(tile_of(c->x) - tile_of(hero.x)) + abs(tile_of(c->y) - tile_of(hero.y)) <= 6;
}

static int crit_speed(int k)
{
	const Crit *c = &cr[k];
	int b = crit_base();
	if (c->st == CS_LEAVE) return 10;
	if (c->dz) return 11;
	switch (k) {
	case K_TOAD: return (life_t + 7) % 24 < 16 ? b * 3 / 2 : 0;
	case K_BAT: return b + 1;
	case K_OWL: return owl_swoop() ? b + 3 : b - 1;
	default: return b;
	}
}

/* The quarter of the garden with the most embers, and their middle. */
static void update_hoard(void)
{
	int cnt[4] = { 0 }, sx[4] = { 0 }, sy[4] = { 0 }, best = 0;
	for (int y = 0; y < MH; y++)
		for (int x = 0; x < MW; x++)
			if (item[y][x] == 1) {
				int q = (x > MW / 2) + 2 * (y > MH / 2);
				cnt[q]++; sx[q] += x; sy[q] += y;
			}
	for (int q = 1; q < 4; q++) if (cnt[q] > cnt[best]) best = q;
	if (cnt[best]) { hoard_x = sx[best] / cnt[best]; hoard_y = sy[best] / cnt[best]; }
	else { hoard_x = tile_of(hero.x); hoard_y = tile_of(hero.y); }
	hoard_dirty = 0;
}

static void crit_target(int k, int *gx, int *gy)
{
	int hx = tile_of(hero.x), hy = tile_of(hero.y);
	*gx = hx; *gy = hy;
	switch (k) {
	case K_TOAD: {
		/* the next crossing on Wick's way */
		int x = hx, y = hy, d = hero.dir;
		for (int i = 0; i < 8; i++) {
			if (!open_dir(x, y, d)) break;
			x += DX[d]; y += DY[d];
			if (exits(x, y) >= 3) break;
		}
		*gx = x; *gy = y;
		break;
	}
	case K_BAT: {
		int back = trail_n < 75 ? trail_n - 1 : 75;
		int i = ((trail_n - 1 - back) % TRAIL + TRAIL) % TRAIL;
		if (trail_n > 0) { *gx = trail_x[i]; *gy = trail_y[i]; }
		break;
	}
	case K_OWL:
		if (!owl_swoop()) {
			if (hoard_dirty) update_hoard();
			*gx = hoard_x; *gy = hoard_y;
		}
		break;
	default:
		break;
	}
}

static int d2(int ax, int ay, int bx, int by) { return (ax - bx) * (ax - bx) + (ay - by) * (ay - by); }

static void add_score(long v)
{
	score += v;
	if (score > 999999) score = 999999;
	if (score >= next_extra) {
		if (lives < 6) lives++;
		next_extra += 30000;
		sfx_extra();
	}
}

static void popup(int x, int y, long v)
{
	for (int i = 0; i < (int)(sizeof pops / sizeof pops[0]); i++)
		if (!pops[i].on) {
			pops[i].on = 1; pops[i].t = 0; pops[i].x = x; pops[i].y = y;
			snprintf(pops[i].txt, sizeof pops[i].txt, "%ld", v);
			return;
		}
}

static void start_dazzle(void)
{
	dz_t = dazzle_len();
	caught_n = 0;
	for (int k = 0; k < NCRIT; k++) {
		Crit *c = &cr[k];
		if (c->st != CS_OUT && c->st != CS_LEAVE) continue;
		c->dz = 1;
		if (c->st == CS_OUT) c->dir = BACK_OF(c->dir);
	}
}

static void set_state(int s) { state = s; state_t = 0; }

static void eat(int tx, int ty)
{
	if (item[ty][tx] == 1) {
		item[ty][tx] = 0;
		embers_left--;
		st_embers++;
		add_score(10);
		draw_tile_bg(tx, ty);
		sfx_ember(hero.streak++);
		hero.streak_t = 12;
		hoard_dirty = 1;
		if (embers_left == 0) {
			long bonus = 250L * level;
			add_score(bonus);
			popup(hero.x, hero.y, bonus);
			st_clears++;
			set_state(S_CLEAR);
			sfx_clear();
		}
	} else if (item[ty][tx] == 2) {
		item[ty][tx] = 0;
		st_blooms++;
		add_score(50);
		start_dazzle();
		sfx_bloom();
	}
}

static void hero_rel_dir(int *nd)
{
	*nd = hero.q == 1 ? LEFT_OF(hero.dir) : RIGHT_OF(hero.dir);
}

/* At a tile centre: where next. -1 waits (a T with no turn chosen). */
static int hero_decide(void)
{
	int tx = tile_of(hero.x), ty = tile_of(hero.y), d = hero.dir, l, r;
	eat(tx, ty);
	if (state != S_PLAY) return -1;
	if (hero.q) {
		int nd;
		hero_rel_dir(&nd);
		if (open_dir(tx, ty, nd)) { hero.q = 0; hero.waiting = 0; st_turns++; t_want = -1; return nd; }
	}
	if (open_dir(tx, ty, d)) { hero.waiting = 0; return d; }
	l = open_dir(tx, ty, LEFT_OF(d));
	r = open_dir(tx, ty, RIGHT_OF(d));
	if (l && !r) { hero.waiting = 0; return LEFT_OF(d); }
	if (r && !l) { hero.waiting = 0; return RIGHT_OF(d); }
	if (!l && !r) return BACK_OF(d);
	hero.waiting = 1;
	return -1;
}

static int crit_decide(int k)
{
	Crit *c = &cr[k];
	int tx = tile_of(c->x), ty = tile_of(c->y), d = c->dir, opts[4], n = 0, best = -1, bv = 0, gx, gy;
	for (int nd = 0; nd < 4; nd++)
		if (nd != BACK_OF(d) && open_dir(tx, ty, nd)) opts[n++] = nd;
	if (n == 0) return open_dir(tx, ty, BACK_OF(d)) ? BACK_OF(d) : -1;
	if (n == 1) return opts[0];
	if (c->dz) {
		int hx = tile_of(hero.x), hy = tile_of(hero.y);
		if (rnd() % 5 == 0) return opts[rnd() % (unsigned)n];
		for (int i = 0; i < n; i++) {
			int v = d2(tx + DX[opts[i]], ty + DY[opts[i]], hx, hy);
			if (best < 0 || v > bv) { best = opts[i]; bv = v; }
		}
		return best;
	}
	if (lull() || (k == K_MOTH && rnd() % 4 == 0)) return opts[rnd() % (unsigned)n];
	crit_target(k, &gx, &gy);
	for (int i = 0; i < n; i++) {
		int v = d2(tx + DX[opts[i]], ty + DY[opts[i]], gx, gy);
		if (best < 0 || v < bv) { best = opts[i]; bv = v; }
	}
	return best;
}

static int decide(int who) { return who < 0 ? hero_decide() : crit_decide(who); }

/* Move up to spd sub-pixels along the grid, asking decide() at each centre. */
static int advance(int *x, int *y, int *dir, int spd, int who)
{
	int moved = 0;
	for (int guard = 0; spd > 0 && guard < 8; guard++) {
		int p, off, dist, step;
		if ((*x - TC) % TU == 0 && (*y - TC) % TU == 0) {
			int nd = decide(who);
			if (nd < 0) break;
			*dir = nd;
		}
		p = DX[*dir] ? *x : *y;
		off = ((p - TC) % TU + TU) % TU;
		dist = DX[*dir] + DY[*dir] > 0 ? TU - off : (off ? off : TU);
		step = spd < dist ? spd : dist;
		*x += DX[*dir] * step;
		*y += DY[*dir] * step;
		spd -= step;
		moved += step;
	}
	return moved;
}

static void crit_home(int k)
{
	Crit *c = &cr[k];
	int sx = k == K_TOAD ? gate_x - 1 : k == K_OWL ? gate_x + 1 : gate_x;
	c->st = CS_DEN;
	c->x = centre(sx);
	c->y = centre(gate_y + 1);
	c->dir = 0;
	c->dz = 0;
}

static void reset_actors(void)
{
	static const int rel_s[NCRIT] = { 0, 2, 5, 9 };
	int f = 10 - (level - 1 < 6 ? level - 1 : 6);   /* tenths */
	hero.x = centre(start_tx); hero.y = centre(start_ty);
	hero.dir = 3; hero.q = 0; hero.waiting = 0; hero.alive = 1; hero.anim = 0;
	for (int k = 0; k < NCRIT; k++) {
		crit_home(k);
		cr[k].rel = rel_s[k] * HZ * f / 10;
		cr[k].anim = (int)(rnd() % 16);
	}
	cr[K_MOTH].st = CS_OUT;
	cr[K_MOTH].x = centre(gate_x);
	cr[K_MOTH].y = centre(gate_y - 1);
	cr[K_MOTH].dir = 3;
	dz_t = 0;
	life_t = 0;
	trail_n = 0;
	memset(pops, 0, sizeof pops);
}

static void load_maze(void)
{
	maze_i = (level - 1) % NMAZE;
	embers_left = 0;
	for (int y = 0; y < MH; y++)
		for (int x = 0; x < MW; x++) {
			char c = mazes[maze_i].rows[y][x];
			item[y][x] = 0;
			grid[y][x] = ' ';
			switch (c) {
			case '#': grid[y][x] = '#'; break;
			case '.': item[y][x] = 1; embers_left++; break;
			case 'o': item[y][x] = 2; break;
			case '=': grid[y][x] = '='; gate_x = x; gate_y = y; break;
			case 'D': grid[y][x] = 'D'; break;
			case 'P': start_tx = x; start_ty = y; break;
			default: break;
			}
		}
	hoard_dirty = 1;
	draw_bg();
}

static void start_level_now(void)
{
	if (level > st_maxlevel) st_maxlevel = level;
	load_maze();
	reset_actors();
	set_state(S_READY);
	sfx_start();
}

static void start_game(void)
{
	st_games++;
	score = 0;
	new_hi = 0;
	lives = 3;
	next_extra = 10000;
	level = start_level;
	paused = 0;
	start_level_now();
}

static void save_hi(void)
{
	FILE *f;
	if (score <= hi) return;
	hi = score;
	new_hi = 1;
	f = hi_path ? fopen(hi_path, "we") : NULL;
	if (!f) return;
	fprintf(f, "%ld\n", hi);
	fclose(f);
}

static int bot_hold_kind(void);

static void to_title(void)
{
	set_state(S_TITLE);
	paused = 0;
	sfx_title();
}

static void quit_to_title(void)
{
	if (bot && pilot && !in_esc && bot_hold_kind() != 3) bad("quit on a hold of kind %d", bot_hold_kind());
	save_hi();
	st_quits++;
	to_title();
}

static void game_over(void)
{
	save_hi();
	set_state(S_OVER);
	sfx_gameover();
}

static void uturn(void)
{
	int tx = tile_of(hero.x), ty = tile_of(hero.y);
	int atc = (hero.x - TC) % TU == 0 && (hero.y - TC) % TU == 0;
	if (atc && !open_dir(tx, ty, BACK_OF(hero.dir))) return;
	hero.dir = BACK_OF(hero.dir);
	hero.q = 0;
	hero.waiting = 0;
	st_uturns++;
	sfx_uturn();
}

static void toggle_pause(void)
{
	paused = !paused;
	if (paused) st_pauses++;
	sfx_pause(paused);
}

/* --------------------------------------------------------------- per tick */

static int held_home(void)
{
	return bot_home || touch_corner_down() ||
	       (have_home ? keydown[KEY_HOMEPAGE] || keydown[KEY_HOME] : keydown[KEY_POWER]);
}

static int vol_down(void)
{
	return keydown[KEY_VOLUMEUP] || keydown[KEY_VOLUMEDOWN] || keydown[KEY_LEFT] || keydown[KEY_RIGHT];
}

/*
 * The Home hold, as in VOIDMOTHS: held, it counts; at HOLD_SHOW a bar comes
 * up, at HOLD_QUIT the game goes to the title (from the title, back to the
 * arcade when started by it). A volume button starts the count over, so a
 * turn during a hold never quits. A key held across a quit (or into the game
 * from the arcade) counts only after it has been let go. Returns 1 for a tap:
 * let go before the bar.
 */
static int hold_update(void)
{
	int held = held_home(), tap = 0, counts = state != S_TITLE || from_picker;
	if (in_home_release && !qk_latch && qk_t < HOLD_SHOW) tap = 1;
	if (in_home_release || !held) qk_latch = 0;
	if (!held || qk_latch || !counts) {
		qk_t = 0;
	} else {
		if (in_home_release) qk_t = 0;
		if (in_turn[1] || in_turn[2] || in_chord || vol_down()) qk_t = 0;
		else qk_t++;
		if (qk_t == HOLD_SHOW) st_bars++;
		if (qk_t >= HOLD_QUIT) {
			qk_t = 0;
			qk_latch = 1;
			if (state == S_TITLE) {
				if (bot && pilot && bot_hold_kind() != 3) bad("left for the arcade on a hold of kind %d", bot_hold_kind());
				exit_now = 1;
			} else {
				quit_to_title();
			}
		}
	}
	return tap;
}

static void tick_crits(void)
{
	for (int k = 0; k < NCRIT; k++) {
		Crit *c = &cr[k];
		int spd = crit_speed(k);
		c->anim++;
		c->moved = 0;
		switch (c->st) {
		case CS_DEN:
			if (life_t >= c->rel) c->st = CS_LEAVE;
			break;
		case CS_LEAVE: {
			int gx = centre(gate_x), ey = centre(gate_y - 1);
			if (c->x != gx) {
				int s = abs(gx - c->x) < spd ? abs(gx - c->x) : spd;
				c->x += c->x < gx ? s : -s;
			} else {
				c->y -= spd;
				if (c->y <= ey) {
					int l = open_dir(gate_x, gate_y - 1, 3), r = open_dir(gate_x, gate_y - 1, 1);
					c->y = ey;
					c->st = CS_OUT;
					c->dir = l && (!r || rnd() & 1) ? 3 : 1;
				}
			}
			c->moved = 1;
			break;
		}
		case CS_OUT:
			c->moved = advance(&c->x, &c->y, &c->dir, spd, k) > 0;
			break;
		case CS_POOF:
			if (--c->t <= 0) {
				crit_home(k);
				c->rel = life_t + 2 * HZ;
			}
			break;
		}
	}
}

static void collide(void)
{
	for (int k = 0; k < NCRIT && hero.alive; k++) {
		Crit *c = &cr[k];
		if (c->st != CS_OUT && c->st != CS_LEAVE) continue;
		if (abs(c->x - hero.x) + abs(c->y - hero.y) >= 7 * SUB) continue;
		if (c->dz) {
			long pts = 200L * ++caught_n;
			add_score(pts);
			popup(c->x, c->y, pts);
			st_caught++;
			c->st = CS_POOF;
			c->t = 15;
			c->dz = 0;
			sfx_catch(caught_n);
		} else {
			hero.alive = 0;
			st_deaths++;
			set_state(S_DYING);
			sfx_death();
		}
	}
}

static void tick_play(void)
{
	life_t++;
	if (hero.q && --hero.q_t <= 0) hero.q = 0;
	if (hero.streak_t && --hero.streak_t == 0) hero.streak = 0;
	if (advance(&hero.x, &hero.y, &hero.dir, hero_speed(), -1) > 0) hero.anim++;
	if (state != S_PLAY) return;     /* the last ember */
	trail_x[trail_n % TRAIL] = tile_of(hero.x);
	trail_y[trail_n % TRAIL] = tile_of(hero.y);
	trail_n++;
	collide();
	if (state != S_PLAY) return;
	tick_crits();
	collide();
	if (dz_t > 0) {
		if (dz_t % 15 == 0) sfx_glow(dz_t / 15);
		if (--dz_t == 0)
			for (int k = 0; k < NCRIT; k++) cr[k].dz = 0;
	}
}

/*
 * The touchscreen, once a tick: the corner as the Home button (press and
 * release edges, so tap and hold work as with the key), swipes as absolute
 * directions (turned into Wick's left/right each tick, so a bend in between
 * does not change where he goes), taps to start, resume or pause.
 */
static void touch_tick(void)
{
	TGesture g;
	int c;
	touch_view(fb.w, fb.h, fb.ox, fb.oy, fb.scale, CW, CH, CNR_W, CNR_H);
	touch_poll(bot ? (int64_t)tick * 1000 / HZ : now_ns() / 1000000, grab);
	c = touch_corner_down();
	if (c && !t_corner) in_home_press++;
	if (!c && t_corner) in_home_release++;
	t_corner = c;
	while (touch_next(&g)) {
		if (g.kind == TG_TAP) {
			st_ttaps++;
			if (state == S_PLAY && !paused && g.y < MY0) tg_pause++;
			else tg_start++;
		} else if (g.kind == TG_SWIPE && hero.alive && (state == S_PLAY || state == S_READY) && !paused) {
			st_tswipes++;
			if (g.dir == BACK_OF(hero.dir) && state == S_PLAY) { t_want = -1; hero.q = 0; uturn(); }
			else if (g.dir != hero.dir || hero.waiting) {
				t_want = g.dir;
				t_want_t = TURN_KEEP;
				sfx_queue();
			}
		}
	}
	/* the wish, relative to where Wick faces now */
	if (t_want >= 0 && hero.alive && (state == S_PLAY || state == S_READY)) {
		if (hero.dir == t_want || --t_want_t <= 0) { t_want = -1; hero.q = 0; }
		else if (t_want == LEFT_OF(hero.dir)) { hero.q = 1; hero.q_t = TURN_KEEP; }
		else if (t_want == RIGHT_OF(hero.dir)) { hero.q = 2; hero.q_t = TURN_KEEP; }
		else hero.q = 0;
	} else if (state != S_PLAY && state != S_READY) t_want = -1;
}

static void game_tick(void)
{
	int tap;
	state_t++;
	touch_tick();
	if (notice_poll()) st_notices++;
	for (int i = 0; i < (int)(sizeof pops / sizeof pops[0]); i++)
		if (pops[i].on && ++pops[i].t > 40) pops[i].on = 0;

	if (in_esc) {
		if (state == S_TITLE) { if (from_picker) exit_now = 1; }
		else quit_to_title();
	}
	tap = hold_update();
	if (in_chord) {
		/* the first button of the pair queued a turn a moment ago: take it back */
		if (tick - q_prev_tick <= 10) hero.q = q_prev;
		if (state == S_TITLE) snd_toggle();
		else if (state == S_PLAY || state == S_READY) toggle_pause();
	}
	if (in_pause_kb && (state == S_PLAY || state == S_READY)) toggle_pause();
	if (in_sound_kb && state == S_TITLE) snd_toggle();

	if (tg_pause && (state == S_PLAY || state == S_READY) && !paused) toggle_pause();
	else if (tg_start && paused) { toggle_pause(); tg_start = 0; }
	switch (state) {
	case S_TITLE:
		if (tap || in_alt || tg_start) start_game();
		break;
	case S_OVER:
		if (state_t > 45 && (tap || in_alt || tg_start)) start_game();
		else if (state_t > 20 * HZ) to_title();
		break;
	default:
		if (paused) {
			if (tap || in_alt) toggle_pause();
			break;
		}
		if (hero.alive && (state == S_PLAY || state == S_READY)) {
			for (int side = 1; side <= 2; side++)
				for (int n = 0; n < in_turn[side]; n++) {
					q_prev = hero.q;
					q_prev_tick = tick;
					hero.q = side;
					hero.q_t = TURN_KEEP;
					sfx_queue();
				}
			if (state == S_PLAY && (in_home_press || in_alt)) uturn();
		}
		switch (state) {
		case S_READY:
			if (state_t >= 75) set_state(S_PLAY);
			break;
		case S_PLAY:
			tick_play();
			break;
		case S_DYING:
			if (state_t >= 75) {
				if (--lives <= 0) game_over();
				else { reset_actors(); set_state(S_READY); }
			}
			break;
		case S_CLEAR:
			if (state_t >= 80) { level++; start_level_now(); }
			break;
		}
		break;
	}
	memset(in_turn, 0, sizeof in_turn);
	in_chord = in_home_press = in_home_release = in_esc = in_pause_kb = in_sound_kb = in_alt = 0;
	tg_start = tg_pause = 0;
}

/* ----------------------------------------------------------------- render */

static void decision_tile(int *x, int *y)
{
	int tx = tile_of(hero.x), ty = tile_of(hero.y), d = hero.dir;
	int along = DX[d] * (hero.x - centre(tx)) + DY[d] * (hero.y - centre(ty));
	if (along > 0 || (along == 0 && !hero.waiting)) { tx += DX[d]; ty += DY[d]; }
	*x = tx; *y = ty;
}

/* Where the queued turn will be taken, if that can be seen from here. */
static int turn_spot(int *sx, int *sy, int *sd)
{
	int x, y, d = hero.dir, nd;
	if (!hero.q || !hero.alive) return 0;
	decision_tile(&x, &y);
	if (hero.waiting) { x = tile_of(hero.x); y = tile_of(hero.y); }
	nd = hero.q == 1 ? LEFT_OF(d) : RIGHT_OF(d);
	for (int i = 0; i < 12 && open_t(x, y); i++) {
		if (open_dir(x, y, nd)) { *sx = x; *sy = y; *sd = nd; return 1; }
		if (!open_dir(x, y, d)) return 0;
		x += DX[d]; y += DY[d];
	}
	return 0;
}

static void draw_crit(int k)
{
	const Crit *c = &cr[k];
	int px = MX0 + c->x / SUB - 5, py = MY0 + c->y / SUB - 5, fr;
	const uint8_t *rm = NULL;
	if (c->st == CS_POOF) {
		int f = (15 - c->t) / 5;
		cv_sprite(px, py, art_poof[f > 2 ? 2 : f], SPR, 0, NULL, 1);
		return;
	}
	if (c->st == CS_DEN) py += (c->anim / 10) % 2 ? -1 : 1;
	switch (k) {
	case K_TOAD: fr = c->dz ? (c->anim / 8) % 2 : c->moved && c->st == CS_OUT; break;
	case K_OWL: fr = c->anim % 70 < 6; break;
	default: fr = (c->anim / 5) % 2; break;
	}
	if (c->dz && !(dz_t < 2 * HZ && (tick / 4) % 2)) rm = remap_dazzle;
	cv_sprite(px, py, art_crit[k][fr], SPR, 0, rm, 1);
}

static void draw_hero(void)
{
	int px = MX0 + hero.x / SUB - 5, py = MY0 + hero.y / SUB - 5;
	int d = hero.dir, fr = (hero.anim / 4) % 2;
	const uint8_t *rm = NULL;
	if (state == S_DYING) {
		if (state_t < 24) rm = (state_t / 3) % 2 ? remap_out : NULL;
		else if (state_t < 48) rm = remap_grey;
		else {
			int f = (state_t - 48) / 9;
			if (f <= 2) cv_sprite(px, py, art_poof[f], SPR, 0, remap_grey, 1);
			return;
		}
	}
	cv_sprite(px, py, art_wick[d == 3 ? 1 : d][fr], SPR, d == 3, rm, 1);
}

static void draw_hud(void)
{
	char b[48];
	snprintf(b, sizeof b, "SCORE %06ld", score);
	txt(2, 3, b, pc('W'), 1, -1);
	snprintf(b, sizeof b, "HI %06ld", score > hi ? score : hi);
	txt(CW - 2 - txt_w(b, 1), 3, b, pc('Y'), 1, -1);
	snprintf(b, sizeof b, "LEVEL %d", level);
	txt(2, 13, b, pc('B'), 1, -1);
	txt_c(CW / 2 + 4, 13, mazes[maze_i].name, pc('H'), 1, -1);
	for (int i = 0; i < lives - 1 && i < 5; i++) cv_sprite(CW - 8 - i * 7, 13, art_lamp, 6, 0, NULL, 1);

	/* bottom: the queued turn, the glow, the buttons */
	if (hero.alive && hero.waiting && state == S_PLAY && !paused) {
		if (touch_present() && !have_vol)
			snprintf(b, sizeof b, "WHICH WAY?  SWIPE %c OR %c", arrow_ch[LEFT_OF(hero.dir)], arrow_ch[RIGHT_OF(hero.dir)]);
		else
			snprintf(b, sizeof b, "WHICH WAY?  VOL+ %c  VOL- %c", arrow_ch[LEFT_OF(hero.dir)], arrow_ch[RIGHT_OF(hero.dir)]);
		txt(2, 270, b, pc((tick / 8) % 2 ? 'Y' : 'W'), 1, -1);
	} else if (hero.alive && hero.q && state != S_TITLE) {
		snprintf(b, sizeof b, "NEXT TURN: %s %c", hero.q == 1 ? "LEFT" : "RIGHT",
			 arrow_ch[hero.q == 1 ? LEFT_OF(hero.dir) : RIGHT_OF(hero.dir)]);
		txt(2, 270, b, pc('Y'), 1, -1);
	}
	if (dz_t > 0) {
		int w = (CW - 34) * dz_t / dazzle_len();
		txt(2, 281, "GLOW", pc('B'), 1, -1);
		cv_rect(30, 282, w, 5, pc(dz_t < 2 * HZ && (tick / 4) % 2 ? 'W' : 'B'));
	}
	if (touch_present()) {
		txt(2, 297, "SWIPE: GO THAT WAY \5 TAP TOP: PAUSE", pc('H'), 1, -1);
		txt(2, 308, have_vol ? "VOL+ LEFT \5 VOL- RIGHT \5 HOME U-TURN" : "HOLD TOP LEFT CORNER 2 S: TITLE", pc('H'), 1, -1);
	} else {
		txt(2, 297, "VOL+ LEFT \5 VOL- RIGHT", pc('H'), 1, -1);
		txt(2, 308, "HOME U-TURN \5 BOTH VOL: PAUSE", pc('H'), 1, -1);
	}
}

/* The touch corner, on the title and paused screens: a small lit wedge. */
static void draw_corner(void)
{
	int c = pc(t_corner ? 'Y' : (state_t / 15) % 2 ? 'B' : 'b');
	if (!touch_present()) return;
	for (int y = 0; y < 8; y++) cv_rect(0, y, 8 - y, 1, c);
}

static void draw_play(void)
{
	int sx, sy, sd;
	char b[48];
	memcpy(fb.cv, bg, sizeof bg);
	for (int i = 0; i < 4; i++) {
		uint32_t h = hash3((int)(tick / 6), i, 7);
		int x = (int)(h % MW), y = (int)((h / MW) % MH);
		if (item[y][x] == 1) ember_at((uint8_t (*)[CW])fb.cv, MX0 + x * TS, MY0 + y * TS, 1);
	}
	for (int y = 0; y < MH; y++)
		for (int x = 0; x < MW; x++)
			if (item[y][x] == 2) cv_sprite(MX0 + x * TS + 1, MY0 + y * TS + 1, art_bloom[(tick / 10) % 2], 7, 0, NULL, 1);
	if (state == S_CLEAR)
		cv_frame(MX0 - 2, MY0 - 2, MW * TS + 4, MH * TS + 4, pc((state_t / 5) % 2 ? 'Y' : 'O'));
	if (state != S_TITLE && turn_spot(&sx, &sy, &sd) && (tick / 6) % 3)
		arrow(MX0 + sx * TS + 4, MY0 + sy * TS + 4, sd, pc('Y'));
	for (int k = 0; k < NCRIT; k++) if (cr[k].st == CS_DEN) draw_crit(k);
	for (int k = 0; k < NCRIT; k++) if (cr[k].st != CS_DEN) draw_crit(k);
	if (state != S_CLEAR || (state_t / 4) % 2) draw_hero();
	for (int i = 0; i < (int)(sizeof pops / sizeof pops[0]); i++)
		if (pops[i].on)
			txt(MX0 + pops[i].x / SUB - txt_w(pops[i].txt, 1) / 2, MY0 + pops[i].y / SUB - 12 - pops[i].t / 5,
			    pops[i].txt, pc(pops[i].t % 8 < 4 ? 'W' : 'y'), 1, pc('K'));
	draw_hud();

	if (state == S_READY) {
		int y = MY0 + (gate_y + 3) * TS - 1;
		cv_box(36, y, CW - 72, 23, pc('n'), pc('Y'));
		txt_c(CW / 2, y + 4, mazes[maze_i].name, pc('Y'), 1, -1);
		snprintf(b, sizeof b, "LEVEL %d", level);
		txt_c(CW / 2, y + 13, b, pc('W'), 1, -1);
	} else if (state == S_CLEAR) {
		int y = MY0 + (gate_y + 3) * TS;
		txt_c(CW / 2, y, "GARDEN LIT!", pc('Y'), 2, pc('K'));
	} else if (state == S_OVER) {
		cv_box(14, 96, CW - 28, 86, pc('n'), pc('R'));
		txt_c(CW / 2, 104, "GAME OVER", pc('R'), 2, pc('K'));
		snprintf(b, sizeof b, "SCORE %06ld", score);
		txt_c(CW / 2, 126, b, pc('W'), 1, -1);
		if (new_hi && (tick / 8) % 2) txt_c(CW / 2, 138, "NEW HIGH SCORE!", pc('Y'), 1, -1);
		txt_c(CW / 2, 154, touch_present() ? "TAP: PLAY AGAIN" : "TAP HOME: PLAY AGAIN", pc('W'), 1, -1);
		txt_c(CW / 2, 166, touch_present() ? "HOLD HOME OR THE CORNER: TITLE" : "HOLD HOME: TITLE", pc('H'), 1, -1);
		draw_corner();
	}
	if (paused) {
		cv_box(16, 110, CW - 32, 58, pc('n'), pc('B'));
		txt_c(CW / 2, 116, "PAUSED", pc('Y'), 2, pc('K'));
		txt_c(CW / 2, 138, touch_present() ? "TAP TO PLAY ON" : "TAP HOME OR VOL+ & VOL-", pc('W'), 1, -1);
		txt_c(CW / 2, 152, touch_present() ? "HOLD THE CORNER 2 S: TITLE" : "HOLD HOME 2 S: TITLE", pc('H'), 1, -1);
		draw_corner();
	}
}

static void draw_title(void)
{
	static const char *desc[4] = { "FLIES AT YOUR LIGHT", "LEAPS WHERE YOU GO", "FOLLOWS YOUR TRAIL", "GUARDS THE EMBERS" };
	int cyc = (int)(state_t % 560), wx, mx, chase = cyc >= 280, t = cyc % 280;
	char b[48];
	cv_clear(pc('K'));
	for (int i = 0; i < 46; i++) {
		uint32_t h = hash3(i, 3, 9);
		cv_px((int)(h % CW), 2 + (int)((h >> 8) % 56), pc(((tick / 7 + i) % 29) ? 'S' : 'W'));
	}
	txt_c(CW / 2, 8, "HEDGEWICK", pc('Y'), 3, pc('t'));
	txt_c(CW / 2, 36, "A LANTERN IN THE HEDGE MAZE", pc('w'), 1, -1);

	/* a strip of garden: Wick walks right with the moth behind; then,
	 * lantern ablaze, he chases the dazzled moth back */
	cv_rect(0, 56, CW, 26, pc('N'));
	for (int x = 0; x < CW; x++) {
		uint32_t h = hash3(x, 1, 5);
		cv_px(x, 82, pc(h % 3 ? 'L' : 'l'));
		for (int y = 83; y < 92; y++) cv_px(x, y, pc(hash3(x, y, 5) % 6 ? 'g' : 'G'));
		cv_px(x, 55, pc('G'));
	}
	wx = chase ? CW + 10 - t : -24 + t;
	mx = chase ? wx - 34 : wx - 40;
	for (int i = 0; i < 9; i++) {
		int ex = 6 + i * 20;
		if (!chase && ex + 4 < wx + 11) continue;
		cv_px(ex, 68, pc('Y')); cv_px(ex - 1, 68, pc('O')); cv_px(ex + 1, 68, pc('O'));
		cv_px(ex, 67, pc('O')); cv_px(ex, 69, pc('O'));
	}
	cv_sprite(mx, 58, art_crit[K_MOTH][(state_t / 5) % 2], SPR, 0, chase ? remap_dazzle : NULL, 2);
	cv_sprite(wx, 58, art_wick[1][(state_t / 4) % 2], SPR, chase, NULL, 2);

	for (int k = 0; k < NCRIT; k++) {
		int y = 100 + k * 14;
		cv_sprite(6, y - 2, art_crit[k][(state_t / 6 + (unsigned)k) % 2 && k != K_OWL], SPR, 0, NULL, 1);
		txt(22, y, crit_name[k], pc(crit_col[k]), 1, -1);
		txt(52, y, desc[k], pc('w'), 1, -1);
	}
	ember_at((uint8_t (*)[CW])fb.cv, 6, 155, 0);
	txt(22, 159, "EMBER 10", pc('O'), 1, -1);
	cv_sprite(78, 156, art_bloom[(state_t / 10) % 2], 7, 0, NULL, 1);
	txt(90, 159, "BLOOM 50", pc('B'), 1, -1);
	txt(22, 171, "DAZZLED: 200 400 600 800", pc('C'), 1, -1);

	if (touch_present()) {
		txt(6, 190, "SWIPE  GO THAT WAY", pc('W'), 1, -1);
		txt(6, 200, "       AT THE NEXT OPENING", pc('H'), 1, -1);
		txt(6, 210, "SWIPE BACK  U-TURN", pc('W'), 1, -1);
		txt(6, 220, "TAP THE TOP BAR  PAUSE", pc('W'), 1, -1);
		txt(6, 230, have_vol ? "KEYS: VOL+ LEFT  VOL- RIGHT" : "HOLD \1 CORNER 2 S  TITLE", pc('H'), 1, -1);
		if ((state_t / 15) % 2 == 0) txt_c(CW / 2, 246, "TAP THE SCREEN TO START", pc('Y'), 1, -1);
	} else {
		txt(6, 190, "VOL+  TURN LEFT", pc('W'), 1, -1);
		txt(6, 200, "VOL-  TURN RIGHT", pc('W'), 1, -1);
		txt(6, 210, "      AT THE NEXT OPENING", pc('H'), 1, -1);
		txt(6, 220, "HOME  U-TURN, HOLD: TITLE", pc('W'), 1, -1);
		txt(6, 230, "VOL+ & VOL-  PAUSE", pc('W'), 1, -1);
		if ((state_t / 15) % 2 == 0) txt_c(CW / 2, 246, "TAP HOME TO START", pc('Y'), 1, -1);
	}
	snprintf(b, sizeof b, "HI %06ld", hi);
	txt_c(CW / 2, 260, b, pc('Y'), 1, -1);
	txt_c(CW / 2, 274, snd_status(), pc('H'), 1, -1);
	if (from_picker)
		txt_c(CW / 2, 286, touch_present() ? "HOLD HOME OR \1 CORNER: ARCADE" : "HOLD HOME 2 S: BACK TO ARCADE",
		      pc('B'), 1, -1);
	snprintf(b, sizeof b, "%.29s", dev_desc);
	txt_c(CW / 2, 300, b, pc('H'), 1, -1);
	if (touch_present()) txt_c(CW / 2, 310, "TOUCHSCREEN", pc('H'), 1, -1);
	draw_corner();
}

static void draw_hold_bar(void)
{
	int w = (qk_t - HOLD_SHOW) * 138 / (HOLD_QUIT - HOLD_SHOW);
	cv_box(10, 146, CW - 20, 32, pc('n'), pc('Y'));
	txt_c(CW / 2, 151, state == S_TITLE ? (t_corner ? "HOLD ON: BACK TO ARCADE" : "HOLD HOME: BACK TO ARCADE")
			   : (t_corner ? "HOLD ON FOR THE TITLE" : "HOLD HOME FOR THE TITLE"), pc('W'), 1, -1);
	cv_frame(20, 163, 140, 9, pc('w'));
	cv_rect(21, 164, w, 7, pc('Y'));
}

static void draw_toast(void)
{
	char lines[4][64];
	int n = wrap_text(ntc_text, 28, lines, 4);
	cv_box(2, 277, CW - 4, 42, pc('n'), pc('B'));
	for (int i = 0; i < n; i++) txt(6, 281 + 9 * i, lines[i], pc('W'), 1, -1);
}

static void render(void)
{
	if (state == S_TITLE) draw_title();
	else draw_play();
	if (qk_t >= HOLD_SHOW) draw_hold_bar();
	if (ntc_t > 0) draw_toast();
}

/* ------------------------------------------------------------------ input */

static void on_key(int code, int value)
{
	int up = swap_lr ? KEY_VOLUMEDOWN : KEY_VOLUMEUP;
	int home = (code == KEY_HOMEPAGE || code == KEY_HOME) && have_home;
	if (value == 1) {
		if (code == KEY_VOLUMEUP || code == KEY_VOLUMEDOWN) {
			int i = code == KEY_VOLUMEDOWN, other = i ? KEY_VOLUMEUP : KEY_VOLUMEDOWN;
			long other_t = vol_t[!i];
			vol_t[i] = tick;
			if (keydown[other] && tick - other_t <= 10) { in_chord++; return; }
			in_turn[code == up ? 1 : 2]++;
		} else if (code == KEY_LEFT) in_turn[1]++;
		else if (code == KEY_RIGHT) in_turn[2]++;
		else if (home || (code == KEY_POWER && !have_home)) in_home_press++;
		else if (code == KEY_POWER || code == KEY_SPACE || code == KEY_ENTER || code == KEY_DOWN) in_alt++;
		else if (code == KEY_P) in_pause_kb++;
		else if (code == KEY_M) in_sound_kb++;
		else if (code == KEY_ESC) in_esc++;
	} else if (value == 0) {
		if (home || (code == KEY_POWER && !have_home)) in_home_release++;
	}
}

/* -------------------------------------------------------------- autopilot */
/*
 * The self-test steers Wick with the same presses a player makes: queued
 * left/right turns before crossings, Home taps for U-turns when a creature
 * comes head on. It heads for the nearest ember along the cheapest path,
 * where tiles near a creature cost a lot, takes a bloom when a creature is
 * close, and hunts dazzled creatures. It also holds Home now and then (holds
 * broken by a turn and holds let go early must not quit; a still 2 s hold
 * must), pauses and unpauses, switches the sound off and on on the title,
 * and with --from-picker leaves for the arcade from the title at last.
 */
static long bq_end = -1, bq_turn = -1;
static int bq_kind, bq_check, bot_tap;
static long bot_uturn_t = -100;
static int bdist[MH][MW], bdanger[MH][MW];

static int bot_hold_kind(void) { return bq_end >= 0 ? bq_kind : 0; }
static void bot_home_press(void) { if (!bot_home) { bot_home = 1; in_home_press++; } }
static void bot_home_release(void) { if (bot_home) { bot_home = 0; in_home_release++; } }
static void bot_hold(int kind, int len) { bq_kind = kind; bq_end = tick + len; bot_home_press(); }

typedef struct { int c, p; } HNode;
static HNode heap[MH * MW * 4];
static int hn;
static void hpush(int c, int p)
{
	int i = hn++;
	if (hn > (int)(sizeof heap / sizeof heap[0])) { hn--; return; }
	while (i > 0 && heap[(i - 1) / 2].c > c) { heap[i] = heap[(i - 1) / 2]; i = (i - 1) / 2; }
	heap[i] = (HNode){ c, p };
}
static HNode hpop(void)
{
	HNode top = heap[0], last = heap[--hn];
	int i = 0;
	for (;;) {
		int ch = 2 * i + 1;
		if (ch >= hn) break;
		if (ch + 1 < hn && heap[ch + 1].c < heap[ch].c) ch++;
		if (heap[ch].c >= last.c) break;
		heap[i] = heap[ch];
		i = ch;
	}
	if (hn > 0) heap[i] = last;
	return top;
}

static void bot_maps(void)
{
	static const int dcost[4] = { 400, 200, 60, 15 };
	int hx = tile_of(hero.x), hy = tile_of(hero.y), near = 0;
	memset(bdanger, 0, sizeof bdanger);
	for (int k = 0; k < NCRIT; k++) {
		const Crit *c = &cr[k];
		int q[MH * MW], depth[MH * MW], qh = 0, qt = 0;
		static uint8_t seen[MH][MW];
		if ((c->st != CS_OUT && c->st != CS_LEAVE) || c->dz) continue;
		if (abs(tile_of(c->x) - hx) + abs(tile_of(c->y) - hy) <= 5) near = 1;
		memset(seen, 0, sizeof seen);
		q[qt] = tile_of(c->y) * MW + tile_of(c->x); depth[qt++] = 0;
		seen[tile_of(c->y)][tile_of(c->x)] = 1;
		while (qh < qt) {
			int p = q[qh], dp = depth[qh++], x = p % MW, y = p / MW;
			if (bdanger[y][x] < dcost[dp]) bdanger[y][x] = dcost[dp];
			if (dp == 3) continue;
			for (int d = 0; d < 4; d++) {
				int nx = x + DX[d], ny = y + DY[d];
				if (open_t(nx, ny) && !seen[ny][nx]) { seen[ny][nx] = 1; q[qt] = ny * MW + nx; depth[qt++] = dp + 1; }
			}
		}
	}
	for (int y = 0; y < MH; y++)
		for (int x = 0; x < MW; x++) bdist[y][x] = INT_MAX / 4;
	hn = 0;
	for (int y = 0; y < MH; y++)
		for (int x = 0; x < MW; x++) {
			int c = -1;
			if (item[y][x] == 1) c = 0;
			else if (item[y][x] == 2) c = near ? 0 : 40;
			if (c >= 0) { bdist[y][x] = c; hpush(c, y * MW + x); }
		}
	if (dz_t > 45)
		for (int k = 0; k < NCRIT; k++)
			if (cr[k].dz && cr[k].st == CS_OUT) {
				int x = tile_of(cr[k].x), y = tile_of(cr[k].y);
				bdist[y][x] = 0;
				hpush(0, y * MW + x);
			}
	while (hn) {
		HNode n = hpop();
		int x = n.p % MW, y = n.p / MW;
		if (n.c > bdist[y][x]) continue;
		for (int d = 0; d < 4; d++) {
			int nx = x + DX[d], ny = y + DY[d], nc;
			if (!open_t(nx, ny)) continue;
			nc = n.c + 1 + bdanger[y][x];
			if (nc < bdist[ny][nx]) { bdist[ny][nx] = nc; hpush(nc, ny * MW + nx); }
		}
	}
}

static void bot_steer(void)
{
	int dx, dy, d = hero.dir, best = -1, bv = INT_MAX, autod, bx, by, htx = tile_of(hero.x), hty = tile_of(hero.y);
	bot_maps();
	decision_tile(&dx, &dy);
	if (!open_t(dx, dy)) return;
	for (int nd = 0; nd < 4; nd++) {
		int v;
		if (nd == BACK_OF(d) || !open_dir(dx, dy, nd)) continue;
		v = bdist[dy + DY[nd]][dx + DX[nd]] + bdanger[dy + DY[nd]][dx + DX[nd]];
		if (v < bv) { bv = v; best = nd; }
	}
	/* a creature head on: turn back, if it is safer behind */
	if (dx == htx && dy == hty) { bx = htx - DX[d]; by = hty - DY[d]; } else { bx = htx; by = hty; }
	if (tick - bot_uturn_t > 20 && open_t(bx, by) &&
	    (bdanger[dy][dx] >= 200 || (best >= 0 && bdanger[dy + DY[best]][dx + DX[best]] >= 400)) &&
	    bdanger[by][bx] < bdanger[dy][dx]) {
		bot_home_press();
		bot_tap = 1;
		bot_uturn_t = tick;
		return;
	}
	if (best < 0) return;
	if (open_dir(dx, dy, d)) autod = d;
	else {
		int l = open_dir(dx, dy, LEFT_OF(d)), r = open_dir(dx, dy, RIGHT_OF(d));
		autod = l && !r ? LEFT_OF(d) : r && !l ? RIGHT_OF(d) : -1;
	}
	if (hero.q) {
		int qd = hero.q == 1 ? LEFT_OF(d) : RIGHT_OF(d);
		if (open_dir(dx, dy, qd)) autod = qd;
	}
	if (best == autod) return;
	if (best == LEFT_OF(d)) in_turn[1]++;
	else if (best == RIGHT_OF(d)) in_turn[2]++;
}

static const char *bot_notice;

static void bot_think(void)
{
	long ph;
	if (bot_tap) { bot_tap = 0; if (bq_end < 0) bot_home_release(); }
	if (bq_end >= 0 && tick >= bq_end) {
		int k = bq_kind;
		bq_end = -1;
		bot_home_release();
		if (k == 4) bq_check = 4;           /* the tap on game over: a game next tick */
		if (k == 2 && state == S_TITLE) bq_check = 2;   /* let go on the title: still there */
		return;
	}
	if (bq_check) {
		if (bq_check == 4 && state == S_OVER) bad("a tap on game over did not start a game");
		if (bq_check == 2 && (state != S_TITLE || exit_now)) bad("a hold let go early on the title did something");
		bq_check = 0;
	}
	if (bot_notice && tick == 250 && ntc_path) {
		FILE *f = fopen(ntc_path, "we");
		if (f) { fprintf(f, "%s\nsecond line, not shown\n", bot_notice); fclose(f); }
	}
	if (bq_end >= 0) {
		if (bq_kind == 1 && tick == bq_turn) in_turn[1 + (int)(tick & 1)]++;   /* the turn that breaks the hold */
		return;
	}
	switch (state) {
	case S_TITLE:
		if (st_games % 3 == 1 && (state_t == 40 || state_t == 50)) in_chord++;   /* sound off, on */
		if (from_picker && state_t == 60) {
			if (st_games == 1) bot_hold(2, HOLD_SHOW + 15);
			else if (st_games >= 2) bot_hold(3, HOLD_QUIT + 10);
		}
		if (state_t == 120) { bot_home_press(); bot_tap = 1; }
		break;
	case S_OVER:
		if (state_t == 50) {
			if (st_games % 2 || (from_picker && st_games >= 2)) bot_hold(3, HOLD_QUIT + 10);
			else bot_hold(4, 3);
		}
		break;
	case S_PLAY:
		ph = tick % 6000;
		if (paused) {
			if (ph == 3060) in_chord++;
			break;
		}
		if (ph == 900) { bot_hold(1, 90); bq_turn = tick + 40; break; }
		if (ph == 2000) { bot_hold(2, HOLD_SHOW + 15); break; }
		if (ph == 3000) { in_chord++; break; }
		if (ph == 5000 && (tick / 6000) % 2) { bot_hold(3, HOLD_QUIT + 10); break; }
		if (hero.alive) bot_steer();
		break;
	default:
		break;
	}
}

/* Invariants, every tick of the self-test. */
static void bot_check(void)
{
	int n = 0;
	if (state == S_TITLE) return;
	if (hero.alive) {
		if ((hero.x - TC) % TU && (hero.y - TC) % TU) bad("Wick off the grid at %d,%d", hero.x, hero.y);
		if (!open_t(tile_of(hero.x), tile_of(hero.y))) bad("Wick in a hedge at %d,%d", tile_of(hero.x), tile_of(hero.y));
	}
	for (int k = 0; k < NCRIT; k++) {
		const Crit *c = &cr[k];
		if (c->st != CS_OUT) continue;
		if ((c->x - TC) % TU && (c->y - TC) % TU) bad("%s off the grid", crit_name[k]);
		if (!open_t(tile_of(c->x), tile_of(c->y))) bad("%s in a hedge at %d,%d", crit_name[k], tile_of(c->x), tile_of(c->y));
	}
	for (int y = 0; y < MH; y++)
		for (int x = 0; x < MW; x++) n += item[y][x] == 1;
	if (n != embers_left) bad("%d embers on the ground, %d counted", n, embers_left);
	if (score < 0 || lives < 0 || lives > 6) bad("score %ld lives %d", score, lives);
}

/* --shot WHEN:FILE: a picture of the screen (PPM) when WHEN first happens. */
static struct { char when[16]; const char *path; int done; } shots[MAXSHOT];
static int nshots, paused_t;

static int shot_due(const char *w)
{
	if (w[0] >= '0' && w[0] <= '9') return tick == atol(w);
	if (!strcmp(w, "title")) return state == S_TITLE && state_t == 75;
	if (!strcmp(w, "ready")) return state == S_READY && state_t == 30;
	if (!strcmp(w, "play")) return state == S_PLAY && !paused && life_t == 150;
	if (!strcmp(w, "dazzle")) return state == S_PLAY && !paused && dz_t == dazzle_len() - 40;
	if (!strcmp(w, "pause")) return paused && paused_t == 10;
	if (!strcmp(w, "hold")) return qk_t == HOLD_SHOW + 12;
	if (!strcmp(w, "toast")) return ntc_t == NOTICE_T - 15;
	if (!strcmp(w, "dying")) return state == S_DYING && state_t == 30;
	if (!strcmp(w, "over")) return state == S_OVER && state_t == 40;
	if (!strcmp(w, "level2")) return level == 2 && state == S_PLAY && !paused && life_t == 150;
	if (!strcmp(w, "level3")) return level == 3 && state == S_PLAY && !paused && life_t == 150;
	return 0;
}

static void take_shots(void)
{
	paused_t = paused ? paused_t + 1 : 0;
	for (int i = 0; i < nshots; i++)
		if (!shots[i].done && shot_due(shots[i].when)) {
			shots[i].done = 1;
			if (fb.fd < 0 || fb_dump_ppm(shots[i].path) != 0) logmsg("shot %s: could not write %s", shots[i].when, shots[i].path);
			else logmsg("shot %s: %s (tick %ld)", shots[i].when, shots[i].path, tick);
		}
}

/* ------------------------------------------------------------- screen off */
/*
 * Power darkens the screen (backlight.h). While it is dark nothing moves:
 * no game tick, no drawing, no sound (the card is closed), the touchscreen
 * is read and dropped; the buttons are slept on and /run/notice is looked at
 * twice a second (a new notice lights the screen). When it comes back a game
 * in progress is paused (as by the pause button) until the player goes on.
 */
static int was_dark;
static int64_t dark_next_notice, dark_next_scan;

static void screen_edges(void)
{
	if (bl_dark() == was_dark) return;
	was_dark = bl_dark();
	touch_discard(clock_ms());
	t_corner = 0;
	memset(in_turn, 0, sizeof in_turn);
	in_chord = in_home_press = in_home_release = in_esc = in_pause_kb = in_sound_kb = in_alt = 0;
	tg_start = tg_pause = 0;
	qk_t = 0;
	if (was_dark) {
		snd_suspend();
		dark_next_notice = 0;
		logmsg("screen dark: state %d, score %ld, lives %d, deaths %ld, life_t %d", state, score, lives, st_deaths, life_t);
		return;
	}
	if (state != S_TITLE && state != S_OVER) paused = 1;
	qk_latch = held_home();
	fb.full = 1;
	snd_resume();
	logmsg("screen lit: state %d, score %ld, lives %d, deaths %ld, life_t %d%s", state, score, lives, st_deaths, life_t,
	       paused ? ", paused" : "");
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
		if (notice_check()) { st_notices++; bl_wake("a notice"); }
		touch_discard(clock_ms());
		dark_next_notice = now + BL_IDLE_MS * 1000000LL;
	}
	if (rescan || (ndev == 0 && now >= dark_next_scan)) { scan_devs(); dark_next_scan = now + 2000000000LL; }
	screen_edges();
}

/* Dark, in the self-test: one tick passes. */
static void dark_bot(void)
{
	if (tick % (HZ / 2) == 0 && notice_check()) { st_notices++; bl_wake("a notice"); }
	touch_discard(clock_ms());
	screen_edges();
}

/* ------------------------------------------------------------------- main */

static void usage(void)
{
	fprintf(stderr,
		"usage: hedgewick [--tty PATH] [--fb PATH] [--hi PATH|none] [--from-picker] [--swap]\n"
		"                 [--no-grab] [--mute] [--vol DB] [--fps 30|15|10] [--notice PATH|none]\n"
		"                 [--fb-write | --fb-mmap]\n"
		"       hedgewick --bot TICKS [--level N] [--seed N] [--shot WHEN:FILE.ppm]... [--bot-notice TEXT] [--no-pilot]\n");
	exit(2);
}

int main(int argc, char **argv)
{
	const char *tty_path = NULL, *fb_path = "/dev/fb0";
	long bot_ticks = 0;
	int mute = 0, notice_set = 0, ret = 0;
	unsigned long seed = 0;

	common_init("hedgewick");
	for (int i = 1; i < argc; i++) {
		const char *a = argv[i], *v = i + 1 < argc ? argv[i + 1] : NULL;
		if (!strcmp(a, "--tty") && v) { tty_path = v; i++; }
		else if (!strcmp(a, "--fb") && v) { fb_path = v; i++; }
		else if (!strcmp(a, "--fb-write")) fb_use_write = 1;
		else if (!strcmp(a, "--fb-mmap")) fb_use_write = 0;
		else if (!strcmp(a, "--hi") && v) { hi_path = strcmp(v, "none") ? v : NULL; i++; }
		else if (!strcmp(a, "--notice") && v) { ntc_path = strcmp(v, "none") ? v : NULL; notice_set = 1; i++; }
		else if (!strcmp(a, "--from-picker")) from_picker = 1;
		else if (!strcmp(a, "--swap")) swap_lr = 1;
		else if (!strcmp(a, "--no-grab")) grab = 0;
		else if (!strcmp(a, "--mute")) mute = 1;
		else if (!strcmp(a, "--vol") && v) {
			snd_vol_db = strtof(v, NULL);
			if (!(snd_vol_db <= VOL_MAX_DB)) snd_vol_db = VOL_MAX_DB;
			if (snd_vol_db < -60.0f) snd_vol_db = -60.0f;
			i++;
		} else if (!strcmp(a, "--fps") && v) {
			fps = atoi(v);
			if (fps != 30 && fps != 15 && fps != 10) usage();
			i++;
		} else if (!strcmp(a, "--bot") && v) { bot = 1; bot_ticks = atol(v); i++; }
		else if (!strcmp(a, "--level") && v) { start_level = atoi(v); if (start_level < 1 || start_level > 99) usage(); i++; }
		else if (!strcmp(a, "--seed") && v) { seed = strtoul(v, NULL, 0); i++; }
		else if (!strcmp(a, "--bot-notice") && v) { bot_notice = v; i++; }
		else if (!strcmp(a, "--no-pilot")) pilot = 0;
		else if (!strcmp(a, "--shot") && v && nshots < MAXSHOT) {
			const char *c = strchr(v, ':');
			if (!c || c == v || (size_t)(c - v) >= sizeof shots[0].when) usage();
			memcpy(shots[nshots].when, v, (size_t)(c - v));
			shots[nshots].when[c - v] = 0;
			shots[nshots++].path = c + 1;
			i++;
		} else usage();
	}

	install_signals();
	if (check_mazes()) { logmsg("the built-in gardens are broken"); st_bad++; }
	for (int i = 0; i < 256; i++) remap_dazzle[i] = remap_out[i] = remap_grey[i] = (uint8_t)i;
	for (int i = 0; i < NPAL; i++) {
		uint32_t rgb = palette[i].rgb;
		int lum = (int)((((rgb >> 16) & 255) * 3 + ((rgb >> 8) & 255) * 6 + (rgb & 255)) / 10);
		remap_dazzle[i] = (uint8_t)pc(lum > 110 ? 'C' : 'c');
		remap_grey[i] = (uint8_t)pc(lum > 110 ? 'S' : 's');
	}
	remap_dazzle[pc('W')] = remap_dazzle[pc('K')] = (uint8_t)pc('b');
	remap_grey[pc('K')] = (uint8_t)pc('K');
	remap_out[pc('O')] = (uint8_t)pc('s');
	remap_out[pc('Y')] = remap_out[pc('y')] = (uint8_t)pc('S');

	if (bot) {
		hi_path = NULL;
		if (!notice_set) ntc_path = NULL;
		have_home = 1;
		snprintf(dev_desc, sizeof dev_desc, "autopilot");
		rs ^= (uint32_t)seed * 2654435761u;
		if (!rs) rs = 1;
	} else {
		rs ^= (uint32_t)now_ns() | 1u;
	}

	if (!bot || fake_fb_given()) {
		if (fb_open(fb_path) < 0) { logmsg("no usable framebuffer; exiting"); return 1; }
	} else {
		fb.w = CW; fb.h = CH;   /* canvas only */
	}
	if (fb_canvas(CW, CH) < 0) return 1;
	logmsg("fb: %s; canvas %dx%d scaled %dx at %d,%d", fb.fd >= 0 ? fb.desc : "none",
	       CW, CH, fb.scale, fb.ox, fb.oy);

	if (!bot && !tty_path) tty_path = access("/dev/tty1", W_OK) == 0 ? "/dev/tty1" : "/dev/tty";
	if (tty_path && tty_open(tty_path) == 0) console_graphics();

	bl_init(clock_ms);
	if (!bot) {
		hi = read_number(hi_path);
		scan_devs();
	} else {
		fakekeys_have(&have_home, &have_power, &have_vol);
		bl_set_keys(have_home, have_power, have_vol);
	}
	touch_view(fb.w, fb.h, fb.ox, fb.oy, fb.scale, CW, CH, CNR_W, CNR_H);
	tp.last_scan = bot ? 0 : now_ns() / 1000000;
	touch_open(grab);
	logmsg("%s", tp.desc);
	if (!bot && held_home()) qk_latch = 1;      /* still held from the arcade */
	notice_sync();
	level = start_level;
	load_maze();
	set_state(S_TITLE);
	if (mute) snd_state = SND_OFF;
	else snd_start_search();
	logmsg("start: %s, sound %s, amp volume %.1f dB, %d fps, buttons: %s",
	       from_picker ? "from the arcade" : "standalone", mute ? "off (--mute)" : "on",
	       (double)snd_vol_db, fps, dev_desc);

	const int64_t period = 1000000000LL / HZ;
	int64_t next = now_ns();
	while (!quit_sig && !exit_now) {
		if (!bot) {
			int64_t now;
			if (bl_dark()) {
				dark_wait();
				next = now_ns();
				continue;
			}
			while ((now = now_ns()) < next && !quit_sig && !bl_dark()) {
				struct pollfd pf[MAXDEV + 1];
				int ms = (int)((next - now + 999999) / 1000000), n = ndev, si = -1;
				for (int i = 0; i < ndev; i++) pf[i] = (struct pollfd){ dev_fd[i], POLLIN, 0 };
				if (snd_state == SND_ON && snd_poll_ok) {
					si = n;
					pf[n++] = (struct pollfd){ snd_pcm, POLLOUT, 0 };
				}
				poll(n ? pf : NULL, (nfds_t)n, ms);
				read_events();
				fakekeys_pump(key_event);
				bl_tick();
				if (si >= 0 && pf[si].revents && !snd_pump()) snd_poll_ok = 0;
			}
			next += period;
			if (now - next > 5 * period) next = now + period;
			read_events();
			fakekeys_pump(key_event);
			bl_tick();
			if (rescan || (ndev == 0 && tick % (2 * HZ) == 0)) scan_devs();
			if (tick % (30 * HZ) == 0) fb.full = 1;   /* repaint over anything else */
		} else {
			fakekeys_pump(key_event);
			bl_tick();
			screen_edges();
			if (bl_dark()) {
				dark_bot();
				if (++tick >= bot_ticks) break;
				continue;
			}
			if (pilot) bot_think();
		}
		screen_edges();
		if (bl_dark()) continue;
		game_tick();
		snd_tick();
		if (snd_opened_now) {
			snd_opened_now = 0;
			if (state == S_TITLE) sfx_title();
		}
		if (tick % (HZ / fps) == 0) {
			int64_t t0 = now_ns(), t1, t2;
			long bytes;
			render();
			t1 = now_ns();
			bytes = fb_present();
			t2 = now_ns();
			if (state != S_TITLE && tick > 0) {
				ft_render += t1 - t0; ft_present += t2 - t1; ft_n++; ft_bytes += bytes;
				if (t1 - t0 > ft_rmax) ft_rmax = t1 - t0;
				if (t2 - t1 > ft_pmax) ft_pmax = t2 - t1;
			}
		}
		if (nshots) take_shots();
		if (bot && pilot) bot_check();
		tick++;
		if (bot && tick >= bot_ticks) break;
	}

	if (quit_sig) logmsg("stopped by signal %d", (int)quit_sig);
	if (exit_now) logmsg("back to the arcade (Home held on the title)");
	bl_exit();
	if (ft_n)
		logmsg("frames: %ld in play; render %.0f us avg (%.0f max), present %.0f us avg (%.0f max), %.0f KB/frame avg",
		       ft_n, (double)ft_render / (double)ft_n / 1e3, (double)ft_rmax / 1e3,
		       (double)ft_present / (double)ft_n / 1e3, (double)ft_pmax / 1e3, (double)ft_bytes / (double)ft_n / 1024.0);
	if (bot) {
		fprintf(stderr, "ticks=%ld state=%d games=%ld score=%ld level=%d maxlevel=%ld embers=%ld blooms=%ld caught=%ld deaths=%ld clears=%ld\n",
			tick, state, st_games, score, level, st_maxlevel, st_embers, st_blooms, st_caught, st_deaths, st_clears);
		fprintf(stderr, "quits=%ld bars=%ld pauses=%ld uturns=%ld turns=%ld toggles=%d notices=%ld exit=%d sound=%d underruns=%ld bad=%ld\n",
			st_quits, st_bars, st_pauses, st_uturns, st_turns, sound_toggles, st_notices, exit_now, snd_state,
			snd_underruns, st_bad);
		fprintf(stderr, "touch=%d contacts=%ld taps=%ld swipes=%ld longs=%ld corner=%ld tswipes=%ld ttaps=%ld tdropped=%ld paused=%d\n",
			touch_present(), tp.n_contact, tp.n_tap, tp.n_swipe, tp.n_long, tp.n_corner, st_tswipes, st_ttaps,
			tp.n_dropped, paused);
		ret = st_bad ? 3 : 0;
	}
	cpu_report();
	snd_close();
	if (kd_fd >= 0) console_text(!from_picker);
	return ret;
}

#ifdef MZ_FAKE_ALSA
#include "fake_alsa.c"
#endif
