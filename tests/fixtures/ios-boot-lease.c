/* SPDX-License-Identifier: GPL-2.0-only */
/* Exercise the extracted production iOS boot lease with local files only. */
#include <assert.h>
#include <errno.h>
#include <fcntl.h>
#include <stdint.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <sys/file.h>
#include <sys/stat.h>
#include <sys/sysctl.h>
#include <sys/time.h>
#include <unistd.h>

static const char *fixture_lease_path;
static const char *fixture_scenario;
static struct timeval fixture_boot = {1700000000, 123456};

static int fixture_sysctl(const char *name, void *output, size_t *length,
			  void *input, size_t input_length)
{
	assert(!strcmp(name, "kern.boottime"));
	assert(output && length && *length == sizeof(fixture_boot));
	assert(!input && !input_length);
	memcpy(output, &fixture_boot, sizeof(fixture_boot));
	*length = sizeof(fixture_boot);
	return 0;
}

static int fixture_fstat(int fd, struct stat *info)
{
	int result = fstat(fd, info);
	if (!result)
		info->st_uid = !strcmp(fixture_scenario, "wrong-owner") ? 501 : 0;
	return result;
}

static ssize_t fixture_pread(int fd, void *bytes, size_t length, off_t offset)
{
	ssize_t result = pread(fd, bytes, length, offset);
	if (!strcmp(fixture_scenario, "short-read") && result > 0)
		return result - 1;
	return result;
}

static ssize_t fixture_write(int fd, const void *bytes, size_t length)
{
	if (!strcmp(fixture_scenario, "zero-write"))
		return 0;
	if (!strcmp(fixture_scenario, "short-write") && length > 1)
		return write(fd, bytes, length - 1);
	return write(fd, bytes, length);
}

static int fixture_fsync(int fd)
{
	if (!strcmp(fixture_scenario, "failed-fsync")) {
		errno = EIO;
		return -1;
	}
	return fsync(fd);
}

#define LEASE_PATH fixture_lease_path
#define sysctlbyname fixture_sysctl
#define fstat fixture_fstat
#define pread fixture_pread
#define write fixture_write
#define fsync fixture_fsync
#include "ios-boot-lease-source.inc"
#undef sysctlbyname
#undef fstat
#undef pread
#undef write
#undef fsync

static void clear_lease(void)
{
	unlink(fixture_lease_path);
}

static void put_lease(const void *bytes, size_t length, mode_t mode)
{
	clear_lease();
	int fd = open(fixture_lease_path, O_WRONLY | O_CREAT | O_EXCL | O_NOFOLLOW, mode);
	assert(fd >= 0);
	assert(write(fd, bytes, length) == (ssize_t)length);
	assert(!close(fd));
}

static void expect_text(const char *expected)
{
	char actual[64] = {0};
	int fd = open(fixture_lease_path, O_RDONLY | O_NOFOLLOW);
	assert(fd >= 0);
	ssize_t count = read(fd, actual, sizeof(actual));
	assert(count == (ssize_t)strlen(expected));
	assert(!memcmp(actual, expected, (size_t)count));
	assert(!close(fd));
}

static void refused_state(const char *name, const void *bytes, size_t length, mode_t mode)
{
	fixture_scenario = name;
	put_lease(bytes, length, mode);
	assert(boot_lease() == -1);
	printf("{\"case\":\"%s\",\"result\":-1}\n", name);
}

int main(int argc, char **argv)
{
	assert(argc == 2);
	fixture_lease_path = argv[1];
	clear_lease();

	fixture_scenario = "first-acquisition";
	int fd = boot_lease();
	assert(fd >= 0);
	expect_text("v1:1700000000:123456\n");
	assert(!close(fd));
	puts("{\"case\":\"first-acquisition\",\"result\":0}");

	fixture_scenario = "same-boot-refusal";
	assert(boot_lease() == -1);
	puts("{\"case\":\"same-boot-refusal\",\"result\":-1}");

	fixture_scenario = "changed-boot";
	fixture_boot.tv_sec++;
	fd = boot_lease();
	assert(fd >= 0);
	expect_text("v1:1700000001:123456\n");
	assert(!close(fd));
	puts("{\"case\":\"changed-boot\",\"result\":0}");

	fixture_scenario = "concurrent-lock";
	fixture_boot.tv_sec++;
	fd = boot_lease();
	assert(fd >= 0);
	assert(boot_lease() == -1);
	assert(!close(fd));
	puts("{\"case\":\"concurrent-lock\",\"result\":-1}");

	fixture_scenario = "canonical-minimum";
	put_lease("v1:1:0\n", 7, 0600);
	fd = boot_lease();
	assert(fd >= 0);
	assert(!close(fd));
	puts("{\"case\":\"canonical-minimum\",\"result\":0}");

	static const char malformed_prefix[] = "broken\n";
	static const char malformed_v1[] = "v1:garbage\n";
	static const char noncanonical_v1[] = "v1:01700000000:123456\n";
	static const char trailing_bytes[] = "v1:1:2\nx";
	static const char seconds_overflow[] = "v1:9223372036854775808:2\n";
	static const char microseconds_range[] = "v1:1:1000000\n";
	static const char zero_seconds[] = "v1:0:2\n";
	static const char embedded_nul[] = {'v', '1', ':', '1', '\0', ':', '2', '\n'};
	refused_state("malformed-prefix", malformed_prefix, sizeof(malformed_prefix) - 1, 0600);
	refused_state("malformed-v1", malformed_v1, sizeof(malformed_v1) - 1, 0600);
	refused_state("noncanonical-v1", noncanonical_v1, sizeof(noncanonical_v1) - 1, 0600);
	refused_state("embedded-nul", embedded_nul, sizeof(embedded_nul), 0600);
	refused_state("trailing-bytes", trailing_bytes, sizeof(trailing_bytes) - 1, 0600);
	refused_state("seconds-overflow", seconds_overflow, sizeof(seconds_overflow) - 1, 0600);
	refused_state("microseconds-range", microseconds_range,
		      sizeof(microseconds_range) - 1, 0600);
	refused_state("zero-seconds", zero_seconds, sizeof(zero_seconds) - 1, 0600);
	refused_state("wrong-owner", "v1:1:2\n", 7, 0600);
	refused_state("wrong-mode", "v1:1:2\n", 7, 0644);

	char oversized[64];
	memset(oversized, 'x', sizeof(oversized));
	refused_state("oversized-state", oversized, sizeof(oversized), 0600);
	refused_state("short-read", "v1:1:2\n", 7, 0600);

	fixture_scenario = "symlink";
	clear_lease();
	char target[1024];
	int count = snprintf(target, sizeof(target), "%s.target", fixture_lease_path);
	assert(count > 0 && count < (int)sizeof(target));
	unlink(target);
	int target_fd = open(target, O_WRONLY | O_CREAT | O_EXCL | O_NOFOLLOW, 0600);
	assert(target_fd >= 0);
	assert(!close(target_fd));
	assert(!symlink(target, fixture_lease_path));
	assert(boot_lease() == -1);
	assert(!unlink(fixture_lease_path));
	assert(!unlink(target));
	puts("{\"case\":\"symlink\",\"result\":-1}");

	fixture_scenario = "short-write";
	clear_lease();
	fixture_boot.tv_sec++;
	assert(boot_lease() == -1);
	fixture_scenario = "short-write-residue";
	assert(boot_lease() == -1);
	puts("{\"case\":\"short-write-residue\",\"result\":-1}");

	fixture_scenario = "zero-write";
	clear_lease();
	fixture_boot.tv_sec++;
	assert(boot_lease() == -1);
	fixture_scenario = "zero-write-residue";
	assert(boot_lease() == -1);
	puts("{\"case\":\"zero-write-residue\",\"result\":-1}");

	fixture_scenario = "failed-fsync";
	clear_lease();
	fixture_boot.tv_sec++;
	assert(boot_lease() == -1);
	fixture_scenario = "failed-fsync-residue";
	assert(boot_lease() == -1);
	puts("{\"case\":\"failed-fsync-residue\",\"result\":-1}");

	fixture_scenario = "prepare-failure-consumes";
	clear_lease();
	fixture_boot.tv_sec++;
	fd = boot_lease();
	assert(fd >= 0);
	assert(!close(fd));
	/* A caller that fails preparation closes the lease before returning. */
	assert(boot_lease() == -1);
	puts("{\"case\":\"prepare-failure-consumes\",\"result\":-1}");

	clear_lease();
	puts("native-ios-boot-lease-passed");
	return 0;
}
