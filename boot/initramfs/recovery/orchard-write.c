/* Restore a zstd stream of disk blocks onto a device, as the map says; the installer's only tool.
 *
 *   orchard-write TARGET MAP BYTES SHA256 < STREAM
 *
 * MAP holds "START COUNT" lines of 4 KiB blocks in stream order. BYTES and SHA256 describe the
 * compressed stream. One thread reads, hashes and decodes while the other writes, so a slow disk and
 * a slow stream don't wait for each other. Exit 0 only if the stream was complete and its hash matched
 * and everything reached the device. Static: it needs nothing from what it runs on.
 */
#define _GNU_SOURCE
#include <errno.h>
#include <fcntl.h>
#include <inttypes.h>
#include <pthread.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <time.h>
#include <unistd.h>
#include <zstd.h>

#define BLOCK 4096
#define PIECE (4u << 20)
#define DEPTH 8

struct piece { unsigned char *data; size_t size; };
static struct piece ring[DEPTH];
static unsigned head, tail, used;
static int finished, failed;
static pthread_mutex_t lock = PTHREAD_MUTEX_INITIALIZER;
static pthread_cond_t changed = PTHREAD_COND_INITIALIZER;
static unsigned long long declared, received;
static char expected[65], actual[65];

/* SHA-256, FIPS 180-4. */
static const uint32_t K[64] = {
    0x428a2f98,0x71374491,0xb5c0fbcf,0xe9b5dba5,0x3956c25b,0x59f111f1,0x923f82a4,0xab1c5ed5,0xd807aa98,0x12835b01,
    0x243185be,0x550c7dc3,0x72be5d74,0x80deb1fe,0x9bdc06a7,0xc19bf174,0xe49b69c1,0xefbe4786,0x0fc19dc6,0x240ca1cc,
    0x2de92c6f,0x4a7484aa,0x5cb0a9dc,0x76f988da,0x983e5152,0xa831c66d,0xb00327c8,0xbf597fc7,0xc6e00bf3,0xd5a79147,
    0x06ca6351,0x14292967,0x27b70a85,0x2e1b2138,0x4d2c6dfc,0x53380d13,0x650a7354,0x766a0abb,0x81c2c92e,0x92722c85,
    0xa2bfe8a1,0xa81a664b,0xc24b8b70,0xc76c51a3,0xd192e819,0xd6990624,0xf40e3585,0x106aa070,0x19a4c116,0x1e376c08,
    0x2748774c,0x34b0bcb5,0x391c0cb3,0x4ed8aa4a,0x5b9cca4f,0x682e6ff3,0x748f82ee,0x78a5636f,0x84c87814,0x8cc70208,
    0x90befffa,0xa4506ceb,0xbef9a3f7,0xc67178f2};
static uint32_t state[8] = {0x6a09e667,0xbb67ae85,0x3c6ef372,0xa54ff53a,0x510e527f,0x9b05688c,0x1f83d9ab,0x5be0cd19};
static unsigned char partial[64];
static size_t partial_size;
static unsigned long long hashed;
#define ROR(x, n) (((x) >> (n)) | ((x) << (32 - (n))))

static void sha_blocks(const unsigned char *p, size_t blocks)
{
    while (blocks--) {
        uint32_t w[64], s[8];
        for (int i = 0; i < 16; i++)
            w[i] = (uint32_t)p[i * 4] << 24 | p[i * 4 + 1] << 16 | p[i * 4 + 2] << 8 | p[i * 4 + 3];
        for (int i = 16; i < 64; i++)
            w[i] = w[i - 16] + (ROR(w[i - 15], 7) ^ ROR(w[i - 15], 18) ^ w[i - 15] >> 3) + w[i - 7] +
                   (ROR(w[i - 2], 17) ^ ROR(w[i - 2], 19) ^ w[i - 2] >> 10);
        memcpy(s, state, sizeof(s));
        for (int i = 0; i < 64; i++) {
            uint32_t t1 = s[7] + (ROR(s[4], 6) ^ ROR(s[4], 11) ^ ROR(s[4], 25)) + ((s[4] & s[5]) ^ (~s[4] & s[6])) + K[i] + w[i];
            uint32_t t2 = (ROR(s[0], 2) ^ ROR(s[0], 13) ^ ROR(s[0], 22)) + ((s[0] & s[1]) ^ (s[0] & s[2]) ^ (s[1] & s[2]));
            s[7] = s[6]; s[6] = s[5]; s[5] = s[4]; s[4] = s[3] + t1;
            s[3] = s[2]; s[2] = s[1]; s[1] = s[0]; s[0] = t1 + t2;
        }
        for (int i = 0; i < 8; i++)
            state[i] += s[i];
        p += 64;
    }
}

static void sha_update(const unsigned char *p, size_t n)
{
    hashed += n;
    if (partial_size) {
        size_t take = n < 64 - partial_size ? n : 64 - partial_size;
        memcpy(partial + partial_size, p, take);
        partial_size += take; p += take; n -= take;
        if (partial_size < 64)
            return;
        sha_blocks(partial, 1);
        partial_size = 0;
    }
    sha_blocks(p, n / 64);
    p += n & ~(size_t)63; n &= 63;
    memcpy(partial, p, n);
    partial_size = n;
}

static void sha_final(char *hex)
{
    unsigned long long bits = hashed * 8;
    unsigned char pad[72] = {0x80};
    size_t pad_size = (partial_size < 56 ? 56 : 120) - partial_size;
    sha_update(pad, pad_size);
    for (int i = 0; i < 8; i++)
        pad[i] = bits >> (56 - i * 8);
    sha_update(pad, 8);
    for (int i = 0; i < 8; i++)
        sprintf(hex + i * 8, "%08x", state[i]);
}

static void fail(const char *message)
{
    fprintf(stderr, "orchard-write: %s\n", message);
    pthread_mutex_lock(&lock);
    failed = 1;
    pthread_cond_broadcast(&changed);
    pthread_mutex_unlock(&lock);
}

static int give(unsigned char *data, size_t size)
{
    pthread_mutex_lock(&lock);
    while (used == DEPTH && !failed)
        pthread_cond_wait(&changed, &lock);
    if (failed) {
        pthread_mutex_unlock(&lock);
        return -1;
    }
    ring[tail] = (struct piece){data, size};
    tail = (tail + 1) % DEPTH;
    used++;
    pthread_cond_broadcast(&changed);
    pthread_mutex_unlock(&lock);
    return 0;
}

static void *reader(void *unused)
{
    (void)unused;
    size_t in_size = ZSTD_DStreamInSize();
    unsigned char *input = malloc(in_size);
    ZSTD_DStream *stream = ZSTD_createDStream();
    size_t last = 1;
    unsigned char *out = malloc(PIECE);
    size_t out_used = 0;

    if (!input || !stream || !out)
        return fail("out of memory"), NULL;
    ZSTD_initDStream(stream);
    for (;;) {
        ssize_t got = read(STDIN_FILENO, input, in_size);
        if (got < 0 && errno == EINTR)
            continue;
        if (got < 0)
            return fail(strerror(errno)), NULL;
        if (got == 0)
            break;
        received += got;
        if (received > declared)
            return fail("the stream is longer than declared"), NULL;
        sha_update(input, got);
        ZSTD_inBuffer in = {input, (size_t)got, 0};
        while (in.pos < in.size) {
            ZSTD_outBuffer o = {out, PIECE, out_used};
            last = ZSTD_decompressStream(stream, &o, &in);
            if (ZSTD_isError(last))
                return fail(ZSTD_getErrorName(last)), NULL;
            out_used = o.pos;
            if (out_used == PIECE) {
                if (give(out, out_used))
                    return NULL;
                out = malloc(PIECE);
                if (!out)
                    return fail("out of memory"), NULL;
                out_used = 0;
            }
        }
    }
    if (last != 0)
        return fail("the stream ended inside a frame"), NULL;
    if (out_used && give(out, out_used))
        return NULL;
    sha_final(actual);
    pthread_mutex_lock(&lock);
    finished = 1;
    pthread_cond_broadcast(&changed);
    pthread_mutex_unlock(&lock);
    return NULL;
}

/* The next piece, or NULL at the end of a good stream. */
static struct piece *next_piece(struct piece *slot)
{
    pthread_mutex_lock(&lock);
    while (!used && !finished && !failed)
        pthread_cond_wait(&changed, &lock);
    if (failed || !used) {
        pthread_mutex_unlock(&lock);
        return NULL;
    }
    *slot = ring[head];
    head = (head + 1) % DEPTH;
    used--;
    pthread_cond_broadcast(&changed);
    pthread_mutex_unlock(&lock);
    return slot;
}

static double now(void)
{
    struct timespec t;
    clock_gettime(CLOCK_MONOTONIC, &t);
    return t.tv_sec + t.tv_nsec / 1e9;
}

int main(int argc, char **argv)
{
    if (argc != 5 || strlen(argv[4]) != 64) {
        fputs("usage: orchard-write TARGET MAP BYTES SHA256 < STREAM\n", stderr);
        return 2;
    }
    declared = strtoull(argv[3], NULL, 10);
    strcpy(expected, argv[4]);
    int fd = open(argv[1], O_WRONLY);
    FILE *map = fopen(argv[2], "r");
    if (fd < 0 || !map) {
        perror("orchard-write");
        return 1;
    }
    pthread_t thread;
    pthread_create(&thread, NULL, reader, NULL);
    double started = now(), writing = 0;
    struct piece slot = {0}, *current = NULL;
    size_t position = 0;
    uint64_t start, count, end = 0;

    while (fscanf(map, "%" SCNu64 " %" SCNu64, &start, &count) == 2) {
        if (start < end || !count) {
            fail("the map's ranges are out of order");
            return 1;
        }
        end = start + count;
        uint64_t offset = start * BLOCK, left = count * BLOCK;
        while (left) {
            if (!current || position == current->size) {
                if (current)
                    free(current->data);
                current = next_piece(&slot);
                position = 0;
                if (!current) {
                    fail(failed ? "stopped" : "the stream ended before the map did");
                    return 1;
                }
            }
            size_t size = current->size - position;
            if (size > left)
                size = left;
            double before = now();
            ssize_t written = pwrite(fd, current->data + position, size, offset);
            writing += now() - before;
            if (written <= 0) {
                perror("orchard-write");
                fail("write failed");
                return 1;
            }
            position += written; offset += written; left -= written;
        }
    }
    if ((current && position != current->size) || next_piece(&slot)) {
        fail("the stream is longer than the map");
        return 1;
    }
    pthread_join(thread, NULL);
    if (failed)
        return 1;
    if (received != declared || strcmp(actual, expected)) {
        fprintf(stderr, "orchard-write: stream differs: %llu bytes, %s\n", received, actual);
        return 1;
    }
    double before = now();
    if (fsync(fd)) {
        perror("orchard-write");
        return 1;
    }
    printf("{\"bytes\": %llu, \"total_s\": %.2f, \"write_s\": %.2f, \"flush_s\": %.2f}\n",
           received, now() - started, writing, now() - before);
    return 0;
}
