// SPDX-License-Identifier: GPL-2.0-only
/* ARM32 iBEC handoff. See NOTICE for the Cascadia source and local changes. */
typedef unsigned int u32;
typedef unsigned char u8;

#include "cascadia-generated.h"

struct config {
	u8 magic[4];
	u32 version, header_size, load_address, kernel_offset;
	u32 dtb_offset, dtb_size, initrd_offset, initrd_size;
	u32 initrd_address, image_size, watchdog_address, l2_address;
	u32 ram_start, ram_end, initrd_limit, crc32;
};

/* The assembler fills the dynamic fields and checksum before upload. */
__attribute__((section(".loader_config"), used))
volatile const struct config cascadia_config = {
	{ 'C', 'B', '3', '2' }, 1, sizeof(struct config), LOAD_ADDRESS,
	KERNEL_OFFSET, 0, 0, 0, 0, INITRD_ADDRESS, 0, WATCHDOG_ADDRESS,
	L2_ADDRESS, RAM_START, RAM_END, INITRD_LIMIT, 0,
};

static int range(u32 start, u32 size, u32 first, u32 end)
{
	return start >= first && start < end && size && size <= end - start;
}

static u32 checksum(void)
{
	volatile const u8 *p = (volatile const u8 *)&cascadia_config;
	u32 crc = ~0u, i, bit;
	for (i = 0; i < sizeof(struct config) - sizeof(u32); i++) {
		crc ^= p[i];
		for (bit = 0; bit < 8; bit++)
			crc = (crc >> 1) ^ (0xedb88320u & (0u - (crc & 1)));
	}
	return ~crc;
}

static u32 big_endian(volatile const u8 *p)
{
	return ((u32)p[0] << 24) | ((u32)p[1] << 16) |
	       ((u32)p[2] << 8) | p[3];
}

u32 cascadia_main(void)
{
	volatile const struct config *c = &cascadia_config;
	volatile const u8 *dtb, *from;
	volatile u8 *to;
	u32 i;

	if (WATCHDOG_ADDRESS) {
		volatile u32 *wdt = (volatile u32 *)WATCHDOG_ADDRESS;
		for (i = 0; i < 4; i++)
			wdt[i] = 0;
	}
	if (c->magic[0] != 'C' || c->magic[1] != 'B' ||
	    c->magic[2] != '3' || c->magic[3] != '2' ||
	    c->version != 1 || c->header_size != sizeof(struct config) ||
	    c->load_address != LOAD_ADDRESS || c->kernel_offset != KERNEL_OFFSET ||
	    c->watchdog_address != WATCHDOG_ADDRESS || c->l2_address != L2_ADDRESS ||
	    c->ram_start != RAM_START || c->ram_end != RAM_END ||
	    c->initrd_limit != INITRD_LIMIT ||
	    checksum() != c->crc32)
		return 0;
	if (!range(LOAD_ADDRESS, c->image_size, RAM_START, RAM_END) ||
	    c->dtb_offset < KERNEL_OFFSET || c->dtb_size < 40 ||
	    c->dtb_offset >= c->image_size ||
	    c->dtb_size > c->image_size - c->dtb_offset ||
	    c->initrd_offset < c->dtb_offset + c->dtb_size ||
	    c->initrd_offset >= c->image_size ||
	    c->initrd_size > c->image_size - c->initrd_offset ||
	    c->initrd_address != INITRD_ADDRESS ||
	    !range(INITRD_ADDRESS, c->initrd_size, RAM_START, INITRD_LIMIT) ||
	    INITRD_ADDRESS < LOAD_ADDRESS + c->image_size)
		return 0;
	dtb = (volatile const u8 *)(LOAD_ADDRESS + c->dtb_offset);
	if (big_endian(dtb) != 0xd00dfeedu || big_endian(dtb + 4) != c->dtb_size)
		return 0;
	from = (volatile const u8 *)(LOAD_ADDRESS + c->initrd_offset);
	to = (volatile u8 *)INITRD_ADDRESS;
	for (i = 0; i < c->initrd_size; i++)
		to[i] = from[i];
	return LOAD_ADDRESS + c->dtb_offset;
}
