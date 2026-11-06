/* SPDX-License-Identifier: GPL-2.0-only */
/* Exercise the producer's actual C entry against anonymous mapped RAM/MMIO. */
#define _GNU_SOURCE
#include <stdint.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <sys/mman.h>

/* The assembler patches this record in writable upload RAM on the device. */
#define const
#include "linux_boot.c"
#undef const

static unsigned int checks;

#define CHECK(test) do { \
	checks++; \
	if (!(test)) { fprintf(stderr, "line %d: %s\n", __LINE__, #test); exit(1); } \
} while (0)

static void *memory(uintptr_t address, size_t bytes)
{
	void *value = mmap((void *)address, bytes, PROT_READ | PROT_WRITE,
			 MAP_PRIVATE | MAP_ANONYMOUS | MAP_FIXED_NOREPLACE, -1, 0);
	CHECK(value == (void *)address);
	return value;
}

static void prepare(void)
{
	struct config value = {
		{ 'C', 'B', '3', '2' }, 1, sizeof(struct config), LOAD_ADDRESS,
		KERNEL_OFFSET, 0x9000, 40, 0xa000, 33, INITRD_ADDRESS,
		0xa000 + 33, WATCHDOG_ADDRESS, L2_ADDRESS,
		RAM_START, RAM_END, INITRD_LIMIT, 0,
	};
	memcpy((void *)&cascadia_config, &value, sizeof(value));
	unsigned char *ram = (void *)(uintptr_t)LOAD_ADDRESS;
	memset(ram, 0, 0x20000);
	ram[0x9000] = 0xd0;
	ram[0x9001] = 0x0d;
	ram[0x9002] = 0xfe;
	ram[0x9003] = 0xed;
	ram[0x9007] = 40;
	for (unsigned int i = 0; i < 33; i++)
		ram[0xa000 + i] = (unsigned char)(i ^ 0x5a);
	memset((void *)(uintptr_t)INITRD_ADDRESS, 0x77, 0x10000);
	memset((void *)(uintptr_t)WATCHDOG_ADDRESS, 0xa5, 16);
	cascadia_config.crc32 = checksum();
}

static void rejects(void)
{
	CHECK(cascadia_main() == 0);
	for (unsigned int i = 0; i < 33; i++)
		CHECK(((unsigned char *)(uintptr_t)INITRD_ADDRESS)[i] == 0x77);
	for (unsigned int i = 0; i < 16; i++)
		CHECK(((unsigned char *)(uintptr_t)WATCHDOG_ADDRESS)[i] == 0xa5);
}

int main(void)
{
	CHECK(sizeof(u32) == 4 && sizeof(struct config) == 68);
	memory(LOAD_ADDRESS, 0x20000);
	memory(INITRD_ADDRESS, 0x10000);
	memory(WATCHDOG_ADDRESS & ~4095u, 4096);
	prepare();
	CHECK(cascadia_main() == LOAD_ADDRESS + 0x9000);
	for (unsigned int i = 0; i < 33; i++)
		CHECK(((unsigned char *)(uintptr_t)INITRD_ADDRESS)[i] == (i ^ 0x5a));
	for (unsigned int i = 0; i < 16; i++)
		CHECK(((unsigned char *)(uintptr_t)WATCHDOG_ADDRESS)[i] == 0);
	CHECK(((unsigned char *)(uintptr_t)INITRD_ADDRESS)[33] == 0x77);

	/* Every field is either profile-fixed or has a checked dynamic bound. */
	volatile u32 *fields = (volatile u32 *)&cascadia_config;
	for (unsigned int i = 0; i < sizeof(struct config) / sizeof(u32); i++) {
		prepare();
		fields[i] = 0xffffffffu;
		if (i != 16)
			cascadia_config.crc32 = checksum();
		rejects();
	}
	prepare();
	cascadia_config.dtb_offset = KERNEL_OFFSET - 4;
	cascadia_config.crc32 = checksum();
	rejects();
	prepare();
	cascadia_config.initrd_offset = 0x9000;
	cascadia_config.crc32 = checksum();
	rejects();
	prepare();
	((unsigned char *)(uintptr_t)(LOAD_ADDRESS + 0x9000))[3] ^= 1;
	rejects();
	prepare();
	((unsigned char *)(uintptr_t)(LOAD_ADDRESS + 0x9000))[7] ^= 1;
	rejects();
	printf("%u runtime checks passed\n", checks);
	return 0;
}
