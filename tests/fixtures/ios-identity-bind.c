/* SPDX-License-Identifier: GPL-2.0-only */
#include <assert.h>
#include <stdio.h>
#include <string.h>
#include "ios-identity-bind.h"

enum { IMAGE_BYTES = 1536, SLOT_OFFSET = 128, SLOT_BYTES = 1023 };
static const char nonce[] = "0123456789abcdef0123456789abcdef";
static const char sha[] = "aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa";

static void fresh(unsigned char image[IMAGE_BYTES])
{
	memset(image, 0xa5, IMAGE_BYTES);
	memset(image + SLOT_OFFSET, ' ', SLOT_BYTES);
}

static int bind(unsigned char *image, size_t length, size_t offset, size_t bytes,
		const char *profile, uint64_t ecid, uint32_t chip, uint32_t board,
		const char *identifier, const char *hash)
{
	return orchard_ios_bind_handoff(image, length, offset, bytes, profile, ecid,
					chip, board, identifier, hash);
}

static void rejected(size_t length, size_t offset, size_t bytes, const char *profile,
		     uint64_t ecid, uint32_t chip, uint32_t board,
		     const char *identifier, const char *hash)
{
	unsigned char image[IMAGE_BYTES], before[IMAGE_BYTES];
	fresh(image);
	memcpy(before, image, sizeof(image));
	assert(bind(image, length, offset, bytes, profile, ecid, chip, board, identifier, hash) == -1);
	assert(!memcmp(before, image, sizeof(image)));
}

static size_t encoded(const char *profile, uint64_t ecid, uint32_t chip, uint32_t board,
		      const char *identifier, const char *hash)
{
	unsigned char image[IMAGE_BYTES], before[IMAGE_BYTES];
	fresh(image);
	memcpy(before, image, sizeof(image));
	assert(!bind(image, sizeof(image), SLOT_OFFSET, SLOT_BYTES, profile, ecid, chip,
		     board, identifier, hash));
	assert(!memcmp(before, image, SLOT_OFFSET));
	assert(!memcmp(before + SLOT_OFFSET + SLOT_BYTES, image + SLOT_OFFSET + SLOT_BYTES,
		       sizeof(image) - SLOT_OFFSET - SLOT_BYTES));
	size_t bytes = SLOT_BYTES;
	while (bytes && image[SLOT_OFFSET + bytes - 1u] == ' ')
		bytes--;
	assert(bytes > 0 && bytes < SLOT_BYTES);
	printf("%.*s\n", (int)bytes, image + SLOT_OFFSET);
	return bytes;
}

static void failures(size_t token_bytes)
{
	char long_profile[82], unterminated[81];
	memset(long_profile, 'a', sizeof(long_profile));
	long_profile[81] = 0;
	memset(unterminated, 'a', sizeof(unterminated));
	const char *profiles[] = {NULL, "", "A", ".bad", "a/b", "a\"b", "a\xff", long_profile, unterminated};
	for (size_t i = 0; i < sizeof(profiles) / sizeof(profiles[0]); i++)
		rejected(IMAGE_BYTES, SLOT_OFFSET, SLOT_BYTES, profiles[i], 123, 1, 0, nonce, sha);
	const char *nonces[] = {NULL, "", "f", "00000000000000000000000000000000",
		"FFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFF", "fffffffffffffffffffffffffffffff",
		"fffffffffffffffffffffffffffffffff"};
	for (size_t i = 0; i < sizeof(nonces) / sizeof(nonces[0]); i++)
		rejected(IMAGE_BYTES, SLOT_OFFSET, SLOT_BYTES, "p105", 123, 1, 0, nonces[i], sha);
	const char *hashes[] = {NULL, "", "a", "AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA",
		"aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa",
		"aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa"};
	for (size_t i = 0; i < sizeof(hashes) / sizeof(hashes[0]); i++)
		rejected(IMAGE_BYTES, SLOT_OFFSET, SLOT_BYTES, "p105", 123, 1, 0, nonce, hashes[i]);
	rejected(IMAGE_BYTES, SLOT_OFFSET, SLOT_BYTES, "p105", 0, 1, 0, nonce, sha);
	rejected(IMAGE_BYTES, SLOT_OFFSET, SLOT_BYTES, "p105", 1, 0, 0, nonce, sha);
	rejected(0, SLOT_OFFSET, SLOT_BYTES, "p105", 1, 1, 0, nonce, sha);
	rejected(ORCHARD_IOS_IMAGE_MAX + 1u, SLOT_OFFSET, SLOT_BYTES, "p105", 1, 1, 0, nonce, sha);
	rejected(SIZE_MAX, SLOT_OFFSET, SLOT_BYTES, "p105", 1, 1, 0, nonce, sha);
	rejected(IMAGE_BYTES, SIZE_MAX, SLOT_BYTES, "p105", 1, 1, 0, nonce, sha);
	rejected(IMAGE_BYTES, IMAGE_BYTES, 1, "p105", 1, 1, 0, nonce, sha);
	rejected(IMAGE_BYTES, SLOT_OFFSET, 0, "p105", 1, 1, 0, nonce, sha);
	rejected(IMAGE_BYTES, SLOT_OFFSET, 1024, "p105", 1, 1, 0, nonce, sha);
	rejected(IMAGE_BYTES, SLOT_OFFSET, SIZE_MAX, "p105", 1, 1, 0, nonce, sha);
	rejected(IMAGE_BYTES, SLOT_OFFSET, token_bytes - 1u, "p105", 123, 0x8942, 10, nonce, sha);
	assert(bind(NULL, IMAGE_BYTES, SLOT_OFFSET, SLOT_BYTES, "p105", 1, 1, 0, nonce, sha) == -1);
	for (size_t at = 0; at < SLOT_BYTES; at += SLOT_BYTES / 2u) {
		for (size_t i = 0; i < 3; i++) {
			const unsigned char values[] = {0, '\t', 'x'};
			unsigned char image[IMAGE_BYTES], before[IMAGE_BYTES];
			fresh(image);
			image[SLOT_OFFSET + at] = values[i];
			memcpy(before, image, sizeof(image));
			assert(bind(image, sizeof(image), SLOT_OFFSET, SLOT_BYTES,
				    "p105", 123, 0x8942, 10, nonce, sha) == -1);
			assert(!memcmp(before, image, sizeof(image)));
		}
	}
	unsigned char image[IMAGE_BYTES];
	fresh(image);
	assert(!bind(image, sizeof(image), SLOT_OFFSET, token_bytes,
		     "p105", 123, 0x8942, 10, nonce, sha));
	assert(image[SLOT_OFFSET + token_bytes] == ' ');
	assert(bind(image, sizeof(image), SLOT_OFFSET, SLOT_BYTES,
		    "p105", 123, 0x8942, 10, nonce, sha) == -1);
}

static void crc_cases(void)
{
	uint32_t crc = 0x12345678;
	assert(!orchard_ios_crc32((const unsigned char *)"123456789", 9, &crc));
	assert(crc == 0xcbf43926u);
	assert(!orchard_ios_crc32(NULL, 0, &crc) && !crc);
	crc = 0x12345678;
	assert(orchard_ios_crc32(NULL, 1, &crc) == -1 && crc == 0x12345678);
	assert(orchard_ios_crc32((const unsigned char *)"x", SIZE_MAX, &crc) == -1 && crc == 0x12345678);
	assert(orchard_ios_crc32((const unsigned char *)"x", ORCHARD_IOS_IMAGE_MAX + 1u, &crc) == -1);
	assert(orchard_ios_crc32((const unsigned char *)"x", 1, NULL) == -1);
	unsigned char image[16] = {0}, before[16] = {0};
	image[3] = 0x78; image[4] = 0x56; image[5] = 0x34; image[6] = 0x12;
	memcpy(before, image, sizeof(image));
	assert(orchard_ios_patch_ibec_crc(image, sizeof(image), 3, 1, 2) == -1);
	assert(!memcmp(before, image, sizeof(image)));
	assert(!orchard_ios_patch_ibec_crc(image, sizeof(image), 3, 0x12345678u, 0x89abcdefu));
	assert(image[3] == 0xef && image[4] == 0xcd && image[5] == 0xab && image[6] == 0x89);
	assert(!memcmp(before, image, 3) && !memcmp(before + 7, image + 7, sizeof(image) - 7));
	memcpy(before, image, sizeof(image));
	assert(orchard_ios_patch_ibec_crc(image, sizeof(image), SIZE_MAX, 0, 0) == -1);
	assert(orchard_ios_patch_ibec_crc(image, sizeof(image), 13, 0, 0) == -1);
	assert(orchard_ios_patch_ibec_crc(image, 3, 0, 0, 0) == -1);
	assert(orchard_ios_patch_ibec_crc(image, ORCHARD_IOS_IBEC_MAX + 1u, 0, 0, 0) == -1);
	assert(orchard_ios_patch_ibec_crc(NULL, sizeof(image), 0, 0, 0) == -1);
	assert(!memcmp(before, image, sizeof(image)));
}

int main(void)
{
	size_t bytes = encoded("p105", 123, 0x8942, 10, nonce, sha);
	encoded("a", 1, 1, 0, "00000000000000000000000000000001",
		"0000000000000000000000000000000000000000000000000000000000000000");
	char longest[81];
	memset(longest, 'a', sizeof(longest) - 1);
	longest[80] = 0;
	encoded(longest, UINT64_MAX, UINT32_MAX, UINT32_MAX,
		"ffffffffffffffffffffffffffffffff", "ffffffffffffffffffffffffffffffffffffffffffffffffffffffffffffffff");
	failures(bytes);
	crc_cases();
	puts("native-boundary-checks-passed");
	return 0;
}
