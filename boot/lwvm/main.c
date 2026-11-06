/* SPDX-License-Identifier: GPL-2.0-only */
#define _GNU_SOURCE
#define _FILE_OFFSET_BITS 64
#include "lwvm.h"
#include <errno.h>
#include <fcntl.h>
#include <inttypes.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <sys/stat.h>
#include <unistd.h>
#ifdef __linux__
#include <linux/dm-ioctl.h>
#include <linux/fs.h>
#include <sys/file.h>
#include <sys/ioctl.h>
#include <sys/sysmacros.h>
#endif

static int error(const char *text)
{
	fprintf(stderr, "orchard-lwvm: %s\n", text);
	return 1;
}
static int read_header(int fd, uint8_t *out, uint64_t offset)
{
	size_t done = 0;
	while (done < LWVM_HEADER_BYTES) {
		ssize_t n = pread(fd, out + done, LWVM_HEADER_BYTES - done, offset + done);
		if (n < 0 && errno == EINTR) continue;
		if (n <= 0) return -1;
		done += n;
	}
	return 0;
}

#ifdef __linux__
#define DM_BUFFER (256 * 1024)
static void dm_init(struct dm_ioctl *d, const char *name)
{
	memset(d, 0, DM_BUFFER);
	d->version[0] = 4;
	d->data_size = DM_BUFFER;
	d->data_start = sizeof(*d);
	snprintf(d->name, sizeof(d->name), "%s", name);
}
static int dm_check(int fd, struct dm_ioctl *d, const char *name,
		    const char *uuid, const struct lwvm_root *root,
		    dev_t disk, int readonly, dev_t *device)
{
	char *cursor, *end;
	size_t i;
	dm_init(d, name);
	if (ioctl(fd, DM_DEV_STATUS, d)) return -1;
	if (strcmp(d->uuid, uuid) || !!(d->flags & DM_READONLY_FLAG) != readonly ||
	    d->flags & DM_SUSPEND_FLAG) return -1;
	*device = (dev_t)d->dev;
	dm_init(d, name);
	d->flags = DM_STATUS_TABLE_FLAG;
	if (ioctl(fd, DM_TABLE_STATUS, d) || d->flags & DM_BUFFER_FULL_FLAG ||
	    d->target_count != root->extent_count || d->data_start < sizeof(*d) ||
	    d->data_start >= d->data_size || d->data_size > DM_BUFFER) return -1;
	cursor = (char *)d + d->data_start; end = (char *)d + d->data_size;
	for (i = 0; i < root->extent_count; i++) {
		struct dm_target_spec *t;
		unsigned int ma, mi;
		uint64_t offset;
		int consumed = 0;
		char *params;
		if ((size_t)(end - cursor) < sizeof(*t)) return -1;
		t = (struct dm_target_spec *)cursor;
		params = cursor + sizeof(*t);
		if (!memchr(t->target_type, 0, sizeof(t->target_type)) || strcmp(t->target_type, "linear") ||
		    !memchr(params, 0, end - params) ||
		    sscanf(params, "%u:%u %" SCNu64 "%n", &ma, &mi, &offset, &consumed) != 3 ||
		    params[consumed] || makedev(ma, mi) != disk ||
		    t->sector_start != root->extents[i].start_sector ||
		    t->length != root->extents[i].sectors || offset != root->extents[i].physical_sector)
			return -1;
		if (i + 1 < root->extent_count) {
			/* Status next is relative to the first target, unlike table load. */
			char *next = (char *)d + d->data_start + t->next;
			if (!t->next || next <= params || next > end || (uintptr_t)next % 8) return -1;
			cursor = next;
		}
	}
	return 0;
}
static int map_root(const struct lwvm_root *root, dev_t disk, int readonly,
		    char name[DM_NAME_LEN], char uuid[DM_UUID_LEN], char path[64])
{
	struct dm_ioctl *d = calloc(1, DM_BUFFER);
	char text[37], *cursor;
	int control = -1, lock = -1, created = 0, rc = -1;
	dev_t device = 0;
	size_t i;
	struct stat st;
	if (!d) return -1;
	lwvm_uuid_format(root->uuid, text);
	snprintf(name, DM_NAME_LEN, "orchard-lwvm-%s", text);
	snprintf(uuid, DM_UUID_LEN, "ORCHARD-LWVM-%s", text);
	lock = open("/run/orchard-lwvm.lock", O_RDWR | O_CREAT | O_NOFOLLOW | O_CLOEXEC, 0600);
	if (lock < 0 || fstat(lock, &st) || !S_ISREG(st.st_mode) || st.st_uid != 0 ||
	    st.st_nlink != 1 || flock(lock, LOCK_EX | LOCK_NB)) goto out;
	control = open("/dev/mapper/control", O_RDWR | O_NOFOLLOW | O_CLOEXEC);
	if (control < 0) goto out;
	dm_init(d, name);
	if (!ioctl(control, DM_DEV_STATUS, d)) {
		if (dm_check(control, d, name, uuid, root, disk, readonly, &device)) goto out;
		goto ready;
	}
	if (errno != ENXIO && errno != ENODEV) goto out;
	dm_init(d, name);
	snprintf(d->uuid, sizeof(d->uuid), "%s", uuid);
	if (ioctl(control, DM_DEV_CREATE, d)) goto out;
	created = 1;
	dm_init(d, name);
	d->target_count = root->extent_count;
	d->flags = readonly ? DM_READONLY_FLAG : 0;
	cursor = (char *)d + d->data_start;
	for (i = 0; i < root->extent_count; i++) {
		struct dm_target_spec *t = (struct dm_target_spec *)cursor;
		size_t n;
		t->sector_start = root->extents[i].start_sector;
		t->length = root->extents[i].sectors;
		strcpy(t->target_type, "linear");
		n = snprintf(cursor + sizeof(*t), 96, "%u:%u %" PRIu64,
			     major(disk), minor(disk), root->extents[i].physical_sector);
		t->next = (sizeof(*t) + n + 1 + 7) & ~(size_t)7;
		cursor += t->next;
		if (cursor + sizeof(*t) + 96 > (char *)d + DM_BUFFER) goto out;
	}
	d->data_size = cursor - (char *)d;
	if (ioctl(control, DM_TABLE_LOAD, d)) goto out;
	dm_init(d, name);
	if (ioctl(control, DM_DEV_SUSPEND, d) ||
	    dm_check(control, d, name, uuid, root, disk, readonly, &device)) goto out;
ready:
	snprintf(path, 64, "/dev/dm-%u", minor(device));
	if (stat(path, &st) && errno == ENOENT) {
		if (mknod(path, S_IFBLK | 0600, device) && errno != EEXIST) goto out;
	}
	if (lstat(path, &st) || !S_ISBLK(st.st_mode) || st.st_rdev != device) goto out;
	rc = 0;
out:
	if (rc && created) {
		dm_init(d, name);
		ioctl(control, DM_DEV_REMOVE, d);
	}
	if (control >= 0) close(control);
	if (lock >= 0) close(lock);
	free(d);
	return rc;
}
#endif

int main(int argc, char **argv)
{
	uint8_t primary[LWVM_HEADER_BYTES], backup[LWVM_HEADER_BYTES], checked[LWVM_HEADER_BYTES], uuid[16];
	struct lwvm_root root;
	struct stat st;
	const char *disk = NULL, *requested = NULL, *mode = "ro", *why;
	char name[128] = "", dm_uuid[129] = "", path[64] = "", formatted[37];
	uint64_t media, chunk;
	int fd, i, json = 0, mapping;
	unsigned int disk_major = 0, disk_minor = 0;
	if (argc < 2 || (strcmp(argv[1], "inspect") && strcmp(argv[1], "map")))
		return error("usage: orchard-lwvm inspect|map --disk PATH --root UUID [--mode ro|rw] [--json]");
	mapping = !strcmp(argv[1], "map");
	for (i = 2; i < argc; i++) {
		if (!strcmp(argv[i], "--json")) json = 1;
		else if (!strcmp(argv[i], "--disk") && ++i < argc && !disk) disk = argv[i];
		else if (!strcmp(argv[i], "--root") && ++i < argc && !requested) requested = argv[i];
		else if (!strcmp(argv[i], "--mode") && ++i < argc) mode = argv[i];
		else return error("invalid or repeated argument");
	}
	if (!disk || !requested || lwvm_uuid_parse(requested, uuid) ||
	    (strcmp(mode, "ro") && strcmp(mode, "rw"))) return error("invalid disk, root UUID or mode");
	fd = open(disk, O_RDONLY | O_CLOEXEC | O_NOFOLLOW);
	if (fd < 0 || fstat(fd, &st)) return error("could not open disk");
	if (S_ISREG(st.st_mode) && !mapping) media = st.st_size;
#ifdef __linux__
	else if (S_ISBLK(st.st_mode)) {
		int ro;
		if (ioctl(fd, BLKGETSIZE64, &media) || ioctl(fd, BLKROGET, &ro) ||
		    (mapping && !strcmp(mode, "rw") && ro)) return error("disk length or writable mode is unavailable");
		disk_major = major(st.st_rdev); disk_minor = minor(st.st_rdev);
	}
#endif
	else return error("inspect requires an image or block device; map requires a block device");
	if (lwvm_geometry(media, &chunk, &why)) return error(why);
	if (read_header(fd, primary, 0) || read_header(fd, backup, chunk - LWVM_HEADER_BYTES))
		return error("could not read both LwVM headers");
	if (read_header(fd, checked, 0) || memcmp(primary, checked, sizeof(primary)))
		return error("LwVM primary changed while reading the header pair");
	if (lwvm_parse(primary, backup, media, uuid, &root, &why)) return error(why);
	if (mapping) {
#ifdef __linux__
		if (map_root(&root, st.st_rdev, !strcmp(mode, "ro"), name, dm_uuid, path))
			return error("could not create or verify the exact DM-linear mapping");
#else
		return error("mapping requires Linux device mapper");
#endif
	}
	close(fd);
	if (mapping && !json) { puts(path); return 0; }
	lwvm_uuid_format(root.uuid, formatted);
	printf("{\"schema\":1,\"kind\":\"orchard-lwvm-root\",\"root_uuid\":\"%s\","
	       "\"partition_index\":%u,\"header_crc32\":\"%08x\",\"media_bytes\":%" PRIu64
	       ",\"chunk_bytes\":%" PRIu64 ",\"bytes\":%" PRIu64 ",\"disk\":{\"major\":",
	       formatted, root.partition_index, root.header_crc32, root.media_bytes, root.chunk_bytes, root.bytes);
	if (S_ISREG(st.st_mode)) printf("null,\"minor\":null}");
	else printf("%u,\"minor\":%u}", disk_major, disk_minor);
	printf(",\"extents\":[");
	for (i = 0; i < (int)root.extent_count; i++) {
		const struct lwvm_extent *e = &root.extents[i];
		printf("%s{\"start_sector\":%" PRIu64 ",\"sectors\":%" PRIu64 ",\"physical_sector\":%" PRIu64 "}",
		       i ? "," : "", e->start_sector, e->sectors, e->physical_sector);
	}
	printf("]");
	if (mapping) printf(",\"mapping\":{\"name\":\"%s\",\"uuid\":\"%s\",\"device\":\"%s\",\"mode\":\"%s\"}", name, dm_uuid, path, mode);
	puts("}");
	return 0;
}
