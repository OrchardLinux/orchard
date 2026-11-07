/* SPDX-License-Identifier: GPL-2.0-only */
/* Fixed-purpose setuid helper for the owner-installed iOS launcher. */
#include <CommonCrypto/CommonDigest.h>
#include <CoreFoundation/CoreFoundation.h>
#include <dlfcn.h>
#include <errno.h>
#include <fcntl.h>
#include <mach/mach.h>
#include <math.h>
#include <spawn.h>
#include <stdint.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <sys/mman.h>
#include <sys/file.h>
#include <sys/stat.h>
#include <sys/sysctl.h>
#include <sys/time.h>
#include <sys/wait.h>
#include <unistd.h>
#include "ios-start-config.h"
#ifdef IOS_IDENTITY_SLOT_OFFSET
#include "ios-identity-bind.h"
#include "ios-live-identity.h"
#endif

static int checked(const char *path, size_t length, const char *expected)
{
	int fd = open(path, O_RDONLY | O_NOFOLLOW);
	struct stat info;
	unsigned char digest[CC_SHA256_DIGEST_LENGTH];
	char text[CC_SHA256_DIGEST_LENGTH * 2 + 1];
	if (fd < 0)
		return -1;
	if (fstat(fd, &info) || !S_ISREG(info.st_mode) || info.st_uid ||
	    (info.st_mode & 022) || (uint64_t)info.st_size != length) {
		close(fd);
		return -1;
	}
	void *bytes = mmap(NULL, length, PROT_READ, MAP_PRIVATE, fd, 0);
	if (bytes == MAP_FAILED) {
		close(fd);
		return -1;
	}
	CC_SHA256(bytes, (CC_LONG)length, digest);
	munmap(bytes, length);
	close(fd);
	for (unsigned int i = 0; i < sizeof(digest); i++)
		snprintf(text + i * 2, 3, "%02x", digest[i]);
	return strcmp(text, expected) ? -1 : 0;
}

#ifdef IOS_IDENTITY_SLOT_OFFSET
static void sha_text(const unsigned char *bytes, size_t length, char text[65])
{
	unsigned char digest[CC_SHA256_DIGEST_LENGTH];
	CC_SHA256(bytes, (CC_LONG)length, digest);
	for (unsigned int i = 0; i < sizeof(digest); i++)
		snprintf(text + i * 2, 3, "%02x", digest[i]);
}

static unsigned char *checked_copy(const char *path, size_t length, const char *expected)
{
	if (!length || length > 64u * 1024u * 1024u)
		return NULL;
	int fd = open(path, O_RDONLY | O_NOFOLLOW);
	struct stat info;
	if (fd < 0)
		return NULL;
	if (fstat(fd, &info) || !S_ISREG(info.st_mode) || info.st_uid ||
	    (info.st_mode & 022) || (uint64_t)info.st_size != length) {
		close(fd);
		return NULL;
	}
	unsigned char *copy = malloc(length);
	if (!copy) {
		close(fd);
		return NULL;
	}
	size_t at = 0;
	while (at < length) {
		ssize_t count = pread(fd, copy + at, length - at, (off_t)at);
		if (count <= 0) {
			close(fd);
			free(copy);
			return NULL;
		}
		at += (size_t)count;
	}
	char digest[65];
	sha_text(copy, length, digest);
	if (close(fd) || strcmp(digest, expected)) {
		free(copy);
		return NULL;
	}
	return copy;
}

static int sync_parent(const char *path)
{
	char directory[1024];
	const char *slash;
	size_t length;
	if (!path || path[0] != '/')
		return -1;
	slash = strrchr(path, '/');
	if (!slash || !slash[1])
		return -1;
	length = slash == path ? 1 : (size_t)(slash - path);
	if (length >= sizeof(directory))
		return -1;
	memcpy(directory, path, length);
	directory[length] = '\0';
	int fd = open(directory, O_RDONLY | O_DIRECTORY | O_NOFOLLOW);
	if (fd < 0)
		return -1;
	int result = fsync(fd);
	if (close(fd))
		result = -1;
	return result ? -1 : 0;
}

static int write_attempt(const char *path, const unsigned char *bytes, size_t length)
{
	char pending[1024], digest[65];
	int count = snprintf(pending, sizeof(pending), "%s.pending", path);
	if (count <= 0 || count >= (int)sizeof(pending))
		return -1;
	int fd = open(pending, O_WRONLY | O_CREAT | O_EXCL | O_NOFOLLOW, 0600);
	if (fd < 0)
		return -1;
	size_t at = 0;
	while (at < length) {
		ssize_t written = write(fd, bytes + at, length - at);
		if (written <= 0)
			goto failed;
		at += (size_t)written;
	}
	if (fsync(fd))
		goto failed;
	if (close(fd)) {
		unlink(pending);
		return -1;
	}
	if (rename(pending, path)) {
		unlink(pending);
		return -1;
	}
	if (sync_parent(path))
		return -1;
	sha_text(bytes, length, digest);
	return checked(path, length, digest);
failed:
	close(fd);
	unlink(pending);
	return -1;
}

static int prepare_attempt(uint64_t ecid)
{
	unsigned char random[16];
	char nonce[33];
	arc4random_buf(random, sizeof(random));
	for (unsigned int i = 0; i < sizeof(random); i++)
		snprintf(nonce + i * 2, 3, "%02x", random[i]);
	unsigned char *image = checked_copy(ROOT_BOOT_PATH, ROOT_BOOT_BYTES, ROOT_BOOT_SHA256);
	unsigned char *ibec = checked_copy(IBEC_PATH, IBEC_BYTES, IBEC_SHA256);
	int result = -1;
	uint32_t crc;
	if (!image || !ibec || orchard_ios_crc32(image, ROOT_BOOT_BYTES, &crc) ||
	    crc != IOS_TEMPLATE_CRC32 ||
	    orchard_ios_bind_handoff(image, ROOT_BOOT_BYTES, IOS_IDENTITY_SLOT_OFFSET,
		IOS_IDENTITY_SLOT_BYTES, TARGET_PROFILE, ecid, TARGET_CHIP, TARGET_BOARD,
		nonce, ROOT_BOOT_SHA256) ||
	    orchard_ios_crc32(image, ROOT_BOOT_BYTES, &crc) ||
	    orchard_ios_patch_ibec_crc(ibec, IBEC_BYTES, IOS_IBEC_CRC_OFFSET,
		IOS_TEMPLATE_CRC32, crc) ||
	    write_attempt(ATTEMPT_BOOT_PATH, image, ROOT_BOOT_BYTES) ||
	    write_attempt(ATTEMPT_IBEC_PATH, ibec, IBEC_BYTES))
		goto done;
	result = 0;
done:
	free(image);
	free(ibec);
	return result;
}
#endif

static int lease_number(const char *text, size_t length, size_t *at,
			uint64_t cutoff, uint64_t last_digit, uint64_t *value)
{
	size_t start = *at;
	uint64_t number = 0;
	while (*at < length && text[*at] >= '0' && text[*at] <= '9') {
		uint64_t digit = (uint64_t)(text[*at] - '0');
		if (number > cutoff || (number == cutoff && digit > last_digit))
			return -1;
		number = number * 10 + digit;
		(*at)++;
	}
	if (*at == start)
		return -1;
	*value = number;
	return 0;
}

static int valid_lease_token(const char *text, size_t length)
{
	char canonical[64];
	size_t at = 3;
	uint64_t seconds, microseconds;
	if (length < 7 || memcmp(text, "v1:", 3) ||
	    lease_number(text, length, &at, UINT64_C(922337203685477580), 7, &seconds) ||
	    at >= length || text[at++] != ':' ||
	    lease_number(text, length, &at, 99999, 9, &microseconds) ||
	    seconds == 0 || at + 1 != length || text[at] != '\n')
		return 0;
	int count = snprintf(canonical, sizeof(canonical), "v1:%lld:%d\n",
		(long long)seconds, (int)microseconds);
	return count > 0 && count == (int)length && !memcmp(text, canonical, length);
}

static int boot_lease(void)
{
	struct timeval boot;
	size_t length = sizeof(boot);
	char token[64], previous[64] = {0};
	struct stat info;
	if (sysctlbyname("kern.boottime", &boot, &length, NULL, 0) ||
	    length != sizeof(boot) || boot.tv_sec <= 0 || boot.tv_usec < 0 ||
	    boot.tv_usec >= 1000000)
		return -1;
	int count = snprintf(token, sizeof(token), "v1:%lld:%d\n",
		(long long)boot.tv_sec, (int)boot.tv_usec);
	if (count <= 0 || count >= (int)sizeof(token))
		return -1;
	int created = 1;
	int fd = open(LEASE_PATH, O_RDWR | O_CREAT | O_EXCL | O_NOFOLLOW, 0600);
	if (fd < 0) {
		if (errno != EEXIST)
			return -1;
		created = 0;
		fd = open(LEASE_PATH, O_RDWR | O_NOFOLLOW);
		if (fd < 0)
			return -1;
	}
	if (flock(fd, LOCK_EX | LOCK_NB) || fstat(fd, &info) ||
	    !S_ISREG(info.st_mode) || info.st_uid || (info.st_mode & 077) ||
	    info.st_size < 0 || info.st_size >= (off_t)sizeof(previous) ||
	    (!created && !info.st_size))
		goto refused;
	if (pread(fd, previous, (size_t)info.st_size, 0) != info.st_size)
		goto refused;
	if (info.st_size && (!valid_lease_token(previous, (size_t)info.st_size) ||
	    !strcmp(previous, token)))
		goto refused;
	if (ftruncate(fd, 0) || write(fd, token, (size_t)count) != count || fsync(fd))
		goto refused;
	/* Keep the lock in the parent and child. A new iOS boot has a different token even
	 * when /var/run retains this file, so the on-device path remains usable.
	 */
	return fd;
refused:
	close(fd);
	return -1;
}

struct startup_wake {
	void *kit;
	CFDateRef date;
	CFStringRef owner;
	CFStringRef type;
	kern_return_t (*schedule)(CFDateRef, CFStringRef, CFStringRef);
	kern_return_t (*cancel)(CFDateRef, CFStringRef, CFStringRef);
	CFArrayRef (*copy_events)(void);
	int armed;
};

static void release_wake(struct startup_wake *wake)
{
	if (wake->date)
		CFRelease(wake->date);
	if (wake->owner)
		CFRelease(wake->owner);
	if (wake->kit)
		dlclose(wake->kit);
}

static int cancel_wake(struct startup_wake *wake)
{
	int failed = wake->armed && wake->cancel(wake->date, wake->owner, wake->type);
	wake->armed = 0;
	return failed ? 16 : 0;
}

static int queue_contains_wake(CFArrayRef events, const struct startup_wake *wake)
{
	if (!events || CFGetTypeID(events) != CFArrayGetTypeID())
		return 0;
	CFIndex count = CFArrayGetCount(events);
	if (count < 0 || count > 4096)
		return 0;
	for (CFIndex i = 0; i < count; i++) {
		CFTypeRef event = CFArrayGetValueAtIndex(events, i);
		if (!event || CFGetTypeID(event) != CFDictionaryGetTypeID())
			continue;
		CFTypeRef date = CFDictionaryGetValue(event, CFSTR("time"));
		CFTypeRef owner = CFDictionaryGetValue(event, CFSTR("scheduledby"));
		CFTypeRef type = CFDictionaryGetValue(event, CFSTR("eventtype"));
		if (date && owner && type && CFEqual(date, wake->date) &&
		    CFEqual(owner, wake->owner) && CFEqual(type, wake->type))
			return 1;
	}
	return 0;
}

static int open_wake_api(struct startup_wake *wake)
{
	wake->kit = dlopen("/System/Library/Frameworks/IOKit.framework/IOKit", RTLD_NOW | RTLD_LOCAL);
	if (!wake->kit)
		return 14;
	wake->schedule =
		(kern_return_t (*)(CFDateRef, CFStringRef, CFStringRef))dlsym(wake->kit, "IOPMSchedulePowerEvent");
	wake->cancel = (kern_return_t (*)(CFDateRef, CFStringRef, CFStringRef))
		dlsym(wake->kit, "IOPMCancelScheduledPowerEvent");
	wake->copy_events = (CFArrayRef (*)(void))dlsym(wake->kit, "IOPMCopyScheduledPowerEvents");
	return wake->schedule && wake->cancel && wake->copy_events ? 0 : 14;
}

static int prepare_wake(struct startup_wake *wake)
{
	if (open_wake_api(wake))
		return 14;
	CFAbsoluteTime now = CFAbsoluteTimeGetCurrent();
	if (!(now > 0 && now < 1e12))
		return 14;
	char owner[96];
	int length = snprintf(owner, sizeof(owner), WAKE_OWNER ".%ld", (long)getpid());
	if (length <= 0 || length >= (int)sizeof(owner))
		return 14;
	wake->date = CFDateCreate(NULL, floor(now) + WAKE_DELAY_SECONDS);
	wake->owner = CFStringCreateWithCString(NULL, owner, kCFStringEncodingUTF8);
	wake->type = CFSTR("wake");
	if (!wake->date || !wake->owner)
		return 14;
	/* A failed schedule is treated as uncertain until this exact tuple is
	 * cancelled. Existing events owned by iOS remain outside our operation.
	 */
	wake->armed = 1;
	if (wake->schedule(wake->date, wake->owner, wake->type))
		return 14;
	CFArrayRef events = wake->copy_events();
	int present = queue_contains_wake(events, wake);
	if (events)
		CFRelease(events);
	return present ? 0 : 14;
}

static int start_with_wake(int lease)
{
	struct startup_wake wake = {0};
	int result = prepare_wake(&wake);
	if (!result) {
		char *const arguments[] = {KLOADER_PATH,
#ifdef IOS_IDENTITY_SLOT_OFFSET
			ATTEMPT_IBEC_PATH, NULL};
#else
			IBEC_PATH, NULL};
#endif
		char *const environment[] = {"PATH=/usr/bin:/bin:/usr/sbin:/sbin", "HOME=/var/root", NULL};
		pid_t child;
		int spawned = posix_spawn(&child, arguments[0], NULL, NULL, arguments, environment);
		result = 15;
		if (!spawned) {
			int status;
			pid_t waited;
			do {
				waited = waitpid(child, &status, 0);
			} while (waited < 0 && errno == EINTR);
			if (waited == child) {
				if (WIFEXITED(status) && WEXITSTATUS(status))
					result = WEXITSTATUS(status);
				else if (WIFSIGNALED(status))
					result = 128 + WTERMSIG(status);
			}
		}
	}
	/* A successful kernel handoff never returns to this process. Any
	 * return, including a child error, cancels only our scheduled event.
	 */
	int cancelled = cancel_wake(&wake);
	release_wake(&wake);
	close(lease);
	return cancelled ? cancelled : result;
}

int main(int argc, char **argv)
{
	char model[32] = {0}, build[32] = {0};
	size_t length = sizeof(model);
	mach_port_t kernel = MACH_PORT_NULL;
	const int inspect = argc == 2 && !strcmp(argv[1], "--check-installed");
	if ((argc != 1 && !inspect) || geteuid() ||
	    sysctlbyname("hw.machine", model, &length, NULL, 0) ||
	    !length || length > sizeof(model) || model[length - 1] ||
	    strcmp(model, TARGET_MACHINE))
		return 10;
	length = sizeof(build);
	if (sysctlbyname("kern.osversion", build, &length, NULL, 0) ||
	    !length || length > sizeof(build) || build[length - 1] ||
	    strcmp(build, TARGET_IOS_BUILD))
		return 10;
	if (setgid(0) || setuid(0) ||
	    task_for_pid(mach_task_self(), 0, &kernel) != KERN_SUCCESS)
		return 11;
	mach_port_deallocate(mach_task_self(), kernel);
	if (checked(ROOT_BOOT_PATH, ROOT_BOOT_BYTES, ROOT_BOOT_SHA256) ||
	    checked(IBEC_PATH, IBEC_BYTES, IBEC_SHA256) ||
	    checked(KLOADER_PATH, KLOADER_BYTES, KLOADER_SHA256))
		return 12;
#ifdef IOS_IDENTITY_SLOT_OFFSET
	uint64_t ecid;
	if (orchard_ios_live_identity(&ecid))
		return 16;
#endif
	if (inspect) {
		struct startup_wake wake = {0};
		int available = open_wake_api(&wake);
		release_wake(&wake);
		if (available)
			return available;
		puts("Orchard installed boot files verified");
		return 0;
	}
	int once = boot_lease();
	if (once < 0)
		return 13;
#ifdef IOS_IDENTITY_SLOT_OFFSET
	if (prepare_attempt(ecid)) {
		close(once);
		return 16;
	}
#endif
	return start_with_wake(once);
}
