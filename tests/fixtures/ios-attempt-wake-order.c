/* SPDX-License-Identifier: GPL-2.0-only */
/* Compile actual startup main/wake code with inert identity and power APIs. */
#include <CoreFoundation/CoreFoundation.h>
#include <assert.h>
#include <dlfcn.h>
#include <errno.h>
#include <mach/mach.h>
#include <math.h>
#include <spawn.h>
#include <stdint.h>
#include <stdio.h>
#include <string.h>
#include <sys/types.h>
#include <sys/wait.h>
#include <unistd.h>

static unsigned int checked_calls, live_calls, lease_calls, prepare_calls;
static unsigned int api_calls, schedule_calls, cancel_calls, copy_calls, spawn_calls;
static int live_error, prepare_error, lease_error, hash_error, inspect_mode, lease_open;
static CFDateRef saved_date;
static CFStringRef saved_owner, saved_type;

static int checked(const char *path, size_t length, const char *hash)
{
	(void)path; (void)length; (void)hash;
	assert(!live_calls && !lease_calls && !prepare_calls && !api_calls);
	checked_calls++;
	return hash_error == (int)checked_calls;
}

static int orchard_ios_live_identity(uint64_t *ecid)
{
	assert(checked_calls == 3 && !live_calls && !lease_calls && !prepare_calls && !api_calls);
	live_calls++;
	if (live_error) return -1;
	*ecid = UINT64_C(0x0102030405060708);
	return 0;
}

static int boot_lease(void)
{
	assert(live_calls == 1 && !lease_calls && !prepare_calls && !api_calls);
	lease_calls++;
	if (lease_error) return -1;
	lease_open = 1;
	return 99;
}

static int prepare_attempt(uint64_t ecid)
{
	assert(ecid == UINT64_C(0x0102030405060708));
	assert(live_calls == 1 && lease_calls == 1 && lease_open && !prepare_calls && !api_calls);
	prepare_calls++;
	return prepare_error ? -1 : 0;
}

static kern_return_t fake_schedule(CFDateRef date, CFStringRef owner, CFStringRef type)
{
	assert(live_calls == 1 && prepare_calls == 1 && lease_open && !inspect_mode);
	assert(!schedule_calls && !spawn_calls && !cancel_calls);
	schedule_calls++;
	saved_date = CFRetain(date);
	saved_owner = CFRetain(owner);
	saved_type = CFRetain(type);
	return 0;
}

static kern_return_t fake_cancel(CFDateRef date, CFStringRef owner, CFStringRef type)
{
	assert(live_calls == 1 && prepare_calls == 1 && lease_open);
	assert(schedule_calls == 1 && !cancel_calls);
	assert(date == saved_date && owner == saved_owner && type == saved_type);
	cancel_calls++;
	return 0;
}

static CFArrayRef fake_copy(void)
{
	assert(schedule_calls == 1 && !copy_calls && !spawn_calls && !cancel_calls);
	copy_calls++;
	const void *keys[] = {CFSTR("time"), CFSTR("scheduledby"), CFSTR("eventtype")};
	const void *values[] = {saved_date, saved_owner, saved_type};
	CFDictionaryRef event = CFDictionaryCreate(NULL, keys, values, 3,
		&kCFTypeDictionaryKeyCallBacks, &kCFTypeDictionaryValueCallBacks);
	const void *events[] = {event};
	CFArrayRef result = CFArrayCreate(NULL, events, 1, &kCFTypeArrayCallBacks);
	CFRelease(event);
	return result;
}

static void *fake_dlopen(const char *path, int flags)
{
	assert(!strcmp(path, "/System/Library/Frameworks/IOKit.framework/IOKit"));
	assert(flags == (RTLD_NOW | RTLD_LOCAL));
	assert(live_calls == 1 && !api_calls && !schedule_calls && !spawn_calls);
	assert(inspect_mode ? !prepare_calls && !lease_calls : prepare_calls == 1 && lease_open);
	api_calls++;
	return (void *)(uintptr_t)1;
}

static void *fake_dlsym(void *handle, const char *name)
{
	assert(handle == (void *)(uintptr_t)1);
	if (!strcmp(name, "IOPMSchedulePowerEvent")) return (void *)fake_schedule;
	if (!strcmp(name, "IOPMCancelScheduledPowerEvent")) return (void *)fake_cancel;
	if (!strcmp(name, "IOPMCopyScheduledPowerEvents")) return (void *)fake_copy;
	assert(0);
	return NULL;
}

static int fake_dlclose(void *handle) { assert(handle == (void *)(uintptr_t)1); return 0; }

static int fake_spawn(pid_t *child, const char *path, const posix_spawn_file_actions_t *actions,
		      const posix_spawnattr_t *attributes, char *const arguments[], char *const environment[])
{
	(void)child; (void)environment;
	assert(live_calls == 1 && prepare_calls == 1 && schedule_calls == 1 && copy_calls == 1);
	assert(!spawn_calls && !cancel_calls && lease_open && !actions && !attributes);
	assert(!strcmp(path, "/fixture/kloader") && !strcmp(arguments[0], path));
	assert(!strcmp(arguments[1], "/fixture/attempt-ibec.raw") && !arguments[2]);
	spawn_calls++;
	return EACCES;
}

static pid_t fake_wait(pid_t child, int *status, int flags)
{
	(void)child; (void)status; (void)flags;
	assert(0);
	return -1;
}

static int fake_close(int fd) { assert(fd == 99 && lease_open); lease_open = 0; return 0; }
static int fake_sysctl(const char *name, void *out, size_t *length, void *newp, size_t newlen)
{
	assert(!newp && !newlen);
	const char *value = !strcmp(name, "hw.machine") ? "iPad2,5" : "13G36";
	assert(!strcmp(name, "hw.machine") || !strcmp(name, "kern.osversion"));
	size_t count = strlen(value) + 1;
	assert(count <= *length);
	memcpy(out, value, count);
	*length = count;
	return 0;
}
static uid_t fake_uid(void) { return 0; }
static int fake_setgid(gid_t value) { assert(!value); return 0; }
static int fake_setuid(uid_t value) { assert(!value); return 0; }
static kern_return_t fake_task(mach_port_t task, int pid, mach_port_t *port)
{ (void)task; assert(!pid); *port = 1; return 0; }
static kern_return_t fake_deallocate(mach_port_t task, mach_port_t port)
{ (void)task; assert(port == 1); return 0; }
static CFAbsoluteTime fake_now(void) { return 1000.75; }
static pid_t fake_pid(void) { return 4242; }

#define TARGET_MACHINE "iPad2,5"
#define TARGET_IOS_BUILD "13G36"
#define ROOT_BOOT_PATH "/fixture/template.bin"
#define IBEC_PATH "/fixture/ibec.raw"
#define KLOADER_PATH "/fixture/kloader"
#define ATTEMPT_IBEC_PATH "/fixture/attempt-ibec.raw"
#define ROOT_BOOT_BYTES 1
#define ROOT_BOOT_SHA256 "stub"
#define IBEC_BYTES 1
#define IBEC_SHA256 "stub"
#define KLOADER_BYTES 1
#define KLOADER_SHA256 "stub"
#define IOS_IDENTITY_SLOT_OFFSET 1
#define WAKE_OWNER "org.orchard.fixture"
#define WAKE_DELAY_SECONDS 60
#define dlopen fake_dlopen
#define dlsym fake_dlsym
#define dlclose fake_dlclose
#define posix_spawn fake_spawn
#define waitpid fake_wait
#define close fake_close
#define sysctlbyname fake_sysctl
#define geteuid fake_uid
#define setgid fake_setgid
#define setuid fake_setuid
#define task_for_pid fake_task
#define mach_port_deallocate fake_deallocate
#define CFAbsoluteTimeGetCurrent fake_now
#define getpid fake_pid
#define main inspected_main
#include "ios-attempt-wake-source.inc"
#undef main

static void reset(void)
{
	if (saved_date) CFRelease(saved_date);
	if (saved_owner) CFRelease(saved_owner);
	if (saved_type) CFRelease(saved_type);
	saved_date = NULL; saved_owner = saved_type = NULL;
	checked_calls = live_calls = lease_calls = prepare_calls = 0;
	api_calls = schedule_calls = cancel_calls = copy_calls = spawn_calls = 0;
	live_error = prepare_error = lease_error = hash_error = inspect_mode = lease_open = 0;
}

static void expect(const char *name, int result, unsigned int live, unsigned int leases,
		   unsigned int prepared, unsigned int scheduled, unsigned int spawned)
{
	char *arguments[] = {"helper", "--check-installed", NULL};
	assert(inspected_main(inspect_mode ? 2 : 1, arguments) == result);
	assert(live_calls == live && lease_calls == leases && prepare_calls == prepared);
	assert(schedule_calls == scheduled && cancel_calls == scheduled && spawn_calls == spawned);
	assert(!lease_open);
	printf("{\"case\":\"%s\",\"live\":%u,\"lease\":%u,\"prepare\":%u,"
	       "\"schedule\":%u,\"spawn\":%u}\n", name, live_calls, lease_calls, prepare_calls,
	       schedule_calls, spawn_calls);
}

int main(void)
{
	reset(); live_error = 1; expect("failed-live", 16, 1, 0, 0, 0, 0); assert(!api_calls);
	reset(); prepare_error = 1; expect("failed-prepare", 16, 1, 1, 1, 0, 0); assert(!api_calls);
	reset(); lease_error = 1; expect("failed-lease", 13, 1, 1, 0, 0, 0); assert(!api_calls);
	reset(); hash_error = 2; expect("failed-hash", 12, 0, 0, 0, 0, 0); assert(!api_calls);
	reset(); inspect_mode = 1; expect("inspect", 0, 1, 0, 0, 0, 0); assert(api_calls == 1);
	reset(); expect("prepared-start", 15, 1, 1, 1, 1, 1); assert(api_calls == 1);
	reset();
	puts("native-attempt-wake-order-passed");
	return 0;
}
