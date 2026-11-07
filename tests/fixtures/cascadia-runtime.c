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
		CHECK(((unsigned char *)(uintptr_t)WATCHDOG_ADDRESS)[i] == 0);
}

int main(void)
{
	CHECK(sizeof(u32) == 4 && sizeof(struct config) == 68);
	memory(LOAD_ADDRESS, 0x2000000);
	memory(INITRD_ADDRESS, 0x2000000);
	memory(WATCHDOG_ADDRESS & ~4095u, 4096);
	prepare();
	/* Validation must work even when high RAM has no inherited mapping. */
	CHECK(mprotect((void *)(uintptr_t)INITRD_ADDRESS, 0x2000000, PROT_NONE) == 0);
	CHECK(cascadia_main() == LOAD_ADDRESS + 0x9000);
	CHECK(mprotect((void *)(uintptr_t)INITRD_ADDRESS, 0x2000000,
		       PROT_READ | PROT_WRITE) == 0);
	for (unsigned int i = 0; i < 33; i++)
		CHECK(((unsigned char *)(uintptr_t)INITRD_ADDRESS)[i] == 0x77);
	cascadia_relocate_initrd(LOAD_ADDRESS + 0xa000, INITRD_ADDRESS, 33);
	for (unsigned int i = 0; i < 33; i++)
		CHECK(((unsigned char *)(uintptr_t)INITRD_ADDRESS)[i] == (i ^ 0x5a));
	for (unsigned int i = 0; i < 16; i++)
		CHECK(((unsigned char *)(uintptr_t)WATCHDOG_ADDRESS)[i] == 0);
	CHECK(((unsigned char *)(uintptr_t)INITRD_ADDRESS)[33] == 0x77);

	/* Exercise exact word/tail sizes and the real attempt-3 initramfs length. */
	const unsigned int sizes[] = { 0, 1, 2, 3, 4, 7, 8, 33, 4096, 4097, 19124158 };
	unsigned char *source = (void *)(uintptr_t)(LOAD_ADDRESS + 0xa000);
	unsigned char *target = (void *)(uintptr_t)(INITRD_ADDRESS + 16);
	unsigned char *guard = (void *)(uintptr_t)(INITRD_ADDRESS + 12);
	for (unsigned int test = 0; test < sizeof(sizes) / sizeof(sizes[0]); test++) {
		unsigned int size = sizes[test];
		for (unsigned int i = 0; i < size + 4; i++)
			source[i] = (unsigned char)(i ^ (i >> 8) ^ 0x5a);
		memset(target - 4, 0x77, size + 8);
		cascadia_relocate_initrd(LOAD_ADDRESS + 0xa000, INITRD_ADDRESS + 16, size);
		CHECK(memcmp(source, target, size) == 0);
		for (unsigned int i = 0; i < 4; i++) {
			CHECK(guard[i] == 0x77);
			CHECK(target[size + i] == 0x77);
		}
	}

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
	cascadia_config.initrd_offset++;
	cascadia_config.image_size++;
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
