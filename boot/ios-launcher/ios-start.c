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
#include <string.h>
#include <sys/mman.h>
#include <sys/file.h>
#include <sys/stat.h>
#include <sys/sysctl.h>
#include <sys/time.h>
#include <sys/wait.h>
#include <unistd.h>
#include "ios-start-config.h"

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
	int fd = open(LEASE_PATH, O_RDWR | O_CREAT | O_NOFOLLOW, 0600);
	if (fd < 0)
		return -1;
	if (flock(fd, LOCK_EX | LOCK_NB) || fstat(fd, &info) ||
	    !S_ISREG(info.st_mode) || info.st_uid || (info.st_mode & 077) ||
	    info.st_size < 0 || info.st_size >= (off_t)sizeof(previous))
		goto refused;
	if (pread(fd, previous, (size_t)info.st_size, 0) != info.st_size)
		goto refused;
	if (info.st_size && (strncmp(previous, "v1:", 3) || !strcmp(previous, token)))
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
			IBEC_PATH, NULL};
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
	return start_with_wake(once);
}
