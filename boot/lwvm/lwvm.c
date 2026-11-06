/* SPDX-License-Identifier: GPL-2.0-only */
#include "lwvm.h"
#include <string.h>

static const uint8_t magic[16] = {
	0x6a, 0x90, 0x88, 0xcf, 0x8a, 0xfd, 0x63, 0x0a,
	0xe3, 0x51, 0xe2, 0x48, 0x87, 0xe0, 0xb9, 0x8b,
};
static const uint8_t linux_type[16] = {
	0x0f, 0xc6, 0x3d, 0xaf, 0x84, 0x83, 0x47, 0x72,
	0x8e, 0x79, 0x3d, 0x69, 0xd8, 0x47, 0x7d, 0xe4,
};
static uint16_t le16(const uint8_t *p)
{
	return p[0] | (uint16_t)p[1] << 8;
}
static uint32_t le32(const uint8_t *p)
{
	return le16(p) | (uint32_t)le16(p + 2) << 16;
}
static uint64_t le64(const uint8_t *p)
{
	return le32(p) | (uint64_t)le32(p + 4) << 32;
}
static int fail(const char **error, const char *text)
{
	*error = text;
	return -1;
}
static int zero_uuid(const uint8_t *uuid)
{
	static const uint8_t zero[16];
	return !memcmp(uuid, zero, 16);
}
static int hex(char c)
{
	if (c >= '0' && c <= '9') return c - '0';
	if (c >= 'a' && c <= 'f') return c - 'a' + 10;
	if (c >= 'A' && c <= 'F') return c - 'A' + 10;
	return -1;
}
int lwvm_uuid_parse(const char *text, uint8_t uuid[16])
{
	size_t i, cursor = 0;
	if (strlen(text) != 36) return -1;
	for (i = 0; i < 16; i++) {
		int a, b;
		if (cursor == 8 || cursor == 13 || cursor == 18 || cursor == 23)
			if (text[cursor++] != '-') return -1;
		a = hex(text[cursor++]); b = hex(text[cursor++]);
		if (a < 0 || b < 0) return -1;
		uuid[i] = a * 16 + b;
	}
	return zero_uuid(uuid) ? -1 : 0;
}
void lwvm_uuid_format(const uint8_t uuid[16], char text[37])
{
	static const char digits[] = "0123456789abcdef";
	size_t i, cursor = 0;
	for (i = 0; i < 16; i++) {
		if (i == 4 || i == 6 || i == 8 || i == 10) text[cursor++] = '-';
		text[cursor++] = digits[uuid[i] >> 4];
		text[cursor++] = digits[uuid[i] & 15];
	}
	text[cursor] = 0;
}
int lwvm_geometry(uint64_t media, uint64_t *chunk, const char **error)
{
	uint64_t shifted;
	if (media % 4096 || media < 8192)
		return fail(error, "media must be a multiple of 4096 bytes");
	shifted = (media - 1) >> 10;
	if (!shifted || shifted > UINT32_MAX)
		return fail(error, "unsupported LwVM media size");
	*chunk = 1;
	while (shifted) { *chunk <<= 1; shifted >>= 1; }
	if (*chunk < 4096 || media / *chunk < 2)
		return fail(error, "unsupported LwVM chunk geometry");
	return 0;
}
static uint32_t header_crc(const uint8_t *header)
{
	uint32_t crc = UINT32_MAX;
	size_t i, bit;
	for (i = 0; i < LWVM_HEADER_BYTES; i++) {
		crc ^= i >= 0x2c && i < 0x30 ? 0 : header[i];
		for (bit = 0; bit < 8; bit++)
			crc = (crc >> 1) ^ (0xedb88320U & (0U - (crc & 1)));
	}
	return ~crc;
}
int lwvm_parse(const uint8_t a[LWVM_HEADER_BYTES], const uint8_t b[LWVM_HEADER_BYTES], uint64_t media,
	       const uint8_t uuid[16], struct lwvm_root *root, const char **error)
{
	uint64_t chunk, starts[LWVM_PARTITIONS], ends[LWVM_PARTITIONS];
	uint16_t physical[LWVM_PARTITIONS][LWVM_CHUNKS];
	uint32_t counts[LWVM_PARTITIONS], needed[LWVM_PARTITIONS], count;
	int selected = -1;
	size_t i, j;
	if (lwvm_geometry(media, &chunk, error)) return -1;
	if (memcmp(a, magic, 16) || memcmp(b, magic, 16))
		return fail(error, "unsupported LwVM signature");
	if (le32(a + 0x2c) != header_crc(a) || le32(b + 0x2c) != header_crc(b))
		return fail(error, "LwVM header CRC mismatch");
	if (memcmp(a, b, LWVM_HEADER_BYTES))
		return fail(error, "LwVM header copies disagree");
	if (le64(a + 0x20) != media || zero_uuid(a + 0x10))
		return fail(error, "LwVM media identity or length is invalid");
	count = le32(a + 0x28);
	if (!count || count > LWVM_PARTITIONS)
		return fail(error, "LwVM partition count is outside 1..8");
	memset(physical, 0xff, sizeof(physical));
	memset(counts, 0, sizeof(counts));
	for (i = 0; i < count; i++) {
		const uint8_t *p = a + 0x200 + i * 0x80;
		starts[i] = le64(p + 32); ends[i] = le64(p + 40);
		if (zero_uuid(p) || zero_uuid(p + 16) || starts[i] < 4096 ||
		    starts[i] % 4096 || ends[i] % 4096 || ends[i] <= starts[i] || ends[i] > media)
			return fail(error, "LwVM partition descriptor is invalid");
		for (j = 0; j < i; j++) {
			if (!memcmp(p + 16, a + 0x210 + j * 0x80, 16))
				return fail(error, "duplicate LwVM partition UUID");
			if (starts[i] < ends[j] && starts[j] < ends[i])
				return fail(error, "overlapping LwVM virtual partitions");
		}
		needed[i] = (ends[i] - starts[i] + chunk - 1) / chunk;
		if (needed[i] > LWVM_CHUNKS)
			return fail(error, "LwVM partition exceeds the chunk table");
		if (!memcmp(uuid, p + 16, 16)) selected = (int)i;
	}
	for (i = 0; i < LWVM_CHUNKS; i++) {
		uint16_t word = le16(a + 0x800 + i * 2);
		unsigned int owner = word >> 12, logical = word & 0xfff;
		if (owner == 15) {
			if ((i == 0 && word == 0xf000) ||
			    (i > 0 && i < media / chunk && word == 0xf111) ||
			    (i >= media / chunk && word == 0xf3ff)) continue;
			return fail(error, "invalid LwVM special chunk marker");
		}
		if (!i || i >= media / chunk || owner >= count || logical >= needed[owner])
			return fail(error, "LwVM chunk lies outside its partition or media");
		if (physical[owner][logical] != UINT16_MAX)
			return fail(error, "duplicate LwVM logical chunk");
		physical[owner][logical] = i;
		counts[owner]++;
	}
	for (i = 0; i < count; i++)
		if (counts[i] != needed[i]) return fail(error, "incomplete LwVM partition chunk map");
	if (selected < 0) return fail(error, "requested LwVM root UUID was not found");
	if (memcmp(a + 0x200 + selected * 0x80, linux_type, 16) ||
	    le64(a + 0x230 + selected * 0x80))
		return fail(error, "requested root must have Linux type and zero attributes");
	memset(root, 0, sizeof(*root));
	root->media_bytes = media; root->chunk_bytes = chunk;
	root->bytes = ends[selected] - starts[selected];
	root->partition_index = selected; root->header_crc32 = le32(a + 0x2c);
	memcpy(root->uuid, uuid, 16);
	for (i = 0; i < needed[selected]; i++) {
		uint64_t start = i * chunk / 512;
		uint64_t size = (root->bytes / 512 - start < chunk / 512) ?
			root->bytes / 512 - start : chunk / 512;
		uint64_t offset = (uint64_t)physical[selected][i] * chunk / 512;
		struct lwvm_extent *previous = root->extent_count ? &root->extents[root->extent_count - 1] : NULL;
		if (previous && previous->physical_sector + previous->sectors == offset)
			previous->sectors += size;
		else root->extents[root->extent_count++] = (struct lwvm_extent){start, size, offset};
	}
	return 0;
}
