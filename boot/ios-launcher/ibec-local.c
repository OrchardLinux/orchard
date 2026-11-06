/* SPDX-License-Identifier: GPL-2.0-only */
/* Compile as freestanding Thumb for one hash-pinned, owner-supplied iBEC.
 * The launcher checks SHA-256 before kexec. CRC-32 here catches a short or
 * damaged filesystem read after iOS has stopped. No flash API is called.
 */
#include "local-boot-config.h"
typedef unsigned int u32;
typedef unsigned char u8;

__attribute__((section(".text.entry")))
int orchard_local_boot(void)
{
	int (*finish_init)(void) = (void *)IBEC_FINISH_INIT;
	int (*mount_boot)(const char *) = (void *)IBEC_MOUNT_BOOT;
	int (*load_file)(const char *, void *, u32 *) = (void *)IBEC_LOAD_FILE;
	u32 length = LOCAL_BOOT_BYTES;
	u32 crc = ~0u;
	u8 *data = (void *)LOCAL_BOOT_ADDRESS;
	finish_init();
	if (mount_boot(LOCAL_BOOT_DEVICE) || load_file(LOCAL_BOOT_PATH, data, &length) ||
	    length != LOCAL_BOOT_BYTES)
		return 0;
	for (u32 i = 0; i < length; i++) {
		crc ^= data[i];
		for (u32 bit = 0; bit < 8; bit++)
			crc = (crc >> 1) ^ (0xedb88320u & (0u - (crc & 1u)));
	}
	if (~crc != LOCAL_BOOT_CRC32)
		return 0;
	/* Filesystem reads may populate the buffer through the data cache.
	 * Clean each possible A9 cache line before instruction fetch can use it.
	 */
	__asm__ volatile("dsb sy" : : : "memory");
	for (u32 address = (u32)data; address < (u32)data + length; address += 32)
		__asm__ volatile("mcr p15, 0, %0, c7, c10, 1" : : "r"(address) : "memory");
	__asm__ volatile("dsb sy\n"
			 "mcr p15, 0, %0, c7, c5, 0\n"
			 "dsb sy\n"
			 "isb sy\n" : : "r"(0) : "memory");
	((void (*)(void))data)();
	return 0;
}
