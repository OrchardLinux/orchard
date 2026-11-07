/* SPDX-License-Identifier: GPL-2.0-only */
#include <assert.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>

#define TARGET_MACHINE "iPad2,5"
#define TARGET_MODEL "P105AP"
#define TARGET_IOS_BUILD "13G36"
#define TARGET_CHIP 0x8942u
#define TARGET_BOARD 0x0au
#define TARGET_REGISTRY_COMPATIBLE "P105AP\0iPad2,5\0AppleARM\0"
#define TARGET_REGISTRY_COMPATIBLE_BYTES 24u
#include "ios-live-identity.h"

static const char *scenario;
static unsigned int opened, closed, properties;

static int is(const char *value)
{
	return !strcmp(scenario, value);
}

int sysctlbyname(const char *name, void *oldp, size_t *oldlenp, void *newp, size_t newlen)
{
	assert(!newp && !newlen);
	const char *value;
	if (!strcmp(name, "hw.machine")) value = TARGET_MACHINE;
	else if (!strcmp(name, "hw.model")) value = TARGET_MODEL;
	else if (!strcmp(name, "kern.osversion")) value = TARGET_IOS_BUILD;
	else abort();
	if (is("sysctl-failure")) return -1;
	if (is("sysctl-oversize")) {
		*oldlenp = 129;
		return 0;
	}
	if (is("wrong-machine") && !strcmp(name, "hw.machine")) value = "iPad2,6";
	if (is("wrong-model") && !strcmp(name, "hw.model")) value = "P106AP";
	if (is("wrong-build") && !strcmp(name, "kern.osversion")) value = "13G37";
	size_t length = strlen(value) + 1u;
	assert(length <= *oldlenp);
	memcpy(oldp, value, length);
	*oldlenp = length;
	if (is("sysctl-short")) (*oldlenp)--;
	if (is("sysctl-unclosed")) ((unsigned char *)oldp)[length - 1u] = 'x';
	return 0;
}

orchard_ios_registry_entry_t IORegistryEntryFromPath(mach_port_t master, const char *path)
{
	assert(!master);
	if (!strcmp(path, "IODeviceTree:/chosen")) {
		if (is("missing-chosen")) return 0;
		opened++;
		return 1;
	}
	if (!strcmp(path, "IODeviceTree:/")) {
		if (is("missing-root")) return 0;
		opened++;
		return 2;
	}
	abort();
}

CFTypeRef IORegistryEntryCreateCFProperty(orchard_ios_registry_entry_t entry, CFStringRef name,
					CFAllocatorRef allocator, uint32_t options)
{
	assert(!allocator && !options);
	properties++;
	unsigned char bytes[128] = {0};
	size_t length;
	const char *key;
	if (entry == 1 && CFEqual(name, CFSTR("unique-chip-id"))) {
		key = "ecid";
		const unsigned char unique[] = {8, 7, 6, 5, 4, 3, 2, 1};
		memcpy(bytes, unique, sizeof(unique));
		length = sizeof(unique);
		if (is("ecid-zero")) memset(bytes, 0, length);
		if (is("fresh-ecid")) bytes[0] = 9;
		if (is("ecid-reversed")) {
			for (unsigned int i = 0; i < 8; i++) bytes[i] = (unsigned char)(i + 1u);
		}
	} else if (entry == 1 && CFEqual(name, CFSTR("chip-id"))) {
		key = "chip";
		bytes[0] = 0x42; bytes[1] = 0x89;
		length = 4;
		if (is("chip-wrong")) bytes[0] = 0x43;
		if (is("chip-reversed")) {
			bytes[0] = 0; bytes[1] = 0; bytes[2] = 0x89; bytes[3] = 0x42;
		}
	} else if (entry == 1 && CFEqual(name, CFSTR("board-id"))) {
		key = "board";
		bytes[0] = 0x0a;
		length = 4;
		if (is("board-wrong")) bytes[0] = 0x0b;
		if (is("board-reversed")) {
			bytes[0] = 0; bytes[3] = 0x0a;
		}
	} else if (entry == 2 && CFEqual(name, CFSTR("model"))) {
		key = "root-model";
		memcpy(bytes, TARGET_MACHINE, sizeof(TARGET_MACHINE));
		length = sizeof(TARGET_MACHINE);
		if (is("root-model-alias")) bytes[6] = '6';
	} else if (entry == 2 && CFEqual(name, CFSTR("compatible"))) {
		key = "compatible";
		memcpy(bytes, TARGET_REGISTRY_COMPATIBLE, TARGET_REGISTRY_COMPATIBLE_BYTES);
		length = TARGET_REGISTRY_COMPATIBLE_BYTES;
		if (is("compatible-alias")) bytes[3] = '6';
		if (is("compatible-reordered"))
			memcpy(bytes, "iPad2,5\0P105AP\0AppleARM\0", length);
	} else abort();
	char variant[64];
	snprintf(variant, sizeof(variant), "%s-missing", key);
	if (is(variant)) return NULL;
	snprintf(variant, sizeof(variant), "%s-type", key);
	if (is(variant))
		return CFStringCreateWithCString(NULL, "wrong CF type", kCFStringEncodingUTF8);
	snprintf(variant, sizeof(variant), "%s-short", key);
	if (is(variant)) length--;
	snprintf(variant, sizeof(variant), "%s-long", key);
	if (is(variant)) length++;
	return CFDataCreate(NULL, bytes, (CFIndex)length);
}

kern_return_t IOObjectRelease(mach_port_t object)
{
	assert(object == 1 || object == 2);
	closed++;
	return KERN_SUCCESS;
}

static void run_case(const char *name, int success, uint64_t expected)
{
	scenario = name;
	opened = closed = properties = 0;
	uint64_t value = UINT64_C(0xa5a5a5a5a5a5a5a5);
	int result = orchard_ios_live_identity(&value);
	assert(opened == closed);
	if (success) {
		assert(!result && value == expected);
		assert(opened == 2 && properties == 5);
	} else {
		assert(result == -1 && value == UINT64_C(0xa5a5a5a5a5a5a5a5));
	}
	printf("{\"case\":\"%s\",\"accepted\":%s,\"opened\":%u,\"closed\":%u,"
	       "\"properties\":%u,\"ecid\":\"%016llx\"}\n", name, success ? "true" : "false",
	       opened, closed, properties, (unsigned long long)value);
}

int main(void)
{
	run_case("valid", 1, UINT64_C(0x0102030405060708));
	run_case("fresh-ecid", 1, UINT64_C(0x0102030405060709));
	/* The byte-order contract is little-endian. An ECID has no independent
	 * expected value, so reversing its bytes yields that distinct value.
	 */
	run_case("ecid-reversed", 1, UINT64_C(0x0807060504030201));
	const char *failures[] = {"sysctl-failure", "sysctl-oversize", "sysctl-short", "sysctl-unclosed",
		"wrong-machine", "wrong-model", "wrong-build", "missing-chosen", "missing-root",
		"ecid-zero", "chip-wrong", "chip-reversed", "board-wrong", "board-reversed",
		"root-model-alias", "compatible-alias", "compatible-reordered"};
	for (size_t i = 0; i < sizeof(failures) / sizeof(failures[0]); i++)
		run_case(failures[i], 0, 0);
	const char *keys[] = {"ecid", "chip", "board", "root-model", "compatible"};
	const char *variants[] = {"missing", "type", "short", "long"};
	for (size_t i = 0; i < sizeof(keys) / sizeof(keys[0]); i++) {
		for (size_t j = 0; j < sizeof(variants) / sizeof(variants[0]); j++) {
			char name[64];
			snprintf(name, sizeof(name), "%s-%s", keys[i], variants[j]);
			run_case(name, 0, 0);
		}
	}
	opened = closed = properties = 0;
	assert(orchard_ios_live_identity(NULL) == -1 && !opened && !properties);
	puts("native-live-identity-checks-passed");
	return 0;
}
