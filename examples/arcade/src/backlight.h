/*
 * backlight.h -- "Power = screen off" for the ARCADE programs (arcade,
 * hedgewick, voidmoths, glint). One header, the same code in the picker and
 * in every game, so the buttons do the same everywhere. arcade.c and
 * hedgewick.c get it through arcade_common.h; voidmoths.c and glint.c
 * include it after touch.h. Each program provides logmsg().
 *
 * The backlight: every entry of /sys/class/backlight (the glob "*", never a
 * name: 20e200080.backlight on the N66, the iPads' own). Of each one only
 * two files are ever touched:
 *   bl_power    off: 4 (FB_BLANK_POWERDOWN), on: 0 (FB_BLANK_UNBLANK);
 *   brightness  only when the device has no bl_power: off keeps the value
 *               (in memory, and in /run/arcade-backlight so that a program
 *               started later can put it back) and writes 0; on writes the
 *               kept value back.
 * max_brightness and the other files are never opened.
 *
 * The keys (bl_key(), called for every key event before the program sees
 * it; bl_set_keys() after every look at the input devices):
 *   BLK_PRESS  Home, Power and the volume keys: a Power press darkens the
 *              screen at once;
 *   BLK_LONG   Home and Power, no volume keys (the picker's "next game" is
 *              Power then): Power held BL_LONG_MS (0.8 s) darkens it; a
 *              shorter press reaches the program on its release, as a press
 *              and a release (BL_TAP), so it keeps its old meaning;
 *   BLK_NONE   no backlight, no Power key, or no Home key (Power is the Home
 *              button then): no screen-off; Power does what it always did.
 * While dark every key press is eaten; Home or Power (the press) lights the
 * screen again. The release of an eaten press is eaten too; releases of
 * keys held from before only update the program's key state (BL_STATE).
 * The program stops drawing, sound and its game clock while bl_dark(),
 * ignores the touchscreen (touch_discard()), keeps looking at /run/notice
 * (a new notice lights the screen) and pauses the game when it comes back.
 *
 * Every way out lights it again: bl_exit() on the normal exit,
 * bl_on_sigsafe() from the fatal-signal handlers and atexit, bl_force_on()
 * at every start (a program restarted by init's respawn loop lights a screen
 * that a killed one left dark), in the picker after each game, and in
 * --restore-tty.
 *
 * Test builds (MZ_TEST, TOUCH_TEST or BL_TEST): $FAKE_BL_ROOT stands in for
 * "/" (<root>/sys/class/backlight/<dev>/{bl_power,brightness},
 * <root>/run/arcade-backlight); without it the root is /nonexistent, so a
 * test never sees the real backlight. $FAKE_KEYS plays button presses:
 * "KEYS/EVENTS", KEYS the keys the board has (home,power,vol or none),
 * EVENTS a list MS:KEY[*HOLD_MS],... (KEY home, power, volup, voldown or
 * mute; held 100 ms unless *HOLD_MS; MS counts from bl_init()).
 */
#ifndef BACKLIGHT_H
#define BACKLIGHT_H

#include <dirent.h>
#include <errno.h>
#include <fcntl.h>
#include <linux/input.h>
#include <poll.h>
#include <signal.h>
#include <stdint.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <sys/resource.h>
#include <time.h>
#include <unistd.h>

#if defined(MZ_TEST) || defined(TOUCH_TEST) || defined(BL_TEST)
#define BL_TESTING 1
#endif

#define BL_MAXDEV  4
#define BL_LONG_MS 800          /* BLK_LONG: Power held this long darkens */
#define BL_IDLE_MS 500          /* while dark: look at notices this often */

enum { BLK_NONE, BLK_PRESS, BLK_LONG };
enum { BL_PASS, BL_EAT, BL_STATE, BL_TAP };

static void logmsg(const char *fmt, ...) __attribute__((format(printf, 1, 2)));

static struct {
	char root[160], state_path[200];
	int n, mode, mode_set;
	struct {
		char name[64], power[264], bright[264];
		int has_power;
		long saved;             /* brightness before dark, -1: nothing kept */
		char saved_s[24];       /* the same as text, for the signal handler */
	} d[BL_MAXDEV];
	volatile sig_atomic_t dark;
	int pw_armed;               /* BLK_LONG: Power down, not decided yet */
	int64_t pw_t0;
	int64_t (*clock)(void);     /* the program's clock, ms */
	uint8_t eat[KEY_CNT];       /* presses eaten: their releases are eaten too */
	long n_off, n_on, n_long, n_tap, n_eaten;
	int64_t t_dark;
	double cpu0, dark_s, dark_cpu;
} bl = { .mode = BLK_NONE };

static int64_t bl_ns(void)
{
	struct timespec ts;
	clock_gettime(CLOCK_MONOTONIC, &ts);
	return (int64_t)ts.tv_sec * 1000000000LL + ts.tv_nsec;
}

static int64_t bl_ms(void) { return bl.clock ? bl.clock() : bl_ns() / 1000000; }

static double bl_cpu(void)
{
	struct rusage ru;
	if (getrusage(RUSAGE_SELF, &ru) != 0) return 0.0;
	return (double)ru.ru_utime.tv_sec + (double)ru.ru_utime.tv_usec / 1e6 +
	       (double)ru.ru_stime.tv_sec + (double)ru.ru_stime.tv_usec / 1e6;
}

/* The first number in a sysfs file; 0 on success. */
static int bl_read(const char *path, long *v)
{
	char b[32];
	ssize_t n;
	int fd = open(path, O_RDONLY | O_CLOEXEC);
	if (fd < 0) return -1;
	n = read(fd, b, sizeof b - 1);
	close(fd);
	if (n <= 0) return -1;
	b[n] = 0;
	*v = strtol(b, NULL, 10);
	return 0;
}

/* One write of s; async-signal-safe. 0 on success, else errno. */
static int bl_write(const char *path, const char *s)
{
	size_t len = strlen(s);
	ssize_t w;
	int err, fd = open(path, O_WRONLY | O_CLOEXEC);
	if (fd < 0) return errno ? errno : EIO;
	do w = write(fd, s, len); while (w < 0 && errno == EINTR);
	err = w == (ssize_t)len ? 0 : w < 0 ? errno : EIO;
	close(fd);
	return err;
}

static int bl_cmp(const void *a, const void *b)
{
	return strcmp(((const char *)a), ((const char *)b));
}

/* Every backlight device: the entries of <root>/sys/class/backlight. */
static void bl_scan(void)
{
	char dir[200], names[BL_MAXDEV][64];
	int k = 0;
	DIR *dp;
	struct dirent *e;
	bl.n = 0;
	snprintf(dir, sizeof dir, "%.159s/sys/class/backlight", bl.root);
	dp = opendir(dir);
	if (!dp) return;
	while ((e = readdir(dp)) && k < BL_MAXDEV) {
		if (e->d_name[0] == '.') continue;
		snprintf(names[k++], sizeof names[0], "%.63s", e->d_name);
	}
	closedir(dp);
	qsort(names, (size_t)k, sizeof names[0], bl_cmp);
	for (int i = 0; i < k; i++) {
		__typeof__(bl.d[0]) *dv = &bl.d[bl.n];
		int has_bright;
		memset(dv, 0, sizeof *dv);
		snprintf(dv->name, sizeof dv->name, "%.63s", names[i]);
		snprintf(dv->power, sizeof dv->power, "%.180s/%.63s/bl_power", dir, names[i]);
		snprintf(dv->bright, sizeof dv->bright, "%.180s/%.63s/brightness", dir, names[i]);
		dv->has_power = access(dv->power, W_OK) == 0;
		has_bright = access(dv->bright, W_OK) == 0;
		dv->saved = -1;
		if (dv->has_power || has_bright) bl.n++;
	}
}

/*
 * Light every backlight that may have been left dark: bl_power not 0 is set
 * to 0; a brightness kept in the state file (a program darkened a device
 * without bl_power and did not come back) is written back if the device is
 * at 0. Writes nothing when everything is lit.
 */
static void bl_force_on(const char *why)
{
	FILE *f;
	for (int i = 0; i < bl.n; i++) {
		long v;
		int err;
		if (!bl.d[i].has_power || bl_read(bl.d[i].power, &v) != 0 || v == 0) continue;
		err = bl_write(bl.d[i].power, "0\n");
		logmsg("backlight: %s: bl_power was %ld, set to 0 (%s)%s%s", bl.d[i].name, v, why,
		       err ? ": " : "", err ? strerror(err) : "");
	}
	f = fopen(bl.state_path, "re");
	if (f) {
		char nm[64];
		long val, cur;
		while (fscanf(f, "%63s %ld", nm, &val) == 2)
			for (int i = 0; i < bl.n; i++) {
				if (strcmp(nm, bl.d[i].name) || bl.d[i].has_power || val <= 0) continue;
				if (bl_read(bl.d[i].bright, &cur) == 0 && cur == 0) {
					char s[24];
					int err;
					snprintf(s, sizeof s, "%ld\n", val);
					err = bl_write(bl.d[i].bright, s);
					logmsg("backlight: %s: brightness 0, set back to %ld (%s)%s%s", nm, val, why,
					       err ? ": " : "", err ? strerror(err) : "");
				}
			}
		fclose(f);
		unlink(bl.state_path);
	}
}

/* Look at what is there and light it; clock: the program's ms clock. */
static void bl_init(int64_t (*clock)(void));

/* Only light the screen (--restore-tty): no keys, no log unless it acts. */
static __attribute__((unused)) void bl_restore_only(void)
{
	const char *r = "";
#ifdef BL_TESTING
	r = getenv("FAKE_BL_ROOT") ? getenv("FAKE_BL_ROOT") : "/nonexistent";
#endif
	snprintf(bl.root, sizeof bl.root, "%s", r);
	snprintf(bl.state_path, sizeof bl.state_path, "%s/run/arcade-backlight", r);
	bl_scan();
	bl_force_on("restore");
}

static __attribute__((unused)) int bl_dark(void) { return bl.dark; }
static __attribute__((unused)) int bl_mode(void) { return bl.mode; }

static void bl_off(const char *why)
{
	char info[160] = "";
	int ok = 0;
	FILE *st = NULL;
	if (bl.dark) return;
	for (int i = 0; i < bl.n; i++) {
		long v = -1, cur;
		int err;
		size_t l = strlen(info);
		if (bl.d[i].has_power) {
			err = bl_write(bl.d[i].power, "4\n");
			if (!err) ok++;
			bl_read(bl.d[i].power, &v);
			snprintf(info + l, sizeof info - l, " %s bl_power=%ld%s%s", bl.d[i].name, v, err ? " " : "", err ? strerror(err) : "");
			continue;
		}
		if (bl_read(bl.d[i].bright, &cur) != 0) continue;
		if (cur <= 0) { ok++; continue; }        /* already at 0: nothing to keep */
		if (!st) st = fopen(bl.state_path, "we");
		if (st) fprintf(st, "%s %ld\n", bl.d[i].name, cur);
		if (st) fflush(st);                      /* kept before the light goes */
		bl.d[i].saved = cur;
		snprintf(bl.d[i].saved_s, sizeof bl.d[i].saved_s, "%ld\n", cur);
		err = bl_write(bl.d[i].bright, "0\n");
		if (!err) ok++;
		else bl.d[i].saved = -1;
		bl_read(bl.d[i].bright, &v);
		snprintf(info + l, sizeof info - l, " %s brightness %ld->%ld%s%s", bl.d[i].name, cur, v, err ? " " : "", err ? strerror(err) : "");
	}
	if (st) fclose(st);
	if (!ok) {
		logmsg("screen: could not darken (%s):%s", why, info);
		return;
	}
	bl.dark = 1;
	bl.n_off++;
	bl.t_dark = bl_ns();
	bl.cpu0 = bl_cpu();
	logmsg("screen: off (%s):%s", why, info);
}

static void bl_on(const char *why)
{
	char info[160] = "";
	double s, c;
	if (!bl.dark) return;
	for (int i = 0; i < bl.n; i++) {
		size_t l = strlen(info);
		int err;
		if (bl.d[i].has_power) {
			err = bl_write(bl.d[i].power, "0\n");
			snprintf(info + l, sizeof info - l, " %s bl_power=0%s%s", bl.d[i].name, err ? " " : "", err ? strerror(err) : "");
		} else if (bl.d[i].saved >= 0) {
			err = bl_write(bl.d[i].bright, bl.d[i].saved_s);
			snprintf(info + l, sizeof info - l, " %s brightness=%ld%s%s", bl.d[i].name, bl.d[i].saved, err ? " " : "", err ? strerror(err) : "");
			bl.d[i].saved = -1;
		}
	}
	unlink(bl.state_path);
	bl.dark = 0;
	bl.n_on++;
	bl.pw_armed = 0;
	s = (double)(bl_ns() - bl.t_dark) / 1e9;
	c = bl_cpu() - bl.cpu0;
	bl.dark_s += s;
	bl.dark_cpu += c;
	logmsg("screen: on (%s) after %.1f s dark, %.3f s CPU (%.2f %% of one core):%s", why, s, c,
	       s > 0 ? 100.0 * c / s : 0.0, info);
}

/* From a signal handler or atexit: only open, write, close, unlink. */
static __attribute__((unused)) void bl_on_sigsafe(void)
{
	if (!bl.dark) return;
	for (int i = 0; i < bl.n; i++) {
		if (bl.d[i].has_power) bl_write(bl.d[i].power, "0\n");
		else if (bl.d[i].saved >= 0) bl_write(bl.d[i].bright, bl.d[i].saved_s);
	}
	unlink(bl.state_path);
	bl.dark = 0;
}

/* What Power does on this board (see the top). */
static __attribute__((unused)) void bl_set_keys(int have_home, int have_power, int have_vol)
{
	int m = !bl.n || !have_power || !have_home ? BLK_NONE : have_vol ? BLK_PRESS : BLK_LONG;
	if (bl.mode_set && m == bl.mode) return;
	bl.mode_set = 1;
	bl.mode = m;
	if (m != BLK_LONG) bl.pw_armed = 0;
	logmsg("screen off: %s", m == BLK_PRESS ? "a Power press; Home or Power lights it again"
		 : m == BLK_LONG ? "Power held 0.8 s (no volume keys: a short Power press keeps its meaning); "
				   "Home or Power lights it again"
		 : !bl.n ? "none (no backlight device)"
		 : !have_power ? "none (no Power key)" : "none (no Home key: Power stands in for it)");
	if (bl.dark && m == BLK_NONE) bl_on("no key left to light it");
}

/* Light the screen from outside the keys (a notice, a lost button). */
static __attribute__((unused)) void bl_wake(const char *why) { bl_on(why); }

/*
 * Every key event (value 0 or 1) goes through here first. BL_PASS: the
 * program handles it as always; BL_EAT: it never happened; BL_STATE: a
 * release while dark, update the key state only; BL_TAP: a short Power press
 * (BLK_LONG), just let go: handle a press and a release of it now.
 */
static __attribute__((unused)) int bl_key(int code, int value)
{
	int pw = code == KEY_POWER, home = code == KEY_HOMEPAGE || code == KEY_HOME;
	if (code < 0 || code >= KEY_CNT) return BL_PASS;
	if (value == 0) {
		if (bl.eat[code]) {
			bl.eat[code] = 0;
			if (pw && bl.pw_armed) {
				bl.pw_armed = 0;
				if (!bl.dark) { bl.n_tap++; return BL_TAP; }
			}
			return BL_EAT;
		}
		return bl.dark ? BL_STATE : BL_PASS;
	}
	if (value != 1) return bl.eat[code] || bl.dark ? BL_EAT : BL_PASS;     /* autorepeat */
	bl.eat[code] = 0;
	if (bl.dark) {
		bl.eat[code] = 1;
		bl.n_eaten++;
		if (pw || home) bl_on(pw ? "Power" : "Home");
		return BL_EAT;
	}
	if (pw && bl.mode == BLK_PRESS) {
		bl.eat[code] = 1;
		bl_off("Power");
		return BL_EAT;
	}
	if (pw && bl.mode == BLK_LONG) {
		bl.eat[code] = 1;
		bl.pw_armed = 1;
		bl.pw_t0 = bl_ms();
		return BL_EAT;
	}
	return BL_PASS;
}

/* Call often (every loop pass): Power held long enough darkens. 1 while a
 * press is still undecided (the caller should come back within 50 ms). */
static __attribute__((unused)) int bl_tick(void)
{
	if (bl.pw_armed && !bl.dark && bl_ms() - bl.pw_t0 >= BL_LONG_MS) {
		bl.pw_armed = 0;
		bl.n_long++;
		bl_off("Power held 0.8 s");
	}
	return bl.pw_armed;
}

/* After reading the key state from the devices: eaten keys are not down. */
static __attribute__((unused)) void bl_mask(uint8_t *keydown)
{
	for (int c = 0; c < KEY_CNT; c++)
		if (bl.eat[c]) keydown[c] = 0;
}

/* The normal exit: light it, and one line of statistics. */
static __attribute__((unused)) void bl_exit(void)
{
	bl_on("exit");
	logmsg("screen: offs=%ld ons=%ld longs=%ld taps=%ld eaten=%ld dark_s=%.1f dark_cpu=%.2f",
	       bl.n_off, bl.n_on, bl.n_long, bl.n_tap, bl.n_eaten, bl.dark_s,
	       bl.dark_s > 0 ? 100.0 * bl.dark_cpu / bl.dark_s : 0.0);
}

/* ------------------------------------------------------- test builds only */
#ifdef BL_TESTING
static struct {
	int on, h, p, v, n, i;
	int64_t t0;
	struct { int64_t t; int code, value; } ev[128];
} fkeys;

static void fakekeys_init(void)
{
	const char *s = getenv("FAKE_KEYS"), *sl;
	char buf[1024], *save = NULL;
	if (!s || !(sl = strchr(s, '/'))) return;
	fkeys.h = strstr(s, "home") && strstr(s, "home") < sl;
	fkeys.p = strstr(s, "power") && strstr(s, "power") < sl;
	fkeys.v = strstr(s, "vol") && strstr(s, "vol") < sl;
	snprintf(buf, sizeof buf, "%s", sl + 1);
	for (char *t = strtok_r(buf, ",", &save); t && fkeys.n < 126; t = strtok_r(NULL, ",", &save)) {
		char key[16] = "";
		long at = 0, hold = 100;
		int code;
		if (sscanf(t, "%ld:%15[a-z]*%ld", &at, key, &hold) < 2) { logmsg("FAKE_KEYS: bad event '%s'", t); continue; }
		code = !strcmp(key, "home") ? KEY_HOMEPAGE : !strcmp(key, "power") ? KEY_POWER
		     : !strcmp(key, "volup") ? KEY_VOLUMEUP : !strcmp(key, "voldown") ? KEY_VOLUMEDOWN
		     : !strcmp(key, "mute") ? KEY_MUTE : -1;
		if (code < 0) { logmsg("FAKE_KEYS: unknown key '%s'", key); continue; }
		fkeys.ev[fkeys.n].t = at; fkeys.ev[fkeys.n].code = code; fkeys.ev[fkeys.n++].value = 1;
		fkeys.ev[fkeys.n].t = at + (hold < 1 ? 1 : hold); fkeys.ev[fkeys.n].code = code; fkeys.ev[fkeys.n++].value = 0;
	}
	for (int i = 1; i < fkeys.n; i++)           /* stable sort by time */
		for (int j = i; j > 0 && fkeys.ev[j].t < fkeys.ev[j - 1].t; j--) {
			__typeof__(fkeys.ev[0]) x = fkeys.ev[j];
			fkeys.ev[j] = fkeys.ev[j - 1];
			fkeys.ev[j - 1] = x;
		}
	fkeys.t0 = bl_ms();
	fkeys.on = 1;
	logmsg("FAKE_KEYS: %d events; keys:%s%s%s", fkeys.n, fkeys.h ? " home" : "", fkeys.p ? " power" : "", fkeys.v ? " vol" : "");
}

/* The keys the recorded board has (after every scan of the devices). */
static __attribute__((unused)) void fakekeys_have(int *h, int *p, int *v)
{
	if (!fkeys.on) return;
	*h = fkeys.h; *p = fkeys.p; *v = fkeys.v;
}

/* Deliver what is due, through the program's own key handler. */
static __attribute__((unused)) void fakekeys_pump(void (*fn)(int code, int value))
{
	int64_t now;
	if (!fkeys.on) return;
	now = bl_ms() - fkeys.t0;
	while (fkeys.i < fkeys.n && fkeys.ev[fkeys.i].t <= now) {
		fn(fkeys.ev[fkeys.i].code, fkeys.ev[fkeys.i].value);
		fkeys.i++;
	}
}
#else
static void fakekeys_init(void) { }
static __attribute__((unused)) void fakekeys_have(int *h, int *p, int *v) { (void)h; (void)p; (void)v; }
static __attribute__((unused)) void fakekeys_pump(void (*fn)(int code, int value)) { (void)fn; }
#endif

static void bl_init(int64_t (*clock)(void))
{
	const char *r = "";
#ifdef BL_TESTING
	r = getenv("FAKE_BL_ROOT") ? getenv("FAKE_BL_ROOT") : "/nonexistent";
#endif
	snprintf(bl.root, sizeof bl.root, "%s", r);
	snprintf(bl.state_path, sizeof bl.state_path, "%s/run/arcade-backlight", r);
	bl.clock = clock;
	bl_scan();
	if (!bl.n) logmsg("backlight: none under %s/sys/class/backlight; Power keeps its meaning", r);
	for (int i = 0; i < bl.n; i++)
		logmsg("backlight: %s (%s)", bl.d[i].name, bl.d[i].has_power ? "bl_power" : "brightness only");
	bl_force_on("start");
	fakekeys_init();
}

#endif /* BACKLIGHT_H */
