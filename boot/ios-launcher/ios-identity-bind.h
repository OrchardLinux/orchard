/* SPDX-License-Identifier: GPL-2.0-only */
#ifndef ORCHARD_IOS_IDENTITY_BIND_H
#define ORCHARD_IOS_IDENTITY_BIND_H

#include <stddef.h>
#include <stdint.h>
#include <stdio.h>

#define ORCHARD_IOS_IMAGE_MAX (64u * 1024u * 1024u)
#define ORCHARD_IOS_IBEC_MAX (1u * 1024u * 1024u)
#define ORCHARD_IOS_HANDOFF_SLOT_MAX 1023u
#define ORCHARD_IOS_HANDOFF_JSON_MAX 512u
#define ORCHARD_IOS_HANDOFF_PREFIX "orchard.recovery-handoff="

struct orchard_ios_text {
	char bytes[ORCHARD_IOS_HANDOFF_JSON_MAX];
	size_t count;
	int failed;
};

static inline void orchard_ios_text_append(struct orchard_ios_text *text,
					 const char *bytes, size_t count)
{
	if (text->failed || count > sizeof(text->bytes) - text->count) {
		text->failed = 1;
		return;
	}
	for (size_t i = 0; i < count; i++)
		text->bytes[text->count++] = bytes[i];
}

static inline void orchard_ios_text_number(struct orchard_ios_text *text, uint64_t value)
{
	char number[21];
	int count = snprintf(number, sizeof(number), "%llu", (unsigned long long)value);
	if (count <= 0 || count >= (int)sizeof(number)) {
		text->failed = 1;
		return;
	}
	orchard_ios_text_append(text, number, (size_t)count);
}

static inline int orchard_ios_profile_length(const char *profile, size_t *length)
{
	if (!profile || !length)
		return -1;
	for (size_t i = 0; i <= 80u; i++) {
		unsigned char value = (unsigned char)profile[i];
		if (!value) {
			if (!i)
				return -1;
			*length = i;
			return 0;
		}
		if (i == 80u || !((value >= 'a' && value <= 'z') ||
			(value >= '0' && value <= '9') ||
			(i && (value == '.' || value == '_' || value == '-'))))
			return -1;
	}
	return -1;
}

static inline int orchard_ios_hex_string(const char *text, size_t length, int nonzero)
{
	unsigned char any = 0;
	if (!text)
		return -1;
	for (size_t i = 0; i < length; i++) {
		unsigned char value = (unsigned char)text[i];
		if (!((value >= '0' && value <= '9') || (value >= 'a' && value <= 'f')))
			return -1;
		any |= (unsigned char)(value != '0');
	}
	return text[length] || (nonzero && !any) ? -1 : 0;
}

/* The caller supplies the parsed /chosen bootargs whitespace slot. This
 * function changes only that range, after validating every input and byte.
 * Its JSON order and unpadded base64url match recovery_identity.py.
 */
static inline int orchard_ios_bind_handoff(unsigned char *image, size_t length,
		size_t slot_offset, size_t slot_bytes, const char *profile,
		uint64_t ecid, uint32_t chip, uint32_t board,
		const char *nonce, const char *template_sha256)
{
	static const char alphabet[] = "ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789-_";
	static const char prefix[] = ORCHARD_IOS_HANDOFF_PREFIX;
	struct orchard_ios_text json = {{0}, 0, 0};
	char encoded[(ORCHARD_IOS_HANDOFF_JSON_MAX * 4u + 2u) / 3u];
	size_t profile_bytes, encoded_bytes = 0;
	if (!image || !length || length > ORCHARD_IOS_IMAGE_MAX ||
	    !slot_bytes || slot_bytes > ORCHARD_IOS_HANDOFF_SLOT_MAX ||
	    slot_offset > length || slot_bytes > length - slot_offset || !ecid || !chip ||
	    orchard_ios_profile_length(profile, &profile_bytes) ||
	    orchard_ios_hex_string(nonce, 32, 1) ||
	    orchard_ios_hex_string(template_sha256, 64, 0))
		return -1;

#define ORCHARD_IOS_TEXT(literal) orchard_ios_text_append(&json, literal, sizeof(literal) - 1u)
	ORCHARD_IOS_TEXT("{\"board\":");
	orchard_ios_text_number(&json, board);
	ORCHARD_IOS_TEXT(",\"chip\":");
	orchard_ios_text_number(&json, chip);
	ORCHARD_IOS_TEXT(",\"ecid\":");
	orchard_ios_text_number(&json, ecid);
	ORCHARD_IOS_TEXT(",\"handoff_id\":\"");
	orchard_ios_text_append(&json, nonce, 32);
	ORCHARD_IOS_TEXT("\",\"profile\":\"");
	orchard_ios_text_append(&json, profile, profile_bytes);
	ORCHARD_IOS_TEXT("\",\"schema\":1,\"source\":\"ios-device\",\"template_sha256\":\"");
	orchard_ios_text_append(&json, template_sha256, 64);
	ORCHARD_IOS_TEXT("\"}");
#undef ORCHARD_IOS_TEXT
	if (json.failed)
		return -1;
	for (size_t i = 0; i < json.count; i += 3u) {
		size_t remaining = json.count - i;
		uint32_t value = (uint32_t)(unsigned char)json.bytes[i] << 16;
		if (remaining > 1u)
			value |= (uint32_t)(unsigned char)json.bytes[i + 1u] << 8;
		if (remaining > 2u)
			value |= (uint32_t)(unsigned char)json.bytes[i + 2u];
		encoded[encoded_bytes++] = alphabet[(value >> 18) & 63u];
		encoded[encoded_bytes++] = alphabet[(value >> 12) & 63u];
		if (remaining > 1u)
			encoded[encoded_bytes++] = alphabet[(value >> 6) & 63u];
		if (remaining > 2u)
			encoded[encoded_bytes++] = alphabet[value & 63u];
	}
	const size_t prefix_bytes = sizeof(prefix) - 1u;
	if (prefix_bytes + encoded_bytes > slot_bytes)
		return -1;
	for (size_t i = 0; i < slot_bytes; i++)
		if (image[slot_offset + i] != ' ')
			return -1;
	for (size_t i = 0; i < prefix_bytes; i++)
		image[slot_offset + i] = (unsigned char)prefix[i];
	for (size_t i = 0; i < encoded_bytes; i++)
		image[slot_offset + prefix_bytes + i] = (unsigned char)encoded[i];
	/* The original whitespace already pads the rest of this fixed slot. */
	return 0;
}

static inline int orchard_ios_crc32(const unsigned char *data, size_t length,
				  uint32_t *result)
{
	uint32_t crc = UINT32_MAX;
	if ((!data && length) || length > ORCHARD_IOS_IMAGE_MAX || !result)
		return -1;
	for (size_t i = 0; i < length; i++) {
		crc ^= data[i];
		for (unsigned int bit = 0; bit < 8; bit++)
			crc = (crc >> 1) ^ (0xedb88320u & (0u - (crc & 1u)));
	}
	*result = ~crc;
	return 0;
}

/* A generated CRC data word is the only permitted iBEC copy modification. */
static inline int orchard_ios_patch_ibec_crc(unsigned char *image, size_t length,
					   size_t offset, uint32_t expected, uint32_t replacement)
{
	if (!image || length < 4u || length > ORCHARD_IOS_IBEC_MAX ||
	    offset > length || 4u > length - offset)
		return -1;
	uint32_t observed = (uint32_t)image[offset] | (uint32_t)image[offset + 1u] << 8 |
		(uint32_t)image[offset + 2u] << 16 | (uint32_t)image[offset + 3u] << 24;
	if (observed != expected)
		return -1;
	for (unsigned int i = 0; i < 4; i++)
		image[offset + i] = (unsigned char)(replacement >> (i * 8u));
	return 0;
}

#endif
