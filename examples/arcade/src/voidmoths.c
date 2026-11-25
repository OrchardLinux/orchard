/*
 * voidmoths.c -- VOIDMOTHS, a fixed-screen shooter for a Linux phone screen.
 *
 * A lantern ship at the bottom of the screen holds off a swarm of void moths.
 * The moths hang in a swaying formation and peel off to dive at the light,
 * dropping sparks on the way down. Clear the swarm and a faster one arrives.
 *
 * Written for an iPhone 6s Plus (N66) running Linux from RAM: the buttons
 * arrive as evdev key events from the gpio-keys driver. The picture is pixel
 * art drawn straight into the framebuffer (/dev/fb0) while the console is in
 * KD_GRAPHICS mode; without a usable framebuffer (or with --text) it is the
 * v2 text picture on the framebuffer console, drawn with ANSI escape codes.
 *
 *   Volume Up    KEY_VOLUMEUP   (115)  move left
 *   Volume Down  KEY_VOLUMEDOWN (114)  move right
 *   Home         KEY_HOMEPAGE   (172)  fire / start  (hold for autofire)
 *                                      held 2 s without moving: back to the title
 *   Power        KEY_POWER      (116)  screen off (backlight.h): Home or Power
 *                                      lights it again, the game paused until a
 *                                      press. Without volume keys: Power held
 *                                      0.8 s; a short press fires as before.
 *                                      Without a backlight or a Home key: fires
 *                                      (the fallback if Home is dead).
 *   Volume Up + Volume Down together on the title: sound on / off
 *   USB keyboard: arrows move, space / enter / up fire, Esc title, M sound.
 *   Touchscreen (touch.h; found by its capabilities, so iPads play too):
 *     drag         the ship follows the finger and fires while it is down
 *     tap          fire; title / game over: start
 *     top left corner, held: the Home button (2 s: title; on the title with
 *                  --from-picker: back to the arcade)
 *
 * Sound effects go to the loudspeaker through the bare ALSA kernel interface
 * (no libasound): only the card with the id "Speaker" is ever opened. No
 * card within 15 s, or any ALSA error, and the game runs silent.
 *
 * Notices: when the file /run/notice changes (checked twice a second), its
 * first line is shown at the top of the screen for 6 s, with a soft chime.
 * `notice TEXT` (rootfs/usr/bin/notice) writes it.
 *
 * usage: voidmoths [--tty PATH] [--fb PATH] [--text] [--hi PATH] [--swap]
 *                  [--no-grab] [--mute] [--vol DB] [--notice PATH|none]
 *        voidmoths --restore-tty PATH
 *            put a console left in graphics mode back into text mode
 *        voidmoths --bot TICKS [--size WxH | --fakefb WxHxBPP[:bgr]]
 *                  [--dump DIR --dump-at T,T,...] [--notice PATH] [--mute]
 *                  [--no-pilot]  (no autopilot: only $FAKE_TOUCH plays,
 *                  in builds with -DTOUCH_TEST)
 *            headless self-test: an autopilot plays TICKS ticks as fast as
 *            it can. In text mode the last frame is printed as plain text;
 *            with --fakefb it draws into a framebuffer in memory and can
 *            dump frames as PPM files.
 *
 * Build: aarch64-linux-gnu-gcc -static -O2 -o voidmoths voidmoths.c -lm
 */
#define _GNU_SOURCE
#include <errno.h>
#include <fcntl.h>
#include <limits.h>
#include <linux/fb.h>
#include <linux/input.h>
#include <linux/kd.h>
#include <math.h>
#include <poll.h>
#include <signal.h>
#include <stdarg.h>
#include <stdint.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <sys/ioctl.h>
#include <sys/mman.h>
#include <sys/stat.h>
#include <termios.h>
#include <time.h>
#include <unistd.h>
#include <sound/asound.h>
#include <sound/tlv.h>
#include "touch.h"
#include "backlight.h"

#define HZ      30          /* game ticks per second */
#define FW_MAX  45          /* play field: 1080/24 columns ... */
#define FH_MAX  40          /* ... by 1920/48 rows with the 24x48 font */
#define TW_MAX  256
#define TH_MAX  160
#define MAXE    48
#define MAXPS   2           /* player shots on screen */
#define MAXES   12          /* enemy shots on screen */
#define MAXB    24
#define MAXP    8
#define NSTAR   28
#define MAXDEV  16
#define PSPD    0.75f       /* ship speed, columns per tick */
#define HOLD_SHOW (1 * HZ)  /* quit key held this long: the "hold for title" bar */
#define HOLD_QUIT (2 * HZ)  /* ... and this long: back to the title */

enum { BLK, RED, GRN, YEL, BLU, MAG, CYN, WHT };
enum { S_TITLE, S_PLAY, S_OVER };
enum { E_DART, E_LANTERN, E_EMPRESS };
enum { M_ENTER, M_FORM, M_DIVE, M_RETURN };

typedef struct { char ch; uint8_t fg, bg, bold; } Cell;
typedef struct {
	int alive, type, hp, mode, t, dir, leader, flash, shots;
	float sx, sy;           /* slot, relative to the formation origin */
	float x, y, vx, vy;     /* position in field cells (x column, y row) */
	float offx, offy;       /* escort offset from its leader */
} Enemy;
typedef struct { int on; float x, y, vx, vy; } Shot;
typedef struct { int on, t, big; float x, y; } Boom;
typedef struct { int on, t; float x, y; char txt[12]; } Popup;
typedef struct { float x, y, v; int c; } Star;

/* ------------------------------------------------------------------ screen */

static Cell scr[TH_MAX][TW_MAX], old[TH_MAX][TW_MAX];
static int tw, th, fw = FW_MAX, fh = FH_MAX, fx0, fy0;
static int tty_fd = -1, full_redraw = 1;
static char ob[1 << 16];
static size_t olen;

static void flush_out(void)
{
	size_t off = 0;
	if (tty_fd < 0) { olen = 0; return; }
	while (off < olen) {
		ssize_t n = write(tty_fd, ob + off, olen - off);
		if (n < 0) {
			if (errno == EINTR || errno == EAGAIN) continue;
			break;
		}
		off += (size_t)n;
	}
	olen = 0;
}

static void out(const char *s, size_t n)
{
	if (olen + n > sizeof ob) flush_out();
	memcpy(ob + olen, s, n);
	olen += n;
}

static void outs(const char *s) { out(s, strlen(s)); }

/* ------------------------------------------------------------- framebuffer */
/*
 * Pixel mode. The picture is drawn in logical pixels into pix[] (one palette
 * index per pixel); present() sends what changed to /dev/fb0, each logical
 * pixel as a gs x gs block in the framebuffer's own pixel format (read with
 * FBIOGET_VSCREENINFO / FBIOGET_FSCREENINFO: 32 bpp XRGB or XBGR, or 16 bpp
 * RGB565; any bitfield layout of 1..8 bits per colour works). On the N66
 * (1080x1920) gs is 4: the logical screen is 270 x 480 and one cell of the
 * play field (still 45 x 40 cells, as in text mode) is 6 x 12 logical
 * pixels, so the game's rules and sizes are exactly those of text mode.
 *
 * present() compares every logical row with what was sent last time, in
 * 8-pixel pieces, and writes only the pieces that changed; every 10 s it
 * sends the whole frame once more.
 */
#define CW 6        /* logical pixels per field cell, across */
#define CH 12       /* ... and down */

static void logmsg(const char *fmt, ...) __attribute__((format(printf, 1, 2)));
static int64_t now_ns(void);

static int gfx;                         /* 1: pixel mode */
static int fb_fd = -1, fb_fake, fb_full = 1;
static uint8_t *fb_map, *fb_base;       /* mapping; first visible pixel */
static size_t fb_maplen;
static unsigned fb_xres, fb_yres, fb_line, fb_Bpp;
static struct fb_bitfield fb_r, fb_g, fb_b, fb_t;
static int gs = 1, lw, lh, gx0, gy0;    /* scale, logical size, field origin */
static uint8_t *pix, *pix_old;
static uint32_t pal_px[256];
static uint8_t *fb_row;                 /* one expanded row piece */
static long fb_frames;
static int64_t fb_ns_sum, fb_ns_max;
static long long fb_bytes;

/* The palette. C_TXT + colour (+ 8 when bold) are the text colours. */
enum {
	C_BG, C_TXT, C_SHADOW = C_TXT + 16, C_STAR1, C_STAR2, C_STAR3, C_STAR4,
	C_DART, C_LANT = C_DART + 4, C_EMP = C_LANT + 4, C_HURT = C_EMP + 4,
	C_SHIP = C_HURT + 4, C_PSHOT = C_SHIP + 6, C_PSHOT2, C_ESHOT, C_ESHOT2,
	C_PANEL, C_EDGE, C_ICON, C_DIM, C_TILE, C_NPAL = C_TILE + 24
};
#define C_WHITE (C_TXT + 15)
static const uint8_t pal_rgb[C_NPAL][3] = {
	[C_BG] = { 5, 5, 16 },
	[C_TXT + 0] = { 40, 40, 56 },     [C_TXT + 8] = { 96, 96, 116 },
	[C_TXT + 1] = { 200, 56, 56 },    [C_TXT + 9] = { 255, 92, 76 },
	[C_TXT + 2] = { 46, 158, 85 },    [C_TXT + 10] = { 96, 226, 140 },
	[C_TXT + 3] = { 200, 160, 48 },   [C_TXT + 11] = { 255, 216, 76 },
	[C_TXT + 4] = { 80, 104, 196 },   [C_TXT + 12] = { 124, 152, 255 },
	[C_TXT + 5] = { 170, 64, 170 },   [C_TXT + 13] = { 255, 116, 226 },
	[C_TXT + 6] = { 42, 160, 176 },   [C_TXT + 14] = { 100, 240, 255 },
	[C_TXT + 7] = { 176, 176, 196 },  [C_TXT + 15] = { 255, 255, 255 },
	[C_SHADOW] = { 0, 0, 6 },
	[C_STAR1] = { 34, 40, 78 }, [C_STAR2] = { 78, 90, 148 },
	[C_STAR3] = { 132, 150, 210 }, [C_STAR4] = { 228, 236, 255 },
	/* dart: green wings, dark edges, pale body, white eyes */
	[C_DART] = { 58, 196, 108 }, [C_DART + 1] = { 18, 96, 58 },
	[C_DART + 2] = { 196, 244, 150 }, [C_DART + 3] = { 255, 255, 228 },
	/* lantern: ochre wings, brown pattern, glowing abdomen and core */
	[C_LANT] = { 192, 122, 52 }, [C_LANT + 1] = { 98, 54, 24 },
	[C_LANT + 2] = { 255, 204, 64 }, [C_LANT + 3] = { 255, 248, 200 },
	/* empress: magenta wings, plum edges, pink eye-spots, white */
	[C_EMP] = { 204, 64, 196 }, [C_EMP + 1] = { 90, 28, 112 },
	[C_EMP + 2] = { 255, 172, 240 }, [C_EMP + 3] = { 255, 250, 255 },
	/* empress after one hit */
	[C_HURT] = { 222, 52, 52 }, [C_HURT + 1] = { 104, 20, 24 },
	[C_HURT + 2] = { 255, 162, 120 }, [C_HURT + 3] = { 255, 240, 220 },
	/* the lantern ship: hull, dark hull, glow, core, flame, flame core */
	[C_SHIP] = { 72, 214, 240 }, [C_SHIP + 1] = { 26, 98, 128 },
	[C_SHIP + 2] = { 255, 196, 64 }, [C_SHIP + 3] = { 255, 250, 222 },
	[C_SHIP + 4] = { 255, 118, 44 }, [C_SHIP + 5] = { 255, 222, 120 },
	[C_PSHOT] = { 255, 255, 255 }, [C_PSHOT2] = { 90, 214, 255 },
	[C_ESHOT] = { 255, 78, 60 }, [C_ESHOT2] = { 255, 176, 64 },
	[C_PANEL] = { 18, 16, 46 }, [C_EDGE] = { 142, 126, 255 }, [C_ICON] = { 255, 140, 64 },
	[C_DIM] = { 48, 36, 64 },
	/* big-letter tiles: light, middle, dark of red, yellow, magenta, cyan */
	[C_TILE + 3 * 1] = { 255, 120, 100 }, [C_TILE + 3 * 1 + 1] = { 226, 52, 52 }, [C_TILE + 3 * 1 + 2] = { 128, 22, 30 },
	[C_TILE + 3 * 3] = { 255, 236, 140 }, [C_TILE + 3 * 3 + 1] = { 255, 196, 56 }, [C_TILE + 3 * 3 + 2] = { 160, 100, 20 },
	[C_TILE + 3 * 5] = { 255, 150, 236 }, [C_TILE + 3 * 5 + 1] = { 206, 66, 198 }, [C_TILE + 3 * 5 + 2] = { 104, 26, 120 },
	[C_TILE + 3 * 6] = { 170, 250, 255 }, [C_TILE + 3 * 6 + 1] = { 60, 200, 230 }, [C_TILE + 3 * 6 + 2] = { 20, 92, 130 },
};

static int tcol(int fg, int bold) { return C_TXT + (fg & 7) + (bold ? 8 : 0); }

/*
 * A 5x7 font (plus a descender row) for printable ASCII, drawn for this
 * game. One string per glyph: rows separated by spaces, '#' set.
 */
static const char *const font_src[95] = {
	"..... ..... ..... ..... ..... ..... .....",               /* space */
	"..#.. ..#.. ..#.. ..#.. ..#.. ..... ..#..",               /* ! */
	".#.#. .#.#. ..... ..... ..... ..... .....",               /* " */
	".#.#. .#.#. ##### .#.#. ##### .#.#. .#.#.",               /* # */
	"..#.. .#### #.#.. .###. ..#.# ####. ..#..",               /* $ */
	"##..# ##..# ...#. ..#.. .#... #..## #..##",               /* % */
	".##.. #..#. #.#.. .#... #.#.# #..#. .##.#",               /* & */
	"..#.. ..#.. ..... ..... ..... ..... .....",               /* ' */
	"...#. ..#.. .#... .#... .#... ..#.. ...#.",               /* ( */
	".#... ..#.. ...#. ...#. ...#. ..#.. .#...",               /* ) */
	"..... ..#.. #.#.# .###. #.#.# ..#.. .....",               /* * */
	"..... ..#.. ..#.. ##### ..#.. ..#.. .....",               /* + */
	"..... ..... ..... ..... ..... ..#.. ..#.. .#...",         /* , */
	"..... ..... ..... .###. ..... ..... .....",               /* - */
	"..... ..... ..... ..... ..... ..... ..#..",               /* . */
	"....# ....# ...#. ..#.. .#... #.... #....",               /* / */
	".###. #...# #..## #.#.# ##..# #...# .###.",               /* 0 */
	"..#.. .##.. ..#.. ..#.. ..#.. ..#.. .###.",               /* 1 */
	".###. #...# ....# ..##. .#... #.... #####",               /* 2 */
	"##### ...#. ..#.. ...#. ....# #...# .###.",               /* 3 */
	"...#. ..##. .#.#. #..#. ##### ...#. ...#.",               /* 4 */
	"##### #.... ####. ....# ....# #...# .###.",               /* 5 */
	"..##. .#... #.... ####. #...# #...# .###.",               /* 6 */
	"##### ....# ...#. ..#.. .#... .#... .#...",               /* 7 */
	".###. #...# #...# .###. #...# #...# .###.",               /* 8 */
	".###. #...# #...# .#### ....# ...#. .##..",               /* 9 */
	"..... ..#.. ..#.. ..... ..#.. ..#.. .....",               /* : */
	"..... ..#.. ..#.. ..... ..#.. ..#.. .#...",               /* ; */
	"...#. ..#.. .#... #.... .#... ..#.. ...#.",               /* < */
	"..... ..... ##### ..... ##### ..... .....",               /* = */
	".#... ..#.. ...#. ....# ...#. ..#.. .#...",               /* > */
	".###. #...# ....# ...#. ..#.. ..... ..#..",               /* ? */
	".###. #...# #.### #.#.# #.### #.... .####",               /* @ */
	".###. #...# #...# ##### #...# #...# #...#",               /* A */
	"####. #...# #...# ####. #...# #...# ####.",               /* B */
	".###. #...# #.... #.... #.... #...# .###.",               /* C */
	"####. #...# #...# #...# #...# #...# ####.",               /* D */
	"##### #.... #.... ####. #.... #.... #####",               /* E */
	"##### #.... #.... ####. #.... #.... #....",               /* F */
	".###. #...# #.... #.### #...# #...# .####",               /* G */
	"#...# #...# #...# ##### #...# #...# #...#",               /* H */
	".###. ..#.. ..#.. ..#.. ..#.. ..#.. .###.",               /* I */
	"..### ...#. ...#. ...#. ...#. #..#. .##..",               /* J */
	"#...# #..#. #.#.. ##... #.#.. #..#. #...#",               /* K */
	"#.... #.... #.... #.... #.... #.... #####",               /* L */
	"#...# ##.## #.#.# #.#.# #...# #...# #...#",               /* M */
	"#...# #...# ##..# #.#.# #..## #...# #...#",               /* N */
	".###. #...# #...# #...# #...# #...# .###.",               /* O */
	"####. #...# #...# ####. #.... #.... #....",               /* P */
	".###. #...# #...# #...# #.#.# #..#. .##.#",               /* Q */
	"####. #...# #...# ####. #.#.. #..#. #...#",               /* R */
	".#### #.... #.... .###. ....# ....# ####.",               /* S */
	"##### ..#.. ..#.. ..#.. ..#.. ..#.. ..#..",               /* T */
	"#...# #...# #...# #...# #...# #...# .###.",               /* U */
	"#...# #...# #...# #...# #...# .#.#. ..#..",               /* V */
	"#...# #...# #...# #.#.# #.#.# #.#.# .#.#.",               /* W */
	"#...# #...# .#.#. ..#.. .#.#. #...# #...#",               /* X */
	"#...# #...# .#.#. ..#.. ..#.. ..#.. ..#..",               /* Y */
	"##### ....# ...#. ..#.. .#... #.... #####",               /* Z */
	".###. .#... .#... .#... .#... .#... .###.",               /* [ */
	"#.... #.... .#... ..#.. ...#. ....# ....#",               /* \ */
	".###. ...#. ...#. ...#. ...#. ...#. .###.",               /* ] */
	"..#.. .#.#. #...# ..... ..... ..... .....",               /* ^ */
	"..... ..... ..... ..... ..... ..... #####",               /* _ */
	".#... ..#.. ..... ..... ..... ..... .....",               /* ` */
	"..... ..... .###. ....# .#### #...# .####",               /* a */
	"#.... #.... #.##. ##..# #...# #...# ####.",               /* b */
	"..... ..... .###. #.... #.... #...# .###.",               /* c */
	"....# ....# .##.# #..## #...# #...# .####",               /* d */
	"..... ..... .###. #...# ##### #.... .###.",               /* e */
	"..##. .#..# .#... ###.. .#... .#... .#...",               /* f */
	"..... ..... .#### #...# #...# .#### ....# .###.",         /* g */
	"#.... #.... #.##. ##..# #...# #...# #...#",               /* h */
	"..#.. ..... .##.. ..#.. ..#.. ..#.. .###.",               /* i */
	"...#. ..... ..##. ...#. ...#. ...#. #..#. .##..",         /* j */
	"#.... #.... #..#. #.#.. ##... #.#.. #..#.",               /* k */
	".##.. ..#.. ..#.. ..#.. ..#.. ..#.. .###.",               /* l */
	"..... ..... ##.#. #.#.# #.#.# #.#.# #.#.#",               /* m */
	"..... ..... #.##. ##..# #...# #...# #...#",               /* n */
	"..... ..... .###. #...# #...# #...# .###.",               /* o */
	"..... ..... ####. #...# #...# ####. #.... #....",         /* p */
	"..... ..... .#### #...# #...# .#### ....# ....#",         /* q */
	"..... ..... #.##. ##..# #.... #.... #....",               /* r */
	"..... ..... .###. #.... .###. ....# ####.",               /* s */
	".#... .#... ###.. .#... .#... .#..# ..##.",               /* t */
	"..... ..... #...# #...# #...# #..## .##.#",               /* u */
	"..... ..... #...# #...# #...# .#.#. ..#..",               /* v */
	"..... ..... #...# #...# #.#.# #.#.# .#.#.",               /* w */
	"..... ..... #...# .#.#. ..#.. .#.#. #...#",               /* x */
	"..... ..... #...# #...# #...# .#### ....# .###.",         /* y */
	"..... ..... ##### ...#. ..#.. .#... #####",               /* z */
	"...#. ..#.. ..#.. .#... ..#.. ..#.. ...#.",               /* { */
	"..#.. ..#.. ..#.. ..#.. ..#.. ..#.. ..#..",               /* | */
	".#... ..#.. ..#.. ...#. ..#.. ..#.. .#...",               /* } */
	"..... ..... .#... #.#.# ...#. ..... .....",               /* ~ */
};
static uint8_t font[95][8];     /* bit 4 = leftmost column */

static void font_init(void)
{
	for (int g = 0; g < 95; g++) {
		const char *s = font_src[g];
		for (int r = 0; r < 8 && *s; r++) {
			for (int c = 0; c < 5 && *s && *s != ' '; c++, s++)
				if (*s == '#') font[g][r] |= (uint8_t)(0x10 >> c);
			while (*s && *s != ' ') s++;
			while (*s == ' ') s++;
		}
	}
}

static inline void g_px(int x, int y, int c)
{
	if ((unsigned)x < (unsigned)lw && (unsigned)y < (unsigned)lh) pix[y * lw + x] = (uint8_t)c;
}

static void g_rect(int x, int y, int w, int h, int c)
{
	if (x < 0) { w += x; x = 0; }
	if (y < 0) { h += y; y = 0; }
	if (x + w > lw) w = lw - x;
	if (y + h > lh) h = lh - y;
	if (w <= 0 || h <= 0) return;
	for (int r = 0; r < h; r++) memset(pix + (size_t)(y + r) * (size_t)lw + (size_t)x, c, (size_t)w);
}

static void g_frame(int x, int y, int w, int h, int c)
{
	g_rect(x, y, w, 1, c); g_rect(x, y + h - 1, w, 1, c);
	g_rect(x, y, 1, h, c); g_rect(x + w - 1, y, 1, h, c);
}

/* One character at logical (x, y), each font pixel sc x sc, with a shadow. */
static void g_glyph(int x, int y, int ch, int c, int sc)
{
	const uint8_t *g;
	if (ch < 32 || ch > 126) ch = '?';
	g = font[ch - 32];
	for (int pass = 0; pass < 2; pass++)
		for (int r = 0; r < 8; r++)
			for (int k = 0; k < 5; k++)
				if (g[r] & (0x10 >> k)) {
					if (pass == 0) g_rect(x + k * sc + 1, y + r * sc + 1, sc, sc, C_SHADOW);
					else g_rect(x + k * sc, y + r * sc, sc, sc, c);
				}
}

static void g_text(int x, int y, const char *s, int c, int sc)
{
	for (; *s; s++, x += 6 * sc) g_glyph(x, y, (unsigned char)*s, c, sc);
}

/* A field cell's top left corner, in logical pixels. */
static int cell_x(int x) { return gx0 + x * CW; }
static int cell_y(int y) { return gy0 + y * CH; }

/*
 * Sprites: rows are the left half; the right half is its mirror image.
 * '.' is transparent, '1'..'6' pick a colour from the map passed in.
 */
typedef struct { int w, h; const char *r[24]; } Spr;

static void g_spr(const Spr *s, int x0, int y0, const uint8_t *map)
{
	int hw = s->w / 2;
	for (int r = 0; r < s->h; r++)
		for (int c = 0; c < hw && s->r[r][c]; c++) {
			char ch = s->r[r][c];
			if (ch < '1' || ch > '6') continue;
			g_px(x0 + c, y0 + r, map[ch - '1']);
			g_px(x0 + s->w - 1 - c, y0 + r, map[ch - '1']);
		}
}

/* ------------------------------------------------- console graphics mode */
/*
 * While the game draws on the framebuffer, the console (tty1) is switched
 * to KD_GRAPHICS so that fbcon does not draw over it. Every way out puts it
 * back to KD_TEXT: the normal exit, SIGTERM / SIGINT (main loop), every
 * other signal that would end the process (on_fatal, then the signal goes
 * on), and exit() (atexit). Only SIGKILL cannot be caught; for that init
 * runs `voidmoths --restore-tty /dev/tty1` after the game ends.
 */
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
	else ioctl(kd_fd, KDSETMODE, KD_TEXT);
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
	kd_on = 1;
	if (ioctl(tty, KDSETMODE, KD_GRAPHICS) < 0) {
		kd_on = 0;
		logmsg("graphics: KDSETMODE KD_GRAPHICS failed: %s (the console may draw over the game)", strerror(errno));
	}
}

static void fb_unpack(uint32_t v, uint8_t rgb[3])
{
	const struct fb_bitfield *f[3] = { &fb_r, &fb_g, &fb_b };
	for (int i = 0; i < 3; i++) {
		unsigned len = f[i]->length, x = (v >> f[i]->offset) & ((1u << len) - 1u);
		rgb[i] = (uint8_t)(len >= 8 ? x : (x << (8 - len)) | (2 * len > 8 ? x >> (2 * len - 8) : 0));
	}
}

static uint32_t fb_pack(const uint8_t rgb[3])
{
	const struct fb_bitfield *f[3] = { &fb_r, &fb_g, &fb_b };
	uint32_t v = 0;
	for (int i = 0; i < 3; i++) v |= ((uint32_t)rgb[i] >> (8 - f[i]->length)) << f[i]->offset;
	if (fb_t.length) v |= ((1u << fb_t.length) - 1u) << fb_t.offset;   /* opaque */
	return v;
}

static int fb_field_ok(const struct fb_bitfield *f, unsigned bpp, int need)
{
	if (!need && f->length == 0) return 1;
	return f->length >= 1 && f->length <= 8 && f->offset + f->length <= bpp && f->msb_right == 0;
}

static void set_size(int w, int h);

/* Take a framebuffer described by v and f, mapped at mem (len bytes). */
static int fb_setup(const struct fb_var_screeninfo *v, const struct fb_fix_screeninfo *f, uint8_t *mem, size_t len)
{
	unsigned bpp = v->bits_per_pixel;
	size_t need;
	if (bpp != 16 && bpp != 32) { logmsg("graphics: %u bits per pixel is not supported; text mode", bpp); return -1; }
	if (!fb_field_ok(&v->red, bpp, 1) || !fb_field_ok(&v->green, bpp, 1) || !fb_field_ok(&v->blue, bpp, 1) ||
	    !fb_field_ok(&v->transp, bpp, 0) || v->grayscale || v->nonstd) {
		logmsg("graphics: unusual pixel layout (r %u/%u g %u/%u b %u/%u); text mode",
		       v->red.offset, v->red.length, v->green.offset, v->green.length, v->blue.offset, v->blue.length);
		return -1;
	}
	if (v->xres < 64 || v->yres < 64 || v->xres > 8192 || v->yres > 8192 || f->line_length < v->xres * (bpp / 8)) {
		logmsg("graphics: odd size %ux%u, %u bytes a line; text mode", v->xres, v->yres, f->line_length);
		return -1;
	}
	need = (size_t)(v->yoffset + v->yres - 1) * f->line_length + (size_t)(v->xoffset + v->xres) * (bpp / 8);
	if (need > len) { logmsg("graphics: mapping too small (%zu < %zu bytes); text mode", len, need); return -1; }
	gs = (int)(v->xres / (FW_MAX * CW));
	if ((int)(v->yres / (FH_MAX * CH)) < gs) gs = (int)(v->yres / (FH_MAX * CH));
	if (gs < 1) gs = 1;
	lw = (int)v->xres / gs; lh = (int)v->yres / gs;
	if (lw / CW < 20 || lh / CH < 16) {
		logmsg("graphics: %ux%u is too small for the field; text mode", v->xres, v->yres);
		return -1;
	}
	fb_xres = v->xres; fb_yres = v->yres; fb_line = f->line_length; fb_Bpp = bpp / 8;
	fb_r = v->red; fb_g = v->green; fb_b = v->blue; fb_t = v->transp;
	fb_map = mem; fb_maplen = len;
	fb_base = mem + (size_t)v->yoffset * fb_line + (size_t)v->xoffset * fb_Bpp;
	pix = calloc((size_t)lw * (size_t)lh, 1);
	pix_old = calloc((size_t)lw * (size_t)lh, 1);
	fb_row = calloc((size_t)lw * (size_t)gs, fb_Bpp);
	if (!pix || !pix_old || !fb_row) { logmsg("graphics: out of memory; text mode"); return -1; }
	for (int i = 0; i < C_NPAL; i++) pal_px[i] = fb_pack(pal_rgb[i]);
	/* the whole visible area to the background, borders included */
	for (unsigned y = 0; y < fb_yres; y++) {
		uint8_t *row = fb_base + (size_t)y * fb_line;
		for (unsigned x = 0; x < fb_xres; x++) {
			if (fb_Bpp == 4) { uint32_t p = pal_px[C_BG]; memcpy(row + 4 * x, &p, 4); }
			else { uint16_t p = (uint16_t)pal_px[C_BG]; memcpy(row + 2 * x, &p, 2); }
		}
	}
	gfx = 1;
	fb_full = 1;
	set_size(lw / CW, lh / CH);
	logmsg("graphics: %ux%u, %u bpp (r %u/%u g %u/%u b %u/%u), %u bytes a line; %dx%d logical pixels, x%d",
	       fb_xres, fb_yres, bpp, fb_r.offset, fb_r.length, fb_g.offset, fb_g.length, fb_b.offset, fb_b.length,
	       fb_line, lw, lh, gs);
	return 0;
}

static void fb_close(void)
{
	if (fb_map && !fb_fake) munmap(fb_map, fb_maplen);
	if (fb_fd >= 0) close(fb_fd);
	fb_map = fb_base = NULL;
	fb_fd = -1;
}

static int fb_open(const char *path)
{
	struct fb_var_screeninfo v;
	struct fb_fix_screeninfo f;
	size_t len;
	void *m;
	int fd = open(path, O_RDWR | O_CLOEXEC);
	if (fd < 0) { logmsg("graphics: %s: %s; text mode", path, strerror(errno)); return -1; }
	memset(&v, 0, sizeof v);
	memset(&f, 0, sizeof f);
	if (ioctl(fd, FBIOGET_VSCREENINFO, &v) < 0 || ioctl(fd, FBIOGET_FSCREENINFO, &f) < 0) {
		logmsg("graphics: %s: screen info: %s; text mode", path, strerror(errno));
		close(fd);
		return -1;
	}
	if (f.type != FB_TYPE_PACKED_PIXELS || f.visual != FB_VISUAL_TRUECOLOR) {
		logmsg("graphics: %s: not packed true colour (type %u, visual %u); text mode", path, f.type, f.visual);
		close(fd);
		return -1;
	}
	len = f.smem_len ? f.smem_len : (size_t)f.line_length * v.yres_virtual;
	m = mmap(NULL, len, PROT_READ | PROT_WRITE, MAP_SHARED, fd, 0);
	if (m == MAP_FAILED) {
		logmsg("graphics: %s: mmap of %zu bytes: %s; text mode", path, len, strerror(errno));
		close(fd);
		return -1;
	}
	fb_fd = fd;
	if (fb_setup(&v, &f, m, len) < 0) {
		fb_close();
		return -1;
	}
	return 0;
}

/* The self-test's framebuffer: memory, described like the real one. Each line
 * has 64 bytes of padding past the picture, filled with 0xA5: present() must
 * honour line_length and never touch it. */
static int fb_open_fake(const char *spec)
{
	struct fb_var_screeninfo v;
	struct fb_fix_screeninfo f;
	unsigned w, h, bpp;
	char order[8] = "rgb";
	uint8_t *m;
	if (sscanf(spec, "%ux%ux%u:%7s", &w, &h, &bpp, order) < 3 || w > 8192 || h > 8192) return -1;
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
	m = malloc(f.smem_len);
	if (!m) return -1;
	memset(m, 0xa5, f.smem_len);
	fb_fake = 1;
	if (fb_setup(&v, &f, m, f.smem_len) < 0) { free(m); fb_fake = 0; return -1; }
	return 0;
}

/* Send the logical pixels that changed. */
static void fb_present(void)
{
	const int S = gs;
	for (int ly = 0; ly < lh; ly++) {
		uint8_t *cur = pix + (size_t)ly * (size_t)lw, *prev = pix_old + (size_t)ly * (size_t)lw;
		int x = 0;
		if (!fb_full && memcmp(cur, prev, (size_t)lw) == 0) continue;
		while (x < lw) {
			int x1, n;
			if (!fb_full)
				while (x < lw && !memcmp(cur + x, prev + x, (size_t)(lw - x < 8 ? lw - x : 8))) x += 8;
			if (x >= lw) break;
			x1 = x;
			if (fb_full) x1 = lw;
			else while (x1 < lw && memcmp(cur + x1, prev + x1, (size_t)(lw - x1 < 8 ? lw - x1 : 8))) x1 += 8;
			if (x1 > lw) x1 = lw;
			n = (x1 - x) * S;
			if (fb_Bpp == 4) {
				uint32_t *o = (uint32_t *)(void *)fb_row;
				for (int i = x; i < x1; i++) {
					uint32_t p = pal_px[cur[i]];
					for (int k = 0; k < S; k++) *o++ = p;
				}
			} else {
				uint16_t *o = (uint16_t *)(void *)fb_row;
				for (int i = x; i < x1; i++) {
					uint16_t p = (uint16_t)pal_px[cur[i]];
					for (int k = 0; k < S; k++) *o++ = p;
				}
			}
			for (int k = 0; k < S; k++)
				memcpy(fb_base + (size_t)(ly * S + k) * fb_line + (size_t)(x * S) * fb_Bpp, fb_row,
				       (size_t)n * fb_Bpp);
			fb_bytes += (long long)n * S * (long long)fb_Bpp;
			x = x1;
		}
		memcpy(prev, cur, (size_t)lw);
	}
	fb_full = 0;
}

/* The self-test: the fake framebuffer, read back through its bitfields, as PPM. */
static void fb_dump(const char *dir, long t)
{
	char p[300];
	uint8_t *row = malloc((size_t)fb_xres * 3);
	FILE *f;
	snprintf(p, sizeof p, "%s/frame-%06ld.ppm", dir, t);
	f = fopen(p, "wb");
	if (!f || !row) { perror(p); free(row); if (f) fclose(f); return; }
	fprintf(f, "P6\n%u %u\n255\n", fb_xres, fb_yres);
	for (unsigned y = 0; y < fb_yres; y++) {
		const uint8_t *src = fb_base + (size_t)y * fb_line;
		for (unsigned x = 0; x < fb_xres; x++) {
			uint32_t v = 0;
			if (fb_Bpp == 4) memcpy(&v, src + 4 * x, 4);
			else { uint16_t s; memcpy(&s, src + 2 * x, 2); v = s; }
			fb_unpack(v, row + 3 * x);
		}
		fwrite(row, 3, fb_xres, f);
	}
	fclose(f);
	free(row);
}

/* The self-test: the padding past each line must still be 0xA5. */
static int fb_padding_ok(void)
{
	for (unsigned y = 0; y < fb_yres; y++)
		for (unsigned x = fb_xres * fb_Bpp; x < fb_line; x++)
			if (fb_base[(size_t)y * fb_line + x] != 0xa5) return 0;
	return 1;
}

/* ------------------------------------------------------------ text cells */

static void set_size(int w, int h)
{
	if (w > TW_MAX) w = TW_MAX;
	if (h > TH_MAX) h = TH_MAX;
	if (w < 1) w = 1;
	if (h < 1) h = 1;
	if (w == tw && h == th) return;
	tw = w; th = h;
	fw = tw < FW_MAX ? tw : FW_MAX;
	fh = th < FH_MAX ? th : FH_MAX;
	fx0 = (tw - fw) / 2;
	fy0 = (th - fh) / 2;
	gx0 = (lw - fw * CW) / 2;
	gy0 = (lh - fh * CH) / 2;
	full_redraw = 1;
}

static void update_size(void)
{
	struct winsize ws;
	if (tty_fd >= 0 && ioctl(tty_fd, TIOCGWINSZ, &ws) == 0 && ws.ws_col && ws.ws_row)
		set_size(ws.ws_col, ws.ws_row);
}

static void clear_cells(void)
{
	for (int y = 0; y < th; y++)
		for (int x = 0; x < tw; x++)
			scr[y][x] = (Cell){ ' ', WHT, BLK, 0 };
}

/* A character in a field cell: text mode, or a glyph in pixel mode. */
static void put_bg(int x, int y, char ch, int fg, int bg, int bold)
{
	if (x < 0 || y < 0 || x >= fw || y >= fh) return;
	if (gfx) {
		if (ch != ' ') g_glyph(cell_x(x), cell_y(y) + 2, (unsigned char)ch, tcol(fg, bold), 1);
		return;
	}
	x += fx0; y += fy0;
	if (x >= tw || y >= th) return;
	scr[y][x] = (Cell){ ch, (uint8_t)fg, (uint8_t)bg, (uint8_t)bold };
}

static void put(int x, int y, char ch, int fg, int bold) { put_bg(x, y, ch, fg, BLK, bold); }

/* A solid cell: a coloured space in text mode, a shaded tile in pixel mode. */
static void block(int x, int y, int bg)
{
	if (x < 0 || y < 0 || x >= fw || y >= fh) return;
	if (gfx) {
		int px0 = cell_x(x), py0 = cell_y(y), t = C_TILE + 3 * (bg & 7);
		g_rect(px0, py0, CW, CH - 1, t + 1);
		g_rect(px0, py0, CW, 3, t);
		g_rect(px0, py0 + CH - 4, CW, 3, t + 2);
		return;
	}
	x += fx0; y += fy0;
	if (x >= tw || y >= th) return;
	scr[y][x] = (Cell){ ' ', WHT, (uint8_t)bg, 0 };
}

static void text(int x, int y, const char *s, int fg, int bold)
{
	for (; *s; s++, x++) put(x, y, *s, fg, bold);
}

static void ctext(int y, const char *s, int fg, int bold)
{
	text((fw - (int)strlen(s)) / 2, y, s, fg, bold);
}

/* A 3-cell sprite; blanks are transparent. */
static void sprite(float fx, int y, const char *s, int fg, int bold)
{
	int x = (int)lroundf(fx) - 1;
	for (int k = 0; s[k]; k++)
		if (s[k] != ' ') put(x + k, y, s[k], fg, bold);
}

/* Send the cells (or pixels) that changed since the last frame. */
static void present(void)
{
	char tmp[32];
	int cx = -1, cy = -1, af = -1, ab = -1, abold = -1;
	if (gfx) { fb_present(); return; }
	if (tty_fd < 0) return;
	if (full_redraw) outs("\033[0m\033[?25l\033[?7l");
	for (int y = 0; y < th; y++) {
		for (int x = 0; x < tw; x++) {
			Cell *c = &scr[y][x], *o = &old[y][x];
			if (y == th - 1 && x == tw - 1) continue;   /* never risk a scroll */
			if (!full_redraw && memcmp(c, o, sizeof *c) == 0) continue;
			if (cx != x || cy != y) {
				int n = snprintf(tmp, sizeof tmp, "\033[%d;%dH", y + 1, x + 1);
				out(tmp, (size_t)n);
			}
			if (c->fg != af || c->bg != ab || c->bold != abold) {
				int n = snprintf(tmp, sizeof tmp, "\033[0;%s3%d;4%dm",
						 c->bold ? "1;" : "", c->fg, c->bg);
				out(tmp, (size_t)n);
				af = c->fg; ab = c->bg; abold = c->bold;
			}
			out(&c->ch, 1);
			*o = *c;
			cx = x + 1; cy = y;
			if (cx >= tw) cx = -1;
		}
	}
	full_redraw = 0;
	flush_out();
}

/* ------------------------------------------------------------------- input */

#define NLONG ((KEY_CNT + 8 * (int)sizeof(long) - 1) / (8 * (int)sizeof(long)))
static int dev_fd[MAXDEV], ndev;
static char dev_desc[96];
static uint8_t keydown[KEY_CNT];
static int fire_presses, left_taps, right_taps;
static int other_fire_presses;          /* fire keys other than the quit key */
static int qk_presses, qk_releases;     /* the quit key: Home, or Power without Home */
static int esc_presses, m_presses;
static int have_home, have_power, have_vol, grab = 1, swap_lr, rescan;
static int bot, bot_l, bot_r, bot_f, bot_q, bot_both;
static int pilot = 1;                   /* the self-test's autopilot */
static int t_corner, t_tap;             /* touch: corner held last tick; taps this tick */
static long st_ttaps, st_tdrag;

static int test_bit(const unsigned long *b, int n)
{
	return (int)((b[n / (8 * (int)sizeof(long))] >> (n % (8 * (int)sizeof(long)))) & 1UL);
}

static int key_left(int c)  { return c == (swap_lr ? KEY_VOLUMEDOWN : KEY_VOLUMEUP) || c == KEY_LEFT; }
static int key_right(int c) { return c == (swap_lr ? KEY_VOLUMEUP : KEY_VOLUMEDOWN) || c == KEY_RIGHT; }
static int key_fire(int c)
{
	return c == KEY_HOMEPAGE || c == KEY_HOME || c == KEY_POWER ||
	       c == KEY_SPACE || c == KEY_ENTER || c == KEY_UP;
}
/* The key that, held, goes back to the title: Home; Power if there is no Home. */
static int key_quit(int c)
{
	return have_home ? (c == KEY_HOMEPAGE || c == KEY_HOME) : c == KEY_POWER;
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
	char first[64] = "";
	for (int i = 0; i < ndev; i++) close(dev_fd[i]);
	ndev = 0;
	have_home = have_power = have_vol = 0;
	for (int i = 0; i < 32 && ndev < MAXDEV; i++) {
		char p[32], name[48] = "?";
		unsigned long bits[NLONG];
		snprintf(p, sizeof p, "/dev/input/event%d", i);
		int fd = open(p, O_RDONLY | O_NONBLOCK | O_CLOEXEC);
		if (fd < 0) continue;
		memset(bits, 0, sizeof bits);
		if (ioctl(fd, EVIOCGBIT(EV_KEY, sizeof bits), bits) < 0) { close(fd); continue; }
		int h  = test_bit(bits, KEY_HOMEPAGE) || test_bit(bits, KEY_HOME);
		int pw = test_bit(bits, KEY_POWER);
		int v  = test_bit(bits, KEY_VOLUMEUP) || test_bit(bits, KEY_VOLUMEDOWN);
		int kb = test_bit(bits, KEY_SPACE) && test_bit(bits, KEY_LEFT);
		if (!h && !pw && !v && !kb) { close(fd); continue; }
		have_home |= h; have_power |= pw; have_vol |= v;
		/* Exclusive: keeps the VT keyboard handler from turning the
		 * buttons into console input. */
		if (grab) ioctl(fd, EVIOCGRAB, 1);
		ioctl(fd, EVIOCGNAME(sizeof name - 1), name);
		name[sizeof name - 1] = 0;
		if (!first[0]) snprintf(first, sizeof first, "%.40s (event%d)", name, i);
		dev_fd[ndev++] = fd;
	}
	if (ndev == 0) snprintf(dev_desc, sizeof dev_desc, "no buttons found - still looking");
	else if (ndev == 1) snprintf(dev_desc, sizeof dev_desc, "%s", first);
	else snprintf(dev_desc, sizeof dev_desc, "%s +%d", first, ndev - 1);
	sync_keys();
	rescan = 0;
	fakekeys_have(&have_home, &have_power, &have_vol);      /* test builds: $FAKE_KEYS */
	bl_set_keys(have_home, have_power, have_vol);
}

static void key_press(int code)
{
	keydown[code] = 1;
	if (key_fire(code)) {
		fire_presses++;
		if (!key_quit(code)) other_fire_presses++;
	}
	if (key_quit(code)) qk_presses++;
	if (key_left(code)) left_taps++;
	if (key_right(code)) right_taps++;
	if (code == KEY_ESC) esc_presses++;
	if (code == KEY_M) m_presses++;
}

static void key_release(int code)
{
	keydown[code] = 0;
	if (key_quit(code)) qk_releases++;
}

/* One key event (value 0 or 1): the screen-off keys first (backlight.h). */
static void key_event(int code, int value)
{
	if (code < 0 || code >= KEY_CNT || value < 0 || value > 1) return;
	switch (bl_key(code, value)) {
	case BL_PASS: if (value) key_press(code); else key_release(code); break;
	case BL_STATE: keydown[code] = 0; break;
	case BL_TAP: key_press(code); key_release(code); break;
	default: break;
	}
}

static void read_events(void)
{
	struct input_event ev[64];
	int dropped = 0;
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

static int held_left(void)
{
	return bot_l || keydown[swap_lr ? KEY_VOLUMEDOWN : KEY_VOLUMEUP] || keydown[KEY_LEFT];
}
static int held_right(void)
{
	return bot_r || keydown[swap_lr ? KEY_VOLUMEUP : KEY_VOLUMEDOWN] || keydown[KEY_RIGHT];
}
static int held_fire(void)
{
	return bot_f || bot_q || keydown[KEY_HOMEPAGE] || keydown[KEY_HOME] || keydown[KEY_POWER] ||
	       keydown[KEY_SPACE] || keydown[KEY_ENTER] || keydown[KEY_UP];
}
static int held_quit(void)
{
	return bot_q || touch_corner_down() ||
	       (have_home ? keydown[KEY_HOMEPAGE] || keydown[KEY_HOME] : keydown[KEY_POWER]);
}
static int held_both_vol(void)
{
	return bot_both || (keydown[KEY_VOLUMEUP] && keydown[KEY_VOLUMEDOWN]);
}

/* ------------------------------------------------------------------- state */

static uint32_t rs = 2463534242u;
static uint32_t rnd(void) { rs ^= rs << 13; rs ^= rs >> 17; rs ^= rs << 5; return rs; }
static float frand(void) { return (float)(rnd() & 0xffffff) / 16777216.0f; }

static long tick;
static int state, state_t;
static long score, hi, next_extra;
static int lives, wave, wave_phase, phase_t, wave_t, deaths_wave;
static float px;
static int p_alive, p_dead_t, p_inv, fire_cd, fire_pending;
static int dive_timer, ffire_timer, form_w, spacing;
static float form_sway;
static Enemy en[MAXE];
static int ne;
static Shot ps[MAXPS], es[MAXES];
static Boom bm[MAXB];
static Popup pp[MAXP];
static Star st[NSTAR];
static char banner_txt[32];
static int banner_t;
static const char *hi_path = "/run/voidmoths.hi";
static long st_games, st_kills, st_maxwave, st_deaths;
static long st_quits, st_bars, st_toggles, st_bad, st_swoops, st_notices;
static int qk_t, qk_latch;          /* quit-key hold, in ticks; latch: wait for release */
static int paused;                  /* the screen was dark: play waits for a press */
static int from_picker;             /* started by arcade: a hold on the title goes back there */
static volatile sig_atomic_t quit_flag;
static int both_latch, snd_flash;

static int64_t now_ns(void)
{
	struct timespec ts;
	clock_gettime(CLOCK_MONOTONIC, &ts);
	return (int64_t)ts.tv_sec * 1000000000LL + ts.tv_nsec;
}

/* One line to stderr (/run/voidmoths.log on the phone), with seconds since start. */
static int64_t t_start;
__attribute__((format(printf, 1, 2)))
static void logmsg(const char *fmt, ...)
{
	va_list ap;
	fprintf(stderr, "voidmoths %7.1f: ", (double)(now_ns() - t_start) / 1e9);
	va_start(ap, fmt);
	vfprintf(stderr, fmt, ap);
	va_end(ap);
	fputc('\n', stderr);
	fflush(stderr);
}

/* The program's clock in ms (touch.h, backlight.h): real time, or the
 * self-test's ticks. */
static int64_t vm_clock_ms(void) { return bot ? (int64_t)tick * 1000 / HZ : now_ns() / 1000000; }

/* ------------------------------------------------------------------- sound */
/*
 * Retro effects on the loudspeaker. The phone's speaker is the ALSA card
 * "iPhone Speaker" (id "Speaker"): MCA2 in TDM mode, CS35L19/21 amplifier,
 * 48 kHz, S16_LE, mono. The kernel interface is used directly, as tinyalsa
 * does: HW_PARAMS, SW_PARAMS, PREPARE, WRITEI_FRAMES on pcmC<n>D0p, and the
 * control ioctls on controlC<n> for "Speaker Volume" only.
 *
 * Safety: no other card is opened (the card is found by its id and checked
 * again with CARD_INFO before anything is written); the only control written
 * is the amp's "Speaker Volume", once per open, to -20 dB by default and
 * never above -6 dB; the mix is hard-limited to PEAK of full scale. The MCA
 * driver itself divides every sample by 4 (-12 dB) on top of that.
 *
 * Never blocking: the PCM is opened O_NONBLOCK, the main loop polls it for
 * room and writes what fits. Underruns are recovered with PREPARE, silently.
 * Any other failure closes the device and the game runs silent.
 */
#define SR          48000
#define NVOICE      16
#define SND_CHUNK   256             /* frames rendered at a time */
#define SND_WAIT    (15 * HZ)       /* how long to look for the card */
#define VOL_DB      -20.0f          /* amp volume */
#define VOL_MAX_DB  -6.0f           /* never above this */
#define PEAK        0.5f            /* mix limit, of full scale (-6 dBFS) */

#ifdef VM_FAKE_ALSA
/* Test build: a fake ALSA device (src/fake_alsa.c) answers the ioctls. */
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
static int snd_state = SND_SEARCH, snd_card = -1, snd_pcm = -1, snd_poll_ok;
static long snd_deadline, snd_next_try, snd_underruns;
static unsigned long snd_period, snd_bufsize;
static float snd_vol_db = VOL_DB;
static int16_t snd_buf[SND_CHUNK];
static int snd_len, snd_off;        /* rendered frames not written yet */
static float hp_x, hp_y;            /* DC blocker */
static const char *snd_root = "";   /* path prefix, for the test build only */

static int snd_live(void) { return snd_state == SND_ON || bot; }

/*
 * The dive swoop: a voice of its own, so that it never takes a slot from
 * the effects and never stacks: a triangle wave, quiet (SWOOP_VOL, about
 * -21 dBFS at its peak) with a fast wing-beat tremolo, whose pitch follows
 * the moth that started diving last, from SWOOP_HI where it left the
 * formation down to SWOOP_LO at the bottom of the screen. A new dive takes
 * the voice over (the pitch glides back up); when its moth is shot, returns
 * or leaves the screen, it fades out in 150 ms.
 */
#define SWOOP_VOL   0.09f
#define SWOOP_HI    1250.0f
#define SWOOP_LO    380.0f
static struct {
	int on, rel, target;
	float f, ftgt, env, phase, trem, y0;
} swp;

static void voice(int wf, float f0, float f1, float ms, float vol, float duty,
		  int curve, float delay_ms)
{
	Voice *v = NULL;
	int len = (int)(ms * (float)SR / 1000.0f);
	if (!snd_live()) return;
	for (int i = 0; i < NVOICE && !v; i++)
		if (!vc[i].on) v = &vc[i];
	if (!v) {
		/* all busy: take the one closest to its end */
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

static void note(float f, float start_ms, float ms, float vol, float duty)
{
	voice(W_SQUARE, f, f * 0.995f, ms, vol, duty, 1, start_ms);
}

/* The effects. Frequencies stay above ~300 Hz: the phone speaker is tiny. */
/* A notice: two soft triangle notes. */
static void sfx_notice(void)
{
	voice(W_TRI, 1175.0f, 1175.0f, 150.0f, 0.16f, 0.0f, 1, 0.0f);
	voice(W_TRI, 1568.0f, 1568.0f, 340.0f, 0.16f, 0.0f, 1, 140.0f);
}
static void sfx_shot(void)  { voice(W_SQUARE, 1800.0f, 600.0f, 70.0f, 0.16f, 0.25f, 1, 0.0f); }
static void sfx_hit(void)   { voice(W_SQUARE, 520.0f, 820.0f, 60.0f, 0.22f, 0.5f, 1, 0.0f); }
static void sfx_kill(int big)
{
	voice(W_NOISE, 9000.0f, 1500.0f, big ? 340.0f : 170.0f, big ? 0.28f : 0.22f, 0.0f, 2, 0.0f);
	voice(W_SQUARE, 700.0f, 180.0f, big ? 120.0f : 60.0f, 0.12f, 0.5f, 1, 0.0f);
}
static void sfx_death(void)
{
	voice(W_NOISE, 6000.0f, 300.0f, 1100.0f, 0.3f, 0.0f, 2, 0.0f);
	voice(W_SQUARE, 660.0f, 60.0f, 850.0f, 0.14f, 0.5f, 1, 0.0f);
}
static void sfx_extra(void)
{
	static const float f[5] = { 1047.0f, 1319.0f, 1568.0f, 2093.0f, 2637.0f };
	for (int i = 0; i < 5; i++) note(f[i], 65.0f * (float)i, i == 4 ? 180.0f : 60.0f, 0.18f, 0.5f);
}
static void sfx_wave(void)
{
	static const float f[4] = { 784.0f, 1047.0f, 1319.0f, 1568.0f };
	for (int i = 0; i < 4; i++) note(f[i], 90.0f * (float)i, i == 3 ? 260.0f : 80.0f, 0.18f, 0.25f);
}
static void sfx_gameover(void)
{
	static const float f[4] = { 659.0f, 523.0f, 440.0f, 330.0f };
	for (int i = 0; i < 4; i++)
		voice(W_SQUARE, f[i], f[i] * 0.94f, i == 3 ? 520.0f : 170.0f, 0.2f, 0.5f, 1, 190.0f * (float)i);
}
/* "the swarm wants your light": a short minor tune over a soft square bass. */
static void sfx_title(void)
{
	static const struct { float f, at, ms; } mel[] = {
		{ 659.0f, 0.0f, 110.0f }, { 784.0f, 125.0f, 110.0f }, { 880.0f, 250.0f, 110.0f },
		{ 1047.0f, 375.0f, 230.0f }, { 988.0f, 625.0f, 110.0f }, { 880.0f, 750.0f, 110.0f },
		{ 1319.0f, 875.0f, 420.0f },
	}, bass[] = {
		{ 440.0f, 0.0f, 360.0f }, { 349.0f, 375.0f, 360.0f }, { 330.0f, 750.0f, 560.0f },
	};
	for (size_t i = 0; i < sizeof mel / sizeof mel[0]; i++) note(mel[i].f, mel[i].at, mel[i].ms, 0.18f, 0.25f);
	for (size_t i = 0; i < sizeof bass / sizeof bass[0]; i++) note(bass[i].f, bass[i].at, bass[i].ms, 0.1f, 0.5f);
}

/* Mix the voices into n frames of S16 mono. */
static void synth(int16_t *out, int n)
{
	for (int k = 0; k < n; k++) {
		float mix = 0.0f, y;
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
				/* 15-bit LFSR, clocked at freq: sample-and-hold noise */
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
				else s = 2.0f * ((v->phase < v->duty ? 1.0f : 0.0f) - v->duty);   /* zero mean */
			}
			mix += s * env * v->vol;
			v->freq *= v->fmul;
			if (++v->pos >= v->len) v->on = 0;
		}
		if (swp.on) {
			float a;
			swp.f += (swp.ftgt - swp.f) * 0.0006f;          /* ~35 ms glide */
			if (swp.rel) {
				swp.env -= 1.0f / (0.15f * (float)SR);
				if (swp.env <= 0.0f) { swp.env = 0.0f; swp.on = 0; }
			} else if (swp.env < 1.0f) {
				swp.env += 1.0f / (0.04f * (float)SR);
				if (swp.env > 1.0f) swp.env = 1.0f;
			}
			swp.phase += swp.f / (float)SR;
			if (swp.phase >= 1.0f) swp.phase -= 1.0f;
			swp.trem += 13.0f / (float)SR;                /* wing beats */
			if (swp.trem >= 1.0f) swp.trem -= 1.0f;
			a = SWOOP_VOL * swp.env * (0.7f + 0.3f * (4.0f * fabsf(swp.trem - 0.5f) - 1.0f));
			mix += (4.0f * fabsf(swp.phase - 0.5f) - 1.0f) * a;
		}
		/* DC blocker (~20 Hz high-pass), then the hard limit */
		y = mix - hp_x + 0.9974f * hp_y;
		hp_x = mix; hp_y = y;
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
		const char *sn;
		int n, l;
		if (sscanf(line, " %d [%31[^]]]", &n, id) != 2 || n < 0 || n > 31) continue;
		for (l = (int)strlen(id); l > 0 && id[l - 1] == ' '; l--) id[l - 1] = 0;
		sn = strstr(line, "]: ");
		sn = sn ? strstr(sn, " - ") : NULL;
		if (!strcmp(id, "Speaker") || (sn && !strncmp(sn + 3, "iPhone Speaker", 14))) found = n;
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
	memset(&swp, 0, sizeof swp);
}

static void snd_fail(const char *what, int err)
{
	logmsg("sound: %s failed: %s; running silent", what, err ? strerror(err) : "unexpected answer");
	snd_close();
	snd_state = SND_FAIL;
}

static int64_t tlv_cdb(const unsigned *tlv, long vmin, long vmax, long v)
{
	/* centi-dB of control value v */
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
	/* 43 ms of buffer: about one game tick of headroom. Fallbacks in order. */
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
	snd_poll_ok = 1;
	logmsg("sound: on, card %d, %lu-frame periods, %lu-frame buffer (%lu ms)",
	       snd_card, snd_period, snd_bufsize, snd_bufsize * 1000 / SR);
	return 0;
}

/* Write what fits; never waits. Returns 1 if anything was written. */
static int snd_pump(void)
{
	int wrote = 0, xruns = 0;
	if (snd_state != SND_ON) return 0;
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
				/* underrun (or suspend): start over, silently */
				snd_underruns++;
				if (err == ESTRPIPE && xioctl(snd_pcm, SNDRV_PCM_IOCTL_RESUME, NULL) == 0) continue;
				if (err == ESTRPIPE && errno == EAGAIN) break;
				if (xioctl(snd_pcm, SNDRV_PCM_IOCTL_PREPARE, NULL) == 0) continue;
				err = errno;
			}
			snd_fail("writing to the PCM", err);
			return 0;
		}
		if (x.result <= 0 || x.result > snd_len) break;
		snd_off += (int)x.result;
		snd_len -= (int)x.result;
		wrote = 1;
	}
	return wrote;
}

static void snd_start_search(void)
{
	snd_state = SND_SEARCH;
	snd_deadline = tick + SND_WAIT;
	snd_next_try = tick;
}

/* Once per game tick: look for the card, then keep the buffer full. */
static void snd_tick(void)
{
	if (snd_state == SND_SEARCH && tick >= snd_next_try) {
		snd_next_try = tick + HZ;
		snd_card = snd_find_card();
		if (snd_card >= 0) {
			if (snd_open() == 0 && state == S_TITLE) sfx_title();
		} else if (tick >= snd_deadline) {
			snd_state = SND_NONE;
			logmsg("sound: no 'Speaker' card in %s/proc/asound/cards after %d s; running silent",
			       snd_root, SND_WAIT / HZ);
		}
	}
	if (snd_state == SND_ON) {
		snd_poll_ok = 1;
		snd_pump();
	} else if (bot) {
		/* the self-test exercises the mixer even without a device */
		int16_t scratch[SR / HZ];
		synth(scratch, SR / HZ);
	}
}

static void snd_toggle(void)
{
	st_toggles++;
	snd_flash = HZ;
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

static const char *snd_status(int narrow)
{
	switch (snd_state) {
	case SND_ON:     return narrow ? "SOUND ON" : "SOUND ON    (VOL+ & VOL-: OFF)";
	case SND_OFF:    return narrow ? "SOUND OFF" : "SOUND OFF   (VOL+ & VOL-: ON)";
	case SND_SEARCH: return narrow ? "SOUND..." : "SOUND: LOOKING FOR THE SPEAKER";
	case SND_NONE:   return narrow ? "NO SPEAKER" : "NO SPEAKER  (VOL+ & VOL-: RETRY)";
	default:         return narrow ? "SOUND ERROR" : "SOUND ERROR (/run/voidmoths.log)";
	}
}

/* ----------------------------------------------------------------- notices */
/*
 * Notices from the Mac, with no network: `orchard exec -- notice TEXT` runs
 * /usr/bin/notice on the phone, which writes /run/notice (a temporary file
 * renamed over it). Twice a second the game stat()s that file; when its
 * mtime, inode or size changes, the first line, cleaned to printable ASCII
 * and at most NOTE_LEN characters, is queued. A notice stays on screen for
 * 6 s (a panel at the top, one or two lines) and comes with a soft two-note
 * chime when the sound is on. Up to NOTE_WAIT notices wait behind the one on
 * screen; when one more arrives, the oldest waiting one is dropped. Notices
 * written less than half a second apart can merge: only the last is seen.
 * A file that is already there when the game starts is shown only if it is
 * less than 30 s old (the game respawns; old news stays old).
 */
#define NOTE_LEN    60
#define NOTE_WAIT   3
#define NOTE_TICKS  (6 * HZ)
#define NOTE_POLL   (HZ / 2)
static const char *notice_path = "/run/notice";
static char note_q[NOTE_WAIT][NOTE_LEN + 1], note_cur[NOTE_LEN + 1];
static int note_n, note_t;
static struct { int seen; long long sec, nsec, size; unsigned long long ino; } note_st;

/* First line of in[0..n) as printable ASCII, spaces collapsed, at most NOTE_LEN. */
static void notice_clean(const char *in, size_t n, char out[NOTE_LEN + 1])
{
	static const struct { const char *u, *a; } map[] = {
		{ "\xe2\x80\x94", "-" }, { "\xe2\x80\x93", "-" }, { "\xe2\x80\x98", "'" }, { "\xe2\x80\x99", "'" },
		{ "\xe2\x80\x9c", "\"" }, { "\xe2\x80\x9d", "\"" }, { "\xe2\x80\xa6", "..." }, { "\xe2\x86\x92", "->" },
		{ "\xe2\x9c\x93", "ok" }, { "\xe2\x9c\x94", "ok" }, { "\xc2\xa0", " " }, { "\xc2\xb7", "-" },
	};
	char buf[NOTE_LEN * 2 + 8];
	size_t o = 0, w = 0;
	for (size_t i = 0; i < n && in[i] != '\n' && in[i] != '\r' && in[i] && o < sizeof buf - 4;) {
		unsigned char c = (unsigned char)in[i];
		if (c >= 0x80) {
			/* one UTF-8 sequence: a known punctuation mark, else '?' */
			const char *a = c >= 0xc0 ? "?" : "";
			size_t len = 1;
			while (len < 4 && i + len < n && ((unsigned char)in[i + len] & 0xc0) == 0x80) len++;
			for (size_t k = 0; c >= 0xc0 && k < sizeof map / sizeof map[0]; k++)
				if (strlen(map[k].u) == len && !memcmp(in + i, map[k].u, len)) a = map[k].a;
			for (; *a && o < sizeof buf - 4; a++) buf[o++] = *a;
			i += len;
			continue;
		}
		if (c == '\t') c = ' ';
		if (c >= 0x20 && c < 0x7f) buf[o++] = (char)c;
		i++;
	}
	for (size_t i = 0; i < o; i++) {
		if (buf[i] == ' ' && (i + 1 == o || buf[i + 1] == ' ' || w == 0)) continue;
		buf[w++] = buf[i];
	}
	if (w > NOTE_LEN) {
		memcpy(out, buf, NOTE_LEN - 3);
		memcpy(out + NOTE_LEN - 3, "...", 4);
	} else {
		memcpy(out, buf, w);
		out[w] = 0;
	}
}

static void notice_show(const char *t)
{
	snprintf(note_cur, sizeof note_cur, "%s", t);
	note_t = NOTE_TICKS;
	st_notices++;
	sfx_notice();
	logmsg("notice: '%s'", note_cur);
}

static long st_notice_in;           /* notices that came in (shown or queued) */
static void notice_push(const char *t)
{
	st_notice_in++;
	if (!note_t) { notice_show(t); return; }
	if (note_n == NOTE_WAIT) {
		logmsg("notice: queue full, dropped '%s'", note_q[0]);
		memmove(note_q[0], note_q[1], sizeof note_q[0] * (NOTE_WAIT - 1));
		note_n--;
	}
	snprintf(note_q[note_n++], sizeof note_q[0], "%s", t);
	logmsg("notice: queued '%s' (%d waiting)", t, note_n);
}

/* Look at the file; first: the call at start (only fresh notices count). */
static void notice_poll(int first)
{
	struct stat sb;
	char raw[512], txt[NOTE_LEN + 1];
	ssize_t n;
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
	notice_clean(raw, (size_t)n, txt);
	if (txt[0]) notice_push(txt);
}

/* Once per tick. */
static void notice_tick(void)
{
	if (tick % NOTE_POLL == 0) notice_poll(0);
	if (note_t && --note_t == 0 && note_n) {
		char t[NOTE_LEN + 1];
		snprintf(t, sizeof t, "%s", note_q[0]);
		memmove(note_q[0], note_q[1], sizeof note_q[0] * (NOTE_WAIT - 1));
		note_n--;
		notice_show(t);
	}
}

/* Split t into one or two lines of at most w characters, at a space if one
 * is near; what does not fit in two lines ends in "...". */
static int notice_lines(const char *t, int w, char l[2][NOTE_LEN + 1])
{
	int n = (int)strlen(t), cut;
	const char *rest;
	if (w < 8) w = 8;
	if (w > NOTE_LEN) w = NOTE_LEN;
	l[1][0] = 0;
	if (n <= w) { snprintf(l[0], NOTE_LEN + 1, "%s", t); return 1; }
	for (cut = w; cut > w / 2 && t[cut] != ' '; cut--) {}
	if (t[cut] != ' ') cut = w;
	snprintf(l[0], NOTE_LEN + 1, "%.*s", cut, t);
	rest = t + cut + (t[cut] == ' ');
	if ((int)strlen(rest) <= w) snprintf(l[1], NOTE_LEN + 1, "%s", rest);
	else snprintf(l[1], NOTE_LEN + 1, "%.*s...", w - 3, rest);
	return 2;
}

#define PLY (fh - 3)        /* ship top row; its body is on PLY + 1 */

static const char *spr[3][2] = { { "}o{", ")o(" }, { ">8<", "-8-" }, { "{@}", "(@)" } };
static const int spr_col[3] = { GRN, YEL, MAG };
static const int points[3][2] = { { 30, 60 }, { 50, 100 }, { 150, 400 } };

static int dive_interval(void) { int v = 75 - 7 * (wave - 1); return v < 18 ? 18 : v; }
static int max_divers(void)    { int v = 1 + wave / 2; return v > 7 ? 7 : v; }
static float dive_speed(void)  { float v = 0.28f + 0.03f * (float)(wave - 1); return v > 0.6f ? 0.6f : v; }
static float fire_prob(void)   { float v = 0.025f + 0.01f * (float)(wave - 1); return v > 0.12f ? 0.12f : v; }
static int max_eshots(void)    { int v = 2 + wave; return v > MAXES ? MAXES : v; }

static float form_x(void) { return (float)fw / 2.0f + form_sway; }
static float form_y(void) { return fh >= 30 ? 3.0f : 2.0f; }

static void load_hi(void)
{
	FILE *f = hi_path ? fopen(hi_path, "r") : NULL;
	if (!f) return;
	if (fscanf(f, "%ld", &hi) != 1) hi = 0;
	fclose(f);
}

static void save_hi(void)
{
	FILE *f = hi_path ? fopen(hi_path, "w") : NULL;
	if (!f) return;
	fprintf(f, "%ld\n", hi);
	fclose(f);
}

static void banner(const char *s, int t)
{
	snprintf(banner_txt, sizeof banner_txt, "%s", s);
	banner_t = t;
}

static void add_score(long v)
{
	score += v;
	if (score > 999999) score = 999999;
	if (score >= next_extra) {
		lives++;
		next_extra += 20000;
		banner("EXTRA SHIP", 60);
		sfx_extra();
	}
}

static void boom_parts(float x, float y, int kind);

/* An explosion; kind: the moth type, or 3 for the ship (pixel mode's dust). */
static void boom(float x, float y, int big, int kind)
{
	boom_parts(x, y, kind);
	for (int i = 0; i < MAXB; i++)
		if (!bm[i].on) { bm[i] = (Boom){ 1, 0, big, x, y }; return; }
}

static void popup(float x, float y, long v)
{
	for (int i = 0; i < MAXP; i++)
		if (!pp[i].on) {
			pp[i].on = 1; pp[i].t = 0; pp[i].x = x; pp[i].y = y;
			snprintf(pp[i].txt, sizeof pp[i].txt, "%ld", v);
			return;
		}
}

static void init_stars(void)
{
	for (int i = 0; i < NSTAR; i++) {
		st[i].x = (float)(rnd() % (unsigned)fw);
		st[i].y = 1.0f + frand() * (float)(fh - 3);
		st[i].v = 0.03f + frand() * 0.10f;
		st[i].c = (rnd() & 3) ? BLU : WHT;
	}
}

static void add_enemy(int type, float sx, float sy)
{
	if (ne >= MAXE) return;
	Enemy *e = &en[ne];
	memset(e, 0, sizeof *e);
	e->alive = 1; e->type = type; e->hp = type == E_EMPRESS ? 2 : 1;
	e->sx = sx; e->sy = sy; e->leader = -1;
	ne++;
}

/* Three swarm shapes: a full block, a downward arrow, two wings around a
 * column of empresses. */
static void build_wave(void)
{
	int s = fw >= 44 ? 5 : 4;
	int mc = (fw - 8) / s + 1;
	int rsp = fh >= 30 ? 2 : 1;
	int pat = (wave - 1) % 3;
	if (mc > 8) mc = 8;
	if (mc < 3) mc = 3;
	spacing = s;
	ne = 0;
	for (int r = 0; r < 5; r++) {
		for (int c = 0; c < mc; c++) {
			int type = r == 0 ? E_EMPRESS : r < 3 ? E_LANTERN : E_DART, on = 0;
			float ox = ((float)c - (float)(mc - 1) / 2.0f) * (float)s;
			if (pat == 0) {
				on = r == 0 ? (c >= mc / 2 - 2 && c < mc / 2 + 2) : 1;
			} else if (pat == 1) {
				int n = 2 * (r + 1), lo;
				if (n > mc) n = mc;
				lo = (mc - n) / 2;
				on = c >= lo && c < lo + n;
			} else {
				int wing = c < 3 || c >= mc - 3;
				if (wing) { on = 1; type = r < 2 ? E_LANTERN : E_DART; }
				else if (r == 0 || r == 2) { on = 1; type = E_EMPRESS; }
			}
			if (on) add_enemy(type, ox, (float)(r * rsp));
		}
	}
	form_w = (mc - 1) * s + 3;
	for (int i = 0; i < ne; i++) {
		Enemy *e = &en[i];
		int side = i & 1;
		e->mode = M_ENTER;
		e->t = -2 * i;
		e->x = side ? (float)fw + 2.0f : -3.0f;
		e->y = 1.0f + (float)(i % 3);
	}
	memset(es, 0, sizeof es);
	wave_phase = 0; phase_t = 0; wave_t = 0; deaths_wave = 0;
	dive_timer = 60; ffire_timer = 120;
	{
		char b[32];
		snprintf(b, sizeof b, "WAVE %d", wave);
		banner(b, 75);
	}
	sfx_wave();
}

static void start_game(void)
{
	score = 0; lives = 2; wave = 1; next_extra = 10000;
	px = (float)fw / 2.0f; p_alive = 1; p_inv = 0; p_dead_t = 0;
	fire_cd = 0; fire_pending = 0;
	memset(ps, 0, sizeof ps); memset(bm, 0, sizeof bm); memset(pp, 0, sizeof pp);
	build_wave();
	state = S_PLAY; state_t = 0;
	paused = 0;
	st_games++;
}

static int visible(const Enemy *e)
{
	return e->alive && !(e->mode == M_ENTER && e->t < 0) && e->y >= 0.5f && e->y < (float)fh - 1.5f;
}

static int count_mode(int m)
{
	int n = 0;
	for (int i = 0; i < ne; i++) n += en[i].alive && en[i].mode == m;
	return n;
}

static int count_alive(void)
{
	int n = 0;
	for (int i = 0; i < ne; i++) n += en[i].alive;
	return n;
}

/* A dive starts: the swoop follows en[i]. */
static void swoop_start(int i)
{
	if (!snd_live()) return;
	if (!swp.on) { swp.env = 0.0f; swp.phase = 0.0f; swp.f = SWOOP_HI; }
	swp.on = 1; swp.rel = 0; swp.target = i; swp.y0 = en[i].y;
	swp.ftgt = SWOOP_HI;
	st_swoops++;
}

/* Once per tick: the swoop's pitch from how far down its moth is. */
static void swoop_tick(void)
{
	const Enemy *e = swp.target >= 0 && swp.target < ne ? &en[swp.target] : NULL;
	float prog;
	if (!swp.on || swp.rel) return;
	if (state != S_PLAY || !e || !e->alive || e->mode != M_DIVE || !snd_live()) { swp.rel = 1; return; }
	prog = (e->y - swp.y0) / ((float)fh - swp.y0 > 4.0f ? (float)fh - swp.y0 : 4.0f);
	if (prog < -0.2f) prog = -0.2f;
	if (prog > 1.0f) prog = 1.0f;
	swp.ftgt = SWOOP_HI * powf(SWOOP_LO / SWOOP_HI, prog);
}

static void enemy_fire(const Enemy *e, int aimed)
{
	int n = 0;
	for (int i = 0; i < MAXES; i++) n += es[i].on;
	if (n >= max_eshots()) return;
	for (int i = 0; i < MAXES; i++) {
		if (es[i].on) continue;
		float vy = 0.42f + 0.01f * (float)wave, vx = 0.0f;
		if (vy > 0.55f) vy = 0.55f;
		if (aimed) {
			float tt = ((float)PLY - e->y) / vy;
			if (tt < 1.0f) tt = 1.0f;
			vx = (px - e->x) / tt;
			if (vx > 0.25f) vx = 0.25f;
			if (vx < -0.25f) vx = -0.25f;
		}
		es[i] = (Shot){ 1, e->x, e->y + 1.0f, vx, vy };
		return;
	}
}

static void start_dive(void)
{
	int cand[MAXE], nc = 0;
	for (int i = 0; i < ne; i++)
		if (en[i].alive && en[i].mode == M_FORM) cand[nc++] = i;
	if (!nc) return;
	int li = cand[rnd() % (unsigned)nc];
	Enemy *L = &en[li];
	L->mode = M_DIVE; L->t = 0; L->shots = 0; L->leader = -1;
	L->dir = L->x < (float)fw / 2.0f ? -1 : 1;
	if (rnd() % 3 == 0) L->dir = -L->dir;
	L->vx = 0.8f * (float)L->dir; L->vy = -0.3f;
	swoop_start(li);
	/* From wave 2 an empress takes up to two lanterns from the row below. */
	if (L->type == E_EMPRESS && wave >= 2) {
		int taken = 0;
		for (int i = 0; i < ne && taken < 2; i++) {
			Enemy *e = &en[i];
			if (!e->alive || e->mode != M_FORM || e->type != E_LANTERN) continue;
			if (fabsf(e->sx - L->sx) > (float)spacing + 0.5f || e->sy <= L->sy) continue;
			e->mode = M_DIVE; e->t = 0; e->shots = 0; e->leader = li;
			e->offx = e->x - L->x; e->offy = e->y - L->y;
			taken++;
		}
	}
}

static void kill_enemy(int i, int diving)
{
	Enemy *e = &en[i];
	long v = points[e->type][diving ? 1 : 0];
	e->alive = 0;
	boom(e->x, e->y, 0, e->type);
	sfx_kill(e->type == E_EMPRESS);
	add_score(v);
	st_kills++;
	if (diving && v >= 100) popup(e->x, e->y, v);
}

static void hit_enemy(int i)
{
	Enemy *e = &en[i];
	if (--e->hp > 0) { e->flash = 6; sfx_hit(); return; }
	kill_enemy(i, e->mode == M_DIVE);
}

static void player_hit(void)
{
	if (!p_alive || p_inv > 0) return;
	p_alive = 0; p_dead_t = 0;
	boom(px, (float)PLY, 1, 3);
	sfx_death();
	deaths_wave++;
	st_deaths++;
	memset(es, 0, sizeof es);
	for (int i = 0; i < ne; i++)
		if (en[i].alive && en[i].mode == M_DIVE) { en[i].mode = M_RETURN; en[i].leader = -1; }
}

/* Move toward (tx, ty) at spd; rows are twice as tall as columns are wide. */
static int move_to(Enemy *e, float tx, float ty, float spd)
{
	float dx = tx - e->x, dy = (ty - e->y) * 2.0f;
	float d = sqrtf(dx * dx + dy * dy);
	if (d <= spd) { e->x = tx; e->y = ty; return 1; }
	e->x += dx / d * spd;
	e->y += dy / d * spd * 0.5f;
	return 0;
}

static void dive_update(Enemy *e)
{
	Enemy *L = e->leader >= 0 ? &en[e->leader] : NULL;
	e->t++;
	if (L && L->alive && L->mode == M_DIVE) {
		e->x = L->x + e->offx; e->y = L->y + e->offy;
		e->vx = L->vx; e->vy = L->vy;
	} else {
		e->leader = -1;
		if (e->t < 22) {
			/* peel off: a short climb and a swing out of the formation */
			e->vx *= 0.94f;
			e->vy += 0.03f;
		} else {
			float target = px, turn = 0.05f + 0.004f * (float)wave, ax, sp;
			if (e->type == E_LANTERN) target += sinf((float)e->t * 0.12f) * 7.0f;
			else if (e->type == E_EMPRESS) target += sinf((float)e->t * 0.06f) * 4.0f;
			if (turn > 0.1f) turn = 0.1f;
			ax = (target - e->x) * 0.02f;
			if (ax > turn) ax = turn;
			if (ax < -turn) ax = -turn;
			e->vx += ax;
			if (e->vx > 0.9f) e->vx = 0.9f;
			if (e->vx < -0.9f) e->vx = -0.9f;
			sp = dive_speed() * (e->type == E_DART ? 1.15f : e->type == E_EMPRESS ? 0.85f : 1.0f);
			e->vy += (sp - e->vy) * 0.08f;
		}
		e->x += e->vx; e->y += e->vy;
	}
	if (e->x < 1.0f) { e->x = 1.0f; e->vx = fabsf(e->vx); }
	if (e->x > (float)fw - 2.0f) { e->x = (float)fw - 2.0f; e->vx = -fabsf(e->vx); }
	if (e->y > (float)fh) {
		/* through the bottom: come back in from the top to the slot */
		e->mode = M_RETURN; e->y = -1.5f; e->leader = -1; e->vx = e->vy = 0.0f;
		return;
	}
	if (p_alive && e->t > 10 && e->y > 2.0f && e->y < (float)PLY - 6.0f &&
	    fabsf(e->x - px) < 9.0f && e->shots < (e->type == E_EMPRESS ? 3 : 2) &&
	    frand() < fire_prob()) {
		enemy_fire(e, 1);
		e->shots++;
	}
}

static void update_enemies(int may_dive)
{
	float A = ((float)fw - (float)form_w) / 2.0f - 1.0f, w = 0.03f + 0.003f * (float)wave;
	if (A > 3.0f) A = 3.0f;
	if (A < 0.0f) A = 0.0f;
	if (w > 0.07f) w = 0.07f;
	form_sway = A * sinf((float)wave_t * w);
	for (int i = 0; i < ne; i++) {
		Enemy *e = &en[i];
		float tx = form_x() + e->sx, ty = form_y() + e->sy;
		if (!e->alive) continue;
		if (e->flash) e->flash--;
		switch (e->mode) {
		case M_ENTER:
			if (++e->t < 0) break;
			if (move_to(e, tx, ty, 0.9f)) e->mode = M_FORM;
			break;
		case M_FORM:
			e->x = tx; e->y = ty;
			break;
		case M_RETURN:
			if (move_to(e, tx, ty, 0.7f)) e->mode = M_FORM;
			break;
		case M_DIVE:
			if (may_dive) dive_update(e);
			else e->mode = M_RETURN;
			break;
		}
	}
}

static void g_stars_update(void);
static void update_parts(void);

static void update_stars(void)
{
	if (gfx) g_stars_update();
	for (int i = 0; i < NSTAR; i++) {
		st[i].y += st[i].v;
		if (st[i].y > (float)fh - 2.0f) {
			st[i].y = 1.0f;
			st[i].x = (float)(rnd() % (unsigned)fw);
		}
	}
}

static void update_effects(void)
{
	update_parts();
	for (int i = 0; i < MAXB; i++)
		if (bm[i].on && ++bm[i].t > (bm[i].big ? 48 : 16)) bm[i].on = 0;
	for (int i = 0; i < MAXP; i++)
		if (pp[i].on && ++pp[i].t > 36) pp[i].on = 0;
	if (banner_t) banner_t--;
}

static void update_player(void)
{
	int L = held_left() || left_taps, R = held_right() || right_taps;
	if (touch_down()) {
		/* the ship heads for the finger's column at its usual speed */
		float col = gfx ? (float)tp.x / (float)CW - 0.5f : (float)tp.x, d = col - px;
		st_tdrag++;
		if (fabsf(d) <= PSPD) px = col;
		else px += d > 0.0f ? PSPD : -PSPD;
	} else if (L && !R) px -= PSPD;
	else if (R && !L) px += PSPD;
	if (px < 1.0f) px = 1.0f;
	if (px > (float)fw - 2.0f) px = (float)fw - 2.0f;
	if (fire_presses) fire_pending = 6;
	if (fire_cd > 0) fire_cd--;
	if ((fire_pending || held_fire() || touch_down()) && fire_cd == 0) {
		for (int i = 0; i < MAXPS; i++) {
			if (ps[i].on) continue;
			ps[i] = (Shot){ 1, (float)lroundf(px), (float)PLY - 1.0f, 0.0f, -1.0f };
			sfx_shot();
			fire_cd = fire_pending ? 5 : 9;
			fire_pending = 0;
			break;
		}
	}
	if (fire_pending) fire_pending--;
	if (p_inv) p_inv--;
}

static void update_shots(void)
{
	for (int i = 0; i < MAXPS; i++) {
		Shot *s = &ps[i];
		for (int sub = 0; sub < 4 && s->on; sub++) {
			int sx = (int)lroundf(s->x);
			s->y += s->vy / 4.0f;
			if (s->y < 1.0f) { s->on = 0; break; }
			for (int k = 0; k < ne; k++) {
				Enemy *e = &en[k];
				if (!visible(e)) continue;
				if (abs(sx - (int)lroundf(e->x)) <= 1 && fabsf(s->y - e->y) < 0.6f) {
					s->on = 0;
					hit_enemy(k);
					break;
				}
			}
		}
	}
	for (int i = 0; i < MAXES; i++) {
		Shot *s = &es[i];
		if (!s->on) continue;
		s->x += s->vx; s->y += s->vy;
		if (s->y > (float)fh - 1.5f || s->x < 0.0f || s->x > (float)fw - 1.0f) { s->on = 0; continue; }
		if (p_alive && !p_inv) {
			int sx = (int)lroundf(s->x), sy = (int)lroundf(s->y), pxi = (int)lroundf(px);
			if ((sy == PLY && sx == pxi) || (sy == PLY + 1 && abs(sx - pxi) <= 1)) {
				s->on = 0;
				player_hit();
			}
		}
	}
	if (p_alive && !p_inv) {
		int pxi = (int)lroundf(px);
		for (int i = 0; i < ne; i++) {
			Enemy *e = &en[i];
			int ey = (int)lroundf(e->y);
			if (!e->alive || e->mode != M_DIVE) continue;
			if ((ey == PLY || ey == PLY + 1) && abs((int)lroundf(e->x) - pxi) <= 2) {
				kill_enemy(i, 1);
				player_hit();
				break;
			}
		}
	}
}

static void game_over(void)
{
	state = S_OVER; state_t = 0;
	if ((long)wave > st_maxwave) st_maxwave = wave;
	if (score > hi) { hi = score; save_hi(); }
	logmsg("game over: score %ld, wave %d (tick %ld)", score, wave, tick);
	/* a Home still held from play (autofire) counts for nothing here */
	qk_t = 0;
	if (held_quit()) qk_latch = 1;
	sfx_gameover();
}

static void to_title(void)
{
	state = S_TITLE; state_t = 0;
	banner_t = 0;
	paused = 0;
	sfx_title();
}

static int bot_hold_kind(void);

/* Quit key held for HOLD_QUIT ticks during play or on game over. */
static void quit_to_title(void)
{
	if (bot && pilot && !esc_presses && bot_hold_kind() != 3) {
		fprintf(stderr, "BAD: quit on a hold of kind %d at tick %ld\n", bot_hold_kind(), tick);
		st_bad++;
	}
	if (state == S_PLAY && (long)wave > st_maxwave) st_maxwave = wave;
	if (score > hi) { hi = score; save_hi(); }
	st_quits++;
	to_title();
}

/*
 * The quit-key hold. Holding it counts up; at HOLD_SHOW a bar appears and
 * fills; at HOLD_QUIT the game goes back to the title. During play a
 * volume button (moving) starts the count over, so steering with autofire
 * held never quits; standing still with it held does, after the bar's
 * warning. A latched key (held across a quit or into game over) counts only
 * after it has been let go. Returns 1 for a short tap (released before the
 * bar), which on game over starts the next game.
 */
static int hold_update(void)
{
	int held = held_quit(), tap = 0;
	if (qk_releases && !qk_latch && qk_t < HOLD_SHOW) tap = 1;
	if (qk_releases || !held) qk_latch = 0;
	if (!held || qk_latch || (state == S_TITLE && !from_picker)) {
		qk_t = 0;
	} else {
		if (qk_releases) qk_t = 0;   /* let go and pressed again within the tick */
		if (state == S_PLAY && (held_left() || held_right() || left_taps || right_taps)) qk_t = 0;
		else qk_t++;
		if (qk_t == HOLD_SHOW) st_bars++;
		if (qk_t >= HOLD_QUIT) {
			qk_t = 0;
			qk_latch = 1;
			if (state == S_TITLE) {
				logmsg("title: Home held, back to the arcade");
				quit_flag = 1;   /* the normal exit path: sound closed, console back to text, status 0 */
			} else {
				quit_to_title();
			}
		}
	}
	return tap;
}

static void tick_play(void)
{
	wave_t++; phase_t++;
	update_enemies(1);
	if (p_alive) update_player();
	update_shots();

	if (wave_phase == 0) {
		if (count_mode(M_ENTER) == 0 || phase_t > 200) { wave_phase = 1; phase_t = 0; }
	} else if (wave_phase == 1) {
		if (p_alive && --dive_timer <= 0) {
			int iv = dive_interval();
			if (count_alive() * 4 <= ne) iv /= 2;   /* the last few get bold */
			dive_timer = iv + (int)(rnd() % (unsigned)(iv / 2 + 1));
			if (count_mode(M_DIVE) < max_divers()) start_dive();
		}
		if (p_alive && wave >= 2 && --ffire_timer <= 0) {
			int iv = 150 - 12 * wave, cand[MAXE], nc = 0;
			if (iv < 30) iv = 30;
			ffire_timer = iv + (int)(rnd() % 30);
			for (int i = 0; i < ne; i++)
				if (en[i].alive && en[i].mode == M_FORM) cand[nc++] = i;
			if (nc) enemy_fire(&en[cand[rnd() % (unsigned)nc]], 0);
		}
		if (count_alive() == 0) {
			wave_phase = 2; phase_t = 0;
			if (deaths_wave == 0) {
				char b[32];
				long bonus = 500L + 100L * wave;
				snprintf(b, sizeof b, "FLAWLESS  +%ld", bonus);
				add_score(bonus);
				banner(b, 80);
			} else {
				banner("SWARM CLEARED", 80);
			}
		}
	} else if (wave_phase == 2) {
		int busy = 0;
		for (int i = 0; i < MAXB; i++) busy |= bm[i].on && bm[i].big;
		if (phase_t > 90 && !busy && p_alive) {
			wave++;
			if ((long)wave > st_maxwave) st_maxwave = wave;
			memset(ps, 0, sizeof ps);
			build_wave();
		}
	}

	if (!p_alive) {
		p_dead_t++;
		if (p_dead_t > 60 && count_mode(M_DIVE) == 0) {
			if (lives > 0) {
				lives--;
				p_alive = 1; p_inv = 75;
				px = (float)fw / 2.0f;
				fire_cd = 10; fire_pending = 0;
			} else {
				game_over();
			}
		}
	}
}

/*
 * The touchscreen, once a tick: the corner is the quit key (press and
 * release edges, so a tap starts and a hold quits, as with Home); a tap
 * fires, or starts on the title and game over; a finger down steers and
 * fires (update_player).
 */
static void touch_tick(void)
{
	TGesture g;
	int c;
	if (gfx) touch_view((int)fb_xres, (int)fb_yres, gx0 * gs, gy0 * gs, gs, fw * CW, fh * CH, fw * CW / 6, fh * CH / 12);
	else touch_view(tw, th, fx0, fy0, 1, fw, fh, fw / 6, fh / 12 > 2 ? fh / 12 : 2);
	touch_poll(bot ? (int64_t)tick * 1000 / HZ : now_ns() / 1000000, grab);
	c = touch_corner_down();
	if (c && !t_corner) qk_presses++;
	if (!c && t_corner) qk_releases++;
	t_corner = c;
	while (touch_next(&g))
		if (g.kind == TG_TAP) {
			st_ttaps++;
			t_tap++;
			if (state == S_PLAY) fire_presses++;
		}
}

static void game_tick(void)
{
	int tap;
	state_t++;
	touch_tick();
	update_stars();
	update_effects();
	if (snd_flash) snd_flash--;
	if (esc_presses && state != S_TITLE) quit_to_title();
	tap = hold_update();
	switch (state) {
	case S_TITLE: {
		int both = held_both_vol();
		if (both && !both_latch) snd_toggle();
		both_latch = both;
		if (m_presses) snd_toggle();
		/* from the arcade the quit key starts on release, so that holding it can mean "back" */
		if (from_picker ? (other_fire_presses || tap || t_tap) : fire_presses || t_tap) start_game();
		break;
	}
	case S_PLAY:
		if (paused) {
			/* back from a dark screen: any press (or a tap) plays on; that
			 * press does nothing else */
			if (fire_presses || left_taps || right_taps || t_tap) {
				paused = 0;
				fire_presses = other_fire_presses = left_taps = right_taps = t_tap = 0;
				logmsg("play on after the dark screen");
			}
			break;
		}
		tick_play();
		break;
	case S_OVER:
		wave_t++;
		update_enemies(0);
		/* the quit key starts on release, so that holding it can mean "title" */
		if (state_t > 45 && (other_fire_presses || tap || t_tap)) start_game();
		else if (state_t > 20 * HZ) to_title();
		break;
	}
	swoop_tick();
	notice_tick();
	fire_presses = other_fire_presses = left_taps = right_taps = 0;
	qk_presses = qk_releases = esc_presses = m_presses = t_tap = 0;
}

/* ------------------------------------------------------------------ render */
/* ------------------------------------------------------------ pixel art */
/*
 * The sprites, drawn for this game: moths 18 x 12 logical pixels (3 x 1
 * field cells, the size of the text sprites and of the hit boxes), two
 * wing-beat frames each; the lantern ship 18 x 24 (3 x 2 cells). Colour
 * slots: moths 1 wing, 2 dark (edges, antennae, thorax), 3 light (body,
 * glow, eye-spot ring), 4 brightest (eyes, glow core).
 */
static const Spr moth_spr[3][2] = {
	{ /* dart: a slim moth, wings swept back like an arrowhead */
		{ 18, 12, { ".....2...", "......2..", ".......23", ".....1143", "...111123", ".11111223",
			    "1111122.3", ".111.2..3", "..1.....3", "........3", ".........", "........." } },
		{ 18, 12, { ".....2...", "......2..", ".......23", "......143", "....11123", "...111223",
			    "...1122.3", "....12..3", "........3", "........3", ".........", "........." } },
	},
	{ /* lantern: round wings with dark spots, a glowing abdomen */
		{ 18, 12, { "......2..", ".......2.", ".1111..22", "111111122", "112211122", "112211.33",
			    ".11111.34", "..1111.34", "..1111.34", "...11..33", "........3", "........." } },
		{ 18, 12, { "......2..", ".......2.", "....11.22", "...111122", "...122122", "...111.33",
			    "....11.34", "....11.34", ".....1.34", ".......33", "........3", "........." } },
	},
	{ /* empress: broad wings with eye-spots, feathered antennae, tails */
		{ 18, 12, { "....2.2..", ".....2.2.", "......2.2", "22.....22", "1111...22", "111311122",
			    "113431122", "111311.22", ".11111.22", "..111...2", "...11...2", "...1....." } },
		{ 18, 12, { "....2.2..", ".....2.2.", "......2.2", "...2...22", "...11..22", "...131122",
			    "..1343122", "...131.22", "...111.22", "....11..2", "....1...2", "....1...." } },
	},
};

/* The lantern ship: a ring to hang it by, a cap, a glass with the light in
 * it, a wide hull with fins and two thrusters (flames drawn apart). */
static const Spr ship_spr = { 18, 24, {
	".........", "........2", ".......2.", "........2", "........1", "......111",
	".....1222", ".....1233", ".....1234", ".....1234", ".....1234", ".....1233",
	"....11222", "...111111", "1..112111", "1.1112211", "11111.211", "111111211",
	"11.111111", "1...22.22", ".........", ".........", ".........", "........." } };

static const Spr life_spr = { 8, 8, { "...2", "...1", "..13", "..14", ".111", "1111", "1..2", "...." } };

static void g_moth(float x, float y, int type, int frame, int hurt, int flash)
{
	uint8_t map[6];
	const Spr *s = &moth_spr[type][frame & 1];
	int base = type == E_DART ? C_DART : type == E_LANTERN ? C_LANT : hurt ? C_HURT : C_EMP;
	int cx = gx0 + (int)lroundf(x * (float)CW) + CW / 2, cy = gy0 + (int)lroundf(y * (float)CH) + CH / 2;
	for (int k = 0; k < 6; k++) map[k] = (uint8_t)(flash ? C_WHITE : base + (k < 4 ? k : 3));
	g_spr(s, cx - s->w / 2, cy - s->h / 2, map);
}

/* Particles: wing dust and sparks. Cosmetic only, with their own random
 * numbers, so the game plays the same in both modes. */
#define MAXPART 400
typedef struct { float x, y, vx, vy; int16_t t, life; uint8_t ramp, big; } Part;
static Part pt[MAXPART];
static int pt_next;
static uint32_t prs = 0x2545f491u;
static float prand(void) { prs ^= prs << 13; prs ^= prs >> 17; prs ^= prs << 5; return (float)(prs & 0xffffff) / 16777216.0f; }
static const uint8_t ramps[6][4] = {
	{ C_WHITE, C_DART + 2, C_DART, C_DART + 1 },
	{ C_LANT + 3, C_LANT + 2, C_LANT, C_LANT + 1 },
	{ C_EMP + 3, C_EMP + 2, C_EMP, C_EMP + 1 },
	{ C_HURT + 3, C_HURT + 2, C_HURT, C_HURT + 1 },
	{ C_WHITE, C_SHIP + 5, C_SHIP + 2, C_SHIP + 4 },
	{ C_WHITE, C_SHIP, C_SHIP + 1, C_DIM },
};

static void burst(float cx, float cy, int n, int ramp, float spd, int life, int big)
{
	for (int i = 0; i < n; i++) {
		Part *p = &pt[pt_next];
		float a = prand() * 6.2831853f, v = spd * (0.2f + 0.8f * prand());
		pt_next = (pt_next + 1) % MAXPART;
		*p = (Part){ cx, cy, cosf(a) * v, sinf(a) * v, 0, (int16_t)((float)life * (0.55f + 0.45f * prand())),
			     (uint8_t)ramp, (uint8_t)big };
	}
}

static void update_parts(void)
{
	for (int i = 0; i < MAXPART; i++) {
		Part *p = &pt[i];
		if (!p->life) continue;
		p->x += p->vx; p->y += p->vy;
		p->vx *= 0.93f; p->vy = p->vy * 0.93f + 0.025f;    /* dust settles */
		if (++p->t >= p->life) p->life = 0;
	}
}

/* An explosion starts (pixel mode): kind is the moth type, or 3 for the ship. */
static void boom_parts(float x, float y, int kind)
{
	float cx = (float)gx0 + x * (float)CW + (float)CW / 2.0f, cy = (float)gy0 + y * (float)CH + (float)CH / 2.0f;
	if (!gfx) return;
	if (kind == 3) {
		cy += (float)CH / 2.0f;
		burst(cx, cy, 70, 4, 3.4f, 48, 1);
		burst(cx, cy, 40, 5, 2.2f, 40, 1);
	} else if (kind == E_EMPRESS) {
		burst(cx, cy, 22, 2, 2.0f, 30, 1);
		burst(cx, cy, 22, 3, 2.0f, 30, 1);
	} else {
		burst(cx, cy, 28, kind, 1.8f, 26, 1);
	}
}

/* A ring of radius r (midpoint circle). */
static void g_ring(int cx, int cy, int r, int c)
{
	int x = r, y = 0, e = 1 - r;
	while (x >= y) {
		g_px(cx + x, cy + y, c); g_px(cx - x, cy + y, c); g_px(cx + x, cy - y, c); g_px(cx - x, cy - y, c);
		g_px(cx + y, cy + x, c); g_px(cx - y, cy + x, c); g_px(cx + y, cy - x, c); g_px(cx - y, cy - x, c);
		y++;
		if (e < 0) e += 2 * y + 1;
		else { x--; e += 2 * (y - x) + 1; }
	}
}

/* The starfield: three layers at three speeds; the nearer ones also shift
 * a little against the ship's movement. */
#define NGSTAR 96
static struct { float x, y; uint8_t layer, ph; } gst[NGSTAR];
static float gpx_s = -1.0f;

static void g_stars_init(void)
{
	for (int i = 0; i < NGSTAR; i++) {
		gst[i].layer = (uint8_t)(i < 52 ? 0 : i < 80 ? 1 : 2);
		gst[i].x = prand() * (float)(fw * CW);
		gst[i].y = prand() * (float)(fh * CH);
		gst[i].ph = (uint8_t)(prand() * 255.0f);
	}
}

static void g_stars_update(void)
{
	static const float sp[3] = { 0.12f, 0.3f, 0.75f };
	float target = state == S_PLAY ? px : (float)fw / 2.0f;
	if (gpx_s < 0.0f) gpx_s = target;
	gpx_s += (target - gpx_s) * 0.08f;
	for (int i = 0; i < NGSTAR; i++) {
		gst[i].y += sp[gst[i].layer];
		if (gst[i].y >= (float)(fh * CH)) {
			gst[i].y -= (float)(fh * CH);
			gst[i].x = prand() * (float)(fw * CW);
		}
	}
}

static void g_stars(void)
{
	static const float par[3] = { 0.5f, 1.2f, 2.6f };
	int W = fw * CW;
	for (int i = 0; i < NGSTAR; i++) {
		int l = gst[i].layer, x, y = gy0 + (int)gst[i].y;
		float fx = gst[i].x - (gpx_s - (float)fw / 2.0f) * par[l];
		fx = fmodf(fx, (float)W);
		if (fx < 0.0f) fx += (float)W;
		x = gx0 + (int)fx;
		if (l == 0) g_px(x, y, ((tick + gst[i].ph) % 97) < 5 ? C_STAR3 : ((gst[i].ph & 1) ? C_STAR1 : C_STAR2));
		else if (l == 1) g_px(x, y, C_STAR3);
		else { g_px(x, y, C_STAR4); g_px(x, y - 1, C_STAR2); }
	}
}

static void g_player(void)
{
	static const uint8_t map[6] = { C_SHIP, C_SHIP + 1, C_SHIP + 2, C_SHIP + 3, C_SHIP + 4, C_SHIP + 5 };
	int cx = gx0 + (int)lroundf(px * (float)CW) + CW / 2, x0 = cx - 9, y0 = cell_y(PLY);
	g_spr(&ship_spr, x0, y0, map);
	/* the light breathes */
	if ((tick / 8) % 2) g_rect(cx - 1, y0 + 8, 2, 3, C_WHITE);
	/* two thruster flames, flickering */
	for (int s = 0; s < 2; s++) {
		int fxp = s ? x0 + 13 : x0 + 4, len = 2 + (int)((tick + (unsigned)s * 2) % 3);
		g_rect(fxp, y0 + 20, 1, len, C_SHIP + 4);
		g_rect(fxp, y0 + 20, 1, len > 2 ? 2 : 1, C_SHIP + 5);
	}
}

static void g_shots(void)
{
	for (int i = 0; i < MAXPS; i++) {
		int x, y;
		if (!ps[i].on) continue;
		x = gx0 + (int)lroundf(ps[i].x * (float)CW) + CW / 2 - 1;
		y = gy0 + (int)lroundf(ps[i].y * (float)CH) + 1;
		g_rect(x - 1, y + 1, 4, 7, C_PSHOT2);
		g_rect(x, y, 2, 9, C_PSHOT);
	}
	for (int i = 0; i < MAXES; i++) {
		int x, y, c;
		if (!es[i].on) continue;
		x = gx0 + (int)lroundf(es[i].x * (float)CW) + CW / 2;
		y = gy0 + (int)lroundf(es[i].y * (float)CH) + CH / 2;
		c = ((tick + (unsigned)i) / 2) % 2 ? C_ESHOT : C_ESHOT2;
		g_rect(x - 1, y - 2, 3, 5, c);
		g_rect(x - 2, y - 1, 5, 3, c);
		g_px(x, y, C_WHITE);
		g_px(x, y - 4, C_ESHOT);
	}
}

static void g_effects(void)
{
	for (int i = 0; i < MAXPART; i++) {
		const Part *p = &pt[i];
		int c, x, y;
		if (!p->life) continue;
		c = ramps[p->ramp][p->t * 4 / p->life];
		x = (int)p->x; y = (int)p->y;
		if (p->big && p->t * 2 < p->life) g_rect(x, y, 2, 2, c);
		else g_px(x, y, c);
	}
	for (int i = 0; i < MAXB; i++) {
		const Boom *b = &bm[i];
		int cx = cell_x(0) + (int)lroundf(b->x * (float)CW) + CW / 2, cy = cell_y(0) + (int)lroundf(b->y * (float)CH) + CH / 2;
		if (!b->on) continue;
		if (b->big) {
			cy += CH / 2;
			if (b->t < 4) g_rect(cx - 6 + b->t, cy - 6 + b->t, 12 - 2 * b->t, 12 - 2 * b->t, C_WHITE);
			if (b->t < 18) g_ring(cx, cy, 4 + b->t * 3, b->t < 6 ? C_WHITE : b->t < 12 ? C_SHIP + 5 : C_SHIP + 4);
			if (b->t > 3 && b->t < 22) g_ring(cx, cy, 2 + (b->t - 3) * 2, C_SHIP + 1);
		} else if (b->t < 6) {
			g_ring(cx, cy, 2 + b->t * 2, b->t < 3 ? C_WHITE : C_STAR3);
		}
	}
	for (int i = 0; i < MAXP; i++) {
		int row = (int)lroundf(pp[i].y);
		if (!pp[i].on || row < 1 || row > fh - 2) continue;
		g_text(cell_x((int)lroundf(pp[i].x) - 1), cell_y(0) + (int)lroundf(pp[i].y * (float)CH) - 2 - pp[i].t / 3,
		       pp[i].txt, tcol(CYN, 1), 1);
	}
}

/* Text at a field row, scale sc, centred. */
static void g_ctext(int row, const char *s, int c, int sc)
{
	int w = (int)strlen(s) * 6 * sc - sc;
	if (w > fw * CW && sc > 1) { sc = 1; w = (int)strlen(s) * 6 - 1; }
	g_text(gx0 + (fw * CW - w) / 2, cell_y(row) + (CH - 8 * sc) / 2 + 1, s, c, sc);
}

/* The notice panel at the top. */
static void draw_notice(void)
{
	char l[2][NOTE_LEN + 1];
	int nl, age;
	if (!note_t || fw < 20) return;
	nl = notice_lines(note_cur, fw - 4, l);
	age = NOTE_TICKS - note_t;
	if (gfx) {
		int h = nl == 1 ? 15 : 25, x0 = cell_x(0) + 2, x1 = cell_x(fw) - 2, y0 = cell_y(1) - 2;
		int slide = age < 8 ? 8 - age : note_t < 8 ? 8 - note_t : 0, ix, iy;
		y0 -= slide * (h + 16) / 8;
		g_rect(x0, y0, x1 - x0, h, C_PANEL);
		g_frame(x0, y0, x1 - x0, h, C_EDGE);
		g_rect(x0 + 1, y0 + h, x1 - x0 - 1, 1, C_SHADOW);
		/* a small spark, turning */
		ix = x0 + 8; iy = y0 + 7;
		if ((tick / 6) % 2) { g_rect(ix - 3, iy, 7, 1, C_ICON); g_rect(ix, iy - 3, 1, 7, C_ICON); }
		else for (int k = -2; k <= 2; k++) { g_px(ix + k, iy + k, C_ICON); g_px(ix + k, iy - k, C_ICON); }
		g_px(ix, iy, C_WHITE);
		for (int k = 0; k < nl; k++) g_text(cell_x(3), y0 + 4 + 10 * k, l[k], C_WHITE, 1);
	} else {
		for (int k = 0; k < nl; k++) {
			for (int x = 0; x < fw; x++) put_bg(x, 1 + k, ' ', WHT, BLU, 0);
			for (int x = 0; l[k][x]; x++) put_bg(3 + x, 1 + k, l[k][x], WHT, BLU, 1);
		}
		put_bg(1, 1, '*', YEL, BLU, 1);
	}
}


static const struct { char c; const char *r[5]; } bigfont[] = {
	{ 'A', { ".XXX.", "X...X", "XXXXX", "X...X", "X...X" } },
	{ 'D', { "XXXX.", "X...X", "X...X", "X...X", "XXXX." } },
	{ 'E', { "XXXXX", "X....", "XXXX.", "X....", "XXXXX" } },
	{ 'G', { ".XXXX", "X....", "X..XX", "X...X", ".XXXX" } },
	{ 'H', { "X...X", "X...X", "XXXXX", "X...X", "X...X" } },
	{ 'I', { "XXXXX", "..X..", "..X..", "..X..", "XXXXX" } },
	{ 'M', { "X...X", "XX.XX", "X.X.X", "X...X", "X...X" } },
	{ 'O', { ".XXX.", "X...X", "X...X", "X...X", ".XXX." } },
	{ 'R', { "XXXX.", "X...X", "XXXX.", "X..X.", "X...X" } },
	{ 'S', { ".XXXX", "X....", ".XXX.", "....X", "XXXX." } },
	{ 'T', { "XXXXX", "..X..", "..X..", "..X..", "..X.." } },
	{ 'V', { "X...X", "X...X", "X...X", ".X.X.", "..X.." } },
};

static void big_text(const char *s, int y, int bg)
{
	int n = (int)strlen(s), x0 = (fw - (6 * n - 1)) / 2;
	for (int i = 0; i < n; i++)
		for (size_t g = 0; g < sizeof bigfont / sizeof bigfont[0]; g++) {
			if (bigfont[g].c != s[i]) continue;
			for (int r = 0; r < 5; r++)
				for (int c = 0; c < 5; c++)
					if (bigfont[g].r[r][c] == 'X') block(x0 + 6 * i + c, y + r, bg);
		}
}

static void draw_stars(void)
{
	if (gfx) { g_stars(); return; }
	for (int i = 0; i < NSTAR; i++)
		put((int)st[i].x, (int)lroundf(st[i].y), '.', st[i].c, 0);
}

static void draw_enemies(void)
{
	for (int i = 0; i < ne; i++) {
		Enemy *e = &en[i];
		int row = (int)lroundf(e->y), fr, col, bold = 1;
		if (!visible(e) || row < 1 || row > fh - 2) continue;
		fr = (int)(((tick + i * 3) / (e->mode == M_FORM ? 10 : 4)) & 1);
		if (gfx) { g_moth(e->x, e->y, e->type, fr, e->hp == 1, e->flash); continue; }
		col = spr_col[e->type];
		if (e->type == E_EMPRESS && e->hp == 1) col = RED;
		if (e->flash) col = WHT;
		sprite(e->x, row, spr[e->type][fr], col, bold);
	}
}

static void draw_player(void)
{
	int y = PLY;
	if (!p_alive) return;
	if (p_inv && (tick / 3) % 2) return;
	if (gfx) { g_player(); return; }
	sprite(px, y, " ^ ", CYN, 1);
	sprite(px, y + 1, "< >", CYN, 1);
	sprite(px, y + 1, " O ", YEL, 1);
}

static void draw_shots(void)
{
	if (gfx) { g_shots(); return; }
	for (int i = 0; i < MAXPS; i++)
		if (ps[i].on) put((int)lroundf(ps[i].x), (int)lroundf(ps[i].y), '|', WHT, 1);
	for (int i = 0; i < MAXES; i++)
		if (es[i].on) put((int)lroundf(es[i].x), (int)lroundf(es[i].y), '!', RED, 1);
}

static void draw_effects(void)
{
	static const char *small[4] = { " * ", "*X*", "x x", ". ." };
	static const char *big[4][2] = {
		{ " \\|/ ", " -O- " }, { "\\ | /", "- X -" }, { " * * ", "* . *" }, { " .   ", "   . " },
	};
	if (gfx) { g_effects(); return; }
	for (int i = 0; i < MAXB; i++) {
		Boom *b = &bm[i];
		int row = (int)lroundf(b->y);
		if (!b->on) continue;
		if (b->big) {
			int f = b->t / 12, x = (int)lroundf(b->x) - 2, col = (b->t / 3) & 1 ? RED : YEL;
			if (f > 3) f = 3;
			for (int r = 0; r < 2; r++)
				for (int k = 0; k < 5; k++)
					if (big[f][r][k] != ' ') put(x + k, row + r, big[f][r][k], col, 1);
		} else {
			int f = b->t / 4;
			if (f > 3) f = 3;
			if (row >= 1 && row <= fh - 2) sprite(b->x, row, small[f], f < 2 ? YEL : RED, 1);
		}
	}
	for (int i = 0; i < MAXP; i++) {
		int row = (int)lroundf(pp[i].y);
		if (!pp[i].on || row < 1 || row > fh - 2) continue;
		text((int)lroundf(pp[i].x) - 1, row, pp[i].txt, CYN, 1);
	}
}

static void draw_hud(void)
{
	char b[32];
	text(0, 0, "SCORE", WHT, 0);
	snprintf(b, sizeof b, "%06ld", score);
	text(6, 0, b, YEL, 1);
	if (fw >= 26) {
		text(fw - 9, 0, "HI", WHT, 0);
		snprintf(b, sizeof b, "%06ld", hi);
		text(fw - 6, 0, b, YEL, 1);
	}
	if (gfx) {
		static const uint8_t map[6] = { C_SHIP, C_SHIP + 1, C_SHIP + 2, C_SHIP + 3, C_SHIP + 3, C_SHIP + 3 };
		int n = lives <= 5 ? lives : 1;
		for (int i = 0; i < n; i++) g_spr(&life_spr, cell_x(i * 2), cell_y(fh - 1) + 2, map);
		if (lives > 5) {
			snprintf(b, sizeof b, "x%d", lives);
			text(2, fh - 1, b, CYN, 1);
		}
	} else if (lives <= 5) {
		for (int i = 0; i < lives; i++) put(i * 2, fh - 1, '^', CYN, 1);
	} else {
		snprintf(b, sizeof b, "^ x%d", lives);
		text(0, fh - 1, b, CYN, 1);
	}
	snprintf(b, sizeof b, "WAVE %02d", wave);
	text(fw - 8, fh - 1, b, CYN, 0);
}

/* The "hold for title" bar: from HOLD_SHOW it fills until HOLD_QUIT. */
static void draw_hold_bar(int y)
{
	char b[32];
	int w = fw >= 30 ? 20 : 10, n, x = (fw - w - 2) / 2;
	if (qk_t < HOLD_SHOW || (state == S_TITLE && !from_picker)) return;
	if (y > fh - 4) y = fh - 4;
	n = (qk_t - HOLD_SHOW + 1) * w / (HOLD_QUIT - HOLD_SHOW);
	if (n > w) n = w;
	snprintf(b, sizeof b, "HOLD %s FOR %s", t_corner ? "ON" : have_home || !have_power ? "HOME" : "POWER",
		 state == S_TITLE ? "ARCADE" : "TITLE");
	ctext(y, b, WHT, 1);
	if (gfx) {
		/* a framed bar that fills pixel by pixel */
		int x0 = cell_x(x), bw = (w + 2) * CW, y0 = cell_y(y + 1) + 2, in = bw - 4;
		int fill = (qk_t - HOLD_SHOW + 1) * in / (HOLD_QUIT - HOLD_SHOW);
		if (fill > in) fill = in;
		g_rect(x0 - 2, y0 - 2, bw + 4, CH - 4 + 4, C_SHADOW);
		g_frame(x0, y0, bw, CH - 4, C_WHITE);
		g_rect(x0 + 2, y0 + 2, fill, CH - 8, C_TILE + 3 * YEL + 1);
		g_rect(x0 + 2, y0 + 2, fill, 1, C_TILE + 3 * YEL);
		return;
	}
	put(x, y + 1, '[', WHT, 1);
	for (int k = 0; k < w; k++) {
		if (k < n) block(x + 1 + k, y + 1, YEL);
		else put(x + 1 + k, y + 1, '.', WHT, 0);
	}
	put(x + w + 1, y + 1, ']', WHT, 1);
}

static void draw_field(void)
{
	draw_stars();
	draw_enemies();
	draw_shots();
	draw_player();
	draw_effects();
	draw_hud();
	if (banner_t && (banner_t > 20 || (tick / 4) % 2)) {
		if (gfx) g_ctext(fh / 2 + 3, banner_txt, tcol(YEL, 1), 2);
		else ctext(fh / 2 + 3, banner_txt, YEL, 1);
	}
	if (paused) {
		if (gfx) g_ctext(fh / 2 - 2, "PAUSED", tcol(YEL, 1), 3);
		else ctext(fh / 2 - 2, "P A U S E D", YEL, 1);
		ctext(fh / 2 + 1, touch_present() ? "TAP OR PRESS TO PLAY ON" : "PRESS A BUTTON TO PLAY ON", WHT, 1);
	}
	draw_hold_bar(fh / 2 + 5 < PLY - 3 ? fh / 2 + 5 : PLY - 3);
}

static void legend(int y, int type, const char *name, int a, int b)
{
	char buf[32];
	int x = (fw - 24) / 2;
	if (gfx) g_moth((float)(x + 1), (float)y, type, (int)(state_t / 8) & 1, 0, 0);
	else text(x, y, spr[type][0], spr_col[type], 1);
	text(x + 5, y, name, WHT, 0);
	snprintf(buf, sizeof buf, "%3d / %3d", a, b);
	text(x + 15, y, buf, YEL, 1);
}

static void draw_title(void)
{
	int g = fh >= 38 ? 2 : 1, y = fh >= 38 ? 3 : 1;
	const char *fire_name = have_home || !have_power ? "HOME" : "POWER";
	char b[48];
	draw_stars();
	if (fw >= 30 && fh >= 28) {
		big_text("VOID", y, MAG);
		big_text("MOTHS", y + 6, CYN);
		y += 11;
	} else {
		ctext(y, "V O I D M O T H S", MAG, 1);
		y += 2;
	}
	if (g == 2) {
		/* one moth flutters across under the title */
		int span = fw + 6, x = (int)((state_t / 2) % span) - 3;
		if (gfx) g_moth((float)(state_t % (2 * span)) / 2.0f - 3.0f, (float)y, E_LANTERN, (state_t / 4) & 1, 0, 0);
		else sprite((float)x, y, spr[E_LANTERN][(state_t / 4) & 1], YEL, 1);
		y += 1;
	}
	ctext(y, "the swarm wants your light", WHT, 0);
	y += g + 1 - gfx;   /* pixel mode: one row less here, one more between the moths */
	ctext(y++, "points: formation / diving", BLU, 1);
	legend(y, E_EMPRESS, "EMPRESS", 150, 400);
	y += 1 + gfx;
	legend(y, E_LANTERN, "LANTERN", 50, 100);
	y += 1 + gfx;
	legend(y++, E_DART, "DART", 30, 60);
	y += g;
	if (touch_present()) {
		int x = (fw - 22) / 2;
		text(x, y, "DRAG", CYN, 1); text(x + 7, y++, "steer, it fires", WHT, 0);
		text(x, y, "TAP", CYN, 1); text(x + 7, y++, "fire", WHT, 0);
		if (have_vol) { text(x, y, "VOL+-", CYN, 1); text(x + 7, y++, "move", WHT, 0); }
		else { text(x, y, fire_name, CYN, 1); text(x + 7, y++, "fire (hold)", WHT, 0); }
	} else {
		int x = (fw - 20) / 2;
		text(x, y, swap_lr ? "VOL-" : "VOL+", CYN, 1); text(x + 7, y++, "move left", WHT, 0);
		text(x, y, swap_lr ? "VOL+" : "VOL-", CYN, 1); text(x + 7, y++, "move right", WHT, 0);
		text(x, y, fire_name, CYN, 1); text(x + 7, y++, "fire (hold)", WHT, 0);
	}
	y += g + 1;
	if (touch_present()) snprintf(b, sizeof b, "TAP THE SCREEN TO START");
	else snprintf(b, sizeof b, "PRESS %s TO START", fire_name);
	if ((state_t / 15) % 2 == 0) ctext(y, b, YEL, 1);
	y += g + 1;
	snprintf(b, sizeof b, "HI-SCORE %06ld", hi);
	ctext(y, b, WHT, 0);
	if (from_picker) draw_hold_bar(y + 1);
	if (from_picker && touch_present() && qk_t < HOLD_SHOW) ctext(y + 2, "HOLD THE TOP LEFT CORNER: ARCADE", BLU, 0);
	if (touch_present()) {
		/* the corner: a small lit wedge (text mode: a mark) */
		int c = t_corner ? YEL : (state_t / 15) % 2 ? BLU : CYN;
		if (gfx) for (int k = 0; k < 8; k++) g_rect(cell_x(0), cell_y(0) + k, 8 - k, 1, tcol(c, 1));
		else put(0, 0, '*', c, 1);
	}
	if (fh >= 28 && y + 2 < fh - 3)
		ctext(fh - 3, snd_status(fw < 34), snd_flash ? YEL : snd_state == SND_ON ? GRN : BLU, snd_flash ? 1 : 0);
	ctext(fh - 2, dev_desc, ndev ? BLU : RED, ndev ? 0 : 1);
}

static void draw_over(void)
{
	char b[48];
	int y = fh / 2 - 8;
	draw_stars();
	draw_enemies();
	draw_effects();
	draw_hud();
	if (y < 2) y = 2;
	if (fw >= 24) {
		big_text("GAME", y, RED);
		big_text("OVER", y + 6, RED);
		y += 12;
	} else {
		ctext(y, "GAME OVER", RED, 1);
		y += 2;
	}
	snprintf(b, sizeof b, "SCORE %06ld   WAVE %d", score, wave);
	ctext(y, b, WHT, 1);
	y += 2;
	if (score > 0 && score >= hi && (state_t / 8) % 2 == 0) ctext(y, "NEW HI-SCORE", YEL, 1);
	y += 2;
	if (state_t > 45 && (state_t / 15) % 2 == 0)
		ctext(y, touch_present() ? "TAP TO PLAY AGAIN" :
		      have_home || !have_power ? "PRESS HOME TO PLAY AGAIN" : "PRESS POWER TO PLAY AGAIN", YEL, 1);
	draw_hold_bar(y + 2);
}

static void render(void)
{
	if (gfx) memset(pix, C_BG, (size_t)lw * (size_t)lh);
	else clear_cells();
	if (fw < 20 || fh < 16) {
		put(0, 0, '?', RED, 1);
		text(0, 1, "screen too small", RED, 1);
		return;
	}
	switch (state) {
	case S_TITLE: draw_title(); break;
	case S_PLAY: draw_field(); break;
	case S_OVER: draw_over(); break;
	}
	draw_notice();
}

/* -------------------------------------------------------------------- bot */

/* The autopilot also holds the quit key now and then: holds that are let go
 * early or broken by moving must not quit, a still 2 s hold must. */
static long bq_end = -1, bq_move = -1;
static int bq_kind;     /* 1 moves, 2 let go early, 3 must quit, 4 tap on game over */

static int bot_hold_kind(void) { return bq_end >= 0 ? bq_kind : 0; }

static void bot_quit_key(int on)
{
	if (on && !bot_q) { qk_presses++; fire_presses++; }
	if (!on && bot_q) qk_releases++;
	bot_q = on;
}

/* The self-test's notices (--notice PATH), written as /usr/bin/notice does:
 * a temporary file renamed over PATH. Five in a row (one too many for the
 * queue), a messy one twice, an empty one, a last one. */
static const char bot_messy[] = "  caf\xc3\xa9 \xe2\x80\x94 step\t7  \xe2\x9c\x93 done\x01\x1b[31m and this "
	"line goes on well past sixty characters of text\nsecond line\n";
static void bot_notices(void)
{
	static const struct { long t; const char *s; } nt[] = {
		{ 100, "first: build started\n" }, { 130, "second\n" }, { 160, "third\n" }, { 190, "fourth\n" },
		{ 220, "fifth\n" }, { 2000, bot_messy }, { 2600, bot_messy }, { 3200, "\n" }, { 3800, "short again" },
	};
	if (!notice_path) return;
	for (size_t i = 0; i < sizeof nt / sizeof nt[0]; i++) {
		char tmp[300];
		FILE *f;
		if (tick != nt[i].t) continue;
		snprintf(tmp, sizeof tmp, "%s.tmp", notice_path);
		f = fopen(tmp, "wb");
		if (!f) continue;
		fputs(nt[i].s, f);
		fclose(f);
		rename(tmp, notice_path);
	}
}

static void bot_think(void)
{
	float target = (float)fw / 2.0f, best = 1e9f;
	bot_l = bot_r = bot_f = bot_both = 0;
	if (bq_end >= 0 && (tick >= bq_end || state == S_TITLE)) {
		bq_end = bq_move = -1;
		if (bq_kind == 4) bq_kind = 5;   /* check the tap's effect next tick */
	} else if (bq_kind == 5) {
		if (state != S_PLAY) {
			fprintf(stderr, "BAD: a tap on game over did not start a game (tick %ld)\n", tick);
			st_bad++;
		}
		bq_kind = 0;
	}
	if (state == S_TITLE) {
		/* every third visit: sound off, then on again */
		if (st_games % 3 == 1 && ((state_t >= 10 && state_t < 12) || (state_t >= 20 && state_t < 22)))
			bot_both = 1;
		/* from the arcade: after two games, hold Home on the title until the game exits */
		if (from_picker && st_games >= 2) {
			if (state_t >= 60) { bot_quit_key(1); return; }
		} else if (state_t == 40) { fire_presses++; other_fire_presses++; }
	} else if (state == S_OVER) {
		/* alternately: a long hold (title) and a tap (play again, on release) */
		if (state_t == 50 && bq_end < 0) {
			bq_end = tick + (st_games % 2 ? HOLD_QUIT + 10 : 3);
			bq_kind = st_games % 2 ? 3 : 4;
		}
	} else if (bq_end < 0) {
		long ph = tick % 2400;
		if (ph == 600) { bq_end = tick + 90; bq_move = tick + 40; bq_kind = 1; }  /* moves: no quit */
		else if (ph == 1000) { bq_end = tick + HOLD_SHOW + 15; bq_kind = 2; }      /* let go: no quit */
		else if (ph == 1800) { bq_end = tick + HOLD_QUIT + 10; bq_kind = 3; }      /* quits */
	}
	bot_quit_key(bq_end >= 0);
	if (state != S_PLAY) return;
	if (paused) { fire_presses++; other_fire_presses++; return; }   /* play on */
	for (int i = 0; i < ne; i++) {
		Enemy *e = &en[i];
		float d;
		if (!visible(e)) continue;
		d = (e->mode == M_DIVE ? 0.0f : 100.0f) + fabsf(e->x - px) - e->y;
		if (d < best) { best = d; target = e->x; }
	}
	for (int i = 0; i < MAXES; i++)
		if (es[i].on && es[i].y > (float)PLY - 7.0f && fabsf(es[i].x - px) < 2.5f)
			target = es[i].x < px ? px + 4.0f : px - 4.0f;
	if (target < px - 0.5f) bot_l = 1;
	else if (target > px + 0.5f) bot_r = 1;
	if (bot_q && bq_move >= 0 && tick >= bq_move && tick < bq_move + 3) {
		/* the "moving" hold: steer during it */
		bot_l = px >= (float)fw / 2.0f;
		bot_r = !bot_l;
	} else if (bot_q) {
		bot_l = bot_r = 0;
	}
	bot_f = 1;
}

/* ------------------------------------------------------------------- main */

/* ------------------------------------------------------------- screen off */
/*
 * Power darkens the screen (backlight.h). While it is dark nothing moves:
 * no game tick, no drawing, no sound (the card is closed), the touchscreen
 * is read and dropped; the buttons are slept on and /run/notice is looked at
 * twice a second (a new notice lights the screen). When it comes back a game
 * in play is paused until a press.
 */
static int was_dark;
static int64_t dark_next_notice, dark_next_scan;

static void screen_edges(void)
{
	if (bl_dark() == was_dark) return;
	was_dark = bl_dark();
	touch_discard(vm_clock_ms());
	t_corner = t_tap = 0;
	fire_presses = other_fire_presses = left_taps = right_taps = 0;
	qk_presses = qk_releases = esc_presses = m_presses = 0;
	qk_t = 0;
	if (was_dark) {
		snd_suspend();
		dark_next_notice = 0;
		logmsg("screen dark: state %d, score %ld, lives %d, deaths %ld, wave %d, wave_t %d", state, score, lives, st_deaths,
		       wave, wave_t);
		return;
	}
	if (state == S_PLAY) paused = 1;
	qk_latch = held_quit();
	full_redraw = fb_full = 1;
	snd_resume();
	logmsg("screen lit: state %d, score %ld, lives %d, deaths %ld, wave %d, wave_t %d%s", state, score, lives, st_deaths,
	       wave, wave_t, paused ? ", paused" : "");
}

static void dark_notice(void)
{
	long before = st_notice_in;
	notice_poll(0);
	if (st_notice_in != before) bl_wake("a notice");
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
		touch_discard(vm_clock_ms());
		dark_next_notice = now + BL_IDLE_MS * 1000000LL;
	}
	if (rescan || (ndev == 0 && now >= dark_next_scan)) { scan_devs(); dark_next_scan = now + 2000000000LL; }
	screen_edges();
}

/* Dark, in the self-test: one tick passes. */
static void dark_bot(void)
{
	if (tick % NOTE_POLL == 0) dark_notice();
	touch_discard(vm_clock_ms());
	screen_edges();
}

/* ------------------------------------------------------------------- main */

static struct termios tio_saved;
static int tio_ok;
static void on_signal(int sig) { (void)sig; quit_flag = 1; }

/* Any other signal that would end the game: console back to text, then the
 * signal goes on to do what it does. Only async-signal-safe calls here. */
static void on_fatal(int sig)
{
	kd_text();
	bl_on_sigsafe();
	if (tty_fd >= 0) {
		say(tty_fd, "\033[0m\033[?7h\033[?25h\033[H\033[2J");
		if (tio_ok) tcsetattr(tty_fd, TCSANOW, &tio_saved);
	}
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
	sa.sa_flags = SA_RESETHAND | SA_NODEFER;
	sigemptyset(&sa.sa_mask);
	for (size_t i = 0; i < sizeof fatal / sizeof fatal[0]; i++) sigaction(fatal[i], &sa, NULL);
	atexit(at_exit);
}

/* --restore-tty: a console left in graphics mode (the game was killed with
 * SIGKILL) back to text mode, cursor and autowrap on. */
static int restore_tty(const char *path)
{
	int r, fd;
	t_start = now_ns();
	bl_restore_only();      /* and a backlight left dark: on */
	fd = open(path, O_RDWR | O_NOCTTY | O_CLOEXEC);
	if (fd < 0) { perror(path); return 1; }
	r = ioctl(fd, KDSETMODE, KD_TEXT);
	if (r < 0) perror("KDSETMODE KD_TEXT");
	say(fd, "\033[0m\033[?7h\033[?25h");
	close(fd);
	return r < 0;
}

static void term_setup(void)
{
	struct termios t;
	if (tcgetattr(tty_fd, &tio_saved) == 0) {
		tio_ok = 1;
		t = tio_saved;
		t.c_lflag &= ~(tcflag_t)(ECHO | ICANON | ECHONL);
		t.c_iflag &= ~(tcflag_t)(IXON | ICRNL);
		tcsetattr(tty_fd, TCSANOW, &t);
	}
	/* cursor off, no autowrap, no console blanking or powerdown */
	outs("\033[0m\033[?25l\033[?7l\033[9;0]\033[14;0]\033[H\033[2J");
	flush_out();
}

static void term_restore(void)
{
	if (tty_fd < 0) return;
	outs("\033[0m\033[?7h\033[?25h\033[H\033[2J");
	flush_out();
	if (tio_ok) tcsetattr(tty_fd, TCSANOW, &tio_saved);
}

static void usage(void)
{
	fprintf(stderr, "usage: voidmoths [--tty PATH] [--fb PATH] [--text] [--hi PATH|none] [--swap] [--no-grab]\n"
			"                 [--mute] [--vol DB] [--notice PATH|none]\n"
			"       voidmoths --restore-tty PATH\n"
			"       voidmoths --bot TICKS [--size WxH | --fakefb WxHxBPP[:bgr]] [--dump DIR --dump-at T,T,...]\n"
			"                 [--notice PATH] [--mute] [--no-pilot]\n");
	exit(2);
}

int main(int argc, char **argv)
{
	const char *tty_path = NULL, *fb_path = "/dev/fb0", *fakefb = NULL, *dump_dir = NULL;
	long bot_ticks = 0, dump_at[32];
	int w = FW_MAX, h = FH_MAX, mute = 0, text_only = 0, notice_set = 0, ndump = 0;
	long full_frames = 0;
	long long part_bytes = 0;
	int64_t full_ns = 0;

	for (int i = 1; i < argc; i++) {
		if (!strcmp(argv[i], "--tty") && i + 1 < argc) tty_path = argv[++i];
		else if (!strcmp(argv[i], "--hi") && i + 1 < argc) {
			hi_path = argv[++i];
			if (!strcmp(hi_path, "none")) hi_path = NULL;
		} else if (!strcmp(argv[i], "--swap")) swap_lr = 1;
		else if (!strcmp(argv[i], "--from-picker")) from_picker = 1;
		else if (!strcmp(argv[i], "--no-grab")) grab = 0;
		else if (!strcmp(argv[i], "--mute")) mute = 1;
		else if (!strcmp(argv[i], "--vol") && i + 1 < argc) {
			/* amp volume in dB, at most VOL_MAX_DB */
			snd_vol_db = strtof(argv[++i], NULL);
			if (!(snd_vol_db <= VOL_MAX_DB)) snd_vol_db = VOL_MAX_DB;
			if (snd_vol_db < -60.0f) snd_vol_db = -60.0f;
		}
		else if (!strcmp(argv[i], "--bot") && i + 1 < argc) { bot = 1; bot_ticks = atol(argv[++i]); }
		else if (!strcmp(argv[i], "--text")) text_only = 1;
		else if (!strcmp(argv[i], "--no-pilot")) pilot = 0;
		else if (!strcmp(argv[i], "--fb") && i + 1 < argc) fb_path = argv[++i];
		else if (!strcmp(argv[i], "--fakefb") && i + 1 < argc) fakefb = argv[++i];
		else if (!strcmp(argv[i], "--dump") && i + 1 < argc) dump_dir = argv[++i];
		else if (!strcmp(argv[i], "--dump-at") && i + 1 < argc) {
			for (char *p = argv[++i]; *p && ndump < 32; p++) {
				dump_at[ndump++] = strtol(p, &p, 10);
				if (*p != ',') break;
			}
		} else if (!strcmp(argv[i], "--notice") && i + 1 < argc) {
			notice_path = argv[++i];
			if (!strcmp(notice_path, "none")) notice_path = NULL;
			notice_set = 1;
		} else if (!strcmp(argv[i], "--restore-tty") && i + 1 < argc) return restore_tty(argv[++i]);
		else if (!strcmp(argv[i], "--size") && i + 1 < argc) {
			if (sscanf(argv[++i], "%dx%d", &w, &h) != 2) usage();
		} else usage();
	}

	catch_signals();
	font_init();
	t_start = now_ns();
	bl_init(vm_clock_ms);
#ifdef VM_FAKE_ALSA
	snd_root = getenv("FAKE_ALSA_ROOT") ? getenv("FAKE_ALSA_ROOT") : "/nonexistent";
#endif

	if (bot) {
		hi_path = NULL;
		if (!notice_set) notice_path = NULL;   /* the self-test reads only its own */
		if (fakefb) {
			/* the self-test's framebuffer and console are pretend: kd_graphics
			 * and kd_text only log */
			if (fb_open_fake(fakefb) == 0) kd_graphics(-1);
			else logmsg("graphics: fake framebuffer %s refused; text mode", fakefb);
		}
		if (!gfx) set_size(w, h);
		snprintf(dev_desc, sizeof dev_desc, "autopilot");
		have_home = 1;
		fakekeys_have(&have_home, &have_power, &have_vol);
		bl_set_keys(have_home, have_power, have_vol);
	} else {
		if (!tty_path) tty_path = access("/dev/tty1", W_OK) == 0 ? "/dev/tty1" : "/dev/tty";
		tty_fd = open(tty_path, O_RDWR | O_NOCTTY | O_CLOEXEC);
		if (tty_fd < 0) { perror(tty_path); return 1; }
		if (text_only) logmsg("graphics: off (--text); text mode");
		else fb_open(fb_path);
		if (!gfx) {
			set_size(w, h);
			update_size();
		}
		term_setup();
		if (gfx) kd_graphics(tty_fd);
		rs ^= (uint32_t)now_ns() | 1u;
		load_hi();
		scan_devs();
	}
	init_stars();
	if (gfx) g_stars_init();
	tp.last_scan = bot ? 0 : now_ns() / 1000000;
	touch_open(grab);
	logmsg("%s", tp.desc);
	notice_poll(1);
	state = S_TITLE;
	if (mute) snd_state = SND_OFF;
	else snd_start_search();
	logmsg("start: sound %s, amp volume %.1f dB", mute ? "off (--mute)" : "on", (double)snd_vol_db);

	const int64_t period = 1000000000LL / HZ;
	int64_t next = now_ns();
	while (!quit_flag) {
		if (!bot) {
			int64_t now;
			if (bl_dark()) {
				dark_wait();
				next = now_ns();
				continue;
			}
			while ((now = now_ns()) < next && !quit_flag && !bl_dark()) {
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
				/* room in the sound buffer: top it up (if none could be
				 * taken, stop polling it until the next tick) */
				if (si >= 0 && pf[si].revents && !snd_pump()) snd_poll_ok = 0;
			}
			next += period;
			if (now - next > 5 * period) next = now + period;
			read_events();
			fakekeys_pump(key_event);
			bl_tick();
			if (rescan || (ndev == 0 && tick % (2 * HZ) == 0)) scan_devs();
			if (bl_dark()) { screen_edges(); continue; }
			if (tick % HZ == 0) tcflush(tty_fd, TCIFLUSH);
			if (tick % (10 * HZ) == 0) full_redraw = fb_full = 1;   /* repaint over stray output */
			if (!gfx) {
				int ow = fw, oh = fh;
				update_size();
				if (fw != ow || fh != oh) {
					/* font change (setbigfont) or rotation: new field */
					init_stars();
					if (px > (float)fw - 2.0f) px = (float)fw - 2.0f;
				}
			}
		} else {
			fakekeys_pump(key_event);
			bl_tick();
			screen_edges();
			if (bl_dark()) {
				dark_bot();
				if (++tick >= bot_ticks) break;
				continue;
			}
			if (tick % (10 * HZ) == 0) fb_full = 1;   /* as on the phone */
			if (pilot) bot_think();
			bot_notices();
		}
		screen_edges();
		game_tick();
		snd_tick();
		if (gfx) {
			int full = fb_full;
			long long b0 = fb_bytes;
			int64_t t0 = now_ns(), d;
			render();
			present();
			d = now_ns() - t0;
			if (full) { full_frames++; full_ns += d; }
			else {
				part_bytes += fb_bytes - b0;
				fb_frames++;
				fb_ns_sum += d;
				if (d > fb_ns_max) fb_ns_max = d;
			}
			for (int i = 0; dump_dir && i < ndump; i++)
				if (dump_at[i] == tick) fb_dump(dump_dir, tick);
		} else {
			render();
			present();
		}
		tick++;
		if (bot && tick >= bot_ticks) break;
	}

	if (bot && gfx) {
		long long full_bytes = (long long)lw * gs * lh * gs * fb_Bpp;
		fprintf(stderr, "frames: %ld partial, render+present avg %.0f us, max %.0f us, %lld KB written avg (%.1f%% of a frame); "
			"%ld full, avg %.0f us\n",
			fb_frames, fb_frames ? (double)fb_ns_sum / (double)fb_frames / 1e3 : 0.0, (double)fb_ns_max / 1e3,
			fb_frames ? part_bytes / fb_frames / 1024 : 0,
			fb_frames ? 100.0 * (double)part_bytes / (double)fb_frames / (double)full_bytes : 0.0,
			full_frames, full_frames ? (double)full_ns / (double)full_frames / 1e3 : 0.0);
		fprintf(stderr, "fakefb: line padding %s\n", fb_padding_ok() ? "intact" : "WRITTEN (BAD)");
		if (!fb_padding_ok()) st_bad++;
	}
	if (bot) {
		for (int y = 0; !gfx && y < th; y++) {
			int n = tw;
			while (n > 0 && scr[y][n - 1].ch == ' ' && scr[y][n - 1].bg == BLK) n--;
			for (int x = 0; x < n; x++) putchar(scr[y][x].bg != BLK ? '#' : scr[y][x].ch);
			putchar('\n');
		}
		fprintf(stderr, "ticks=%ld state=%d games=%ld score=%ld wave=%d maxwave=%ld kills=%ld deaths=%ld lives=%d\n",
			tick, state, st_games, score, wave, st_maxwave, st_kills, st_deaths, lives);
		fprintf(stderr, "quits=%ld bars=%ld toggles=%ld sound=%d underruns=%ld bad=%ld swoops=%ld notices=%ld\n",
			st_quits, st_bars, st_toggles, snd_state, snd_underruns, st_bad, st_swoops, st_notices);
		fprintf(stderr, "touch=%d contacts=%ld taps=%ld swipes=%ld longs=%ld corner=%ld ttaps=%ld tdrag=%ld px=%.2f exit=%d tdropped=%ld paused=%d\n",
			touch_present(), tp.n_contact, tp.n_tap, tp.n_swipe, tp.n_long, tp.n_corner, st_ttaps, st_tdrag,
			(double)px, (int)quit_flag, tp.n_dropped, paused);
		bl_exit();
		snd_close();
		kd_text();
		return st_bad ? 3 : 0;
	}
	bl_exit();
	snd_close();
	kd_text();
	term_restore();
	fb_close();
	return 0;
}

#ifdef VM_FAKE_ALSA
#include "fake_alsa.c"
#endif
