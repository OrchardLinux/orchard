/*
 * arcade_common.h -- code shared by HEDGEWICK (hedgewick.c) and the game
 * picker ARCADE (arcade.c). Each of them includes it once; everything here is
 * static. Nothing here is shared with voidmoths.c: the sound and input code
 * started as a copy of VOIDMOTHS' and follows the same rules.
 *
 *  - framebuffer: /dev/fb0 (simpledrm's fbdev, 1080x1920 XRGB8888 on the N66),
 *    geometry, stride and colour bitfields read with FBIOGET_VSCREENINFO /
 *    FBIOGET_FSCREENINFO; 16, 24 and 32 bpp. The program draws into a small
 *    8-bit "canvas" (one byte per logical pixel, a palette index), which is
 *    scaled up by an integer factor and centred. Only the canvas rows that
 *    changed since the last frame are converted and copied into the
 *    mmap()ed framebuffer (the way VOIDMOTHS draws, the path proven on the
 *    phone); --fb-write sends them with pwrite() instead (mmap() then only as
 *    a fallback). Either way a frame where little moves costs little.
 *  - console: the VT (/dev/tty1) is put in KD_GRAPHICS while drawing, so
 *    fbcon does not draw over the picture, and back in KD_TEXT on every way
 *    out: normal exit, SIGTERM/SIGINT/SIGHUP/SIGQUIT, and the fatal signals.
 *  - input: every /dev/input/event* with a volume, home, power or keyboard
 *    key, grabbed (EVIOCGRAB), read without blocking.
 *  - sound: the speaker card only ("Speaker" / "iPhone Speaker"), bare ALSA
 *    ioctls, amp volume -20 dB (never above -6 dB, read back), mix limited to
 *    -6 dBFS, silent on any problem. See README-maze.md.
 *  - notices: the first line of /run/notice, shown for 6 s with a soft chime
 *    whenever the file's mtime (or inode or size) changes.
 *  - screen off: every key event goes through backlight.h (Power darkens the
 *    screen, Home or Power lights it again; see there) before on_key().
 *  - a 5x7 bitmap font and the shared palette.
 *
 * Test builds (-DMZ_TEST) take a fake framebuffer from $FAKE_FB and log
 * console mode changes to $FAKE_KD instead of calling KDSETMODE; with
 * -DMZ_FAKE_ALSA the ALSA ioctls go to src/fake_alsa.c. Phone builds have
 * neither.
 */
#ifndef ARCADE_COMMON_H
#define ARCADE_COMMON_H

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
#include <sys/resource.h>
#include <sys/stat.h>
#include <sys/types.h>
#include <time.h>
#include <unistd.h>
#include <sound/asound.h>
#include <sound/tlv.h>
#include "backlight.h"

#define FN static __attribute__((unused))
#define HZ 30                   /* ticks per second */

static const char *prog_name = "?";
static int64_t t_start;
static long tick;               /* 30 Hz ticks since start */
static int bot;                 /* headless self-test: no real devices */

FN int64_t now_ns(void)
{
	struct timespec ts;
	clock_gettime(CLOCK_MONOTONIC, &ts);
	return (int64_t)ts.tv_sec * 1000000000LL + ts.tv_nsec;
}

/* One line to stderr (/run/<program>.log on the phone), seconds since start. */
__attribute__((format(printf, 1, 2)))
FN void logmsg(const char *fmt, ...)
{
	va_list ap;
	fprintf(stderr, "%s %7.1f: ", prog_name, (double)(now_ns() - t_start) / 1e9);
	va_start(ap, fmt);
	vfprintf(stderr, fmt, ap);
	va_end(ap);
	fputc('\n', stderr);
	fflush(stderr);
}

FN void cpu_report(void)
{
	struct rusage ru;
	double wall = (double)(now_ns() - t_start) / 1e9, u, s;
	if (getrusage(RUSAGE_SELF, &ru) != 0) return;
	u = (double)ru.ru_utime.tv_sec + (double)ru.ru_utime.tv_usec / 1e6;
	s = (double)ru.ru_stime.tv_sec + (double)ru.ru_stime.tv_usec / 1e6;
	logmsg("cpu: %.2f s user + %.2f s sys in %.1f s (%.2f %% of one core)",
	       u, s, wall, wall > 0 ? 100.0 * (u + s) / wall : 0.0);
}

/* --------------------------------------------------------------- palette */
/*
 * One palette for both programs. Sprites are strings; each letter names a
 * colour, '.' is transparent.
 */
static const struct { char ch; uint32_t rgb; } palette[] = {
	{ 'K', 0x000000 },      /* black */
	{ 'N', 0x0a1020 },      /* night: the maze paths */
	{ 'n', 0x18213a },      /* panel */
	{ 'G', 0x113a1e },      /* hedge, dark */
	{ 'g', 0x1d5a2b },      /* hedge */
	{ 'L', 0x3f9a45 },      /* hedge, lit edge */
	{ 'l', 0x8fd16a },      /* leaf highlight */
	{ 'O', 0xff8a1f },      /* ember */
	{ 'o', 0xb4460e },      /* ember, dark */
	{ 'Y', 0xffd24a },      /* lantern gold */
	{ 'y', 0xfff3b0 },      /* pale glow */
	{ 'W', 0xffffff },      /* white */
	{ 'w', 0xc8d0e0 },      /* light grey */
	{ 'S', 0x5b6b85 },      /* slate */
	{ 's', 0x2c3548 },      /* dark slate */
	{ 'T', 0x2ab5b5 },      /* teal: the lantern-bearer's cloak */
	{ 't', 0x0e5e66 },      /* dark teal */
	{ 'B', 0x7cc8ff },      /* sky blue: glow-bloom petals */
	{ 'b', 0x2a5bd7 },      /* blue */
	{ 'V', 0xb58cff },      /* moth violet */
	{ 'v', 0x5a2fa0 },      /* moth, dark */
	{ 'F', 0xb7e34d },      /* toad */
	{ 'f', 0x4f7a1c },      /* toad, dark */
	{ 'R', 0xe0344a },      /* bat crimson */
	{ 'r', 0x6e1428 },      /* bat, dark */
	{ 'A', 0xd39a5a },      /* owl tan */
	{ 'a', 0x6e4422 },      /* owl brown */
	{ 'D', 0x2e2216 },      /* den soil */
	{ 'd', 0x9a6a32 },      /* gate wood */
	{ 'C', 0xd8e8ff },      /* dazzled, pale */
	{ 'c', 0x7f90c0 },      /* dazzled, mid */
	{ 'P', 0xff6fae },      /* pink */
	{ 'M', 0xe040e0 },      /* magenta (VOIDMOTHS empress) */
	{ 'E', 0x70f070 },      /* green (VOIDMOTHS dart) */
	{ 'Z', 0xf0c8a0 },      /* face */
	{ 'H', 0x6a7a99 },      /* dim text */
	{ 'h', 0x10182c },      /* card background */
	{ 'Q', 0x3a4a6a },      /* card border, idle */
	{ 'U', 0x8a5230 },      /* wood (GLINT's board, in the picker) */
	{ 'u', 0x5a2f1b },      /* wood, dark grain */
	{ 'k', 0xa86a3c },      /* wood, light grain */
	{ 'I', 0xe0b955 },      /* brass */
	{ 'i', 0x140c06 },      /* a hole in the board */
	{ 'X', 0xdff0ff },      /* glass marble, light */
	{ 'x', 0x5f8fc0 },      /* glass marble, shade */
	{ 'J', 0x9be28a },      /* the cup's green */
};
#define NPAL ((int)(sizeof palette / sizeof palette[0]))
static uint8_t pal_of[128];     /* letter -> palette index */

FN int pc(char ch) { return pal_of[(unsigned char)ch & 127]; }

/* ------------------------------------------------------------ framebuffer */

typedef struct { int off, len; } Bitf;
static int fb_use_write;        /* --fb-write: pwrite() rows instead of the mapping */
static struct {
	int fd, w, h, bpp, Bpp, stride, fake;
	size_t base, size;
	Bitf r, g, b, a;
	uint8_t *map;
	size_t maplen;
	int cw, ch, scale, ox, oy;
	uint8_t *cv, *prev;     /* canvas now, as on screen */
	uint32_t pix[256];      /* palette index -> pixel value */
	uint8_t *row;           /* one physical row */
	int full;               /* repaint everything next present */
	long frames, rows_written;
	long long bytes;
	char desc[128];
} fb = { .fd = -1 };

FN void fb_pack_palette(void)
{
	for (int i = 0; i < 256; i++) {
		uint32_t rgb = i < NPAL ? palette[i].rgb : 0, v = 0;
		const Bitf *f[3] = { &fb.r, &fb.g, &fb.b };
		uint32_t c8[3] = { (rgb >> 16) & 255, (rgb >> 8) & 255, rgb & 255 };
		for (int k = 0; k < 3; k++) {
			int len = f[k]->len > 8 ? 8 : f[k]->len;
			if (len <= 0) continue;
			v |= (c8[k] >> (8 - len)) << (f[k]->off + (f[k]->len - len));
		}
		if (fb.a.len > 0 && fb.a.len <= 8) v |= ((1u << fb.a.len) - 1u) << fb.a.off;   /* opaque */
		fb.pix[i] = v;
	}
}

static void fb_defaults(int bpp, int bgr)
{
	if (bpp == 16) {
		fb.r = (Bitf){ bgr ? 0 : 11, 5 }; fb.g = (Bitf){ 5, 6 }; fb.b = (Bitf){ bgr ? 11 : 0, 5 };
	} else {
		fb.r = (Bitf){ bgr ? 0 : 16, 8 }; fb.g = (Bitf){ 8, 8 }; fb.b = (Bitf){ bgr ? 16 : 0, 8 };
	}
	fb.a = (Bitf){ 0, 0 };
}

#ifdef MZ_TEST
/* FAKE_FB=path:WxHxBPP[:stride][:bgr] -- a plain file stands in for fb0. */
static int fb_open_fake(const char *spec)
{
	char path[256];
	const char *c = strchr(spec, ':');
	int w, h, bpp, stride = 0, bgr = 0;
	if (!c || (size_t)(c - spec) >= sizeof path) return -1;
	memcpy(path, spec, (size_t)(c - spec));
	path[c - spec] = 0;
	if (sscanf(c + 1, "%dx%dx%d", &w, &h, &bpp) != 3) return -1;
	c = strchr(c + 1, ':');
	while (c) {
		if (!strncmp(c + 1, "bgr", 3)) bgr = 1;
		else stride = atoi(c + 1);
		c = strchr(c + 1, ':');
	}
	if (w < 1 || h < 1 || w > 8192 || h > 8192 || (bpp != 16 && bpp != 24 && bpp != 32)) return -1;
	fb.w = w; fb.h = h; fb.bpp = bpp; fb.Bpp = bpp / 8;
	fb.stride = stride >= w * fb.Bpp ? stride : w * fb.Bpp;
	fb.base = 0;
	fb.size = (size_t)fb.stride * (size_t)h;
	fb_defaults(bpp, bgr);
	if (bpp == 32 && !bgr) fb.a = (Bitf){ 24, 8 };   /* ARGB: tests the alpha path */
	fb.fd = open(path, O_RDWR | O_CREAT | O_CLOEXEC, 0644);
	if (fb.fd < 0) return -1;
	if (ftruncate(fb.fd, (off_t)fb.size) != 0) return -1;
	fb.fake = 1;
	return 0;
}
#endif

/* 0 on success. */
FN int fb_open(const char *path)
{
	struct fb_var_screeninfo v;
	struct fb_fix_screeninfo f;
#ifdef MZ_TEST
	const char *fake = getenv("FAKE_FB");
	if (fake) {
		if (fb_open_fake(fake) < 0) { logmsg("fb: bad FAKE_FB '%s'", fake); return -1; }
		goto done;
	}
#endif
	fb.fd = open(path, O_RDWR | O_CLOEXEC);
	if (fb.fd < 0) { logmsg("fb: %s: %s", path, strerror(errno)); return -1; }
	memset(&v, 0, sizeof v);
	memset(&f, 0, sizeof f);
	if (ioctl(fb.fd, FBIOGET_VSCREENINFO, &v) < 0 || ioctl(fb.fd, FBIOGET_FSCREENINFO, &f) < 0) {
		logmsg("fb: %s: FBIOGET_*SCREENINFO: %s", path, strerror(errno));
		return -1;
	}
	fb.w = (int)v.xres; fb.h = (int)v.yres; fb.bpp = (int)v.bits_per_pixel; fb.Bpp = fb.bpp / 8;
	fb.stride = (int)f.line_length;
	if (fb.bpp != 16 && fb.bpp != 24 && fb.bpp != 32) { logmsg("fb: %d bpp is not supported", fb.bpp); return -1; }
	if (f.type != FB_TYPE_PACKED_PIXELS || (f.visual != FB_VISUAL_TRUECOLOR && f.visual != FB_VISUAL_DIRECTCOLOR)) {
		logmsg("fb: type %u visual %u is not packed true colour", f.type, f.visual);
		return -1;
	}
	if (fb.w < 1 || fb.h < 1 || fb.w > 8192 || fb.h > 8192) { logmsg("fb: odd size %dx%d", fb.w, fb.h); return -1; }
	if (fb.stride < fb.w * fb.Bpp) fb.stride = fb.w * fb.Bpp;
	fb.r = (Bitf){ (int)v.red.offset, (int)v.red.length };
	fb.g = (Bitf){ (int)v.green.offset, (int)v.green.length };
	fb.b = (Bitf){ (int)v.blue.offset, (int)v.blue.length };
	fb.a = (Bitf){ (int)v.transp.offset, (int)v.transp.length };
	if (!fb.r.len || !fb.g.len || !fb.b.len) fb_defaults(fb.bpp, 0);
	fb.base = (size_t)v.yoffset * (size_t)fb.stride + (size_t)v.xoffset * (size_t)fb.Bpp;
	fb.size = f.smem_len;
	if (fb.size && fb.base + (size_t)fb.stride * (size_t)fb.h > fb.size) {
		logmsg("fb: offset %zu + %d rows of %d bytes does not fit in %zu", fb.base, fb.h, fb.stride, fb.size);
		fb.base = 0;
		if ((size_t)fb.stride * (size_t)fb.h > fb.size) return -1;
	}
#ifdef MZ_TEST
done:
#endif
	snprintf(fb.desc, sizeof fb.desc, "%dx%d %d bpp, stride %d, r%d/%d g%d/%d b%d/%d a%d/%d%s",
		 fb.w, fb.h, fb.bpp, fb.stride, fb.r.off, fb.r.len, fb.g.off, fb.g.len,
		 fb.b.off, fb.b.len, fb.a.off, fb.a.len, fb.fake ? " (fake)" : "");
	fb_pack_palette();
	fb.row = malloc((size_t)fb.w * (size_t)fb.Bpp + 16);
	if (!fb.row) return -1;
	if (!fb_use_write) {
		/* the mapping, as VOIDMOTHS does; if it fails, pwrite() */
		fb.maplen = fb.base + (size_t)fb.stride * (size_t)fb.h;
		fb.map = mmap(NULL, fb.maplen, PROT_READ | PROT_WRITE, MAP_SHARED, fb.fd, 0);
		if (fb.map == MAP_FAILED) {
			logmsg("fb: mmap: %s; writing rows with pwrite() instead", strerror(errno));
			fb.map = NULL;
		}
	}
	snprintf(fb.desc + strlen(fb.desc), sizeof fb.desc - strlen(fb.desc), ", %s", fb.map ? "mmap" : "pwrite");
	return 0;
}

/* The self-test draws into a fake framebuffer only in test builds. */
FN int fake_fb_given(void)
{
#ifdef MZ_TEST
	return getenv("FAKE_FB") != NULL;
#else
	return 0;
#endif
}

/* Write len bytes at byte offset off of the visible screen. */
static void fb_out(size_t off, const void *buf, size_t len)
{
	const uint8_t *p = buf;
	off += fb.base;
	if (fb.map) { memcpy(fb.map + off, p, len); goto count; }
	while (len) {
		ssize_t n = pwrite(fb.fd, p, len, (off_t)off);
		if (n < 0 && errno == EINTR) continue;
		if (n <= 0) {
			/* fall back to mmap once */
			logmsg("fb: pwrite: %s; trying mmap", n < 0 ? strerror(errno) : "short write");
			fb.maplen = fb.base + (size_t)fb.stride * (size_t)fb.h;
			fb.map = mmap(NULL, fb.maplen, PROT_READ | PROT_WRITE, MAP_SHARED, fb.fd, 0);
			if (fb.map == MAP_FAILED) { fb.map = NULL; logmsg("fb: mmap: %s", strerror(errno)); return; }
			memcpy(fb.map + off, p, len);
			break;
		}
		p += n; off += (size_t)n; len -= (size_t)n;
	}
count:
	fb.rows_written++;
}

static inline void fb_store(uint8_t *d, uint32_t v)
{
	switch (fb.Bpp) {
	case 4: memcpy(d, &v, 4); break;
	case 2: { uint16_t s = (uint16_t)v; memcpy(d, &s, 2); break; }
	default: d[0] = (uint8_t)v; d[1] = (uint8_t)(v >> 8); d[2] = (uint8_t)(v >> 16); break;
	}
}

/* Choose the canvas: cw x ch logical pixels, scaled by the largest integer
 * that fits and centred. */
FN int fb_canvas(int cw, int ch)
{
	int sx = fb.w / cw, sy = fb.h / ch;
	fb.cw = cw; fb.ch = ch;
	fb.scale = sx < sy ? sx : sy;
	if (fb.scale < 1) fb.scale = 1;
	fb.ox = (fb.w - cw * fb.scale) / 2;
	fb.oy = (fb.h - ch * fb.scale) / 2;
	free(fb.cv); free(fb.prev);
	fb.cv = calloc((size_t)cw * (size_t)ch, 1);
	fb.prev = calloc((size_t)cw * (size_t)ch, 1);
	fb.full = 1;
	return fb.cv && fb.prev ? 0 : -1;
}

/* Build physical pixels of canvas row cy, logical columns x0..x1, into
 * fb.row; returns the byte offset in the screen row and sets *len. */
static size_t fb_span(int cy, int x0, int x1, size_t *len)
{
	const uint8_t *src = fb.cv + (size_t)cy * (size_t)fb.cw;
	int px0 = fb.ox + x0 * fb.scale, px1 = fb.ox + (x1 + 1) * fb.scale;
	int sc = fb.scale, B = fb.Bpp;
	uint8_t *d;
	if (px0 < 0) px0 = 0;
	if (px1 > fb.w) px1 = fb.w;
	if (px1 <= px0) { *len = 0; return 0; }
	d = fb.row;
	for (int px = px0; px < px1; ) {
		int lx = (px - fb.ox) / sc, n = fb.ox + (lx + 1) * sc - px;
		uint32_t v = fb.pix[src[lx]];
		if (px + n > px1) n = px1 - px;
		if (B == 4) {
			for (int k = 0; k < n; k++, d += 4) memcpy(d, &v, 4);
		} else {
			for (int k = 0; k < n; k++, d += B) fb_store(d, v);
		}
		px += n;
	}
	*len = (size_t)(px1 - px0) * (size_t)B;
	return (size_t)px0 * (size_t)B;
}

/* Put the canvas on the screen: only rows that changed, from the first to
 * the last changed pixel of each. Returns the bytes written. */
FN long fb_present(void)
{
	long long before = fb.bytes;
	int sc = fb.scale;
	if (fb.fd < 0 || !fb.cv) return 0;
	if (fb.full) {
		/* everything, borders included */
		uint32_t black = fb.pix[pc('K')];
		for (int y = 0; y < fb.h; y++) {
			int cy = y - fb.oy;
			size_t len, off;
			for (int x = 0; x < fb.w; x++) fb_store(fb.row + (size_t)x * (size_t)fb.Bpp, black);
			if (cy >= 0 && cy < fb.ch * sc) {
				off = fb_span(cy / sc, 0, fb.cw - 1, &len);
				/* fb_span wrote at fb.row from 0; move it to its place */
				if (len && off) memmove(fb.row + off, fb.row, len);
				if (len) {
					/* the borders left of the canvas were overwritten by the move source */
					for (size_t x = 0; x < off; x += (size_t)fb.Bpp) fb_store(fb.row + x, black);
				}
			}
			fb_out((size_t)y * (size_t)fb.stride, fb.row, (size_t)fb.w * (size_t)fb.Bpp);
			fb.bytes += (long long)fb.w * fb.Bpp;
		}
		fb.full = 0;
	} else {
		/* changed runs of each row; runs closer than GAP pixels are joined
		 * (one write costs about as much as copying a few hundred bytes) */
		enum { GAP = 16 };
		for (int cy = 0; cy < fb.ch; cy++) {
			const uint8_t *a = fb.cv + (size_t)cy * (size_t)fb.cw, *b = fb.prev + (size_t)cy * (size_t)fb.cw;
			int x = 0;
			if (!memcmp(a, b, (size_t)fb.cw)) continue;
			while (x < fb.cw) {
				int x0, x1, same = 0;
				size_t len, off;
				while (x < fb.cw && a[x] == b[x]) x++;
				if (x >= fb.cw) break;
				x0 = x1 = x;
				for (x++; x < fb.cw && same < GAP; x++) {
					if (a[x] != b[x]) { x1 = x; same = 0; }
					else same++;
				}
				x = x1 + 1;
				off = fb_span(cy, x0, x1, &len);
				if (!len) continue;
				for (int k = 0; k < sc; k++) {
					int y = fb.oy + cy * sc + k;
					if (y < 0 || y >= fb.h) continue;
					fb_out((size_t)y * (size_t)fb.stride + off, fb.row, len);
					fb.bytes += (long long)len;
				}
			}
		}
	}
	memcpy(fb.prev, fb.cv, (size_t)fb.cw * (size_t)fb.ch);
	fb.frames++;
	return (long)(fb.bytes - before);
}

/* Read the screen back and save it as a binary PPM (tests, --shot). */
FN int fb_dump_ppm(const char *path)
{
	FILE *o = fopen(path, "wb");
	uint8_t *line = malloc((size_t)fb.stride);
	if (!o || !line) { if (o) fclose(o); free(line); return -1; }
	fprintf(o, "P6\n%d %d\n255\n", fb.w, fb.h);
	for (int y = 0; y < fb.h; y++) {
		size_t off = fb.base + (size_t)y * (size_t)fb.stride;
		if (fb.map) memcpy(line, fb.map + off, (size_t)fb.stride);
		else if (pread(fb.fd, line, (size_t)fb.stride, (off_t)off) != (ssize_t)fb.stride) memset(line, 0, (size_t)fb.stride);
		for (int x = 0; x < fb.w; x++) {
			const uint8_t *p = line + (size_t)x * (size_t)fb.Bpp;
			uint32_t v = fb.Bpp == 4 ? (uint32_t)p[0] | (uint32_t)p[1] << 8 | (uint32_t)p[2] << 16 | (uint32_t)p[3] << 24
				   : fb.Bpp == 2 ? (uint32_t)p[0] | (uint32_t)p[1] << 8
				   : (uint32_t)p[0] | (uint32_t)p[1] << 8 | (uint32_t)p[2] << 16;
			const Bitf *f[3] = { &fb.r, &fb.g, &fb.b };
			uint8_t rgb[3];
			for (int k = 0; k < 3; k++) {
				int len = f[k]->len > 8 ? 8 : f[k]->len;
				uint32_t c = (v >> (f[k]->off + f[k]->len - len)) & ((1u << len) - 1u);
				rgb[k] = (uint8_t)(len >= 8 ? c : (c << (8 - len)) | (c >> (2 * len - 8 > 0 ? 2 * len - 8 : 0)));
			}
			fwrite(rgb, 1, 3, o);
		}
	}
	free(line);
	return fclose(o);
}

/* ---------------------------------------------------------------- canvas */

FN void cv_clear(int c) { memset(fb.cv, c, (size_t)fb.cw * (size_t)fb.ch); }

static inline void cv_px(int x, int y, int c)
{
	if ((unsigned)x < (unsigned)fb.cw && (unsigned)y < (unsigned)fb.ch) fb.cv[(size_t)y * (size_t)fb.cw + (size_t)x] = (uint8_t)c;
}

FN void cv_rect(int x, int y, int w, int h, int c)
{
	if (x < 0) { w += x; x = 0; }
	if (y < 0) { h += y; y = 0; }
	if (x + w > fb.cw) w = fb.cw - x;
	if (y + h > fb.ch) h = fb.ch - y;
	if (w <= 0 || h <= 0) return;
	for (int j = 0; j < h; j++) memset(fb.cv + (size_t)(y + j) * (size_t)fb.cw + (size_t)x, c, (size_t)w);
}

FN void cv_frame(int x, int y, int w, int h, int c)
{
	cv_rect(x, y, w, 1, c);
	cv_rect(x, y + h - 1, w, 1, c);
	cv_rect(x, y, 1, h, c);
	cv_rect(x + w - 1, y, 1, h, c);
}

/* A box with a border and rounded-off corners. */
FN void cv_box(int x, int y, int w, int h, int fill, int border)
{
	cv_rect(x + 1, y + 1, w - 2, h - 2, fill);
	cv_rect(x + 1, y, w - 2, 1, border);
	cv_rect(x + 1, y + h - 1, w - 2, 1, border);
	cv_rect(x, y + 1, 1, h - 2, border);
	cv_rect(x + w - 1, y + 1, 1, h - 2, border);
}

/*
 * A sprite: h strings of equal length, one palette letter per pixel, '.'
 * transparent. flip mirrors it left-right; remap (may be NULL) recolours
 * palette indices; sc scales it up.
 */
FN void cv_sprite(int x, int y, const char *const *rows, int h, int flip, const uint8_t *remap, int sc)
{
	for (int j = 0; j < h; j++) {
		int w = (int)strlen(rows[j]);
		for (int i = 0; i < w; i++) {
			char ch = rows[j][flip ? w - 1 - i : i];
			int c;
			if (ch == '.' || ch == ' ') continue;
			c = pc(ch);
			if (remap) c = remap[c];
			if (sc == 1) cv_px(x + i, y + j, c);
			else cv_rect(x + i * sc, y + j * sc, sc, sc, c);
		}
	}
}

/* ------------------------------------------------------------------ font */
/* 5x7 glyphs, rows top to bottom, bit 4 = left column. ASCII 32..126, and
 * 1..5: arrow left, right, up, down, a middle dot. Lower case is drawn as
 * upper case. */
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
	/* 'a'..'z' are drawn from 'A'..'Z'; these 26 slots are unused */
	{ 0 }, { 0 }, { 0 }, { 0 }, { 0 }, { 0 }, { 0 }, { 0 }, { 0 }, { 0 }, { 0 }, { 0 }, { 0 },
	{ 0 }, { 0 }, { 0 }, { 0 }, { 0 }, { 0 }, { 0 }, { 0 }, { 0 }, { 0 }, { 0 }, { 0 }, { 0 },
	{ 0x02,0x04,0x04,0x08,0x04,0x04,0x02 }, { 0x04,0x04,0x04,0x04,0x04,0x04,0x04 },
	{ 0x08,0x04,0x04,0x02,0x04,0x04,0x08 }, { 0x00,0x00,0x08,0x15,0x02,0x00,0x00 },
};
static const uint8_t font_extra[5][7] = {
	{ 0x00,0x04,0x08,0x1F,0x08,0x04,0x00 },     /* \1 arrow left */
	{ 0x00,0x04,0x02,0x1F,0x02,0x04,0x00 },     /* \2 arrow right */
	{ 0x04,0x0E,0x15,0x04,0x04,0x04,0x00 },     /* \3 arrow up */
	{ 0x00,0x04,0x04,0x04,0x15,0x0E,0x04 },     /* \4 arrow down */
	{ 0x00,0x00,0x0C,0x0C,0x00,0x00,0x00 },     /* \5 middle dot */
};

static const uint8_t *glyph(unsigned char ch)
{
	static const uint8_t box[7] = { 0x1F,0x11,0x11,0x11,0x11,0x11,0x1F };
	if (ch >= 1 && ch <= 5) return font_extra[ch - 1];
	if (ch >= 'a' && ch <= 'z') ch = (unsigned char)(ch - 32);
	if (ch >= 32 && ch < 127) return font57[ch - 32];
	return box;
}

FN int txt_w(const char *s, int sz) { int n = (int)strlen(s); return n ? (n * 6 - 1) * sz : 0; }

/* Text at (x, y), sz times the 5x7 size; shadow >= 0 draws a drop shadow. */
FN void txt(int x, int y, const char *s, int c, int sz, int shadow)
{
	for (; *s; s++, x += 6 * sz) {
		const uint8_t *g = glyph((unsigned char)*s);
		for (int j = 0; j < 7; j++)
			for (int i = 0; i < 5; i++)
				if (g[j] & (0x10 >> i)) {
					if (shadow >= 0) cv_rect(x + i * sz + (sz + 1) / 2, y + j * sz + (sz + 1) / 2, sz, sz, shadow);
					cv_rect(x + i * sz, y + j * sz, sz, sz, c);
				}
	}
}

FN void txt_c(int cx, int y, const char *s, int c, int sz, int shadow)
{
	txt(cx - txt_w(s, sz) / 2, y, s, c, sz, shadow);
}

/*
 * Word-wrap s into at most maxl lines of at most w characters each (w < 64).
 * A word longer than a line is cut. If the text does not fit, the last line
 * ends in "...". Returns the number of lines.
 */
FN int wrap_text(const char *s, int w, char out[][64], int maxl)
{
	int n = 0;
	if (w > 63) w = 63;
	if (w < 4) w = 4;
	while (*s == ' ') s++;
	while (*s && n < maxl) {
		int len = (int)strlen(s), cut;
		if (len <= w) cut = len;
		else {
			cut = w;
			while (cut > 0 && s[cut] != ' ') cut--;
			if (cut == 0) cut = w;
		}
		memcpy(out[n], s, (size_t)cut);
		out[n][cut] = 0;
		while (cut > 0 && out[n][cut - 1] == ' ') out[n][--cut] = 0;
		s += cut;
		while (*s == ' ') s++;
		n++;
		if (*s && n == maxl) {
			int l = (int)strlen(out[n - 1]);
			if (l > w - 3) l = w - 3;
			memcpy(out[n - 1] + l, "...", 4);
		}
	}
	return n;
}

/* -------------------------------------------------------------- console */

static int kd_fd = -1;
static volatile sig_atomic_t kd_graphics_on;
#ifdef MZ_TEST
static char kd_fake[256];       /* $FAKE_KD: a log of mode changes instead of KDSETMODE */
#endif

/* Async-signal-safe: also called from the fatal-signal handler. */
static void kd_set(int graphics)
{
#ifdef MZ_TEST
	if (kd_fake[0]) {
		int fd = open(kd_fake, O_WRONLY | O_CREAT | O_APPEND | O_CLOEXEC, 0644);
		if (fd >= 0) {
			char b[96];
			size_t n = 0;
			const char *m = graphics ? " graphics\n" : " text\n";
			for (const char *p = prog_name; *p && n < 60; p++) b[n++] = *p;
			for (const char *p = m; *p; p++) b[n++] = *p;
			if (write(fd, b, n) < 0) { /* nothing to do in a signal handler */ }
			close(fd);
		}
		kd_graphics_on = graphics;
		return;
	}
#endif
	if (kd_fd >= 0) ioctl(kd_fd, KDSETMODE, graphics ? KD_GRAPHICS : KD_TEXT);
	kd_graphics_on = graphics;
}

FN void tty_write(const char *s)
{
	size_t n = strlen(s);
	while (kd_fd >= 0 && n) {
		ssize_t k = write(kd_fd, s, n);
		if (k < 0 && errno == EINTR) continue;
		if (k <= 0) break;
		s += k; n -= (size_t)k;
	}
}

FN int tty_open(const char *path)
{
#ifdef MZ_TEST
	const char *k = getenv("FAKE_KD");
	if (k) snprintf(kd_fake, sizeof kd_fake, "%s", k);
#endif
	kd_fd = open(path, O_RDWR | O_NOCTTY | O_CLOEXEC);
	if (kd_fd < 0) { logmsg("console: %s: %s", path, strerror(errno)); return -1; }
	return 0;
}

/* Take the console for drawing: no blanking, no cursor, KD_GRAPHICS. */
FN void console_graphics(void)
{
	tty_write("\033[9;0]\033[14;0]\033[?25l");
	kd_set(1);
}

/* Give it back: an empty text screen with the cursor on. The clear is
 * written while still in graphics mode, so the old text never flashes up. */
FN void console_text(int cursor)
{
	tty_write(cursor ? "\033[0m\033[H\033[2J\033[?25h" : "\033[0m\033[H\033[2J\033[?25l");
	kd_set(0);
}

static volatile sig_atomic_t quit_sig;
static void on_quit_signal(int s) { quit_sig = s; }
static void on_fatal_signal(int s)
{
	if (kd_graphics_on) kd_set(0);
	bl_on_sigsafe();
	signal(s, SIG_DFL);
	raise(s);
}
static void at_exit_console(void)
{
	if (kd_graphics_on) kd_set(0);
	bl_on_sigsafe();
}

FN void install_signals(void)
{
	struct sigaction sa;
	static const int quit[] = { SIGTERM, SIGINT, SIGHUP, SIGQUIT };
	static const int fatal[] = { SIGSEGV, SIGBUS, SIGFPE, SIGILL, SIGABRT };
	memset(&sa, 0, sizeof sa);
	sigemptyset(&sa.sa_mask);
	sa.sa_handler = on_quit_signal;
	sa.sa_flags = 0;                /* no SA_RESTART: poll() and waitpid() return */
	for (size_t i = 0; i < sizeof quit / sizeof quit[0]; i++) sigaction(quit[i], &sa, NULL);
	sa.sa_handler = on_fatal_signal;
	sa.sa_flags = SA_RESETHAND | SA_NODEFER;
	for (size_t i = 0; i < sizeof fatal / sizeof fatal[0]; i++) sigaction(fatal[i], &sa, NULL);
	signal(SIGPIPE, SIG_IGN);
	signal(SIGTTOU, SIG_IGN);
	atexit(at_exit_console);
}

/* ------------------------------------------------------------------ input */

#define MAXDEV 16
#define NLONG ((KEY_CNT + 8 * (int)sizeof(long) - 1) / (8 * (int)sizeof(long)))
static int dev_fd[MAXDEV], ndev;
static char dev_desc[96];
static uint8_t keydown[KEY_CNT];
static int have_home, have_power, have_vol, grab = 1, rescan;
static void on_key(int code, int value);        /* in each program */

static int test_bit(const unsigned long *b, int n)
{
	return (int)((b[n / (8 * (int)sizeof(long))] >> (n % (8 * (int)sizeof(long)))) & 1UL);
}

FN void sync_keys(void)
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

FN void close_devs(void)
{
	for (int i = 0; i < ndev; i++) close(dev_fd[i]);   /* also ends the grab */
	ndev = 0;
	memset(keydown, 0, sizeof keydown);
}

FN void scan_devs(void)
{
	char first[64] = "";
	close_devs();
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
		/* exclusive: the VT keyboard handler does not see the buttons */
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

/* One key event (value 0 or 1), from a device, a script or $FAKE_KEYS: the
 * screen-off keys first (backlight.h), then the program's on_key(). */
FN void key_event(int code, int value)
{
	if (code < 0 || code >= KEY_CNT) return;
	switch (bl_key(code, value)) {
	case BL_PASS: keydown[code] = (uint8_t)value; on_key(code, value); break;
	case BL_STATE: keydown[code] = (uint8_t)value; break;
	case BL_TAP:
		keydown[code] = 1; on_key(code, 1);
		keydown[code] = 0; on_key(code, 0);
		break;
	default: break;
	}
}

FN void read_events(void)
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
				if (e->type != EV_KEY || e->code >= KEY_CNT || e->value > 1) continue;
				key_event(e->code, e->value);
			}
		}
	}
	if (dropped) sync_keys();
}

/* ------------------------------------------------------------------- sound */
/*
 * Effects on the loudspeaker, as in VOIDMOTHS. The speaker is the ALSA card
 * "iPhone Speaker" (id "Speaker"): MCA2 in TDM mode and the CS35L19/21 amp,
 * 48 kHz, S16_LE, mono. The kernel interface is used directly, as tinyalsa
 * does: HW_PARAMS, SW_PARAMS, PREPARE, WRITEI_FRAMES on pcmC<n>D0p, and the
 * control ioctls on controlC<n> for "Speaker Volume" only.
 *
 * Safety: no other card is opened (found by its id, checked again with
 * CARD_INFO before anything is written); the only control written is the
 * amp's "Speaker Volume", once per open, -20 dB by default, never above
 * -6 dB, read back; the mix is hard-limited to PEAK of full scale.
 *
 * Two ways to run: continuous (the game: found within 15 s, then the stream
 * runs the whole time so the amp stays powered) and on demand (the picker:
 * opened for a sound, closed after 2 s of quiet, and always closed before a
 * game starts so the game can open it).
 */
#define SR          48000
#define NVOICE      16
#define SND_CHUNK   256
#define SND_WAIT    (15 * HZ)
#define VOL_DB      -20.0f
#define VOL_MAX_DB  -6.0f
#define PEAK        0.5f

#ifdef MZ_FAKE_ALSA
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

enum { SND_OFF, SND_SEARCH, SND_ON, SND_NONE, SND_FAIL, SND_IDLE };
static Voice vc[NVOICE];
static int snd_state = SND_SEARCH, snd_card = -1, snd_pcm = -1, snd_poll_ok, snd_ondemand, snd_opened_now;
static long snd_deadline, snd_next_try, snd_underruns, snd_quiet_since;
static unsigned long snd_period, snd_bufsize;
static float snd_vol_db = VOL_DB;
static int16_t snd_buf[SND_CHUNK];
static int snd_len, snd_off;
static float hp_x, hp_y;
static const char *snd_root = "";   /* path prefix, test builds only */
static int snd_open(void);
static int snd_find_card(void);

static int snd_live(void)
{
	if (snd_state == SND_IDLE && snd_ondemand && tick >= snd_next_try) {
		snd_next_try = tick + HZ;
		snd_card = snd_find_card();
		if (snd_card >= 0) snd_open();
	}
	return snd_state == SND_ON || bot;
}

FN void voice(int wf, float f0, float f1, float ms, float vol, float duty, int curve, float delay_ms)
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
	snd_quiet_since = tick;
}

FN void note(float f, float start_ms, float ms, float vol, float duty)
{
	voice(W_SQUARE, f, f * 0.995f, ms, vol, duty, 1, start_ms);
}

FN void tnote(float f, float start_ms, float ms, float vol)
{
	voice(W_TRI, f, f, ms, vol, 0.5f, 1, start_ms);
}

/* The notice chime: two soft triangle notes, a fifth apart, falling. */
FN void sfx_chime(void)
{
	tnote(1568.0f, 0.0f, 260.0f, 0.16f);
	tnote(1047.0f, 160.0f, 520.0f, 0.14f);
}

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
			if (v->pos < 96) env *= (float)v->pos / 96.0f;
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
			mix += s * env * v->vol;
			v->freq *= v->fmul;
			if (++v->pos >= v->len) v->on = 0;
		}
		y = mix - hp_x + 0.9974f * hp_y;
		hp_x = mix; hp_y = y;
		if (y > PEAK) y = PEAK;
		if (y < -PEAK) y = -PEAK;
		out[k] = (int16_t)lrintf(y * 32767.0f);
	}
}

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

/* Close the PCM; the state is the caller's business. */
FN void snd_close(void)
{
	if (snd_pcm >= 0) {
		close(snd_pcm);
		logmsg("sound: closed (%ld underruns)", snd_underruns);
	}
	snd_pcm = -1;
	snd_len = snd_off = 0;
	memset(vc, 0, sizeof vc);
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

static int snd_open(void)
{
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
	snd_opened_now = 1;
	snd_quiet_since = tick;
	logmsg("sound: on, card %d, %lu-frame periods, %lu-frame buffer (%lu ms)",
	       snd_card, snd_period, snd_bufsize, snd_bufsize * 1000 / SR);
	return 0;
}

/* Write what fits; never waits. 1 if anything was written. */
FN int snd_pump(void)
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

FN void snd_start_search(void)
{
	snd_state = SND_SEARCH;
	snd_deadline = tick + SND_WAIT;
	snd_next_try = tick;
}

/* On demand (the picker): closed and ready to open for the next sound. */
FN void snd_set_idle(void)
{
	if (snd_pcm >= 0) snd_close();
	if (snd_state != SND_FAIL && snd_state != SND_OFF) snd_state = SND_IDLE;
	snd_next_try = tick;
}

/* The screen goes dark: no sound. The card is closed (on demand: idle);
 * snd_resume() looks for it again when the screen comes back. */
static int snd_was_on;
FN void snd_suspend(void)
{
	if (snd_ondemand) { snd_set_idle(); return; }
	snd_was_on = snd_state == SND_ON || snd_state == SND_SEARCH;
	snd_close();
	if (snd_was_on) {
		snd_state = SND_OFF;
		logmsg("sound: paused while the screen is off");
	}
}

FN void snd_resume(void)
{
	if (snd_ondemand || !snd_was_on) return;
	snd_was_on = 0;
	snd_start_search();
}

/* Once per tick: look for the card, keep the buffer full, and (on demand)
 * close after 2 s without a voice. */
FN void snd_tick(void)
{
	if (snd_state == SND_SEARCH && tick >= snd_next_try) {
		snd_next_try = tick + HZ;
		snd_card = snd_find_card();
		if (snd_card >= 0) {
			snd_open();
		} else if (tick >= snd_deadline) {
			snd_state = SND_NONE;
			logmsg("sound: no 'Speaker' card in %s/proc/asound/cards after %d s; running silent",
			       snd_root, SND_WAIT / HZ);
		}
	}
	if (snd_state == SND_ON) {
		int busy = 0;
		snd_poll_ok = 1;
		snd_pump();
		for (int i = 0; i < NVOICE; i++) busy |= vc[i].on;
		if (busy) snd_quiet_since = tick;
		else if (snd_ondemand && tick - snd_quiet_since > 2 * HZ) snd_set_idle();
	} else if (bot) {
		int16_t scratch[SR / HZ];
		synth(scratch, SR / HZ);
	}
}

/* -------------------------------------------------------------- notices */
/*
 * /run/notice: whenever the file changes (mtime, inode or size), its first
 * line is shown for NOTICE_T ticks with a chime. At start the current file
 * only sets the baseline, so an old notice is not shown again by every
 * program that starts.
 */
#define NOTICE_T (6 * HZ)
static const char *ntc_path = "/run/notice";
static struct { int exists; int64_t mt; ino_t ino; off_t size; } ntc_seen;
static char ntc_text[128];
static int ntc_t;               /* ticks left on screen */
static long ntc_shown;

static int ntc_stat(int64_t *mt, ino_t *ino, off_t *size)
{
	struct stat st;
	if (!ntc_path || stat(ntc_path, &st) != 0) return 0;
	*mt = (int64_t)st.st_mtim.tv_sec * 1000000000LL + st.st_mtim.tv_nsec;
	*ino = st.st_ino;
	*size = st.st_size;
	return 1;
}

/* Take the file as it is now as seen. */
FN void notice_sync(void)
{
	ntc_seen.exists = ntc_stat(&ntc_seen.mt, &ntc_seen.ino, &ntc_seen.size);
}

/* Look at the file now. 1 if a new notice is up (ntc_text, ntc_t, chime). */
FN int notice_check(void)
{
	int64_t mt;
	ino_t ino;
	off_t size;
	int ex;
	FILE *f;
	char line[256];
	size_t n = 0;

	ex = ntc_stat(&mt, &ino, &size);
	if (!ex) { ntc_seen.exists = 0; return 0; }
	if (ntc_seen.exists && mt == ntc_seen.mt && ino == ntc_seen.ino && size == ntc_seen.size) return 0;
	ntc_seen.exists = 1; ntc_seen.mt = mt; ntc_seen.ino = ino; ntc_seen.size = size;
	f = fopen(ntc_path, "re");
	if (!f) return 0;
	if (!fgets(line, sizeof line, f)) line[0] = 0;
	fclose(f);
	/* printable ASCII only; a UTF-8 sequence becomes one '?' */
	for (const unsigned char *p = (const unsigned char *)line; *p && *p != '\n' && *p != '\r' && n < sizeof ntc_text - 1; p++) {
		if (*p == '\t') ntc_text[n++] = ' ';
		else if (*p >= 32 && *p < 127) ntc_text[n++] = (char)*p;
		else if (*p >= 0xc0 || (*p < 0x80)) ntc_text[n++] = '?';
	}
	while (n && ntc_text[n - 1] == ' ') n--;
	ntc_text[n] = 0;
	if (!n) return 0;
	ntc_t = NOTICE_T;
	ntc_shown++;
	logmsg("notice: %s", ntc_text);
	sfx_chime();
	return 1;
}

/* For a program with a 30 Hz tick: call every tick; looks twice a second. */
FN int notice_poll(void)
{
	if (ntc_t > 0) ntc_t--;
	if (tick % (HZ / 2)) return 0;
	return notice_check();
}

/* ------------------------------------------------------------------ misc */

FN void common_init(const char *name)
{
	prog_name = name;
	t_start = now_ns();
	for (int i = 0; i < 128; i++) pal_of[i] = 0;
	for (int i = 0; i < NPAL; i++) pal_of[(unsigned char)palette[i].ch] = (uint8_t)i;
#ifdef MZ_FAKE_ALSA
	snd_root = getenv("FAKE_ALSA_ROOT") ? getenv("FAKE_ALSA_ROOT") : "/nonexistent";
#endif
}

/* The first number in a file, or 0. */
FN long read_number(const char *path)
{
	long v = 0;
	FILE *f = path ? fopen(path, "re") : NULL;
	if (!f) return 0;
	if (fscanf(f, "%ld", &v) != 1 || v < 0) v = 0;
	fclose(f);
	return v;
}

#endif /* ARCADE_COMMON_H */
