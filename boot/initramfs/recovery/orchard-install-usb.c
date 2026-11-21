/* The install receiver: the device side of docs/install-protocol.md (protocol 1, GPT disks).
 *
 *   orchard-install-usb              in the install boot (boot/initramfs/install/run), on the
 *                                    FunctionFS instance mounted at /dev/usb-ffs/install
 *   orchard-install-usb --test DIR   tests only: a Unix socket DIR/socket, one connection per USB
 *                                    session; disk, files and programs under DIR (see `configure`)
 *
 * The host stages files (Put), sends the plan (Plan), which this checks against the live GPT and
 * writes, then streams the root image (Stream) through orchard-write. Once the image verifies, a
 * worker finishes by itself: filesystem identity and size, the finishing program inside the new root
 * (install_device.py finish, as the SSH install runs it), unmount, and /run/orchard/done, after which
 * the boot hook continues into the installed system. No command from the host runs here, and the
 * contents of secrets.json are never read by this program: the finishing program reads and deletes it.
 * Static, no libraries but libc; the SHA-256 and the JSON reader are the small ones below.
 */
#define _GNU_SOURCE
#include <ctype.h>
#include <dirent.h>
#include <errno.h>
#include <fcntl.h>
#include <inttypes.h>
#include <pthread.h>
#include <signal.h>
#include <stdarg.h>
#include <stdint.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <sys/ioctl.h>
#include <sys/socket.h>
#include <sys/stat.h>
#include <sys/statvfs.h>
#include <sys/types.h>
#include <sys/un.h>
#include <sys/wait.h>
#include <time.h>
#include <unistd.h>
#ifdef __linux__
#include <endian.h>
#include <sys/mount.h>
#include <linux/usb/functionfs.h>
#endif

#define PROTOCOL 1
#define MAX_PAYLOAD (1u << 20)
#define MAX_NAME 100
#define MAX_STAGED 32
#define REPORT_EVERY (64ull << 20)
#define XBOOTLDR "bc13c2ff-59e6-4262-a352-b275fd6f7172"
#define ROOT_ARM64 "b921b045-1df0-41c3-af44-4c6f280d3fae"
#define LINUX_DATA "0fc63daf-8483-4772-8e79-3d69d8477de4"
#define FFS_DIR "/dev/usb-ffs/install"

enum { INFO = 0x01, INSPECT = 0x02, PUT = 0x03, PLAN = 0x04, STREAM = 0x05, DATA = 0x06,
       OK = 0x81, ERROR = 0x82, EVENT = 0x83 };
enum phase { READY, PLANNED, STREAM_INCOMPLETE, FINISHING, DONE, FAILED };
static const char *phase_names[] = {"ready", "planned", "stream-incomplete", "finishing", "done", "failed"};

/* Where everything is: the install boot's paths, or a test directory's. */
static struct {
    int test;
    char dir[256], disk[256], run[256], tools[256], newroot[256], ecid[256], adt[256], compatible[256],
        boot_id[256];
    unsigned sector;     /* tests: the disk image's sector size */
} cfg;

static void die(const char *what)
{
    fprintf(stderr, "orchard-install-usb: %s: %s\n", what, strerror(errno));
    exit(1);
}

static void *grow(void *p, size_t size)
{
    p = realloc(p, size ? size : 1);
    if (!p)
        die("out of memory");
    return p;
}

static void join(char *out, size_t size, const char *dir, const char *name)
{
    if ((size_t)snprintf(out, size, "%s/%s", dir, name) >= size) {
        errno = ENAMETOOLONG;
        die(name);
    }
}

static double uptime(void)
{
    FILE *f = fopen("/proc/uptime", "r");
    double value = 0;
    if (f && fscanf(f, "%lf", &value) == 1) {
        fclose(f);
        return value;
    }
    if (f)
        fclose(f);
    struct timespec t;
    clock_gettime(CLOCK_MONOTONIC, &t);
    return t.tv_sec + t.tv_nsec / 1e9;
}

/* A point in /run/orchard/timeline, beside the boot hook's. */
static void mark(const char *what)
{
    char path[300];
    join(path, sizeof(path), cfg.run, "timeline");
    FILE *f = fopen(path, "a");
    if (f) {
        fprintf(f, "%.2f %s\n", uptime(), what);
        fclose(f);
    }
    fprintf(stderr, "orchard-install-usb: %s\n", what);
}

/* --- growable text ---------------------------------------------------------------------------- */

struct buf { char *data; size_t size, cap; };

static void add(struct buf *b, const void *p, size_t n)
{
    if (b->size + n + 1 > b->cap) {
        b->cap = (b->size + n + 1) * 2;
        b->data = grow(b->data, b->cap);
    }
    memcpy(b->data + b->size, p, n);
    b->size += n;
    b->data[b->size] = 0;
}

static void add_text(struct buf *b, const char *text)
{
    add(b, text, strlen(text));
}

static void addf(struct buf *b, const char *format, ...)
{
    char small[512];
    va_list args;
    va_start(args, format);
    int n = vsnprintf(small, sizeof(small), format, args);
    va_end(args);
    if (n < (int)sizeof(small)) {
        add(b, small, n);
        return;
    }
    char *large = grow(NULL, n + 1);
    va_start(args, format);
    vsnprintf(large, n + 1, format, args);
    va_end(args);
    add(b, large, n);
    free(large);
}

/* Length of the valid UTF-8 sequence at p, or 0. */
static int utf8_length(const unsigned char *p, size_t left)
{
    int n = p[0] < 0x80 ? 1 : (p[0] & 0xe0) == 0xc0 ? 2 : (p[0] & 0xf0) == 0xe0 ? 3 : (p[0] & 0xf8) == 0xf0 ? 4 : 0;
    if (!n || (size_t)n > left || (n == 2 && p[0] < 0xc2))
        return 0;
    for (int i = 1; i < n; i++)
        if ((p[i] & 0xc0) != 0x80)
            return 0;
    if ((n == 3 && p[0] == 0xe0 && p[1] < 0xa0) || (n == 3 && p[0] == 0xed && p[1] >= 0xa0) ||
        (n == 4 && p[0] == 0xf0 && p[1] < 0x90) || (n == 4 && (p[0] > 0xf4 || (p[0] == 0xf4 && p[1] >= 0x90))))
        return 0;
    return n;
}

/* A JSON string; bytes that are not UTF-8 become U+FFFD, as Python's errors='replace'. */
static void add_string(struct buf *b, const char *s)
{
    if (!s) {
        add_text(b, "null");
        return;
    }
    add_text(b, "\"");
    for (const unsigned char *p = (const unsigned char *)s; *p;) {
        int n = utf8_length(p, strlen((const char *)p));
        if (!n) {
            add_text(b, "\xef\xbf\xbd");
            p++;
        } else if (*p == '"' || *p == '\\') {
            addf(b, "\\%c", *p++);
        } else if (*p < 0x20) {
            addf(b, "\\u%04x", *p++);
        } else {
            add(b, p, n);
            p += n;
        }
    }
    add_text(b, "\"");
}

/* --- SHA-256 (FIPS 180-4) and CRC-32 ----------------------------------------------------------- */

static const uint32_t K[64] = {
    0x428a2f98,0x71374491,0xb5c0fbcf,0xe9b5dba5,0x3956c25b,0x59f111f1,0x923f82a4,0xab1c5ed5,0xd807aa98,0x12835b01,
    0x243185be,0x550c7dc3,0x72be5d74,0x80deb1fe,0x9bdc06a7,0xc19bf174,0xe49b69c1,0xefbe4786,0x0fc19dc6,0x240ca1cc,
    0x2de92c6f,0x4a7484aa,0x5cb0a9dc,0x76f988da,0x983e5152,0xa831c66d,0xb00327c8,0xbf597fc7,0xc6e00bf3,0xd5a79147,
    0x06ca6351,0x14292967,0x27b70a85,0x2e1b2138,0x4d2c6dfc,0x53380d13,0x650a7354,0x766a0abb,0x81c2c92e,0x92722c85,
    0xa2bfe8a1,0xa81a664b,0xc24b8b70,0xc76c51a3,0xd192e819,0xd6990624,0xf40e3585,0x106aa070,0x19a4c116,0x1e376c08,
    0x2748774c,0x34b0bcb5,0x391c0cb3,0x4ed8aa4a,0x5b9cca4f,0x682e6ff3,0x748f82ee,0x78a5636f,0x84c87814,0x8cc70208,
    0x90befffa,0xa4506ceb,0xbef9a3f7,0xc67178f2};
#define ROR(x, n) (((x) >> (n)) | ((x) << (32 - (n))))

struct sha { uint32_t state[8]; unsigned char partial[64]; size_t partial_size; uint64_t total; };

static void sha_init(struct sha *s)
{
    static const uint32_t start[8] = {0x6a09e667,0xbb67ae85,0x3c6ef372,0xa54ff53a,0x510e527f,0x9b05688c,0x1f83d9ab,0x5be0cd19};
    memcpy(s->state, start, sizeof(start));
    s->partial_size = 0;
    s->total = 0;
}

static void sha_block(struct sha *s, const unsigned char *p)
{
    uint32_t w[64], v[8];
    for (int i = 0; i < 16; i++)
        w[i] = (uint32_t)p[i * 4] << 24 | p[i * 4 + 1] << 16 | p[i * 4 + 2] << 8 | p[i * 4 + 3];
    for (int i = 16; i < 64; i++)
        w[i] = w[i - 16] + (ROR(w[i - 15], 7) ^ ROR(w[i - 15], 18) ^ w[i - 15] >> 3) + w[i - 7] +
               (ROR(w[i - 2], 17) ^ ROR(w[i - 2], 19) ^ w[i - 2] >> 10);
    memcpy(v, s->state, sizeof(v));
    for (int i = 0; i < 64; i++) {
        uint32_t t1 = v[7] + (ROR(v[4], 6) ^ ROR(v[4], 11) ^ ROR(v[4], 25)) + ((v[4] & v[5]) ^ (~v[4] & v[6])) + K[i] + w[i];
        uint32_t t2 = (ROR(v[0], 2) ^ ROR(v[0], 13) ^ ROR(v[0], 22)) + ((v[0] & v[1]) ^ (v[0] & v[2]) ^ (v[1] & v[2]));
        v[7] = v[6]; v[6] = v[5]; v[5] = v[4]; v[4] = v[3] + t1;
        v[3] = v[2]; v[2] = v[1]; v[1] = v[0]; v[0] = t1 + t2;
    }
    for (int i = 0; i < 8; i++)
        s->state[i] += v[i];
}

static void sha_update(struct sha *s, const void *data, size_t n)
{
    const unsigned char *p = data;
    s->total += n;
    while (n) {
        if (!s->partial_size && n >= 64) {
            sha_block(s, p);
            p += 64; n -= 64;
            continue;
        }
        size_t take = 64 - s->partial_size < n ? 64 - s->partial_size : n;
        memcpy(s->partial + s->partial_size, p, take);
        s->partial_size += take; p += take; n -= take;
        if (s->partial_size == 64) {
            sha_block(s, s->partial);
            s->partial_size = 0;
        }
    }
}

static void sha_hex(struct sha *s, char hex[65])
{
    uint64_t bits = s->total * 8;
    unsigned char pad[64] = {0x80}, length[8];
    sha_update(s, pad, (s->partial_size < 56 ? 56 : 120) - s->partial_size);
    for (int i = 0; i < 8; i++)
        length[i] = bits >> (56 - i * 8);
    sha_update(s, length, 8);
    for (int i = 0; i < 8; i++)
        sprintf(hex + i * 8, "%08x", s->state[i]);
}

static uint32_t crc32(const void *data, size_t n)
{
    static uint32_t table[256];
    if (!table[1])
        for (uint32_t i = 0; i < 256; i++) {
            uint32_t c = i;
            for (int k = 0; k < 8; k++)
                c = c & 1 ? 0xedb88320 ^ (c >> 1) : c >> 1;
            table[i] = c;
        }
    uint32_t c = 0xffffffff;
    for (const unsigned char *p = data; n--; p++)
        c = table[(c ^ *p) & 0xff] ^ (c >> 8);
    return c ^ 0xffffffff;
}

/* --- JSON, just enough to read requests, plans, maps and the finishing program's events -------- */

enum { J_NULL, J_BOOL, J_NUM, J_STR, J_ARR, J_OBJ };
struct json {
    int type, boolean, integral;
    long long integer;
    double number;
    char *string;
    size_t count;
    struct json *items;
    char **keys;
};
struct parser { const char *p, *end; int depth; };

static void skip_space(struct parser *ps)
{
    while (ps->p < ps->end && (*ps->p == ' ' || *ps->p == '\t' || *ps->p == '\n' || *ps->p == '\r'))
        ps->p++;
}

static int hex4(const char *p, unsigned *out)
{
    *out = 0;
    for (int i = 0; i < 4; i++) {
        int c = p[i], v = isdigit(c) ? c - '0' : c >= 'a' && c <= 'f' ? c - 'a' + 10 : c >= 'A' && c <= 'F' ? c - 'A' + 10 : -1;
        if (v < 0)
            return -1;
        *out = *out << 4 | v;
    }
    return 0;
}

static void add_codepoint(struct buf *b, unsigned c)
{
    unsigned char u[4];
    int n = c < 0x80 ? 1 : c < 0x800 ? 2 : c < 0x10000 ? 3 : 4;
    if (n == 1)
        u[0] = c;
    else {
        for (int i = n - 1; i > 0; i--, c >>= 6)
            u[i] = 0x80 | (c & 0x3f);
        u[0] = (n == 2 ? 0xc0 : n == 3 ? 0xe0 : 0xf0) | c;
    }
    add(b, u, n);
}

static int parse_string(struct parser *ps, char **out)
{
    struct buf b = {0};
    add_text(&b, "");
    ps->p++;
    while (ps->p < ps->end && *ps->p != '"') {
        unsigned char c = *ps->p;
        if (c < 0x20)
            goto bad;
        if (c != '\\') {
            int n = utf8_length((const unsigned char *)ps->p, ps->end - ps->p);
            if (!n)
                goto bad;
            add(&b, ps->p, n);
            ps->p += n;
            continue;
        }
        if (++ps->p >= ps->end)
            goto bad;
        const char *simple = strchr("\"\\/bfnrt", *ps->p);
        if (simple && *ps->p) {
            add(&b, &"\"\\/\b\f\n\r\t"[simple - "\"\\/bfnrt"], 1);
            ps->p++;
            continue;
        }
        unsigned code, low;
        if (*ps->p != 'u' || ps->end - ps->p < 5 || hex4(ps->p + 1, &code))
            goto bad;
        ps->p += 5;
        if (code >= 0xd800 && code < 0xdc00) {
            if (ps->end - ps->p < 6 || ps->p[0] != '\\' || ps->p[1] != 'u' || hex4(ps->p + 2, &low) ||
                low < 0xdc00 || low >= 0xe000)
                goto bad;
            ps->p += 6;
            code = 0x10000 + ((code - 0xd800) << 10) + (low - 0xdc00);
        } else if ((code >= 0xdc00 && code < 0xe000) || code == 0) {
            goto bad;      /* lone low surrogates, and NUL, which C strings can't hold */
        }
        add_codepoint(&b, code);
    }
    if (ps->p >= ps->end)
        goto bad;
    ps->p++;
    *out = b.data;
    return 0;
bad:
    free(b.data);
    return -1;
}

static void json_free(struct json *j);

static int parse_value(struct parser *ps, struct json *out)
{
    memset(out, 0, sizeof(*out));
    skip_space(ps);
    if (ps->p >= ps->end || ++ps->depth > 32)
        return -1;
    int result = -1;
    const char *p = ps->p;
    size_t left = ps->end - p;
    if (*p == '{' || *p == '[') {
        int object = *p == '{';
        out->type = object ? J_OBJ : J_ARR;
        ps->p++;
        skip_space(ps);
        if (ps->p < ps->end && *ps->p == (object ? '}' : ']')) {
            ps->p++;
            result = 0;
            goto out;
        }
        for (;;) {
            char *key = NULL;
            if (object) {
                skip_space(ps);
                if (ps->p >= ps->end || *ps->p != '"' || parse_string(ps, &key))
                    goto out;
                skip_space(ps);
                if (ps->p >= ps->end || *ps->p != ':') {
                    free(key);
                    goto out;
                }
                ps->p++;
            }
            out->items = grow(out->items, (out->count + 1) * sizeof(*out->items));
            out->keys = grow(out->keys, (out->count + 1) * sizeof(*out->keys));
            out->keys[out->count] = key;
            if (parse_value(ps, &out->items[out->count])) {
                out->count++;
                goto out;
            }
            out->count++;
            skip_space(ps);
            if (ps->p < ps->end && *ps->p == ',') {
                ps->p++;
                continue;
            }
            if (ps->p < ps->end && *ps->p == (object ? '}' : ']')) {
                ps->p++;
                result = 0;
            }
            goto out;
        }
    } else if (*p == '"') {
        out->type = J_STR;
        result = parse_string(ps, &out->string);
    } else if (left >= 4 && !memcmp(p, "true", 4)) {
        out->type = J_BOOL; out->boolean = 1; ps->p += 4; result = 0;
    } else if (left >= 5 && !memcmp(p, "false", 5)) {
        out->type = J_BOOL; ps->p += 5; result = 0;
    } else if (left >= 4 && !memcmp(p, "null", 4)) {
        out->type = J_NULL; ps->p += 4; result = 0;
    } else if (*p == '-' || isdigit((unsigned char)*p)) {
        const char *q = p + (*p == '-');
        int digits = 0, integral = 1;
        if (q < ps->end && *q == '0')
            q++, digits = 1;
        else
            while (q < ps->end && isdigit((unsigned char)*q))
                q++, digits++;
        if (!digits)
            goto out;
        if (q < ps->end && *q == '.') {
            integral = 0;
            if (++q >= ps->end || !isdigit((unsigned char)*q))
                goto out;
            while (q < ps->end && isdigit((unsigned char)*q))
                q++;
        }
        if (q < ps->end && (*q == 'e' || *q == 'E')) {
            integral = 0;
            q++;
            if (q < ps->end && (*q == '+' || *q == '-'))
                q++;
            if (q >= ps->end || !isdigit((unsigned char)*q))
                goto out;
            while (q < ps->end && isdigit((unsigned char)*q))
                q++;
        }
        char text[64];
        if (q - p >= (long)sizeof(text))
            goto out;
        memcpy(text, p, q - p);
        text[q - p] = 0;
        out->type = J_NUM;
        out->number = strtod(text, NULL);
        out->integral = integral && digits <= 18;
        out->integer = out->integral ? strtoll(text, NULL, 10) : 0;
        ps->p = q;
        result = 0;
    }
out:
    ps->depth--;
    return result;
}

static void json_free(struct json *j)
{
    for (size_t i = 0; i < j->count; i++) {
        json_free(&j->items[i]);
        free(j->keys[i]);
    }
    free(j->items);
    free(j->keys);
    free(j->string);
    memset(j, 0, sizeof(*j));
}

/* A whole document; 0 if it is one valid JSON value. */
static int json_parse(const char *text, size_t size, struct json *out)
{
    struct parser ps = {text, text + size, 0};
    if (parse_value(&ps, out)) {
        json_free(out);
        return -1;
    }
    skip_space(&ps);
    if (ps.p != ps.end) {
        json_free(out);
        return -1;
    }
    return 0;
}

static struct json *jget(struct json *o, const char *key)
{
    if (!o || o->type != J_OBJ)
        return NULL;
    for (size_t i = o->count; i-- > 0;)       /* the last of duplicate keys, as Python */
        if (!strcmp(o->keys[i], key))
            return &o->items[i];
    return NULL;
}

static const char *jstr(struct json *o, const char *key)
{
    struct json *v = jget(o, key);
    return v && v->type == J_STR ? v->string : NULL;
}

static int jint(struct json *o, const char *key, long long *out)
{
    struct json *v = jget(o, key);
    if (!v || v->type != J_NUM || !v->integral)
        return -1;
    *out = v->integer;
    return 0;
}

static struct json *jobj(struct json *o, const char *key)
{
    struct json *v = jget(o, key);
    return v && v->type == J_OBJ ? v : NULL;
}

/* --- outgoing messages: one writer thread, so no one waits on a host that isn't reading --------- */

struct message { struct message *next; int droppable; size_t size; unsigned char data[]; };
static pthread_mutex_t out_lock = PTHREAD_MUTEX_INITIALIZER;
static pthread_cond_t out_changed = PTHREAD_COND_INITIALIZER;
static struct message *queue_head, *queue_tail;
static int queued, out_fd = -1, writing;

/* One message as one write, so it is one USB transfer. A JSON payload is padded with a space when
 * the message would be a whole number of 64-byte packets: then every message ends in a short packet
 * and a host reading into a large buffer never waits for the next one. */
static void send_message(int type, const struct buf *payload, int droppable)
{
    size_t size = payload ? payload->size : 0, pad = (8 + size) % 64 == 0;
    struct message *m = grow(NULL, sizeof(*m) + 8 + size + pad);
    m->next = NULL;
    m->droppable = droppable;
    m->size = 8 + size + pad;
    m->data[0] = type;
    m->data[1] = m->data[2] = m->data[3] = 0;
    uint32_t length = size + pad;
    for (int i = 0; i < 4; i++)
        m->data[4 + i] = length >> (8 * i);
    if (size)
        memcpy(m->data + 8, payload->data, size);
    if (pad)
        m->data[8 + size] = ' ';
    pthread_mutex_lock(&out_lock);
    if (droppable && queued > 2) {
        free(m);
    } else {
        if (queue_tail)
            queue_tail->next = m;
        else
            queue_head = m;
        queue_tail = m;
        queued++;
        pthread_cond_broadcast(&out_changed);
    }
    pthread_mutex_unlock(&out_lock);
}

static void *writer_thread(void *unused)
{
    (void)unused;
    for (;;) {
        pthread_mutex_lock(&out_lock);
        while (!queue_head)
            pthread_cond_wait(&out_changed, &out_lock);
        struct message *m = queue_head;
        queue_head = m->next;
        if (!queue_head)
            queue_tail = NULL;
        queued--;
        int fd = out_fd;
        writing = 1;
        pthread_mutex_unlock(&out_lock);
        for (size_t done = 0; fd >= 0 && done < m->size;) {
            ssize_t n = write(fd, m->data + done, m->size - done);
            if (n < 0 && errno == EINTR)
                continue;
            if (n <= 0)
                break;           /* the host is gone; the message with it */
            done += n;
        }
        free(m);
        pthread_mutex_lock(&out_lock);
        writing = 0;
        pthread_cond_broadcast(&out_changed);
        pthread_mutex_unlock(&out_lock);
    }
    return NULL;
}

#define KEEP_FD (-2)
/* A new session: what was queued for the last one is dropped. WAIT: until nothing writes to the old fd. */
static void output_reset(int fd, int wait)
{
    pthread_mutex_lock(&out_lock);
    while (queue_head) {
        struct message *m = queue_head;
        queue_head = m->next;
        free(m);
    }
    queue_tail = NULL;
    queued = 0;
    while (wait && writing)
        pthread_cond_wait(&out_changed, &out_lock);
    if (fd != KEEP_FD)
        out_fd = fd;
    pthread_mutex_unlock(&out_lock);
}

static void output_drain(int milliseconds)
{
    for (int i = 0; i < milliseconds / 10; i++) {
        pthread_mutex_lock(&out_lock);
        int busy = queue_head || writing;
        pthread_mutex_unlock(&out_lock);
        if (!busy)
            return;
        usleep(10000);
    }
}

static void answer_ok(const char *json)
{
    struct buf b = {0};
    add(&b, json, strlen(json));
    send_message(OK, &b, 0);
    free(b.data);
}

static void answer_error(const char *code, const char *message)
{
    struct buf b = {0};
    add_text(&b, "{\"code\": ");
    add_string(&b, code);
    add_text(&b, ", \"message\": ");
    add_string(&b, message);
    add_text(&b, "}");
    send_message(ERROR, &b, 0);
    free(b.data);
    fprintf(stderr, "orchard-install-usb: refused: %s: %s\n", code, message);
}

/* --- state ---------------------------------------------------------------------------------- */

static pthread_mutex_t state_lock = PTHREAD_MUTEX_INITIALIZER;
static enum phase phase = READY;
static int streaming;
static uint64_t stream_written, stream_bytes;
static char *record;                       /* the completion record, JSON */
static char error_code[32], error_message[300];
static char staged[MAX_STAGED][MAX_NAME + 1];
static int staged_count;
static char device_ecid[20], board[64], boot_id[64];
static int identity_ok;
static char identity_problem[200];

/* The plan, as written to plan.json, and what Plan found out. */
static struct json plan;
static int have_plan;
static char root_node[300], boot_node[300];

static void emit_event(const struct buf *b, int droppable)
{
    send_message(EVENT, b, droppable);
}

/* Stop for good: the phase is failed with CODE, and the host hears it once. Caller holds no lock. */
static void fail(const char *code, const char *format, ...)
{
    va_list args;
    pthread_mutex_lock(&state_lock);
    phase = FAILED;
    streaming = 0;
    snprintf(error_code, sizeof(error_code), "%s", code);
    va_start(args, format);
    vsnprintf(error_message, sizeof(error_message), format, args);
    va_end(args);
    struct buf b = {0};
    add_text(&b, "{\"event\": \"failed\", \"code\": ");
    add_string(&b, error_code);
    add_text(&b, ", \"message\": ");
    add_string(&b, error_message);
    add_text(&b, "}");
    pthread_mutex_unlock(&state_lock);
    emit_event(&b, 0);
    free(b.data);
    fprintf(stderr, "orchard-install-usb: failed: %s: %s\n", code, error_message);
    mark("install failed");
}

static void set_phase(enum phase next)
{
    pthread_mutex_lock(&state_lock);
    phase = next;
    pthread_mutex_unlock(&state_lock);
}

static enum phase current_phase(void)
{
    pthread_mutex_lock(&state_lock);
    enum phase p = phase;
    pthread_mutex_unlock(&state_lock);
    return p;
}

static void *heartbeat_thread(void *unused)
{
    (void)unused;
    for (;;) {
        sleep(1);
        struct buf b = {0}, s = {0};
        pthread_mutex_lock(&state_lock);
        addf(&b, "{\"event\": \"heartbeat\", \"phase\": \"%s\"}", phase_names[phase]);
        if (streaming)
            addf(&s, "{\"event\": \"stream\", \"written\": %" PRIu64 ", \"bytes\": %" PRIu64 "}", stream_written, stream_bytes);
        pthread_mutex_unlock(&state_lock);
        emit_event(&b, 1);
        if (s.size)
            emit_event(&s, 1);
        free(b.data);
        free(s.data);
    }
    return NULL;
}

/* --- files ---------------------------------------------------------------------------------- */

static int read_file(const char *path, struct buf *out, size_t limit)
{
    int fd = open(path, O_RDONLY | O_CLOEXEC);
    if (fd < 0)
        return -1;
    char chunk[65536];
    ssize_t n;
    while ((n = read(fd, chunk, sizeof(chunk))) > 0) {
        if (out->size + n > limit) {
            close(fd);
            errno = EFBIG;
            return -1;
        }
        add(out, chunk, n);
    }
    close(fd);
    if (!out->data)
        add_text(out, "");
    return n < 0 ? -1 : 0;
}

static int write_all(int fd, const void *data, size_t size)
{
    for (const char *p = data; size;) {
        ssize_t n = write(fd, p, size);
        if (n < 0 && errno == EINTR)
            continue;
        if (n <= 0)
            return -1;
        p += n;
        size -= n;
    }
    return 0;
}

/* A file in /run/orchard, root's alone, replaced whole. */
static int write_private(const char *name, const void *data, size_t size)
{
    char path[300], part[310];
    join(path, sizeof(path), cfg.run, name);
    snprintf(part, sizeof(part), "%s.part", path);
    int fd = open(part, O_WRONLY | O_CREAT | O_TRUNC | O_NOFOLLOW | O_CLOEXEC, 0600);
    if (fd < 0)
        return -1;
    fchmod(fd, 0600);
    int bad = write_all(fd, data, size) || fsync(fd);
    close(fd);
    if (bad || rename(part, path)) {
        unlink(part);
        return -1;
    }
    return 0;
}

static int copy_private(const char *from, const char *name)
{
    struct buf b = {0};
    int result = read_file(from, &b, 64u << 20) ? -1 : write_private(name, b.data, b.size);
    free(b.data);
    return result;
}

static int exists(const char *dir, const char *name)
{
    char path[300];
    struct stat st;
    join(path, sizeof(path), dir, name);
    return !stat(path, &st) && S_ISREG(st.st_mode);
}

/* A plain file name for /run/orchard: letters, digits, '.', '-', '_'; none of the receiver's own. */
static int plain_name(const char *name)
{
    static const char *reserved[] = {"done", "plan.json", "timeline", "os-release", "root.map", "write.log",
                                     "finish.log", "install.log", "install-usb.log", "install_device.py",
                                     "boot-entries", NULL};
    size_t n = strlen(name);
    if (!n || n > MAX_NAME || name[0] == '.')
        return 0;
    for (const char *p = name; *p; p++)
        if (!isalnum((unsigned char)*p) && *p != '.' && *p != '-' && *p != '_')
            return 0;
    if (n > 5 && !strcmp(name + n - 5, ".part"))
        return 0;
    for (int i = 0; reserved[i]; i++)
        if (!strcmp(name, reserved[i]))
            return 0;
    return 1;
}

/* The staged name a plan path means: "/run/orchard/NAME", or NULL. */
static const char *staged_name(const char *path)
{
    if (!path || strncmp(path, "/run/orchard/", 13) || !plain_name(path + 13))
        return NULL;
    return path + 13;
}

static int sha_text(const char *s)
{
    if (!s || strlen(s) != 64)
        return 0;
    for (; *s; s++)
        if (!isdigit((unsigned char)*s) && !(*s >= 'a' && *s <= 'f'))
            return 0;
    return 1;
}

/* --- this device ---------------------------------------------------------------------------- */

/* The ECID in an Apple device tree: /chosen's unique-chip-id, which must be there exactly once
 * (the rules of src/orchard/apple/adt.py). 0 if the tree is malformed. */
struct adt { const unsigned char *data; size_t size, position; int budget, found; uint64_t ecid; };

static uint32_t le32(const unsigned char *p) { return p[0] | p[1] << 8 | p[2] << 16 | (uint32_t)p[3] << 24; }
static uint64_t le64(const unsigned char *p) { return le32(p) | (uint64_t)le32(p + 4) << 32; }

static int adt_node(struct adt *a, int depth, int parent_is_root, int *is_root)
{
    if (--a->budget < 0 || depth > 64 || a->position + 8 > a->size)
        return -1;
    uint32_t props = le32(a->data + a->position), children = le32(a->data + a->position + 4);
    a->position += 8;
    if (props > 4096 || children > 4096)
        return -1;
    char name[33] = "";
    const unsigned char *chip = NULL;
    uint32_t chip_size = 0;
    for (uint32_t i = 0; i < props; i++) {
        if (a->position + 36 > a->size)
            return -1;
        const unsigned char *label = a->data + a->position;
        uint32_t size = le32(label + 32) & 0xffffff;
        a->position += 36;
        if (a->position + ((size + 3) & ~3u) > a->size)
            return -1;
        if (!strncmp((const char *)label, "name", 32)) {
            size_t n = size < 32 ? size : 32;
            memcpy(name, a->data + a->position, n);
            name[n] = 0;
        } else if (!strncmp((const char *)label, "unique-chip-id", 32)) {
            chip = a->data + a->position;
            chip_size = size;
        }
        a->position += (size + 3) & ~3u;
    }
    int root = depth == 0 && !strcmp(name, "device-tree");
    if (parent_is_root && !strcmp(name, "chosen")) {
        if (!chip || chip_size != 8)
            return -1;
        a->found++;
        a->ecid = le64(chip);
    }
    for (uint32_t i = 0; i < children; i++)
        if (adt_node(a, depth + 1, root, NULL))
            return -1;
    if (is_root)
        *is_root = root;
    return 0;
}

static uint64_t adt_ecid(const unsigned char *data, size_t size)
{
    struct adt a = {data, size, 0, 100000, 0, 0};
    int root = 0;
    if (size < 8 || size > 16u << 20 || adt_node(&a, 0, 0, &root) || !root || a.found != 1)
        return 0;
    return a.ecid;
}

/* The live ADT m1n1 passes as an MTD named "adt", as the recovery's identity.py reads it. */
static int live_adt(char *path, size_t size, size_t *bytes)
{
    FILE *f = fopen("/proc/mtd", "r");
    char line[200];
    int found = 0;
    while (f && fgets(line, sizeof(line), f)) {
        unsigned number, length, erase;
        char name[64];
        if (sscanf(line, "mtd%u: %x %x \"%63[^\"]\"", &number, &length, &erase, name) == 4 && !strcmp(name, "adt")) {
            snprintf(path, size, "/dev/mtd%u", number);
            *bytes = length;
            found++;
        }
    }
    if (f)
        fclose(f);
    return found == 1 ? 0 : -1;
}

static void first_line(const char *path, char *out, size_t size)
{
    struct buf b = {0};
    out[0] = 0;
    if (!read_file(path, &b, 65536)) {
        size_t n = strcspn(b.data, "\n");
        if (n >= size)
            n = size - 1;
        memcpy(out, b.data, n);
        out[n] = 0;
    }
    free(b.data);
}

/* Who this is (board, boot ID, ECID), and whether it is the device the add-on was made for. */
static void identify(void)
{
    char compatible[128];
    first_line(cfg.compatible, compatible, sizeof(compatible));    /* the first string ends at its NUL */
    snprintf(board, sizeof(board), "%s", strncmp(compatible, "apple,", 6) ? compatible : compatible + 6);
    first_line(cfg.boot_id, boot_id, sizeof(boot_id));
    char text[64], adt[64];
    first_line(cfg.ecid, text, sizeof(text));
    char *end;
    errno = 0;
    uint64_t wanted = strtoull(text, &end, 16);
    struct buf blob = {0};
    size_t limit = 16u << 20;
    uint64_t ecid = 0;
    if (cfg.test ? read_file(cfg.adt, &blob, limit) == 0 :
                   live_adt(adt, sizeof(adt), &limit) == 0 && read_file(adt, &blob, limit) == 0)
        ecid = adt_ecid((const unsigned char *)blob.data, blob.size);
    free(blob.data);
    if (ecid)
        snprintf(device_ecid, sizeof(device_ecid), "%" PRIx64, ecid);
    if (!text[0] || *end || errno || !wanted)
        snprintf(identity_problem, sizeof(identity_problem), "The add-on names no ECID");
    else if (!ecid)
        snprintf(identity_problem, sizeof(identity_problem), "This device's ECID could not be read from its ADT");
    else if (ecid != wanted)
        snprintf(identity_problem, sizeof(identity_problem), "The add-on is for ECID %" PRIx64 ", this device is %" PRIx64,
                 wanted, ecid);
    else
        identity_ok = 1;
}

/* --- the disk ------------------------------------------------------------------------------- */

/* nvme<N>n1 (A9 and later) or asp<N>n1 (A7-A8X): the first namespace, where iOS and Linux live. */
static int internal_disk(const char *name)
{
    const char *p = !strncmp(name, "nvme", 4) ? name + 4 : !strncmp(name, "asp", 3) ? name + 3 : NULL;
    if (!p || !isdigit((unsigned char)*p))
        return 0;
    while (isdigit((unsigned char)*p))
        p++;
    return !strcmp(p, "n1");
}

static int find_disk(char *message, size_t size)
{
    if (cfg.disk[0])
        return 0;
    for (int tries = 0; tries < 300; tries++) {
        DIR *d = opendir("/sys/class/block");
        struct dirent *e;
        char found[2][64];
        int count = 0;
        while (d && (e = readdir(d))) {
            char partition[400];
            snprintf(partition, sizeof(partition), "/sys/class/block/%s/partition", e->d_name);
            if (internal_disk(e->d_name) && access(partition, F_OK)) {
                if (count < 2)
                    snprintf(found[count], sizeof(found[count]), "%s", e->d_name);
                count++;
            }
        }
        if (d)
            closedir(d);
        if (count > 1) {
            snprintf(message, size, "More than one internal disk: %s, %s", found[0], found[1]);
            return -1;
        }
        if (count == 1) {
            snprintf(cfg.disk, sizeof(cfg.disk), "/dev/%s", found[0]);
            return 0;
        }
        usleep(100000);
    }
    snprintf(message, size, "Storage did not come up within 30 s");
    return -1;
}

static unsigned sector_size(void)
{
    if (cfg.test)
        return cfg.sector;
    char path[300], text[32];
    snprintf(path, sizeof(path), "/sys/class/block/%s/queue/logical_block_size", strrchr(cfg.disk, '/') + 1);
    first_line(path, text, sizeof(text));
    return (unsigned)strtoul(text, NULL, 10);
}

static uint64_t disk_bytes(int fd)
{
    struct stat st;
    if (fstat(fd, &st))
        return 0;
#ifdef BLKGETSIZE64
    uint64_t bytes;
    if (S_ISBLK(st.st_mode) && !ioctl(fd, BLKGETSIZE64, &bytes))
        return bytes;
#endif
    return st.st_size;
}

static void node_path(char *out, size_t size, int number)
{
    size_t n = strlen(cfg.disk);
    snprintf(out, size, isdigit((unsigned char)cfg.disk[n - 1]) ? "%sp%d" : "%s%d", cfg.disk, number);
}

static int read_exact_at(int fd, void *data, size_t size, uint64_t offset)
{
    for (char *p = data; size;) {
        ssize_t n = pread(fd, p, size, offset);
        if (n < 0 && errno == EINTR)
            continue;
        if (n <= 0)
            return -1;
        p += n; size -= n; offset += n;
    }
    return 0;
}

static int write_exact_at(int fd, const void *data, size_t size, uint64_t offset)
{
    for (const char *p = data; size;) {
        ssize_t n = pwrite(fd, p, size, offset);
        if (n < 0 && errno == EINTR)
            continue;
        if (n <= 0)
            return -1;
        p += n; size -= n; offset += n;
    }
    return 0;
}

/* GUIDs as GPT stores them (mixed endian) and as text. */
static void guid_text(const unsigned char *b, char out[37])
{
    snprintf(out, 37, "%08x-%04x-%04x-%02x%02x-%02x%02x%02x%02x%02x%02x", le32(b), b[4] | b[5] << 8, b[6] | b[7] << 8,
             b[8], b[9], b[10], b[11], b[12], b[13], b[14], b[15]);
}

static int guid_bytes(const char *text, unsigned char out[16])
{
    static const int order[16] = {3, 2, 1, 0, 5, 4, 7, 6, 8, 9, 10, 11, 12, 13, 14, 15};
    unsigned char raw[16];
    if (!text || strlen(text) != 36)
        return -1;
    for (int i = 0, j = 0; i < 36; i++) {
        if (i == 8 || i == 13 || i == 18 || i == 23) {
            if (text[i] != '-')
                return -1;
            continue;
        }
        int c = tolower((unsigned char)text[i]), v = isdigit(c) ? c - '0' : c >= 'a' && c <= 'f' ? c - 'a' + 10 : -1;
        if (v < 0)
            return -1;
        if (j % 2 == 0)
            raw[j / 2] = v << 4;
        else
            raw[j / 2] |= v;
        j++;
    }
    for (int i = 0; i < 16; i++)
        out[i] = raw[order[i]];
    return 0;
}

static int same_guid(const char *a, const char *b)
{
    return a && b && !strcasecmp(a, b);
}

struct part { int number; uint64_t start, blocks; char type[37], partuuid[37], name[160]; };
struct range { uint64_t start, blocks; };
struct gpt {
    unsigned sector;
    unsigned char *header, *entries;
    uint32_t header_size, count, entry_size;
    uint64_t current, alternate, first, last, entries_lba;
    char sha[65];
    struct part parts[128];
    int nparts;
    struct range free[130];
    int nfree;
};

static void gpt_free(struct gpt *g)
{
    free(g->header);
    free(g->entries);
    g->header = g->entries = NULL;
}

/* A partition name, UTF-16LE in the entry, as UTF-8. */
static void entry_name(const unsigned char *raw, char *out, size_t size)
{
    struct buf b = {0};
    add_text(&b, "");
    for (int i = 0; i < 36; i++) {
        unsigned c = raw[i * 2] | raw[i * 2 + 1] << 8;
        if (!c)
            break;
        if (c >= 0xd800 && c < 0xdc00 && i + 1 < 36) {
            unsigned low = raw[i * 2 + 2] | raw[i * 2 + 3] << 8;
            if (low >= 0xdc00 && low < 0xe000) {
                add_codepoint(&b, 0x10000 + ((c - 0xd800) << 10) + (low - 0xdc00));
                i++;
                continue;
            }
        }
        add_codepoint(&b, c >= 0xd800 && c < 0xe000 ? 0xfffd : c);
    }
    snprintf(out, size, "%s", b.data);
    free(b.data);
}

static int by_start(const void *a, const void *b)
{
    const struct part *x = a, *y = b;
    return x->start < y->start ? -1 : x->start > y->start;
}

/* The disk's GPT, as install_device.parse_gpt reads it, after checking both CRCs. */
static int read_gpt(int fd, struct gpt *g, char *message, size_t size)
{
    memset(g, 0, sizeof(*g));
    g->sector = sector_size();
    if (g->sector < 512 || g->sector > 65536 || (g->sector & (g->sector - 1))) {
        snprintf(message, size, "Unusable sector size %u", g->sector);
        return -1;
    }
    g->header = grow(NULL, g->sector);
    if (read_exact_at(fd, g->header, g->sector, g->sector)) {
        snprintf(message, size, "Reading the partition table failed: %s", strerror(errno));
        return -1;
    }
    if (memcmp(g->header, "EFI PART", 8)) {
        snprintf(message, size, "%s has no GPT", cfg.disk);
        return -1;
    }
    g->header_size = le32(g->header + 12);
    g->current = le64(g->header + 24);
    g->alternate = le64(g->header + 32);
    g->first = le64(g->header + 40);
    g->last = le64(g->header + 48);
    g->entries_lba = le64(g->header + 72);
    g->count = le32(g->header + 80);
    g->entry_size = le32(g->header + 84);
    if (g->header_size < 92 || g->header_size > g->sector || g->count > 1024 || g->entry_size != 128 ||
        g->first > g->last) {
        snprintf(message, size, "Unsupported GPT header");
        return -1;
    }
    unsigned char *copy = grow(NULL, g->header_size);
    memcpy(copy, g->header, g->header_size);
    memset(copy + 16, 0, 4);
    int header_ok = crc32(copy, g->header_size) == le32(g->header + 16);
    free(copy);
    size_t table = (size_t)g->count * g->entry_size;
    g->entries = grow(NULL, table);
    if (read_exact_at(fd, g->entries, table, g->entries_lba * g->sector)) {
        snprintf(message, size, "Reading the partition entries failed: %s", strerror(errno));
        return -1;
    }
    if (!header_ok || crc32(g->entries, table) != le32(g->header + 88)) {
        snprintf(message, size, "The GPT's checksums are wrong");
        return -1;
    }
    for (uint32_t i = 0; i < g->count; i++) {
        const unsigned char *raw = g->entries + (size_t)i * g->entry_size;
        static const unsigned char zero[16];
        if (!memcmp(raw, zero, 16))
            continue;
        if (g->nparts == 128) {
            snprintf(message, size, "More than 128 partitions");
            return -1;
        }
        struct part *p = &g->parts[g->nparts++];
        p->number = i + 1;
        p->start = le64(raw + 32);
        p->blocks = le64(raw + 40) - p->start + 1;
        guid_text(raw, p->type);
        guid_text(raw + 16, p->partuuid);
        entry_name(raw + 56, p->name, sizeof(p->name));
    }
    qsort(g->parts, g->nparts, sizeof(g->parts[0]), by_start);
    uint64_t cursor = g->first;
    for (int i = 0; i < g->nparts; i++) {
        if (g->parts[i].start > cursor)
            g->free[g->nfree++] = (struct range){cursor, g->parts[i].start - cursor};
        if (g->parts[i].start + g->parts[i].blocks > cursor)
            cursor = g->parts[i].start + g->parts[i].blocks;
    }
    if (cursor <= g->last)
        g->free[g->nfree++] = (struct range){cursor, g->last - cursor + 1};
    struct sha s;
    sha_init(&s);
    sha_update(&s, g->header, g->header_size);
    sha_update(&s, g->entries, table);
    sha_hex(&s, g->sha);
    return 0;
}

/* Filesystem, UUID and label from a partition's first 4 KiB, as install_direct.probe. */
static void probe(int fd, uint64_t offset, struct buf *b)
{
    unsigned char s[4096];
    char text[40], label[20];
    if (read_exact_at(fd, s, sizeof(s), offset)) {
        add_text(b, ", \"fs\": null, \"fs_uuid\": null, \"label\": null");
        return;
    }
    if (s[1080] == 0x53 && s[1081] == 0xef) {
        snprintf(text, sizeof(text), "%02x%02x%02x%02x-%02x%02x-%02x%02x-%02x%02x-%02x%02x%02x%02x%02x%02x",
                 s[1128], s[1129], s[1130], s[1131], s[1132], s[1133], s[1134], s[1135], s[1136], s[1137], s[1138],
                 s[1139], s[1140], s[1141], s[1142], s[1143]);
        memcpy(label, s + 1144, 16);
        label[16] = 0;
        add_text(b, ", \"fs\": \"ext4\", \"fs_uuid\": ");
        add_string(b, text);
        add_text(b, ", \"label\": ");
        add_string(b, label[0] ? label : NULL);
    } else if (!memcmp(s + 82, "FAT32   ", 8)) {
        snprintf(text, sizeof(text), "%02X%02X%02X%02X", s[70], s[69], s[68], s[67]);
        memcpy(label, s + 71, 11);
        int n = 11;
        while (n && label[n - 1] == ' ')
            n--;
        int start = 0;
        while (start < n && label[start] == ' ')
            start++;
        label[n] = 0;
        add_text(b, ", \"fs\": \"vfat\", \"fs_uuid\": ");
        add_string(b, text);
        add_text(b, ", \"label\": ");
        add_string(b, label[start] ? label + start : NULL);
    } else if (!memcmp(s + 32, "NXSB", 4)) {
        add_text(b, ", \"fs\": \"apfs\", \"fs_uuid\": null, \"label\": null");
    } else {
        add_text(b, ", \"fs\": null, \"fs_uuid\": null, \"label\": null");
    }
}

static int open_disk(int flags, char *message, size_t size)
{
    if (find_disk(message, size))
        return -1;
    int fd = open(cfg.disk, flags | O_CLOEXEC);
    if (fd < 0)
        snprintf(message, size, "Opening %s failed: %s", cfg.disk, strerror(errno));
    return fd;
}

static void inspect(void)
{
    char message[300];
    struct gpt g;
    int fd = open_disk(O_RDONLY, message, sizeof(message));
    if (fd < 0) {
        answer_error("disk", message);
        return;
    }
    if (read_gpt(fd, &g, message, sizeof(message))) {
        gpt_free(&g);
        close(fd);
        answer_error("disk", message);
        return;
    }
    struct buf b = {0};
    add_text(&b, "{\"disk\": {\"path\": ");
    add_string(&b, cfg.disk);
    addf(&b, ", \"sector\": %u, \"first\": %" PRIu64 ", \"last\": %" PRIu64 ", \"table_sha256\": \"%s\", \"bytes\": %" PRIu64
             "}, \"partitions\": [", g.sector, g.first, g.last, g.sha, disk_bytes(fd));
    for (int i = 0; i < g.nparts; i++) {
        struct part *p = &g.parts[i];
        addf(&b, "%s{\"number\": %d, \"start\": %" PRIu64 ", \"blocks\": %" PRIu64 ", \"type\": \"%s\", \"partuuid\": \"%s\", "
                 "\"name\": ", i ? ", " : "", p->number, p->start, p->blocks, p->type, p->partuuid);
        add_string(&b, p->name);
        probe(fd, p->start * g.sector, &b);
        add_text(&b, "}");
    }
    add_text(&b, "], \"free\": [");
    for (int i = 0; i < g.nfree; i++)
        addf(&b, "%s{\"start\": %" PRIu64 ", \"blocks\": %" PRIu64 "}", i ? ", " : "", g.free[i].start, g.free[i].blocks);
    add_text(&b, "]}");
    send_message(OK, &b, 0);
    free(b.data);
    gpt_free(&g);
    close(fd);
}

/* --- the plan ------------------------------------------------------------------------------- */

/* An empty FAT32 filesystem as (offset, bytes) regions: install_device.fat32_regions. */
struct region { uint64_t offset; size_t size; unsigned char *data; };

static void put16(unsigned char *p, unsigned v) { p[0] = v; p[1] = v >> 8; }
static void put32(unsigned char *p, uint32_t v) { for (int i = 0; i < 4; i++) p[i] = v >> (8 * i); }
static void put64(unsigned char *p, uint64_t v) { for (int i = 0; i < 8; i++) p[i] = v >> (8 * i); }

static int fat32_regions(uint64_t blocks, unsigned sector, uint32_t volume_id, struct region out[6])
{
    const unsigned reserved = 8, fats = 2;
    uint64_t fat_sectors = 1, clusters;
    if (blocks > 0xffffffffu || blocks < reserved + 2)
        return -1;
    for (;;) {
        clusters = blocks - reserved - fats * fat_sectors;
        uint64_t needed = ((clusters + 2) * 4 + sector - 1) / sector;
        if (needed <= fat_sectors)
            break;
        fat_sectors = needed;
    }
    if (clusters < 65525)
        return -1;
    unsigned char *boot = calloc(1, sector), *info = calloc(1, sector), *fat = calloc(fat_sectors, sector);
    if (!boot || !info || !fat)
        die("out of memory");
    memcpy(boot, "\xeb\x58\x90ORCHARD ", 11);
    put16(boot + 11, sector);
    boot[13] = 1;
    put16(boot + 14, reserved);
    boot[16] = fats;
    boot[21] = 0xf8;
    put16(boot + 24, 32);
    put16(boot + 26, 64);
    put32(boot + 32, blocks);
    put32(boot + 36, fat_sectors);
    put32(boot + 44, 2);
    put16(boot + 48, 1);
    put16(boot + 50, 6);
    boot[64] = 0x80;
    boot[66] = 0x29;
    put32(boot + 67, volume_id);
    memcpy(boot + 71, "BOOT       FAT32   ", 19);
    boot[510] = 0x55;
    boot[511] = 0xaa;
    put32(info, 0x41615252);
    put32(info + 484, 0x61417272);
    put32(info + 488, clusters - 1);
    put32(info + 492, 3);
    put32(info + 508, 0xaa550000);
    put32(fat, 0x0ffffff8);
    put32(fat + 4, 0x0fffffff);
    put32(fat + 8, 0x0fffffff);
    out[0] = (struct region){0, sector, boot};
    out[1] = (struct region){sector, sector, info};
    out[2] = (struct region){6 * (uint64_t)sector, sector, boot};
    out[3] = (struct region){7 * (uint64_t)sector, sector, info};
    out[4] = (struct region){reserved * (uint64_t)sector, fat_sectors * sector, fat};
    out[5] = (struct region){(reserved + fat_sectors) * (uint64_t)sector, fat_sectors * sector, fat};
    return 0;     /* the root cluster, one empty sector, follows the second FAT */
}

struct planned {
    long long root_start, root_blocks, boot_start, boot_blocks, remove_number;
    const char *root_partuuid, *root_label, *boot_partuuid, *remove_partuuid;
    int boot_create;
    uint32_t volume_id;
};

static int text_ok(const char *s, size_t max)
{
    return s && *s && strlen(s) <= max && !strpbrk(s, "\n\r");
}

/* install_device.check_plan against the live table, and what the plan must say for this protocol.
 * Returns the error code, or NULL. */
static const char *check_plan(struct json *p, struct gpt *g, struct planned *out, char *message, size_t size)
{
    long long value;
    struct json *disk = jobj(p, "disk"), *root = jobj(p, "root"), *boot = jobj(p, "boot"), *remove = jget(p, "remove");
    memset(out, 0, sizeof(*out));
    if (jint(p, "version", &value) || value != 2) {
        snprintf(message, size, "Unsupported plan version");
        return "plan-version";
    }
    if (!disk || !boot || (jstr(disk, "table") && strcmp(jstr(disk, "table"), "gpt"))) {
        snprintf(message, size, "This receiver installs on GPT disks only (protocol 1)");
        return "plan-version";
    }
    if (!jstr(disk, "table_sha256") || strcmp(jstr(disk, "table_sha256"), g->sha) || jint(disk, "sector", &value) ||
        value != g->sector) {
        snprintf(message, size, "The partition table changed since it was inspected; inspect again");
        return "layout-changed";
    }
#define MISMATCH(...) do { snprintf(message, size, __VA_ARGS__); return "plan-mismatch"; } while (0)
    if (!jstr(disk, "path") || strcmp(jstr(disk, "path"), cfg.disk))
        MISMATCH("The plan is for another disk");
    if (!root || jint(root, "start", &out->root_start) || jint(root, "blocks", &out->root_blocks) ||
        out->root_start <= 0 || out->root_blocks <= 0)
        MISMATCH("The plan's root is incomplete");
    out->root_partuuid = jstr(root, "partuuid");
    out->root_label = jstr(root, "label");
    unsigned char guid[16];
    if (guid_bytes(out->root_partuuid, guid) || guid_bytes(jstr(root, "fs_uuid"), guid) ||
        !text_ok(out->root_label, 16) || (jstr(root, "fs") && strcmp(jstr(root, "fs"), "ext4")))
        MISMATCH("The plan's root is incomplete");
    struct json *create = jget(boot, "create");
    out->boot_create = create && create->type == J_BOOL && create->boolean;
    out->boot_partuuid = jstr(boot, "partuuid");
    if (guid_bytes(out->boot_partuuid, guid))
        MISMATCH("The plan's boot partition is incomplete");
    if (out->boot_create) {
        const char *volume = jstr(boot, "fs_uuid");
        char *end = NULL;
        if (jint(boot, "start", &out->boot_start) || jint(boot, "blocks", &out->boot_blocks) || out->boot_start <= 0 ||
            out->boot_blocks <= 0 || !volume || strlen(volume) != 8)
            MISMATCH("The plan's boot partition is incomplete");
        out->volume_id = strtoul(volume, &end, 16);
        if (*end)
            MISMATCH("The plan's boot partition is incomplete");
        struct region regions[6];
        if (fat32_regions(out->boot_blocks, g->sector, 0, regions))
            MISMATCH("The boot partition is too small for FAT32");
        free(regions[0].data); free(regions[1].data); free(regions[4].data);
        if (out->boot_start + out->boot_blocks > out->root_start && out->root_start + out->root_blocks > out->boot_start)
            MISMATCH("The new partitions overlap");
    }
    /* Ranges the new partitions may take: free space, and the one replaced Linux partition. */
    struct range allowed[131];
    int n = 0;
    for (int i = 0; i < g->nfree; i++)
        allowed[n++] = g->free[i];
    if (remove && remove->type != J_NULL) {
        out->remove_partuuid = jstr(remove, "partuuid");
        struct part *match = NULL;
        if (!jint(remove, "number", &out->remove_number))
            for (int i = 0; i < g->nparts; i++)
                if (g->parts[i].number == out->remove_number)
                    match = &g->parts[i];
        if (!match || !same_guid(match->partuuid, out->remove_partuuid) ||
            !(same_guid(match->type, ROOT_ARM64) || same_guid(match->type, LINUX_DATA)))
            MISMATCH("The partition to replace is not the inspected Linux partition");
        allowed[n++] = (struct range){match->start, match->blocks};
    }
    for (int k = 0; k < 1 + out->boot_create; k++) {
        uint64_t start = k ? out->boot_start : out->root_start, end = start + (k ? out->boot_blocks : out->root_blocks);
        int inside = 0;
        for (int i = 0; i < n; i++)
            inside |= allowed[i].start <= start && end <= allowed[i].start + allowed[i].blocks;
        if (!inside)
            MISMATCH("A new partition lies outside free space and the replaced partition");
    }
    if (!out->boot_create) {
        int found = 0;
        for (int i = 0; i < g->nparts; i++)
            found |= same_guid(g->parts[i].partuuid, out->boot_partuuid) && same_guid(g->parts[i].type, XBOOTLDR);
        if (!found)
            MISMATCH("The boot partition to reuse is missing");
    }
    /* Every file the install reads is staged in /run/orchard under a plain name. */
    struct json *image = jobj(p, "image"), *map = jobj(image, "map"), *layer = jobj(p, "layer");
    struct json *files[3] = {layer, jobj(p, "firmware"), jobj(p, "kernel_package")};
    if (!image || jint(image, "bytes", &value) || value <= 0 || !sha_text(jstr(image, "sha256")))
        MISMATCH("The plan's image is incomplete");
    if (!map || jint(map, "block_size", &value) || value != 4096 || !jget(map, "ranges") ||
        jget(map, "ranges")->type != J_ARR)
        MISMATCH("The plan's image has no map of 4 KiB blocks (image.map, root.map.json's content)");
    if (!layer)
        MISMATCH("The plan names no platform layer");
    if (jget(p, "firmware") && jget(p, "firmware")->type != J_NULL && !files[1])
        MISMATCH("The plan's firmware is not a file");
    if (jget(p, "kernel_package") && jget(p, "kernel_package")->type != J_NULL && !files[2])
        MISMATCH("The plan's kernel package is not a file");
    for (int i = 0; i < 3; i++)
        if (files[i] && (!staged_name(jstr(files[i], "path")) || jint(files[i], "bytes", &value) || value < 0 ||
                         !sha_text(jstr(files[i], "sha256"))))
            MISMATCH("Every file the plan names must be a staged file, /run/orchard/NAME, with its bytes and sha256");
    if (!jobj(p, "entry"))
        MISMATCH("The plan has no boot entry");
    if (jget(p, "account") && jget(p, "account")->type != J_NULL && !jobj(p, "account"))
        MISMATCH("The plan's account is not an object");
#undef MISMATCH
    return NULL;
}

/* The entry of a new partition, as install_direct.entry_bytes. */
static void entry_bytes(unsigned char *raw, const char *type, const char *partuuid, uint64_t start, uint64_t blocks,
                        const char *name)
{
    memset(raw, 0, 128);
    guid_bytes(type, raw);
    guid_bytes(partuuid, raw + 16);
    put64(raw + 32, start);
    put64(raw + 40, start + blocks - 1);
    size_t units = 0;
    for (const unsigned char *p = (const unsigned char *)name; *p && units < 36;) {
        int n = utf8_length(p, strlen((const char *)p));
        unsigned c = n == 1 ? p[0] : n == 2 ? (p[0] & 0x1f) << 6 | (p[1] & 0x3f) :
                     n == 3 ? (p[0] & 0x0f) << 12 | (p[1] & 0x3f) << 6 | (p[2] & 0x3f) :
                     (p[0] & 0x07) << 18 | (p[1] & 0x3f) << 12 | (p[2] & 0x3f) << 6 | (p[3] & 0x3f);
        p += n ? n : 1;
        if (c >= 0x10000) {
            if (units + 2 > 36)
                break;
            c -= 0x10000;
            put16(raw + 56 + units++ * 2, 0xd800 + (c >> 10));
            c = 0xdc00 + (c & 0x3ff);
        }
        put16(raw + 56 + units++ * 2, c);
    }
}

/* A header for one copy of the table: install_direct.new_tables's `sealed`. */
static void sealed(const struct gpt *g, unsigned char *out, uint64_t this, uint64_t other, uint64_t where, uint32_t crc)
{
    memset(out, 0, g->sector);
    memcpy(out, g->header, g->header_size);
    put64(out + 24, this);
    put64(out + 32, other);
    put64(out + 72, where);
    put32(out + 88, crc);
    put32(out + 16, 0);
    put32(out + 16, crc32(out, g->header_size));
}

static int all_nodes(const int *numbers, int count)
{
    for (int i = 0; i < count; i++) {
        char node[300];
        node_path(node, sizeof(node), numbers[i]);
        if (access(node, F_OK))
            return 0;
    }
    return 1;
}

/* Write the plan's partitions into both copies of the GPT (the backup first, as install_direct
 * writes them), wait for their nodes and read the table back. Returns the error code, or NULL. */
static const char *write_partitions(int fd, struct gpt *g, struct planned *pl, struct buf *present,
                                    char *message, size_t size)
{
    size_t table_size = (size_t)g->count * g->entry_size;
    unsigned char *table = grow(NULL, table_size);
    memcpy(table, g->entries, table_size);
    if (pl->remove_partuuid)
        memset(table + (pl->remove_number - 1) * g->entry_size, 0, g->entry_size);
    int numbers[2], wanted = 1 + pl->boot_create, found = 0;
    for (int number = 1; number <= 128 && found < 2; number++) {
        int used = 0;
        for (int i = 0; i < g->nparts; i++)
            used |= g->parts[i].number == number && !(pl->remove_partuuid && number == pl->remove_number);
        if (!used)
            numbers[found++] = number;
    }
    if (found < 2 || numbers[1] > (int)g->count) {
        free(table);
        snprintf(message, size, "The partition table is full");
        return "plan-mismatch";
    }
    int next = 0, root_number, boot_number = 0;
    if (pl->boot_create) {
        boot_number = numbers[next++];
        entry_bytes(table + (boot_number - 1) * g->entry_size, XBOOTLDR, pl->boot_partuuid, pl->boot_start,
                    pl->boot_blocks, "boot");
    }
    root_number = numbers[next++];
    entry_bytes(table + (root_number - 1) * g->entry_size, ROOT_ARM64, pl->root_partuuid, pl->root_start,
                pl->root_blocks, pl->root_label);
    uint32_t crc = crc32(table, table_size);
    unsigned char *header = grow(NULL, g->sector);
    size_t padded = (table_size + g->sector - 1) / g->sector * g->sector;
    unsigned char *entries = calloc(1, padded);
    if (!entries)
        die("out of memory");
    memcpy(entries, table, table_size);
    int bad = write_exact_at(fd, entries, padded, (g->last + 1) * g->sector);
    sealed(g, header, g->alternate, g->current, g->last + 1, crc);
    bad = bad || write_exact_at(fd, header, g->sector, g->alternate * g->sector);
    bad = bad || write_exact_at(fd, entries, padded, g->entries_lba * g->sector);
    sealed(g, header, g->current, g->alternate, g->entries_lba, crc);
    bad = bad || write_exact_at(fd, header, g->sector, g->current * g->sector) || fsync(fd);
    free(header);
    free(entries);
    free(table);
    if (bad) {
        snprintf(message, size, "Writing the partition table failed: %s", strerror(errno));
        return "disk";
    }
    mark("partition table written");
    /* A re-read right after the write can be refused while the disk settles, so keep asking. */
    int created[2] = {root_number, boot_number};
    for (int tries = 0; tries < 150; tries++) {
#ifdef BLKRRPART
        ioctl(fd, BLKRRPART);
#endif
        if (all_nodes(created, wanted))
            break;
        usleep(100000);
    }
    if (!all_nodes(created, wanted)) {
        snprintf(message, size, "The new partitions did not appear");
        return "disk";
    }
    struct gpt now;
    if (read_gpt(fd, &now, message, size)) {
        gpt_free(&now);
        return "disk";
    }
    for (int k = 0; k < wanted; k++) {
        const char *partuuid = k ? pl->boot_partuuid : pl->root_partuuid;
        uint64_t start = k ? pl->boot_start : pl->root_start, blocks = k ? pl->boot_blocks : pl->root_blocks;
        int ok = 0;
        for (int i = 0; i < now.nparts; i++)
            ok |= same_guid(now.parts[i].partuuid, partuuid) && now.parts[i].start == start && now.parts[i].blocks == blocks;
        if (!ok) {
            gpt_free(&now);
            snprintf(message, size, "A new partition is not as planned");
            return "disk";
        }
    }
    /* The partitions that stay, for the finishing program's old-entry cleanup. */
    add_text(present, "[");
    for (int i = 0, first = 1; i < now.nparts; i++) {
        if (pl->remove_partuuid && same_guid(now.parts[i].partuuid, pl->remove_partuuid))
            continue;
        addf(present, "%s\"%s\"", first ? "" : ", ", now.parts[i].partuuid);
        first = 0;
        if (same_guid(now.parts[i].partuuid, pl->boot_partuuid))
            boot_number = now.parts[i].number;
    }
    add_text(present, "]");
    gpt_free(&now);
    node_path(root_node, sizeof(root_node), root_number);
    node_path(boot_node, sizeof(boot_node), boot_number);
    return NULL;
}

static void do_plan(const char *text, size_t size)
{
    char message[300];
    struct json p;
    if (json_parse(text, size, &p) || p.type != J_OBJ) {
        json_free(&p);
        answer_error("protocol", "The plan is not a JSON object");
        return;
    }
    int fd = open_disk(O_RDWR, message, sizeof(message));
    if (fd < 0) {
        json_free(&p);
        answer_error("disk", message);
        return;
    }
    struct gpt g;
    struct planned pl;
    const char *code = read_gpt(fd, &g, message, sizeof(message)) ? "disk" : check_plan(&p, &g, &pl, message, sizeof(message));
    struct buf present = {0};
    if (!code) {
        code = write_partitions(fd, &g, &pl, &present, message, sizeof(message));
        if (code && !strcmp(code, "disk")) {
            fail("disk", "%s", message);    /* the table may be half written: only a fresh start is safe */
        }
    }
    if (!code && pl.boot_create) {
        struct region regions[6];
        fat32_regions(pl.boot_blocks, g.sector, pl.volume_id, regions);
        int bad = 0;
        for (int i = 0; i < 6; i++)
            bad = bad || write_exact_at(fd, regions[i].data, regions[i].size, pl.boot_start * g.sector + regions[i].offset);
        if (!bad) {
            /* the root cluster, one sector after the FATs, is empty */
            unsigned char *zero = calloc(1, g.sector);
            bad = write_exact_at(fd, zero, g.sector, pl.boot_start * g.sector + regions[5].offset + regions[5].size);
            free(zero);
        }
        bad = bad || fsync(fd);
        free(regions[0].data); free(regions[1].data); free(regions[4].data);
        if (bad) {
            code = "disk";
            snprintf(message, sizeof(message), "Writing the boot filesystem failed: %s", strerror(errno));
            fail("disk", "%s", message);
        }
    }
    if (!code) {
        /* plan.json is the plan as sent, with the partitions that stay (`present`) added. */
        size_t end = size;
        while (end && text[end - 1] != '}')
            end--;
        struct buf saved = {0};
        add(&saved, text, end - 1);
        add_text(&saved, ", \"present\": ");
        add(&saved, present.data, present.size);
        add_text(&saved, "}\n");
        if (write_private("plan.json", saved.data, saved.size)) {
            code = "disk";
            snprintf(message, sizeof(message), "Keeping the plan failed: %s", strerror(errno));
            fail("disk", "%s", message);
        } else {
            json_free(&plan);
            if (json_parse(saved.data, saved.size, &plan))
                die("plan.json");
            have_plan = 1;
        }
        free(saved.data);
    }
    gpt_free(&g);
    close(fd);
    json_free(&p);
    free(present.data);
    if (code) {
        answer_error(code, message);
        return;
    }
    set_phase(PLANNED);
    mark("planned");
    char ok[700];
    snprintf(ok, sizeof(ok), "{\"root\": \"%s\", \"boot\": \"%s\"}", root_node, boot_node);
    answer_ok(ok);
}

/* --- transfers: Put and Stream -------------------------------------------------------------- */

static struct {
    int kind;                 /* 0, PUT or STREAM */
    uint64_t bytes, received, reported;
    char sha[65], name[MAX_NAME + 1];
    struct sha ctx;
    int fd;                   /* the .part file, or the writer's stdin */
    pid_t writer;
    const char *code;         /* set once the transfer has failed; the rest is read and dropped */
    char message[200];
} transfer;

static void finish_put(void);

static void start_put(struct json *request)
{
    long long bytes;
    const char *name = jstr(request, "name"), *sha = jstr(request, "sha256");
    enum phase p = current_phase();
    if (p != READY && p != PLANNED) {
        answer_error("bad-state", "Put is allowed when ready or planned");
        return;
    }
    if (!name || !plain_name(name)) {
        answer_error("bad-name", "A staged file needs a plain name: letters, digits, '.', '-', '_'");
        return;
    }
    if (jint(request, "bytes", &bytes) || bytes < 0 || !sha_text(sha)) {
        answer_error("protocol", "Put needs bytes and a lowercase hex sha256");
        return;
    }
    struct statvfs fs;
    if (!statvfs(cfg.run, &fs) && (uint64_t)bytes + (8u << 20) > (uint64_t)fs.f_bavail * fs.f_frsize) {
        answer_error("too-large", "The file does not fit in the device's memory");
        return;
    }
    char path[300], part[310];
    join(path, sizeof(path), cfg.run, name);
    snprintf(part, sizeof(part), "%s.part", path);
    unlink(part);
    int fd = open(part, O_WRONLY | O_CREAT | O_EXCL | O_NOFOLLOW | O_CLOEXEC, 0600);
    if (fd < 0) {
        answer_error("disk", strerror(errno));
        return;
    }
    fchmod(fd, 0600);
    memset(&transfer, 0, sizeof(transfer));
    transfer.kind = PUT;
    transfer.fd = fd;
    transfer.bytes = bytes;
    snprintf(transfer.sha, sizeof(transfer.sha), "%s", sha);
    snprintf(transfer.name, sizeof(transfer.name), "%s", name);
    sha_init(&transfer.ctx);
    answer_ok("{}");
    if (!bytes)
        finish_put();
}

static void finish_put(void)
{
    char hex[65], path[300], part[310];
    sha_hex(&transfer.ctx, hex);
    join(path, sizeof(path), cfg.run, transfer.name);
    snprintf(part, sizeof(part), "%s.part", path);
    int bad = close(transfer.fd);
    transfer.kind = 0;
    if (transfer.code || bad) {
        unlink(part);
        answer_error(transfer.code ? transfer.code : "too-large", transfer.code ? transfer.message : "Writing the file failed");
        return;
    }
    if (strcmp(hex, transfer.sha)) {
        unlink(part);
        answer_error("sha-mismatch", "The file's bytes don't match its sha256");
        return;
    }
    if (rename(part, path)) {
        unlink(part);
        answer_error("disk", strerror(errno));
        return;
    }
    pthread_mutex_lock(&state_lock);
    int known = 0;
    for (int i = 0; i < staged_count; i++)
        known |= !strcmp(staged[i], transfer.name);
    if (!known && staged_count < MAX_STAGED)
        snprintf(staged[staged_count++], MAX_NAME + 1, "%s", transfer.name);
    pthread_mutex_unlock(&state_lock);
    struct buf b = {0};
    add_text(&b, "{\"name\": ");
    add_string(&b, transfer.name);
    addf(&b, ", \"bytes\": %" PRIu64 "}", transfer.bytes);
    send_message(OK, &b, 0);
    free(b.data);
}

/* The plan's image map (root.map.json's content) as orchard-write's "START COUNT" lines, as
 * service.py makes them; the ranges must be in order and fit in the root partition. */
static const char *write_map(struct json *map, char *message, size_t size)
{
    long long root_blocks, sector;
    struct json *ranges = jget(map, "ranges");
    jint(jobj(&plan, "root"), "blocks", &root_blocks);
    jint(jobj(&plan, "disk"), "sector", &sector);
    uint64_t end = 0, limit = (uint64_t)root_blocks * sector / 4096;
    struct buf text = {0};
    add_text(&text, "");
    for (size_t i = 0; i < ranges->count; i++) {
        struct json *r = &ranges->items[i];
        if (r->type != J_ARR || r->count != 2 || r->items[0].type != J_NUM || !r->items[0].integral ||
            r->items[1].type != J_NUM || !r->items[1].integral || r->items[0].integer < (long long)end ||
            r->items[1].integer <= 0) {
            free(text.data);
            snprintf(message, size, "The image map's ranges are out of order");
            return "plan-mismatch";
        }
        end = r->items[0].integer + r->items[1].integer;
        addf(&text, "%lld %lld\n", r->items[0].integer, r->items[1].integer);
    }
    if (end > limit) {
        free(text.data);
        snprintf(message, size, "The image does not fit in the root partition");
        return "too-large";
    }
    int bad = write_private("root.map", text.data, text.size);
    free(text.data);
    if (bad) {
        snprintf(message, size, "Keeping the map failed");
        return "disk";
    }
    return NULL;
}

static void start_stream(struct json *request)
{
    long long bytes, planned_bytes;
    char message[300];
    const char *sha = jstr(request, "sha256");
    enum phase p = current_phase();
    if (p != PLANNED && p != STREAM_INCOMPLETE) {
        answer_error("bad-state", "Stream is allowed when planned or stream-incomplete");
        return;
    }
    struct json *image = jobj(&plan, "image");
    if (jint(request, "bytes", &bytes) || !sha_text(sha)) {
        answer_error("protocol", "Stream needs bytes and a lowercase hex sha256");
        return;
    }
    if (jint(image, "bytes", &planned_bytes) || bytes != planned_bytes || strcmp(sha, jstr(image, "sha256"))) {
        answer_error("plan-mismatch", "The stream is not the plan's image");
        return;
    }
    /* Every file the plan names, and the account's secrets, must be here before anything is written. */
    struct json *files[3] = {jobj(&plan, "layer"), jobj(&plan, "firmware"), jobj(&plan, "kernel_package")};
    for (int i = 0; i < 3; i++)
        if (files[i] && !exists(cfg.run, staged_name(jstr(files[i], "path")))) {
            snprintf(message, sizeof(message), "%s is not staged", staged_name(jstr(files[i], "path")));
            answer_error("missing-file", message);
            return;
        }
    if (jobj(&plan, "account") && !exists(cfg.run, "secrets.json")) {
        answer_error("missing-file", "secrets.json is not staged; the plan has an account");
        return;
    }
    const char *code = write_map(jobj(image, "map"), message, sizeof(message));
    if (code) {
        answer_error(code, message);
        return;
    }
    char program[300], map[300], log[300], count[32];
    join(program, sizeof(program), cfg.tools, "orchard-write");
    join(map, sizeof(map), cfg.run, "root.map");
    join(log, sizeof(log), cfg.run, "write.log");
    snprintf(count, sizeof(count), "%lld", bytes);
    int pipefd[2];
    if (pipe(pipefd)) {
        answer_error("disk", strerror(errno));
        return;
    }
#ifdef F_SETPIPE_SZ
    fcntl(pipefd[1], F_SETPIPE_SZ, 1 << 20);
#endif
    char *argv[] = {program, root_node, map, count, (char *)sha, NULL};
    pid_t pid = fork();
    if (pid == 0) {
        int out = open(log, O_WRONLY | O_CREAT | O_TRUNC, 0600);
        dup2(pipefd[0], 0);
        if (out >= 0) {
            dup2(out, 1);
            dup2(out, 2);
        }
        close(pipefd[1]);
        execv(program, argv);
        _exit(127);
    }
    close(pipefd[0]);
    if (pid < 0) {
        close(pipefd[1]);
        answer_error("disk", strerror(errno));
        return;
    }
    fcntl(pipefd[1], F_SETFD, FD_CLOEXEC);
    memset(&transfer, 0, sizeof(transfer));
    transfer.kind = STREAM;
    transfer.fd = pipefd[1];
    transfer.writer = pid;
    transfer.bytes = bytes;
    snprintf(transfer.sha, sizeof(transfer.sha), "%s", sha);
    sha_init(&transfer.ctx);
    pthread_mutex_lock(&state_lock);
    streaming = 1;
    stream_written = 0;
    stream_bytes = bytes;
    pthread_mutex_unlock(&state_lock);
    mark("stream started");
    answer_ok("{}");
}

static int wait_writer(void)
{
    int status = 0;
    close(transfer.fd);
    while (waitpid(transfer.writer, &status, 0) < 0 && errno == EINTR)
        ;
    transfer.kind = 0;
    pthread_mutex_lock(&state_lock);
    streaming = 0;
    pthread_mutex_unlock(&state_lock);
    return WIFEXITED(status) ? WEXITSTATUS(status) : 128;
}

static void stream_incomplete(void)
{
    set_phase(STREAM_INCOMPLETE);
    mark("stream cut off");
}

static void *finish_worker(void *unused);

static void finish_stream(void)
{
    char hex[65];
    sha_hex(&transfer.ctx, hex);
    const char *code = transfer.code;
    char message[200];
    snprintf(message, sizeof(message), "%s", transfer.message);
    int status = wait_writer();
    if (strcmp(hex, transfer.sha)) {
        stream_incomplete();
        answer_error("sha-mismatch", "The image's bytes don't match its sha256; send it again");
        return;
    }
    if (code || status) {
        char log[300], text[160] = "";
        join(log, sizeof(log), cfg.run, "write.log");
        struct buf b = {0};
        if (!read_file(log, &b, 1 << 20) && b.size) {
            size_t start = b.size > 150 ? b.size - 150 : 0;
            snprintf(text, sizeof(text), "%s", b.data + start);
            text[strcspn(text, "\n")] = 0;
        }
        free(b.data);
        fail("disk", "Writing the image failed%s%s", text[0] ? ": " : "", text[0] ? text : message);
        answer_error("disk", error_message);
        return;
    }
    set_phase(FINISHING);
    mark("image verified");
    struct buf b = {0};
    addf(&b, "{\"bytes\": %" PRIu64 ", \"sha256\": \"%s\"}", transfer.bytes, hex);
    send_message(OK, &b, 0);
    free(b.data);
    pthread_t thread;
    if (pthread_create(&thread, NULL, finish_worker, NULL))
        fail("finish", "The finishing step could not start");
    else
        pthread_detach(thread);
}

/* The link ended, or the host broke the framing, inside a transfer: nothing of it counts. */
static void cut_transfer(void)
{
    if (transfer.kind == PUT) {
        char path[300], part[310];
        close(transfer.fd);
        join(path, sizeof(path), cfg.run, transfer.name);
        snprintf(part, sizeof(part), "%s.part", path);
        unlink(part);
        transfer.kind = 0;
    } else if (transfer.kind == STREAM) {
        kill(transfer.writer, SIGKILL);
        wait_writer();
        stream_incomplete();
    }
}

static void data(const unsigned char *payload, size_t size)
{
    if (size > transfer.bytes - transfer.received) {
        cut_transfer();
        answer_error("protocol", "More data than the transfer announced");
        return;
    }
    sha_update(&transfer.ctx, payload, size);
    transfer.received += size;
    if (!transfer.code && write_all(transfer.fd, payload, size)) {
        transfer.code = transfer.kind == PUT ? "too-large" : "disk";
        snprintf(transfer.message, sizeof(transfer.message), "%s",
                 transfer.kind == PUT ? "The file does not fit in the device's memory" : "The writer stopped");
    }
    if (transfer.kind == STREAM) {
        pthread_mutex_lock(&state_lock);
        stream_written = transfer.received;
        pthread_mutex_unlock(&state_lock);
        if (transfer.received - transfer.reported >= REPORT_EVERY) {
            transfer.reported = transfer.received;
            struct buf b = {0};
            addf(&b, "{\"event\": \"stream\", \"written\": %" PRIu64 ", \"bytes\": %" PRIu64 "}", transfer.received, transfer.bytes);
            emit_event(&b, 0);
            free(b.data);
        }
    }
    if (transfer.received == transfer.bytes)
        transfer.kind == PUT ? finish_put() : finish_stream();
}

/* --- finishing ------------------------------------------------------------------------------ */

static void step(const char *name)
{
    struct buf b = {0};
    addf(&b, "{\"event\": \"step\", \"step\": \"%s\"}", name);
    emit_event(&b, 0);
    free(b.data);
    char text[64];
    snprintf(text, sizeof(text), "step %s", name);
    mark(text);
}

/* Run a program; its output goes to /run/orchard/finish.log. Returns its exit status. */
static int run(char *const argv[])
{
    char log[300];
    join(log, sizeof(log), cfg.run, "finish.log");
    pid_t pid = fork();
    if (pid == 0) {
        int out = open(log, O_WRONLY | O_CREAT | O_APPEND, 0600), none = open("/dev/null", O_RDONLY);
        dup2(none, 0);
        if (out >= 0) {
            dup2(out, 1);
            dup2(out, 2);
        }
        execvp(argv[0], argv);
        _exit(127);
    }
    int status = 0;
    if (pid < 0)
        return -1;
    while (waitpid(pid, &status, 0) < 0 && errno == EINTR)
        ;
    return WIFEXITED(status) ? WEXITSTATUS(status) : 128;
}

/* The root and boot partitions mounted as the root will see them: install_direct.mount_new_root. */
static const char MOUNT[] =
    "set -e\n"
    "mkdir -p \"$3\"\n"
    "mount -t ext4 \"$1\" \"$3\"\n"
    "mount -t proc proc \"$3/proc\"\n"
    "mount -t sysfs sysfs \"$3/sys\"\n"
    "mount -t devtmpfs dev \"$3/dev\"\n"
    "mount -t tmpfs -o mode=0755 tmpfs \"$3/run\"\n"
    "mkdir -p \"$3/run/orchard\" \"$3/boot\"\n"
    "mount --bind \"$4\" \"$3/run/orchard\"\n"
    "mount -t vfat \"$2\" \"$3/boot\"\n"
    "cp \"$3/etc/os-release\" \"$4/os-release\"\n";
/* Everything on storage: install_direct.unmount_new_root. */
static const char UNMOUNT[] =
    "sync\n"
    "for point in boot run/orchard run dev sys proc ''; do umount \"$1/$point\" || exit 1; done\n"
    "sync\n";

static void os_field(const char *text, const char *key, struct buf *out)
{
    size_t n = strlen(key);
    for (const char *line = text; line && *line; line = strchr(line, '\n') ? strchr(line, '\n') + 1 : NULL) {
        if (strncmp(line, key, n) || line[n] != '=')
            continue;
        const char *value = line + n + 1;
        size_t length = strcspn(value, "\n");
        if (length >= 2 && (value[0] == '"' || value[0] == '\'') && value[length - 1] == value[0]) {
            value++;
            length -= 2;
        }
        char *copy = strndup(value, length);
        add_string(out, copy);
        free(copy);
        return;
    }
    add_text(out, "null");
}

/* The finishing program inside the new root (install_device.py finish), its done-steps turned into
 * this protocol's step starts. Returns 0, or -1 with MESSAGE. */
static int run_finish(char *entry, size_t entry_size, char *message, size_t size)
{
    char log[300];
    join(log, sizeof(log), cfg.run, "finish.log");
    int pipefd[2];
    if (pipe(pipefd)) {
        snprintf(message, size, "finishing program: %s", strerror(errno));
        return -1;
    }
    char *argv[] = {"chroot", cfg.newroot, "python3", "/run/orchard/install_device.py", "finish",
                    "/run/orchard/plan.json", NULL};
    pid_t pid = fork();
    if (pid == 0) {
        int out = open(log, O_WRONLY | O_CREAT | O_APPEND, 0600), none = open("/dev/null", O_RDONLY);
        dup2(none, 0);
        dup2(pipefd[1], 1);
        if (out >= 0)
            dup2(out, 2);
        close(pipefd[0]);
        execvp(argv[0], argv);
        _exit(127);
    }
    close(pipefd[1]);
    int has_firmware = jobj(&plan, "firmware") != NULL, has_account = jobj(&plan, "account") != NULL;
    step(has_firmware ? "firmware" : has_account ? "account" : "kernel");
    FILE *events = fdopen(pipefd[0], "r");
    char line[8192], failed[256] = "", finished = 0;
    while (events && fgets(line, sizeof(line), events)) {
        struct json e;
        if (json_parse(line, strlen(line), &e))
            continue;
        const char *kind = jstr(&e, "event"), *name = jstr(&e, "step"), *state = jstr(&e, "state");
        if (kind && !strcmp(kind, "step") && name && state && !strcmp(state, "done")) {
            if (!strcmp(name, "firmware"))
                step(has_account ? "account" : "kernel");
            else if (!strcmp(name, "account"))
                step("kernel");
            else if (!strcmp(name, "platform-layer"))
                step("entry");
            else if (!strcmp(name, "boot-entry") && jstr(&e, "entry")) {
                snprintf(entry, entry_size, "%s", jstr(&e, "entry"));
                size_t n = strlen(entry);
                if (n > 5 && !strcmp(entry + n - 5, ".conf"))
                    entry[n - 5] = 0;
            }
        } else if (kind && !strcmp(kind, "error")) {
            snprintf(failed, sizeof(failed), "%s: %s", jstr(&e, "code") ? jstr(&e, "code") : "error",
                     jstr(&e, "detail") ? jstr(&e, "detail") : "");
        } else if (kind && !strcmp(kind, "finished")) {
            finished = 1;
        }
        json_free(&e);
    }
    if (events)
        fclose(events);
    int status = 0;
    while (pid > 0 && waitpid(pid, &status, 0) < 0 && errno == EINTR)
        ;
    if (pid < 0 || !WIFEXITED(status) || WEXITSTATUS(status) || !finished) {
        snprintf(message, size, "finishing program: %s", failed[0] ? failed : "it stopped without finishing");
        return -1;
    }
    return 0;
}

static void *finish_worker(void *unused)
{
    (void)unused;
    char message[400] = "", entry[200] = "", program[300], other[300];
    struct json *root = jobj(&plan, "root");
    const char *fs_uuid = jstr(root, "fs_uuid"), *label = jstr(root, "label");
    int mounted = 0, ok = 0;
    step("filesystem");
    join(program, sizeof(program), cfg.tools, "tune2fs");
    char *tune[] = {program, "-U", (char *)fs_uuid, "-L", (char *)label, root_node, NULL};
    join(other, sizeof(other), cfg.tools, "resize2fs");
    char *resize[] = {other, root_node, NULL};
    char *mount_argv[] = {"/bin/sh", "-c", (char *)MOUNT, "mount", root_node, boot_node, cfg.newroot, cfg.run, NULL};
    char from[300];
    if (run(tune) || run(resize)) {
        snprintf(message, sizeof(message), "filesystem: setting the root's identity and size failed");
    } else if (run(mount_argv)) {
        snprintf(message, sizeof(message), "filesystem: mounting the new root failed");
        mounted = 1;     /* partly: unmount what is there */
    } else {
        mounted = 1;
        /* The finishing program and the boot-entries script it runs, from the add-on. */
        join(from, sizeof(from), cfg.tools, "install_device.py");
        int bad = copy_private(from, "install_device.py");
        join(from, sizeof(from), cfg.tools, "boot-entries");
        bad = bad || copy_private(from, "boot-entries");
        if (bad)
            snprintf(message, sizeof(message), "finishing program: the add-on lacks install_device.py or boot-entries");
        else
            ok = !run_finish(entry, sizeof(entry), message, sizeof(message));
    }
    /* secrets.json is the finishing program's to read and delete; it never outlives the step. */
    char secrets[300];
    join(secrets, sizeof(secrets), cfg.run, "secrets.json");
    unlink(secrets);
    if (ok)
        step("sync");
    char *unmount_argv[] = {"/bin/sh", "-c", (char *)UNMOUNT, "unmount", cfg.newroot, NULL};
    if (mounted && run(unmount_argv) && ok) {
        ok = 0;
        snprintf(message, sizeof(message), "sync: unmounting the new root failed");
    }
    if (!ok) {
        fail("finish", "%s", message);
        return NULL;
    }
    /* The completion record. */
    struct buf b = {0}, os = {0};
    char path[300];
    join(path, sizeof(path), cfg.run, "os-release");
    read_file(path, &os, 1 << 20);
    add_text(&b, "{\"root_partuuid\": ");
    add_string(&b, jstr(root, "partuuid"));
    add_text(&b, ", \"boot_partuuid\": ");
    add_string(&b, jstr(jobj(&plan, "boot"), "partuuid"));
    add_text(&b, ", \"entry\": ");
    add_string(&b, entry[0] ? entry : NULL);
    add_text(&b, ", \"os\": {\"id\": ");
    os_field(os.data ? os.data : "", "ID", &b);
    add_text(&b, ", \"name\": ");
    os_field(os.data ? os.data : "", "NAME", &b);
    add_text(&b, ", \"version\": ");
    os_field(os.data ? os.data : "", "VERSION_ID", &b);
    add_text(&b, "}, \"image_sha256\": ");
    add_string(&b, jstr(jobj(&plan, "image"), "sha256"));
    add_text(&b, ", \"layer_sha256\": ");
    add_string(&b, jstr(jobj(&plan, "layer"), "sha256"));
    add_text(&b, "}");
    free(os.data);
    pthread_mutex_lock(&state_lock);
    record = strdup(b.data);
    phase = DONE;
    pthread_mutex_unlock(&state_lock);
    struct buf event = {0};
    add_text(&event, "{\"event\": \"done\", \"record\": ");
    add(&event, b.data, b.size);
    add_text(&event, "}");
    emit_event(&event, 0);
    free(event.data);
    free(b.data);
    mark("installed");
    /* Let the host hear it, then hand over: the boot hook continues into the root named here. */
    output_drain(2000);
    char done[64];
    snprintf(done, sizeof(done), "%s\n", jstr(root, "partuuid"));
    if (write_private("done", done, strlen(done)))
        fprintf(stderr, "orchard-install-usb: writing done failed: %s\n", strerror(errno));
    return NULL;
}

/* --- requests ------------------------------------------------------------------------------- */

static void info(void)
{
    struct buf b = {0};
    pthread_mutex_lock(&state_lock);
    addf(&b, "{\"protocol\": %d, \"ecid\": ", PROTOCOL);
    add_string(&b, device_ecid[0] ? device_ecid : NULL);
    add_text(&b, ", \"board\": ");
    add_string(&b, board);
    add_text(&b, ", \"boot_id\": ");
    add_string(&b, boot_id);
    addf(&b, ", \"phase\": \"%s\", \"staged\": [", phase_names[phase]);
    for (int i = 0, first = 1; i < staged_count; i++)
        if (exists(cfg.run, staged[i])) {
            if (!first)
                add_text(&b, ", ");
            add_string(&b, staged[i]);
            first = 0;
        }
    add_text(&b, "], \"stream\": ");
    if (streaming)
        addf(&b, "{\"written\": %" PRIu64 ", \"bytes\": %" PRIu64 "}", stream_written, stream_bytes);
    else
        add_text(&b, "null");
    add_text(&b, ", \"record\": ");
    add(&b, record ? record : "null", strlen(record ? record : "null"));
    add_text(&b, ", \"error\": ");
    if (phase == FAILED) {
        add_text(&b, "{\"code\": ");
        add_string(&b, error_code);
        add_text(&b, ", \"message\": ");
        add_string(&b, error_message);
        add_text(&b, "}");
    } else {
        add_text(&b, "null");
    }
    add_text(&b, "}");
    pthread_mutex_unlock(&state_lock);
    send_message(OK, &b, 0);
    free(b.data);
}

static void request(int type, const unsigned char *payload, size_t size)
{
    if (type == DATA) {
        answer_error("protocol", "Data without a transfer");
        return;
    }
    if (type > STREAM) {
        answer_error("unknown-request", "This receiver speaks protocol 1, which has no such request");
        return;
    }
    if (type == PLAN) {
        if (!identity_ok)
            answer_error("wrong-device", identity_problem);
        else if (current_phase() != READY)
            answer_error("bad-state", "Plan is allowed when ready");
        else
            do_plan((const char *)payload, size);
        return;
    }
    struct json j;
    if (json_parse((const char *)payload, size, &j) || j.type != J_OBJ) {
        json_free(&j);
        answer_error("protocol", "The payload is not a JSON object");
        return;
    }
    enum phase p = current_phase();
    if (type == INFO)
        info();
    else if (!identity_ok)
        answer_error("wrong-device", identity_problem);
    else if (type == INSPECT)
        p == READY || p == FAILED ? inspect() : answer_error("bad-state", "Inspect is allowed when ready or failed");
    else if (type == PUT)
        start_put(&j);
    else if (type == STREAM)
        start_stream(&j);
    json_free(&j);
}

/* --- the link ------------------------------------------------------------------------------- */

static pthread_mutex_t epoch_lock = PTHREAD_MUTEX_INITIALIZER;
static unsigned epoch;          /* FunctionFS ENABLE events: each one starts a fresh session */

static unsigned current_epoch(void)
{
    pthread_mutex_lock(&epoch_lock);
    unsigned e = epoch;
    pthread_mutex_unlock(&epoch_lock);
    return e;
}

static int read_full(int fd, void *data, size_t size)
{
    for (char *p = data; size;) {
        ssize_t n = read(fd, p, size);
        if (n < 0 && errno == EINTR)
            continue;
        if (n <= 0)
            return -1;
        p += n;
        size -= n;
    }
    return 0;
}

/* Messages until the link ends. The device reads exactly each header and payload, so the host may
 * split its transfers however it likes and needs no zero-length packets. */
static void session(int in)
{
    static unsigned char payload[MAX_PAYLOAD];
    unsigned char header[8];
    unsigned started = current_epoch();
    while (!read_full(in, header, 8)) {
        if (current_epoch() != started) {
            /* SET_INTERFACE or a new configuration: a fresh session; the old one's transfer is cut. */
            cut_transfer();
            output_reset(KEEP_FD, 0);
            started = current_epoch();
        }
        uint32_t length = le32(header + 4);
        int type = header[0];
        if (header[1] || header[2] || header[3] || length > MAX_PAYLOAD || type == 0 || (type & 0x80)) {
            cut_transfer();
            answer_error("protocol", "Malformed message header; send SET_INTERFACE to start a fresh session");
            /* The framing is lost: drop everything until the session starts afresh. */
            while (current_epoch() == started && read(in, payload, 512) > 0)
                ;
            return;
        }
        if (read_full(in, payload, length))
            break;
        if (transfer.kind && type != DATA) {
            cut_transfer();
            answer_error("protocol", "A request arrived inside a transfer; the transfer is dropped");
            continue;
        }
        if (transfer.kind)
            data(payload, length);
        else
            request(type, payload, length);
    }
    cut_transfer();
}

static void serve_socket(void)
{
    char path[300];
    join(path, sizeof(path), cfg.dir, "socket");
    int listener = socket(AF_UNIX, SOCK_STREAM, 0);
    struct sockaddr_un address = {.sun_family = AF_UNIX};
    if (strlen(path) >= sizeof(address.sun_path)) {
        errno = ENAMETOOLONG;
        die(path);
    }
    strcpy(address.sun_path, path);
    unlink(path);
    if (listener < 0 || bind(listener, (struct sockaddr *)&address, sizeof(address)) || listen(listener, 1))
        die("socket");
    mark("receiver ready");
    for (;;) {
        int fd = accept(listener, NULL, NULL);
        if (fd < 0) {
            if (errno == EINTR)
                continue;
            die("accept");
        }
        output_reset(fd, 1);
        session(fd);
        output_reset(-1, 1);
        close(fd);
    }
}

#ifdef __linux__
/* The interface: vendor class 0xff, subclass 0x4f, protocol 0x01, bulk OUT then bulk IN. */
struct speed {
    struct usb_interface_descriptor interface;
    struct usb_endpoint_descriptor_no_audio out, in;
} __attribute__((packed));

static struct speed speed(unsigned packet)
{
    struct speed s = {
        .interface = {.bLength = sizeof(s.interface), .bDescriptorType = USB_DT_INTERFACE, .bNumEndpoints = 2,
                      .bInterfaceClass = 0xff, .bInterfaceSubClass = 0x4f, .bInterfaceProtocol = 0x01, .iInterface = 1},
        .out = {.bLength = sizeof(s.out), .bDescriptorType = USB_DT_ENDPOINT, .bEndpointAddress = 1 | USB_DIR_OUT,
                .bmAttributes = USB_ENDPOINT_XFER_BULK, .wMaxPacketSize = htole16(packet)},
        .in = {.bLength = sizeof(s.in), .bDescriptorType = USB_DT_ENDPOINT, .bEndpointAddress = 2 | USB_DIR_IN,
               .bmAttributes = USB_ENDPOINT_XFER_BULK, .wMaxPacketSize = htole16(packet)},
    };
    return s;
}

static void *ep0_thread(void *arg)
{
    int ep0 = *(int *)arg;
    struct usb_functionfs_event events[4];
    for (;;) {
        ssize_t n = read(ep0, events, sizeof(events));
        if (n < 0 && errno == EINTR)
            continue;
        if (n <= 0) {
            usleep(100000);
            continue;
        }
        for (size_t i = 0; i < n / sizeof(events[0]); i++) {
            if (events[i].type == FUNCTIONFS_ENABLE) {
                pthread_mutex_lock(&epoch_lock);
                epoch++;
                pthread_mutex_unlock(&epoch_lock);
                mark("host attached");
            } else if (events[i].type == FUNCTIONFS_SETUP) {
                /* No class or vendor requests: stall them (the wrong direction stalls ep0). */
                char none;
                if (events[i].u.setup.bRequestType & USB_DIR_IN)
                    (void)!read(ep0, &none, 0);
                else
                    (void)!write(ep0, &none, 0);
            }
        }
    }
    return NULL;
}

static void serve_usb(void)
{
    static struct {
        struct usb_functionfs_descs_head_v2 head;
        uint32_t fs_count, hs_count;
        struct speed fs, hs;
    } __attribute__((packed)) descriptors;
    static struct {
        struct usb_functionfs_strings_head head;
        uint16_t language;
        char name[sizeof("Orchard Install")];
    } __attribute__((packed)) strings;
    descriptors.head.magic = htole32(FUNCTIONFS_DESCRIPTORS_MAGIC_V2);
    descriptors.head.length = htole32(sizeof(descriptors));
    descriptors.head.flags = htole32(FUNCTIONFS_HAS_FS_DESC | FUNCTIONFS_HAS_HS_DESC);
    descriptors.fs_count = descriptors.hs_count = htole32(3);
    descriptors.fs = speed(64);
    descriptors.hs = speed(512);
    strings.head.magic = htole32(FUNCTIONFS_STRINGS_MAGIC);
    strings.head.length = htole32(sizeof(strings));
    strings.head.str_count = htole32(1);
    strings.head.lang_count = htole32(1);
    strings.language = htole16(0x0409);
    memcpy(strings.name, "Orchard Install", sizeof(strings.name));
    static int ep0;
    ep0 = open(FFS_DIR "/ep0", O_RDWR | O_CLOEXEC);
    if (ep0 < 0 || write(ep0, &descriptors, sizeof(descriptors)) != sizeof(descriptors) ||
        write(ep0, &strings, sizeof(strings)) != sizeof(strings))
        die("FunctionFS descriptors");
    int out = open(FFS_DIR "/ep1", O_RDONLY | O_CLOEXEC), in = open(FFS_DIR "/ep2", O_WRONLY | O_CLOEXEC);
    if (out < 0 || in < 0)
        die("FunctionFS endpoints");
    pthread_t thread;
    pthread_create(&thread, NULL, ep0_thread, &ep0);
    output_reset(in, 0);
    mark("receiver ready");
    for (;;) {
        session(out);
        usleep(50000);      /* not attached: reads fail until the host enables the interface */
    }
}
#endif

static void configure(int argc, char **argv)
{
    if (argc == 3 && !strcmp(argv[1], "--test")) {
        /* Tests only: everything under DIR, programs from DIR/bin, the host on DIR/socket. */
        const char *dir = argv[2];
        cfg.test = 1;
        snprintf(cfg.dir, sizeof(cfg.dir), "%s", dir);
        join(cfg.disk, sizeof(cfg.disk), dir, "disk");
        join(cfg.run, sizeof(cfg.run), dir, "run");
        join(cfg.tools, sizeof(cfg.tools), dir, "bin");
        join(cfg.newroot, sizeof(cfg.newroot), dir, "newroot");
        join(cfg.ecid, sizeof(cfg.ecid), dir, "ecid");
        join(cfg.adt, sizeof(cfg.adt), dir, "adt");
        join(cfg.compatible, sizeof(cfg.compatible), dir, "compatible");
        join(cfg.boot_id, sizeof(cfg.boot_id), dir, "boot_id");
        char sector[300], text[32], path[1200];
        join(sector, sizeof(sector), dir, "sector");
        first_line(sector, text, sizeof(text));
        cfg.sector = text[0] ? strtoul(text, NULL, 10) : 4096;
        snprintf(path, sizeof(path), "%s:%s", cfg.tools, getenv("PATH") ? getenv("PATH") : "/usr/bin:/bin");
        setenv("PATH", path, 1);
        return;
    }
    if (argc != 1) {
        fputs("usage: orchard-install-usb (from the install boot only)\n", stderr);
        exit(2);
    }
    snprintf(cfg.run, sizeof(cfg.run), "/run/orchard");
    snprintf(cfg.tools, sizeof(cfg.tools), "/install");
    snprintf(cfg.newroot, sizeof(cfg.newroot), "/newroot");
    snprintf(cfg.ecid, sizeof(cfg.ecid), "/install/ecid");
    snprintf(cfg.compatible, sizeof(cfg.compatible), "/proc/device-tree/compatible");
    snprintf(cfg.boot_id, sizeof(cfg.boot_id), "/proc/sys/kernel/random/boot_id");
    setenv("PATH", "/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin", 1);
}

int main(int argc, char **argv)
{
    configure(argc, argv);
    signal(SIGPIPE, SIG_IGN);
    umask(077);
    mkdir(cfg.run, 0700);
    identify();
    if (!identity_ok)
        fail("wrong-device", "%s", identity_problem);
    pthread_t writer, heartbeat;
    if (pthread_create(&writer, NULL, writer_thread, NULL) || pthread_create(&heartbeat, NULL, heartbeat_thread, NULL))
        die("threads");
    if (cfg.test)
        serve_socket();
#ifdef __linux__
    serve_usb();
#else
    fputs("orchard-install-usb: FunctionFS needs Linux; use --test DIR\n", stderr);
#endif
    return 1;
}
