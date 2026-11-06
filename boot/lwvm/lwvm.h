/* SPDX-License-Identifier: GPL-2.0-only */
#ifndef ORCHARD_LWVM_H
#define ORCHARD_LWVM_H
#include <stddef.h>
#include <stdint.h>

#define LWVM_HEADER_BYTES 4096
#define LWVM_CHUNKS 1024
#define LWVM_PARTITIONS 8

struct lwvm_extent {
	uint64_t start_sector, sectors, physical_sector;
};
struct lwvm_root {
	uint64_t media_bytes, chunk_bytes, bytes;
	uint32_t header_crc32, partition_index;
	uint8_t uuid[16];
	size_t extent_count;
	struct lwvm_extent extents[LWVM_CHUNKS];
};

int lwvm_uuid_parse(const char *text, uint8_t uuid[16]);
void lwvm_uuid_format(const uint8_t uuid[16], char text[37]);
int lwvm_geometry(uint64_t media, uint64_t *chunk, const char **error);
int lwvm_parse(const uint8_t primary[LWVM_HEADER_BYTES],
	       const uint8_t backup[LWVM_HEADER_BYTES], uint64_t media,
	       const uint8_t uuid[16], struct lwvm_root *root, const char **error);
#endif
