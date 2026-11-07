/* SPDX-License-Identifier: GPL-2.0-only */
/* Exercise the extracted production file preparation with local files only. */
#include <CommonCrypto/CommonDigest.h>
#include <assert.h>
#include <errno.h>
#include <fcntl.h>
#include <stdint.h>
#include <stdarg.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <sys/mman.h>
#include <sys/stat.h>
#include <unistd.h>
#include "ios-identity-bind.h"
#include "ios-attempt-config.h"

static char root_hash[65], ibec_hash[65];
static uint32_t template_crc;
static const char *scenario;
static unsigned int reads, writes, file_syncs, directory_opens, directory_syncs;
static unsigned int directory_closes;
static unsigned int renames, random_calls;
static int close_fault;

static int is(const char *name) { return !strcmp(scenario, name); }

static int fixture_fstat(int fd, struct stat *info)
{
	int result = fstat(fd, info);
	if (!result)
		info->st_uid = is("wrong-owner") ? 501 : 0;
	return result;
}

static ssize_t fixture_pread(int fd, void *bytes, size_t length, off_t offset)
{
	reads++;
	if (is("failed-read")) { errno = EIO; return -1; }
	if (is("short-read-zero") && reads > 1) return 0;
	if ((is("short-read-zero") || is("partial-read")) && length > 7) length = 7;
	return pread(fd, bytes, length, offset);
}

static ssize_t fixture_write(int fd, const void *bytes, size_t length)
{
	writes++;
	if (is("failed-write") || (is("second-write-failed") && writes == 2) ||
	    (is("short-write-error") && writes > 1)) { errno = EIO; return -1; }
	if (is("zero-write") || (is("short-write-zero") && writes > 1)) return 0;
	if ((is("short-write-zero") || is("short-write-error") || is("partial-write")) && length > 7)
		length = 7;
	return write(fd, bytes, length);
}

static int fixture_open(const char *path, int flags, ...)
{
	mode_t mode = 0;
	if (flags & O_CREAT) {
		va_list arguments;
		va_start(arguments, flags);
		mode = (mode_t)va_arg(arguments, int);
		va_end(arguments);
	}
	if (flags & O_DIRECTORY) {
		directory_opens++;
		if (is("failed-directory-open") ||
		    (is("second-directory-open-failed") && directory_opens == 2)) {
			errno = EIO;
			return -1;
		}
	}
	return flags & O_CREAT ? open(path, flags, mode) : open(path, flags);
}

static int fixture_fsync(int fd)
{
	struct stat info;
	assert(!fstat(fd, &info));
	if (S_ISDIR(info.st_mode)) {
		directory_syncs++;
		if (is("failed-directory-sync") ||
		    (is("second-directory-sync-failed") && directory_syncs == 2)) {
			errno = EIO;
			return -1;
		}
	} else {
		file_syncs++;
		if (is("failed-sync")) { errno = EIO; return -1; }
	}
	return fsync(fd);
}

static int fixture_close(int fd)
{
	struct stat info;
	int directory = !fstat(fd, &info) && S_ISDIR(info.st_mode);
	int result = close(fd);
	if (directory) {
		directory_closes++;
		if (is("failed-directory-close") ||
		    (is("second-directory-close-failed") && directory_closes == 2)) {
			errno = EIO;
			return -1;
		}
	}
	if (is("failed-write-close") && writes && !close_fault) {
		close_fault = 1;
		errno = EIO;
		return -1;
	}
	return result;
}

static int fixture_rename(const char *source, const char *target)
{
	renames++;
	if (is("failed-rename") || (is("second-rename-failed") && renames == 2)) {
		errno = EIO;
		return -1;
	}
	return rename(source, target);
}

static void fixture_random(void *output, size_t length)
{
	assert(length == 16);
	random_calls++;
	for (size_t i = 0; i < length; i++)
		((unsigned char *)output)[i] = is("zero-nonce") ? 0 :
			(unsigned char)(i + (is("fresh-nonce") ? 17 : 1));
}

#define open fixture_open
#define fstat fixture_fstat
#define pread fixture_pread
#define write fixture_write
#define fsync fixture_fsync
#define close fixture_close
#define rename fixture_rename
#define arc4random_buf fixture_random
#include "ios-attempt-source.inc"
#undef open
#undef fstat
#undef pread
#undef write
#undef fsync
#undef close
#undef rename
#undef arc4random_buf

static void put_file(const char *path, const unsigned char *bytes, size_t length)
{
	int fd = open(path, O_WRONLY | O_CREAT | O_TRUNC | O_NOFOLLOW, 0600);
	assert(fd >= 0);
	size_t at = 0;
	while (at < length) {
		ssize_t count = write(fd, bytes + at, length - at);
		assert(count > 0);
		at += (size_t)count;
	}
	assert(!close(fd));
}

static unsigned char *get_file(const char *path, size_t length)
{
	unsigned char *bytes = malloc(length);
	assert(bytes);
	int fd = open(path, O_RDONLY | O_NOFOLLOW);
	assert(fd >= 0);
	size_t at = 0;
	while (at < length) {
		ssize_t count = read(fd, bytes + at, length - at);
		assert(count > 0);
		at += (size_t)count;
	}
	assert(!close(fd));
	return bytes;
}

static int exists(const char *path)
{
	struct stat info;
	return !lstat(path, &info);
}

int main(int argc, char **argv)
{
	assert(argc == 2);
	scenario = argv[1];
	unsigned char *boot = get_file(FIXTURE_BOOT_SOURCE, ROOT_BOOT_BYTES);
	unsigned char *ibec = get_file(FIXTURE_IBEC_SOURCE, IBEC_BYTES);
	assert(!orchard_ios_crc32(boot, ROOT_BOOT_BYTES, &template_crc));
	if (is("bad-template-crc")) boot[0] ^= 1;
	if (is("nonspace-slot")) {
		boot[IOS_IDENTITY_SLOT_OFFSET] = 'x';
		assert(!orchard_ios_crc32(boot, ROOT_BOOT_BYTES, &template_crc));
		for (unsigned int i = 0; i < 4; i++)
			ibec[IOS_IBEC_CRC_OFFSET + i] = (unsigned char)(template_crc >> (i * 8));
	}
	if (is("bad-ibec-crc")) ibec[IOS_IBEC_CRC_OFFSET] ^= 1;
	sha_text(boot, ROOT_BOOT_BYTES, root_hash);
	sha_text(ibec, IBEC_BYTES, ibec_hash);
	if (is("bad-sha")) memset(root_hash, '0', 64);
	unlink(ROOT_BOOT_PATH);
	unlink(IBEC_PATH);
	unlink(ATTEMPT_BOOT_PATH);
	unlink(ATTEMPT_IBEC_PATH);
	unlink(ATTEMPT_BOOT_PATH ".pending");
	unlink(ATTEMPT_IBEC_PATH ".pending");
	put_file(ROOT_BOOT_PATH, boot, ROOT_BOOT_BYTES);
	put_file(IBEC_PATH, ibec, IBEC_BYTES);
	const unsigned char victim[] = "untouched-owner-local-victim";
	if (is("linked-pending")) {
		put_file(FIXTURE_VICTIM_PATH, victim, sizeof(victim));
		assert(!symlink(FIXTURE_VICTIM_PATH, ATTEMPT_BOOT_PATH ".pending"));
	}
	if (is("input-symlink")) {
		unlink(ROOT_BOOT_PATH);
		assert(!symlink(FIXTURE_BOOT_SOURCE, ROOT_BOOT_PATH));
	}
	if (is("wrong-mode")) assert(!chmod(ROOT_BOOT_PATH, 0622));
	int result = prepare_attempt(UINT64_C(0x0102030405060708));
	assert(random_calls == 1);
	if (!is("input-symlink")) {
		unsigned char *after = get_file(ROOT_BOOT_PATH, ROOT_BOOT_BYTES);
		assert(!memcmp(boot, after, ROOT_BOOT_BYTES));
		free(after);
	}
	unsigned char *after = get_file(IBEC_PATH, IBEC_BYTES);
	assert(!memcmp(ibec, after, IBEC_BYTES));
	free(after);
	if (is("linked-pending")) {
		after = get_file(FIXTURE_VICTIM_PATH, sizeof(victim));
		assert(!memcmp(victim, after, sizeof(victim)));
		free(after);
		struct stat info;
		assert(!lstat(ATTEMPT_BOOT_PATH ".pending", &info) && S_ISLNK(info.st_mode));
	} else assert(!exists(ATTEMPT_BOOT_PATH ".pending"));
	assert(!exists(ATTEMPT_IBEC_PATH ".pending"));
	printf("{\"case\":\"%s\",\"result\":%d,\"reads\":%u,\"writes\":%u,"
	       "\"file_syncs\":%u,\"directory_opens\":%u,\"directory_syncs\":%u,"
	       "\"directory_closes\":%u,"
	       "\"renames\":%u,\"random_calls\":%u,\"boot_exists\":%s,\"ibec_exists\":%s}\n",
	       scenario, result, reads, writes, file_syncs, directory_opens, directory_syncs,
	       directory_closes,
	       renames, random_calls,
	       exists(ATTEMPT_BOOT_PATH) ? "true" : "false", exists(ATTEMPT_IBEC_PATH) ? "true" : "false");
	free(boot);
	free(ibec);
	return 0;
}
