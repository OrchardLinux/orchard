/* SPDX-License-Identifier: GPL-2.0-only */
#ifndef ORCHARD_IOS_LIVE_IDENTITY_H
#define ORCHARD_IOS_LIVE_IDENTITY_H

#include <CoreFoundation/CoreFoundation.h>
#include <mach/mach.h>
#include <stddef.h>
#include <stdint.h>
#include <sys/sysctl.h>

/* All target values are generated from the selected device and boot recipe. */
_Static_assert(sizeof(TARGET_MACHINE) > 1u && sizeof(TARGET_MACHINE) <= 128u,
	       "TARGET_MACHINE must be a bounded C string");
_Static_assert(sizeof(TARGET_MODEL) > 1u && sizeof(TARGET_MODEL) <= 128u,
	       "TARGET_MODEL must be a bounded C string");
_Static_assert(sizeof(TARGET_IOS_BUILD) > 1u && sizeof(TARGET_IOS_BUILD) <= 128u,
	       "TARGET_IOS_BUILD must be a bounded C string");
_Static_assert((uint64_t)TARGET_CHIP > 0u && (uint64_t)TARGET_CHIP <= UINT32_MAX,
	       "TARGET_CHIP must be a nonzero 32-bit integer");
_Static_assert((uint64_t)TARGET_BOARD <= UINT32_MAX, "TARGET_BOARD must be a 32-bit integer");
_Static_assert(TARGET_REGISTRY_COMPATIBLE_BYTES > 0u &&
	       TARGET_REGISTRY_COMPATIBLE_BYTES <= 128u &&
	       TARGET_REGISTRY_COMPATIBLE_BYTES <= sizeof(TARGET_REGISTRY_COMPATIBLE),
	       "TARGET_REGISTRY_COMPATIBLE must carry its bounded exact byte count");

typedef mach_port_t orchard_ios_registry_entry_t;
extern orchard_ios_registry_entry_t IORegistryEntryFromPath(mach_port_t, const char *);
extern CFTypeRef IORegistryEntryCreateCFProperty(orchard_ios_registry_entry_t, CFStringRef,
					       CFAllocatorRef, uint32_t);
extern kern_return_t IOObjectRelease(mach_port_t);

static inline int orchard_ios_same_bytes(const unsigned char *first,
					const unsigned char *second, size_t count)
{
	for (size_t i = 0; i < count; i++)
		if (first[i] != second[i])
			return -1;
	return 0;
}

static inline int orchard_ios_sysctl_exact(const char *name, const char *expected,
					 size_t expected_bytes)
{
	unsigned char value[128] = {0};
	size_t length = sizeof(value);
	if (!expected_bytes || expected_bytes > sizeof(value) ||
	    sysctlbyname(name, value, &length, NULL, 0) || length != expected_bytes)
		return -1;
	return orchard_ios_same_bytes(value, (const unsigned char *)expected, length);
}

static inline int orchard_ios_data_exact(orchard_ios_registry_entry_t entry,
					CFStringRef name, unsigned char *out, size_t bytes)
{
	if (!entry || !out || !bytes || bytes > 128u)
		return -1;
	CFTypeRef value = IORegistryEntryCreateCFProperty(entry, name, NULL, 0);
	if (!value)
		return -1;
	int result = -1;
	if (CFGetTypeID(value) == CFDataGetTypeID() &&
	    CFDataGetLength((CFDataRef)value) == (CFIndex)bytes) {
		const unsigned char *source = CFDataGetBytePtr((CFDataRef)value);
		if (source) {
			for (size_t i = 0; i < bytes; i++)
				out[i] = source[i];
			result = 0;
		}
	}
	CFRelease(value);
	return result;
}

static inline uint32_t orchard_ios_le32(const unsigned char bytes[4])
{
	return (uint32_t)bytes[0] | (uint32_t)bytes[1] << 8 |
		(uint32_t)bytes[2] << 16 | (uint32_t)bytes[3] << 24;
}

/* Only the exact live sysctls and named IODeviceTree CFData properties are
 * accepted. The caller's ECID is written after all guards have passed.
 */
static inline int orchard_ios_live_identity(uint64_t *ecid)
{
	unsigned char unique[8], chip[4], board[4];
	unsigned char model[sizeof(TARGET_MACHINE)];
	unsigned char compatible[TARGET_REGISTRY_COMPATIBLE_BYTES];
	if (!ecid ||
	    TARGET_REGISTRY_COMPATIBLE[TARGET_REGISTRY_COMPATIBLE_BYTES - 1u] ||
	    orchard_ios_sysctl_exact("hw.machine", TARGET_MACHINE, sizeof(TARGET_MACHINE)) ||
	    orchard_ios_sysctl_exact("hw.model", TARGET_MODEL, sizeof(TARGET_MODEL)) ||
	    orchard_ios_sysctl_exact("kern.osversion", TARGET_IOS_BUILD, sizeof(TARGET_IOS_BUILD)))
		return -1;

	orchard_ios_registry_entry_t chosen = IORegistryEntryFromPath(MACH_PORT_NULL, "IODeviceTree:/chosen");
	if (!chosen)
		return -1;
	int failed = orchard_ios_data_exact(chosen, CFSTR("unique-chip-id"), unique, sizeof(unique)) ||
		orchard_ios_data_exact(chosen, CFSTR("chip-id"), chip, sizeof(chip)) ||
		orchard_ios_data_exact(chosen, CFSTR("board-id"), board, sizeof(board));
	IOObjectRelease(chosen);
	if (failed || orchard_ios_le32(chip) != TARGET_CHIP ||
	    orchard_ios_le32(board) != TARGET_BOARD)
		return -1;
	uint64_t observed = 0;
	for (unsigned int i = 0; i < 8; i++)
		observed |= (uint64_t)unique[i] << (i * 8u);
	if (!observed)
		return -1;

	orchard_ios_registry_entry_t root = IORegistryEntryFromPath(MACH_PORT_NULL, "IODeviceTree:/");
	if (!root)
		return -1;
	failed = orchard_ios_data_exact(root, CFSTR("model"), model, sizeof(model)) ||
		orchard_ios_data_exact(root, CFSTR("compatible"), compatible, sizeof(compatible));
	IOObjectRelease(root);
	if (failed || orchard_ios_same_bytes(model, (const unsigned char *)TARGET_MACHINE, sizeof(model)) ||
	    orchard_ios_same_bytes(compatible, (const unsigned char *)TARGET_REGISTRY_COMPATIBLE,
			   sizeof(compatible)))
		return -1;
	*ecid = observed;
	return 0;
}

#endif
