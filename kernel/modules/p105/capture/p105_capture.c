// SPDX-License-Identifier: GPL-2.0-only
/* Read both P105 flash views through the bound driver's canonical page reader. */
#include <linux/capability.h>
#include <linux/fs.h>
#include <linux/miscdevice.h>
#include <linux/module.h>
#include <linux/mtd/apple-ppn.h>
#include <linux/mutex.h>
#include <linux/of.h>
#include <linux/platform_device.h>
#include <linux/slab.h>

#define BLOCKS 2088
#define NORMAL_PAGES 128
#define SLC_PAGES 64
#define LANES 16

static unsigned int bus = UINT_MAX, cau = UINT_MAX, block = UINT_MAX;
module_param(bus, uint, 0444);
module_param(cau, uint, 0444);
module_param(block, uint, 0444);

static struct device *parent, *child;

struct ppns_record {
	u8 magic[4];
	u8 version, bus, cau, status;
	__le32 row, length;
	u8 raw[APPLE_PPN_PAGE_SIZE + APPLE_PPN_OOB_SIZE];
};

struct capture {
	struct mutex lock;
	u32 next;
	loff_t offset;
	int error;
	struct ppns_record record;
	u8 data[APPLE_PPN_PAGE_SIZE], oob[APPLE_PPN_OOB_SIZE];
};

static int match_controller(struct device *dev, const void *unused)
{
	return of_device_is_compatible(dev->of_node, "apple,s5l8940x-fmi");
}

static int match_sftl(struct device *dev, const void *controller)
{
	return !strcmp(to_platform_device(dev)->name, "apple-sftl") &&
	       dev->parent == controller;
}

static int read_record(struct capture *s)
{
	struct ppns_record *r = &s->record;
	const struct apple_ppn *ppn;
	u32 index = s->next / (NORMAL_PAGES + SLC_PAGES);
	u32 page = s->next % (NORMAL_PAGES + SLC_PAGES);
	u32 physical = block;
	bool slc = page >= NORMAL_PAGES;
	int ret, i;

	if (index < LANES) {
		r->bus = index / 8;
		r->cau = index % 8;
	} else {
		r->bus = bus;
		r->cau = cau;
		physical = index == LANES && block ? block - 1 : block + 1;
	}
	if (slc)
		page -= NORMAL_PAGES;

	/* The parent owns the provider and destroys the child before freeing
	 * it. Lock both in driver-core order so unbind cannot free its data.
	 */
	device_lock(parent);
	device_lock(child);
	ppn = dev_get_platdata(child);
	if (!device_is_registered(child) || !device_is_bound(parent) ||
	    strcmp(dev_driver_string(parent), "apple-h2fmi") ||
	    !device_is_bound(child) || !ppn || ppn->dev != parent ||
	    ppn->buses != 2 || ppn->caus != 8 || ppn->blocks != BLOCKS ||
	    ppn->pages != NORMAL_PAGES || !ppn->ops || !ppn->ops->read_page)
		ret = -ENODEV;
	else
		ret = ppn->ops->read_page(ppn->priv, r->bus, r->cau, physical, page,
					  slc, s->data, s->oob, &r->status);
	device_unlock(child);
	device_unlock(parent);
	if (ret)
		return ret;
	/* These statuses carry a data phase. A retired row does not. */
	if (r->status != 0x40 && r->status != 0x42 && r->status != 0x49)
		return -EIO;
	memcpy(r->magic, "PPNS", 4);
	r->version = 1;
	r->row = cpu_to_le32((slc ? BIT(22) : 0) | (r->cau << 19) | (physical << 7) | page);
	r->length = cpu_to_le32(sizeof(r->raw));
	for (i = 0; i < 4; i++) {
		u8 *dst = r->raw + 4112 * i;
		const u8 *data = s->data + 4096 * i;

		memcpy(dst, data, 1024);
		memcpy(dst + 1024, s->oob + 16 * i, 16);
		memcpy(dst + 1040, data + 1024, 3072);
	}
	return 0;
}

static int capture_open(struct inode *inode, struct file *file)
{
	struct capture *s;

	if (!capable(CAP_SYS_RAWIO))
		return -EPERM;
	if (file->f_mode & FMODE_WRITE)
		return -EROFS;
	s = kvzalloc(sizeof(*s), GFP_KERNEL);
	if (!s)
		return -ENOMEM;
	mutex_init(&s->lock);
	s->offset = sizeof(s->record);
	file->private_data = s;
	return stream_open(inode, file);
}

static ssize_t capture_read(struct file *file, char __user *out, size_t count, loff_t *pos)
{
	struct capture *s = file->private_data;
	u32 groups = LANES + (block > 0) + (block + 1 < BLOCKS);
	ssize_t ret;

	if (!count)
		return 0;
	if (mutex_lock_interruptible(&s->lock))
		return -ERESTARTSYS;
	ret = s->error;
	if (ret)
		goto out;
	if (s->offset == sizeof(s->record)) {
		if (s->next == groups * (NORMAL_PAGES + SLC_PAGES))
			goto out;
		ret = read_record(s);
		if (ret) {
			s->error = ret;
			goto out;
		}
		s->next++;
		s->offset = 0;
	}
	ret = simple_read_from_buffer(out, count, &s->offset, &s->record, sizeof(s->record));
out:
	mutex_unlock(&s->lock);
	return ret;
}

static int capture_release(struct inode *inode, struct file *file)
{
	kvfree(file->private_data);
	return 0;
}

static const struct file_operations capture_ops = {
	.owner = THIS_MODULE,
	.open = capture_open,
	.read = capture_read,
	.release = capture_release,
};

static struct miscdevice capture_device = {
	.minor = MISC_DYNAMIC_MINOR,
	.name = "p105-capture",
	.fops = &capture_ops,
	.mode = 0400,
};

static int __init capture_init(void)
{
	int ret;

	static_assert(sizeof(struct ppns_record) == 16 + 16448);
	if (!of_machine_is_compatible("apple,p105ap"))
		return -ENODEV;
	if (bus >= 2 || cau >= 8 || block >= BLOCKS)
		return -EINVAL;
	parent = bus_find_device(&platform_bus_type, NULL, NULL, match_controller);
	if (!parent)
		return -ENODEV;
	child = bus_find_device(&platform_bus_type, NULL, parent, match_sftl);
	if (!child) {
		put_device(parent);
		return -ENODEV;
	}
	ret = misc_register(&capture_device);
	if (ret) {
		put_device(child);
		put_device(parent);
	}
	return ret;
}

static void __exit capture_exit(void)
{
	misc_deregister(&capture_device);
	put_device(child);
	put_device(parent);
}

module_init(capture_init);
module_exit(capture_exit);
MODULE_DESCRIPTION("Read-only P105 normal/SLC PPNS capture");
MODULE_LICENSE("GPL");
