// SPDX-License-Identifier: GPL-2.0-only OR MIT
/*
 * a8-gpu-drm: replay a Mesa A8 recording through powervr's real
 * DRM interface (render node, CREATE_VM_CONTEXT, CREATE_BO, VM_MAP,
 * CREATE_CONTEXT, CREATE_FREE_LIST, CREATE_HWRT_DATASET, SUBMIT_JOBS, a
 * syncobj wait) and check what the GPU wrote. Static, no libc: raw aarch64
 * system calls, for the iPod's busybox initramfs.
 *
 *   a8-gpu-drm compute FILE.rpl   k_store_lid: 32 dwords 0x12345678 in the output BO
 *   a8-gpu-drm triangle FILE.rpl  tri-twiddled: the 64x64 target, checked the way the
 *                                 bring-up module did (unflipped, twiddled layout)
 *   a8-gpu-drm info               the device's GPU ID and heaps
 *   a8-gpu-drm dry compute|triangle FILE.rpl
 *                                 walk the recording without a device (every ioctl succeeds,
 *                                 BOs are anonymous memory): the check then fails
 *
 * The .rpl files come from tools/gen-replay.py. One line starts "RESULT".
 * Exit status 0 is a pass.
 */
#include <linux/types.h>
#include <drm/drm.h>
#include <drm/pvr_drm.h>

typedef unsigned long uintptr_t;	/* for DRM_PVR_OBJ_ARRAY() */

typedef __u8 u8;
typedef __u32 u32;
typedef __s32 s32;
typedef __u64 u64;
typedef long ssize;
typedef unsigned long usize;

/* ---- system calls (aarch64) ---- */

#define SYS_ioctl	29
#define SYS_openat	56
#define SYS_close	57
#define SYS_read	63
#define SYS_write	64
#define SYS_exit_group	94
#define SYS_clock_gettime 113
#define SYS_munmap	215
#define SYS_mmap	222

#define AT_FDCWD	-100
#define O_RDONLY	0
#define O_RDWR		2
#define O_CLOEXEC	02000000
#define PROT_READ	1
#define PROT_WRITE	2
#define MAP_SHARED	1
#define MAP_PRIVATE	2
#define MAP_ANONYMOUS	0x20
#define CLOCK_MONOTONIC	1

static long sys6(long n, long a, long b, long c, long d, long e, long f)
{
	register long x8 __asm__("x8") = n;
	register long x0 __asm__("x0") = a;
	register long x1 __asm__("x1") = b;
	register long x2 __asm__("x2") = c;
	register long x3 __asm__("x3") = d;
	register long x4 __asm__("x4") = e;
	register long x5 __asm__("x5") = f;

	__asm__ volatile("svc #0"
			 : "+r"(x0)
			 : "r"(x8), "r"(x1), "r"(x2), "r"(x3), "r"(x4), "r"(x5)
			 : "memory", "cc");
	return x0;
}

#define sys3(n, a, b, c) sys6(n, (long)(a), (long)(b), (long)(c), 0, 0, 0)

static __attribute__((noreturn)) void sys_exit(int code)
{
	for (;;)
		sys6(SYS_exit_group, code, 0, 0, 0, 0, 0);
}

static void *sys_mmap(void *addr, usize len, int prot, int flags, int fd, u64 off)
{
	return (void *)sys6(SYS_mmap, (long)addr, (long)len, prot, flags, fd, (long)off);
}

static int is_err(long v)
{
	return v < 0 && v > -4096;
}

/* The compiler may emit calls to these */
void *memset(void *d, int c, usize n)
{
	u8 *p = d;

	while (n--)
		*p++ = (u8)c;
	return d;
}

void *memcpy(void *d, const void *s, usize n)
{
	u8 *p = d;
	const u8 *q = s;

	while (n--)
		*p++ = *q++;
	return d;
}

int memcmp(const void *a, const void *b, usize n)
{
	const u8 *p = a, *q = b;

	for (; n--; p++, q++)
		if (*p != *q)
			return *p < *q ? -1 : 1;
	return 0;
}

/* ---- output ---- */

static char obuf[512];
static usize olen;

static void flush(void)
{
	if (olen)
		sys3(SYS_write, 1, obuf, olen);
	olen = 0;
}

static void putch(char c)
{
	if (olen == sizeof(obuf))
		flush();
	obuf[olen++] = c;
	if (c == '\n')
		flush();
}

static void puts_(const char *s)
{
	while (*s)
		putch(*s++);
}

static void putu(u64 v)
{
	char b[24];
	int i = 0;

	do {
		b[i++] = '0' + v % 10;
		v /= 10;
	} while (v);
	while (i--)
		putch(b[i]);
}

static void puth(u64 v)
{
	static const char hex[] = "0123456789abcdef";
	char b[18];
	int i = 0;

	do {
		b[i++] = hex[v & 15];
		v >>= 4;
	} while (v);
	puts_("0x");
	while (i--)
		putch(b[i]);
}

static void puti(long v)
{
	if (v < 0) {
		putch('-');
		v = -v;
	}
	putu((u64)v);
}

static int streq(const char *a, const char *b)
{
	while (*a && *a == *b)
		a++, b++;
	return *a == *b;
}

/* ---- time ---- */

struct ts {
	long sec, nsec;
};

static u64 now_us(void)
{
	struct ts t;

	sys3(SYS_clock_gettime, CLOCK_MONOTONIC, &t, 0);
	return (u64)t.sec * 1000000 + t.nsec / 1000;
}

/* ---- the replay file (tools/gen-replay.py) ---- */

#define RPL_MAGIC	"A8RPLAY1"
enum {
	RPL_BO = 1,		/* u32 index, u32 kind (0 CPU, 1 PM protected), u64 size */
	RPL_MAP,		/* u32 index, u32 0, u64 va, u64 size */
	RPL_DATA,		/* u32 index, u32 0, u64 offset, bytes */
	RPL_FREELIST,		/* u32 index, u32 0, u64 va, u32 initial, max, grow, threshold */
	RPL_HWRT,		/* u32 free list 0, u32 free list 1, create_hwrt_dataset args */
	RPL_CONTEXT,		/* u32 DRM_PVR_CTX_TYPE_* */
	RPL_JOB,		/* u32 type, u32 flags, u32 data index, u32 0, stream */
	RPL_CHECK_STORE,	/* u64 va, u32 count, u32 value */
	RPL_CHECK_TRI,		/* u64 va */
	RPL_NAME,		/* text */
};

#define MAX_BOS		64
#define MAX_FREELISTS	4
#define MAX_JOBS	4
#define KIND_PM		1

static struct bo {
	u32 handle;
	u32 kind;
	u64 size;
	u8 *map;
	u64 va;		/* its first mapping, for the checks */
} bos[MAX_BOS];
static u32 nbos;
static u32 freelists[MAX_FREELISTS];
static int fd = -1;
static u32 vm, hwrt, ctx;

static int dry;		/* no device: every ioctl succeeds */
static u32 dry_handle;

static int ioc(unsigned long req, void *arg, const char *what)
{
	long r;

	if (dry) {
		/* every handle-returning argument struct of ours has its handle where the kernel puts it */
		if (req == DRM_IOCTL_PVR_CREATE_BO)
			((struct drm_pvr_ioctl_create_bo_args *)arg)->handle = ++dry_handle;
		else if (req == DRM_IOCTL_PVR_CREATE_VM_CONTEXT)
			((struct drm_pvr_ioctl_create_vm_context_args *)arg)->handle = ++dry_handle;
		else if (req == DRM_IOCTL_PVR_CREATE_FREE_LIST)
			((struct drm_pvr_ioctl_create_free_list_args *)arg)->handle = ++dry_handle;
		else if (req == DRM_IOCTL_PVR_CREATE_HWRT_DATASET)
			((struct drm_pvr_ioctl_create_hwrt_dataset_args *)arg)->handle = ++dry_handle;
		else if (req == DRM_IOCTL_PVR_CREATE_CONTEXT)
			((struct drm_pvr_ioctl_create_context_args *)arg)->handle = ++dry_handle;
		else if (req == DRM_IOCTL_SYNCOBJ_CREATE)
			((struct drm_syncobj_create *)arg)->handle = ++dry_handle;
		puts_("dry: ");
		puts_(what);
		putch('\n');
		return 0;
	}
	r = sys3(SYS_ioctl, fd, req, arg);

	if (r < 0) {
		puts_("a8-gpu-drm: ");
		puts_(what);
		puts_(" failed: ");
		puti(r);
		putch('\n');
	}
	return (int)r;
}

static const u8 *rpl;
static u64 rpl_size;

static u32 rd32(const u8 *p)
{
	return p[0] | p[1] << 8 | p[2] << 16 | (u32)p[3] << 24;
}

static u64 rd64(const u8 *p)
{
	return rd32(p) | (u64)rd32(p + 4) << 32;
}

static int load(const char *path)
{
	int f = (int)sys6(SYS_openat, AT_FDCWD, (long)path, O_RDONLY | O_CLOEXEC, 0, 0, 0);
	u64 cap = 1 << 20, n = 0;
	u8 *buf;

	if (f < 0)
		return f;
	buf = sys_mmap(0, 64 << 20, PROT_READ | PROT_WRITE, MAP_PRIVATE | MAP_ANONYMOUS, -1, 0);
	if (is_err((long)buf))
		return -12;
	for (;;) {
		long r = sys3(SYS_read, f, buf + n, cap);

		if (r < 0)
			return (int)r;
		if (!r)
			break;
		n += r;
		if (n + cap > (64 << 20))
			return -27;
	}
	sys3(SYS_close, f, 0, 0);
	rpl = buf;
	rpl_size = n;
	return n >= 12 && !memcmp(buf, RPL_MAGIC, 8) ? 0 : -22;
}

/* ---- the triangle check (as the bring-up module checked it) ---- */

#define TRI_DIM		64
#define TRI_CLEAR_PX	0xff000033u
#define TRI_COLOUR_PX	0xffff4000u
enum { TL_TWID_YLO, TL_TWID_XLO, TL_LINEAR, TL_KINDS };
#define TL_FLIP		TL_KINDS
#define TL_COUNT	(2 * TL_KINDS)

static const char *const tl_name[TL_COUNT] = {
	"twiddled-y0", "twiddled-x0", "linear", "twiddled-y0-flipped", "twiddled-x0-flipped",
	"linear-flipped",
};

static u32 tri_index(unsigned int layout, u32 x, u32 y)
{
	u32 i, idx = 0;

	if (layout >= TL_FLIP)
		y = TRI_DIM - 1 - y;
	switch (layout % TL_KINDS) {
	case TL_LINEAR:
		return y * TRI_DIM + x;
	case TL_TWID_YLO:
		for (i = 0; i < 6; i++)
			idx |= ((y >> i) & 1) << (2 * i) | ((x >> i) & 1) << (2 * i + 1);
		return idx;
	default:
		for (i = 0; i < 6; i++)
			idx |= ((x >> i) & 1) << (2 * i) | ((y >> i) & 1) << (2 * i + 1);
		return idx;
	}
}

/* 0: clear, 1: triangle, 2: either (pixel centre within 0.75 px of an edge) */
static int tri_expect(u32 x, u32 y)
{
	static const int v[3][2] = { { 32, 32 }, { 96, 32 }, { 64, 96 } };
	long long px = 2 * x + 1, py = 2 * y + 1;
	int i, inside = 1, near = 0;

	for (i = 0; i < 3; i++) {
		long long ax = v[i][0], ay = v[i][1], bx = v[(i + 1) % 3][0], by = v[(i + 1) % 3][1];
		long long e = (bx - ax) * (py - ay) - (by - ay) * (px - ax);
		long long len2 = (bx - ax) * (bx - ax) + (by - ay) * (by - ay);
		int close = 4 * e * e < 9 * len2;

		if (e <= 0 && !close)
			return 0;
		if (e <= 0)
			inside = 0;
		near |= close;
	}
	return near ? 2 : inside;
}

static int px_near(u32 a, u32 b)
{
	int i;

	for (i = 0; i < 32; i += 8) {
		int d = (int)((a >> i) & 0xff) - (int)((b >> i) & 0xff);

		if (d < -2 || d > 2)
			return 0;
	}
	return 1;
}

static int px_class(u32 v)
{
	return px_near(v, TRI_CLEAR_PX) ? 0 : px_near(v, TRI_COLOUR_PX) ? 1 : 2;
}

struct tri_result {
	u32 clear, colour, edge, other, bad;
};

static int check_triangle(const u8 *img)
{
	struct tri_result r[TL_COUNT];
	unsigned int l, best = 0;
	u32 x, y;
	int pass;

	for (l = 0; l < TL_COUNT; l++) {
		struct tri_result *t = &r[l];

		memset(t, 0, sizeof(*t));
		for (y = 0; y < TRI_DIM; y++)
			for (x = 0; x < TRI_DIM; x++) {
				u32 v = rd32(img + 4 * tri_index(l, x, y));
				int c = px_class(v), e = tri_expect(x, y);

				if (c == 2)
					t->other++;
				else if (e == 2)
					t->edge++;
				else if (c != e)
					t->bad++;
				else if (c)
					t->colour++;
				else
					t->clear++;
			}
		if (t->bad + t->other < r[best].bad + r[best].other)
			best = l;
		puts_("target as ");
		puts_(tl_name[l]);
		puts_(": clear ");
		putu(t->clear);
		puts_(" colour ");
		putu(t->colour);
		puts_(" edge ");
		putu(t->edge);
		puts_(" other ");
		putu(t->other);
		puts_(" bad ");
		putu(t->bad);
		putch('\n');
	}
	for (y = 0; y < 16; y++) {
		for (x = 0; x < 16; x++) {
			unsigned int n[3] = { 0, 0, 0 }, yy, xx;

			for (yy = 4 * y; yy < 4 * y + 4; yy++)
				for (xx = 4 * x; xx < 4 * x + 4; xx++)
					n[px_class(rd32(img + 4 * tri_index(best, xx, yy)))]++;
			putch(n[2] ? '?' : n[1] == 16 ? '#' : n[0] == 16 ? '.' : 'x');
		}
		putch('\n');
	}
	pass = best < TL_FLIP && best != TL_LINEAR && !r[best].bad && !r[best].other && r[best].colour;
	puts_("RESULT triangle ");
	puts_(pass ? "PASS" : "FAIL");
	puts_(" layout ");
	puts_(tl_name[best]);
	puts_(" colour ");
	putu(r[best].colour);
	puts_(" bad ");
	putu(r[best].bad);
	puts_(" other ");
	putu(r[best].other);
	puts_(" pixel(0,0) ");
	puth(rd32(img));
	puts_(" pixel(32,32) ");
	puth(rd32(img + 4 * tri_index(best, 32, 32)));
	putch('\n');
	return pass ? 0 : 1;
}

static struct bo *bo_at(u64 va, u64 len)
{
	u32 i;

	for (i = 0; i < nbos; i++)
		if (bos[i].va && va >= bos[i].va && va + len <= bos[i].va + bos[i].size)
			return &bos[i];
	return 0;
}

static int open_node(void)
{
	static const char *const nodes[] = { "/dev/dri/renderD128", "/dev/dri/renderD129",
					     "/dev/dri/renderD130" };
	unsigned int i;

	for (i = 0; i < sizeof(nodes) / sizeof(nodes[0]); i++) {
		char name[16] = { 0 };
		struct drm_version v = { .name = name, .name_len = sizeof(name) - 1 };
		int f = (int)sys6(SYS_openat, AT_FDCWD, (long)nodes[i], O_RDWR | O_CLOEXEC, 0, 0, 0);

		if (f < 0)
			continue;
		if (!sys3(SYS_ioctl, f, DRM_IOCTL_VERSION, &v) && streq(name, "powervr"))
			return f;
		sys3(SYS_close, f, 0, 0);
	}
	return -19;
}

static int info(void)
{
	struct drm_pvr_dev_query_gpu_info gpu = { 0 };
	struct drm_pvr_heap heaps[8];
	struct drm_pvr_dev_query_heap_info hi = {
		.heaps = { .stride = sizeof(struct drm_pvr_heap), .count = 8, .array = (u64)heaps },
	};
	struct drm_pvr_ioctl_dev_query_args q = {
		.type = DRM_PVR_DEV_QUERY_GPU_INFO_GET, .pointer = (u64)&gpu, .size = sizeof(gpu),
	};
	u32 i;

	if (ioc(DRM_IOCTL_PVR_DEV_QUERY, &q, "DEV_QUERY gpu"))
		return 1;
	puts_("gpu id ");
	puth(gpu.gpu_id);
	puts_(" (BVNC ");
	putu(gpu.gpu_id >> 48);
	putch('.');
	putu((gpu.gpu_id >> 32) & 0xffff);
	putch('.');
	putu((gpu.gpu_id >> 16) & 0xffff);
	putch('.');
	putu(gpu.gpu_id & 0xffff);
	puts_(")\n");
	q = (struct drm_pvr_ioctl_dev_query_args) {
		.type = DRM_PVR_DEV_QUERY_HEAP_INFO_GET, .pointer = (u64)&hi, .size = sizeof(hi),
	};
	if (ioc(DRM_IOCTL_PVR_DEV_QUERY, &q, "DEV_QUERY heaps"))
		return 1;
	for (i = 0; i < hi.heaps.count && i < 8; i++) {
		puts_("heap ");
		putu(i);
		puts_(" base ");
		puth(heaps[i].base);
		puts_(" size ");
		puth(heaps[i].size);
		puts_(" page 2^");
		putu(heaps[i].page_size_log2);
		putch('\n');
	}
	puts_("RESULT info PASS\n");
	return 0;
}

static int replay(int triangle)
{
	struct drm_pvr_job jobs[MAX_JOBS];
	static u8 streams[MAX_JOBS][1024];
	struct drm_pvr_sync_op signal;
	struct drm_syncobj_create sync = { 0 };
	struct drm_pvr_ioctl_create_vm_context_args cvm = { 0 };
	u64 check_va = 0, start;
	u32 check_count = 0, check_value = 0, njobs = 0, nfl = 0, i;
	const u8 *p = rpl + 12, *end = rpl + rpl_size;
	s32 ctx_type = -1;
	int err;

	if (ioc(DRM_IOCTL_PVR_CREATE_VM_CONTEXT, &cvm, "CREATE_VM_CONTEXT"))
		return 2;
	vm = cvm.handle;
	while (p + 8 <= end) {
		u32 type = rd32(p), len = rd32(p + 4);
		const u8 *d = p + 8;

		if (d + len > end)
			return 2;
		p = d + len;
		switch (type) {
		case RPL_NAME:
			puts_("recording: ");
			for (i = 0; i < len; i++)
				putch(d[i]);
			putch('\n');
			break;
		case RPL_BO: {
			u32 idx = rd32(d);
			struct bo *b = &bos[idx];
			struct drm_pvr_ioctl_create_bo_args c = { .size = rd64(d + 8) };
			struct drm_pvr_ioctl_get_bo_mmap_offset_args o = { 0 };

			if (idx >= MAX_BOS)
				return 2;
			b->kind = rd32(d + 4);
			b->size = c.size;
			c.flags = b->kind == KIND_PM ? DRM_PVR_BO_PM_FW_PROTECT :
				  DRM_PVR_BO_BYPASS_DEVICE_CACHE | DRM_PVR_BO_ALLOW_CPU_USERSPACE_ACCESS;
			if (ioc(DRM_IOCTL_PVR_CREATE_BO, &c, "CREATE_BO"))
				return 2;
			b->handle = c.handle;
			if (b->kind != KIND_PM) {
				o.handle = b->handle;
				if (ioc(DRM_IOCTL_PVR_GET_BO_MMAP_OFFSET, &o, "GET_BO_MMAP_OFFSET"))
					return 2;
				b->map = dry ? sys_mmap(0, b->size, PROT_READ | PROT_WRITE,
							MAP_PRIVATE | MAP_ANONYMOUS, -1, 0) :
					       sys_mmap(0, b->size, PROT_READ | PROT_WRITE, MAP_SHARED, fd,
							o.offset);
				if (is_err((long)b->map)) {
					puts_("a8-gpu-drm: mmap failed\n");
					return 2;
				}
			}
			if (idx >= nbos)
				nbos = idx + 1;
			break;
		}
		case RPL_DATA: {
			struct bo *b = &bos[rd32(d)];
			u64 off = rd64(d + 8);

			if (!b->map || off + len - 16 > b->size)
				return 2;
			memcpy(b->map + off, d + 16, len - 16);
			break;
		}
		case RPL_MAP: {
			struct bo *b = &bos[rd32(d)];
			struct drm_pvr_ioctl_vm_map_args m = {
				.vm_context_handle = vm, .device_addr = rd64(d + 8), .handle = b->handle,
				.size = rd64(d + 16),
			};

			if (ioc(DRM_IOCTL_PVR_VM_MAP, &m, "VM_MAP"))
				return 2;
			if (!b->va)
				b->va = m.device_addr;
			break;
		}
		case RPL_FREELIST: {
			struct drm_pvr_ioctl_create_free_list_args f = {
				.free_list_gpu_addr = rd64(d + 8), .initial_num_pages = rd32(d + 16),
				.max_num_pages = rd32(d + 20), .grow_num_pages = rd32(d + 24),
				.grow_threshold = rd32(d + 28), .vm_context_handle = vm,
			};

			if (nfl >= MAX_FREELISTS || ioc(DRM_IOCTL_PVR_CREATE_FREE_LIST, &f, "CREATE_FREE_LIST"))
				return 2;
			freelists[rd32(d)] = f.handle;
			nfl++;
			break;
		}
		case RPL_HWRT: {
			struct drm_pvr_ioctl_create_hwrt_dataset_args h;

			if (len != 8 + sizeof(h))
				return 2;
			memcpy(&h, d + 8, sizeof(h));
			h.free_list_handles[0] = freelists[rd32(d)];
			h.free_list_handles[1] = freelists[rd32(d + 4)];
			h.handle = 0;
			if (ioc(DRM_IOCTL_PVR_CREATE_HWRT_DATASET, &h, "CREATE_HWRT_DATASET"))
				return 2;
			hwrt = h.handle;
			break;
		}
		case RPL_CONTEXT: {
			struct drm_pvr_ioctl_create_context_args c = {
				.type = rd32(d), .priority = DRM_PVR_CTX_PRIORITY_NORMAL,
				.vm_context_handle = vm,
			};

			if (ioc(DRM_IOCTL_PVR_CREATE_CONTEXT, &c, "CREATE_CONTEXT"))
				return 2;
			ctx = c.handle;
			ctx_type = (s32)c.type;
			break;
		}
		case RPL_JOB:
			if (njobs >= MAX_JOBS || len - 16 > sizeof(streams[0]))
				return 2;
			memcpy(streams[njobs], d + 16, len - 16);
			/*
			 * Every job's sync_ops carries the stride, even with no ops: the
			 * kernel checks the stride before the count (pvr_get_uobj_array),
			 * so a zeroed array is -EINVAL: the triangle's geometry
			 * job, the one job without the signal op.
			 */
			jobs[njobs] = (struct drm_pvr_job) {
				.type = rd32(d), .context_handle = ctx, .flags = rd32(d + 4),
				.cmd_stream_len = len - 16, .cmd_stream = (u64)streams[njobs],
				.sync_ops = DRM_PVR_OBJ_ARRAY(0, &signal),
			};
			if (jobs[njobs].type != DRM_PVR_JOB_TYPE_COMPUTE)
				jobs[njobs].hwrt = (struct drm_pvr_hwrt_data_ref) {
					.set_handle = hwrt, .data_index = rd32(d + 8),
				};
			njobs++;
			break;
		case RPL_CHECK_STORE:
			check_va = rd64(d);
			check_count = rd32(d + 8);
			check_value = rd32(d + 12);
			break;
		case RPL_CHECK_TRI:
			check_va = rd64(d);
			break;
		default:
			return 2;
		}
	}
	if (!njobs || ctx_type < 0 || !check_va || triangle != (check_count == 0))
		return 2;

	if (ioc(DRM_IOCTL_SYNCOBJ_CREATE, &sync, "SYNCOBJ_CREATE"))
		return 2;
	signal = (struct drm_pvr_sync_op) {
		.handle = sync.handle,
		.flags = DRM_PVR_SYNC_OP_FLAG_HANDLE_TYPE_SYNCOBJ | DRM_PVR_SYNC_OP_FLAG_SIGNAL,
	};
	jobs[njobs - 1].sync_ops = (struct drm_pvr_obj_array) DRM_PVR_OBJ_ARRAY(1, &signal);
	{
		struct drm_pvr_ioctl_submit_jobs_args s = {
			.jobs = DRM_PVR_OBJ_ARRAY(njobs, jobs),
		};
		struct drm_syncobj_wait w = {
			.handles = (u64)&sync.handle, .count_handles = 1,
		};
		struct ts t;

		puts_("submitting ");
		putu(njobs);
		puts_(triangle ? " render jobs\n" : " compute job\n");
		start = now_us();
		err = ioc(DRM_IOCTL_PVR_SUBMIT_JOBS, &s, "SUBMIT_JOBS");
		if (err) {
			puts_(triangle ? "RESULT triangle FAIL submit " : "RESULT compute FAIL submit ");
			puti(err);
			putch('\n');
			return 1;
		}
		sys3(SYS_clock_gettime, CLOCK_MONOTONIC, &t, 0);
		w.timeout_nsec = (t.sec + 10) * 1000000000ll + t.nsec;
		err = ioc(DRM_IOCTL_SYNCOBJ_WAIT, &w, "SYNCOBJ_WAIT");
		puts_("jobs ended after ");
		putu(now_us() - start);
		puts_(" us\n");
		if (err) {
			puts_(triangle ? "RESULT triangle FAIL wait " : "RESULT compute FAIL wait ");
			puti(err);
			putch('\n');
			return 1;
		}
	}

	if (triangle) {
		struct bo *b = bo_at(check_va, TRI_DIM * TRI_DIM * 4);

		if (!b || !b->map)
			return 2;
		return check_triangle(b->map + (check_va - b->va));
	}
	{
		struct bo *b = bo_at(check_va, 4ull * check_count);
		u32 good = 0, n;
		const u8 *out;

		if (!b || !b->map)
			return 2;
		out = b->map + (check_va - b->va);
		for (n = 0; n < check_count; n++)
			good += rd32(out + 4 * n) == check_value;
		puts_("out[0..3] ");
		for (n = 0; n < 4; n++) {
			puth(rd32(out + 4 * n));
			putch(' ');
		}
		putch('\n');
		puts_(good == check_count ? "RESULT compute PASS " : "RESULT compute FAIL ");
		putu(good);
		putch('/');
		putu(check_count);
		puts_(" dwords ");
		puth(check_value);
		puts_(" at ");
		puth(check_va);
		putch('\n');
		return good == check_count ? 0 : 1;
	}
}

static int main_(int argc, char **argv)
{
	int err;

	if (argc == 4 && streq(argv[1], "dry")) {
		dry = 1;
		argv++;
		argc--;
	}
	if (argc < 2 || (!streq(argv[1], "info") && argc < 3)) {
		puts_("usage: a8-gpu-drm compute|triangle FILE.rpl | info | dry compute|triangle FILE.rpl\n");
		return 2;
	}
	fd = dry ? -2 : open_node();
	if (!dry && fd < 0) {
		puts_("RESULT ");
		puts_(argv[1]);
		puts_(" FAIL no powervr render node\n");
		return 2;
	}
	if (streq(argv[1], "info"))
		return info();
	err = load(argv[2]);
	if (err) {
		puts_("RESULT ");
		puts_(argv[1]);
		puts_(" FAIL cannot read the recording: ");
		puti(err);
		putch('\n');
		return 2;
	}
	err = replay(streq(argv[1], "triangle"));
	if (err == 2) {
		puts_("RESULT ");
		puts_(argv[1]);
		puts_(" FAIL the recording does not replay (see above)\n");
	}
	return err;
}

__attribute__((used)) void start_c(long *sp)
{
	int argc = (int)sp[0];
	char **argv = (char **)(sp + 1);
	int r = main_(argc, argv);

	flush();
	sys_exit(r);
}

__asm__(".text\n"
	".global _start\n"
	"_start:\n"
	"	mov x0, sp\n"
	"	and sp, x0, #~15\n"
	"	bl start_c\n");
