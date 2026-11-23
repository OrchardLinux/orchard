// SPDX-License-Identifier: GPL-2.0-only
/* Recover the P105 map through a provider with a read callback only. */
#include <linux/debugfs.h>
#include <linux/completion.h>
#include <linux/delay.h>
#include <linux/module.h>
#include <linux/mtd/apple-ppn.h>
#include <linux/mtd/apple-ppn-access.h>
#include <linux/of.h>
#include <linux/platform_device.h>
#include <linux/seq_file.h>
#include <linux/sizes.h>
#include <linux/slab.h>
#include <linux/unaligned.h>
#include <linux/workqueue.h>
#include "apple-sftl.h"
#include "apple-sftl-account.h"
#include "apple-sftl-reclaim.h"
#include "apple-sftl-vfl.h"

static unsigned int block = UINT_MAX;
module_param(block, uint, 0444);
MODULE_PARM_DESC(block, "Physical block index whose live logical mappings to report");
static bool survey;
module_param(survey, bool, 0444);
MODULE_PARM_DESC(survey, "Sample page zero in both views on lane zero of unmapped dirty-free neighborhoods");

static u64 checkpoint, journal;
static u32 run_count, opaque_count, owned_count;
static u32 *owners;
static struct dentry *report;
static struct {
	u16 saved, mapped, wear;
	u8 role, flags;
} usage[APPLE_SFTL_SUBBLOCKS];
static struct apple_sftl_allocation saved_plan;
static u8 saved_vfl[88], saved_lower[464];
static struct {
	u8 status[2];
	bool payload[2], nonblank[2], sampled;
} samples[APPLE_SFTL_SUBBLOCKS];

static bool dirty_free_neighborhood(u32 b, const struct apple_sftl_vfl_source *vfl)
{
	u32 i;

	for (i = b - 1; i <= b + 1; i++)
		if (usage[i].role != 9 || usage[i].flags || usage[i].saved || usage[i].mapped)
			return false;
	for (i = 0; i < ARRAY_SIZE(vfl->members); i++)
		if (vfl->members[i].block >= b - 1 && vfl->members[i].block <= b + 1)
			return false;
	return true;
}

static int sample_neighborhoods(struct apple_ppn *reader, struct apple_sftl *cold)
{
	struct apple_sftl_vfl_source vfl;
	u8 *data, oob[APPLE_PPN_OOB_SIZE];
	u32 b, view;
	int ret = apple_sftl_vfl_source(cold, &vfl);

	if (ret)
		return ret;
	data = kmalloc(APPLE_PPN_PAGE_SIZE, GFP_KERNEL);
	if (!data)
		return -ENOMEM;
	for (b = 33; b < 2054; b++) {
		if (!dirty_free_neighborhood(b, &vfl))
			continue;
		for (view = 0; view < 2; view++) {
			u8 status = 0;

			ret = reader->ops->read_page(reader->priv, 0, 0, b, 0, view,
						     data, oob, &status);
			samples[b].status[view] = status;
			if ((status == 0x45 && !ret) ||
			    (view && status == 0x61 && ret == -EIO)) {
				ret = 0;
				continue;
			}
			if (ret)
				goto out;
			if (status != 0x40 && status != 0x42 && status != 0x49) {
				ret = -EBADMSG;
				goto out;
			}
			samples[b].payload[view] = true;
			samples[b].nonblank[view] = memchr_inv(data, 0xff, APPLE_PPN_PAGE_SIZE) ||
						    memchr_inv(oob, 0xff, sizeof(oob));
			if (status == 0x49 && samples[b].nonblank[view]) {
				ret = -EBADMSG;
				goto out;
			}
		}
		samples[b].sampled = true;
		cond_resched();
	}
out:
	kfree(data);
	return ret;
}

static int read_checkpoint_stats(const u8 *page)
{
	u32 i, pos = 4;

	if (get_unaligned_le32(page) != 45)
		return -EBADMSG;
	for (i = 0; i < 45; i++) {
		u32 words, id;

		if (pos > APPLE_SFTL_CHUNK - 8)
			return -EBADMSG;
		id = get_unaligned_le32(page + pos);
		words = get_unaligned_le32(page + pos + 4);
		pos += 8;
		if (words > (APPLE_SFTL_CHUNK - pos) / 4 ||
		    (i < 43 && (id != i + 1 || words != 2)) ||
		    (i == 43 && (id != 255 || words * 4 != sizeof(saved_vfl))) ||
		    (i == 44 && (id != 254 || words * 4 != sizeof(saved_lower))))
			return -EBADMSG;
		if (i == 43)
			memcpy(saved_vfl, page + pos, sizeof(saved_vfl));
		else if (i == 44)
			memcpy(saved_lower, page + pos, sizeof(saved_lower));
		pos += words * 4;
	}
	return 0;
}

struct claim_check {
	struct work_struct work;
	struct completion entered, finished;
	struct apple_ppn_access *access;
	int result;
};

static void attempt_claim(struct work_struct *work)
{
	struct claim_check *check = container_of(work, struct claim_check, work);

	complete(&check->entered);
	check->result = apple_ppn_claim_sftl(check->access);
	complete(&check->finished);
}

/* Hold the actual raw program semaphore without submitting I/O. The child
 * remains bound under its device lock, so the second claim must wait and
 * then report EBUSY. Both operations use the shipping ownership code.
 */
static int check_drain(struct apple_ppn_access *access)
{
	struct claim_check check = { .access = access };
	unsigned long until;
	bool queued;

	INIT_WORK(&check.work, attempt_claim);
	init_completion(&check.entered);
	init_completion(&check.finished);
	down_read(&access->lock);
	if (rwsem_is_contended(&access->lock)) {
		up_read(&access->lock);
		return -EAGAIN;
	}
	queue_work(system_unbound_wq, &check.work);
	queued = wait_for_completion_timeout(&check.entered, 2 * HZ) != 0;
	until = jiffies + 2 * HZ;
	while (queued && !rwsem_is_contended(&access->lock) &&
	       !completion_done(&check.finished) && time_before(jiffies, until))
		usleep_range(500, 1000);
	queued = queued && rwsem_is_contended(&access->lock) && !completion_done(&check.finished);
	up_read(&access->lock);
	flush_work(&check.work);
	return queued && check.result == -EBUSY ? 0 : -EIO;
}

static int match_controller(struct device *dev, const void *ignored)
{
	return of_device_is_compatible(dev->of_node, "apple,s5l8940x-fmi");
}

static int match_child(struct device *dev, const void *parent)
{
	return dev->parent == parent &&
	       !strcmp(to_platform_device(dev)->name, "apple-sftl");
}

static int collect(struct apple_sftl_snapshot *snapshot)
{
	u32 first = block * APPLE_SFTL_STRIDE, i;
	int ret;

	/* This production parser checks the complete native accounting prefix. */
	ret = apple_sftl_account_plan_allocation(snapshot, &saved_plan);
	if (ret)
		return ret;
	ret = read_checkpoint_stats(snapshot->opaque[4].data);
	if (ret)
		return ret;
	for (i = 0; i < APPLE_SFTL_SUBBLOCKS; i++) {
		const u8 *entry = snapshot->opaque[8 + i / 512].data + 8 * (i % 512);

		usage[i].role = entry[0];
		usage[i].flags = entry[1];
		usage[i].saved = get_unaligned_le16(entry + 2);
		usage[i].wear = get_unaligned_le16(entry + 6);
	}

	memset(owners, 0xff, APPLE_SFTL_STRIDE * sizeof(*owners));
	for (i = 0; i < snapshot->nr_runs; i++) {
		const struct apple_sftl_extent *run = &snapshot->runs[i];
		u64 limit = (u64)run->provider + run->length;
		u32 start, end, pos;

		if (run->provider == APPLE_SFTL_NO_ADDRESS)
			continue;
		if (limit > APPLE_SFTL_SUBBLOCKS * APPLE_SFTL_STRIDE ||
		    (u64)run->logical + run->length > APPLE_SFTL_LOGICAL_PAGES)
			return -EBADMSG;
		for (pos = run->provider; pos < limit; pos = end) {
			u32 b = pos / APPLE_SFTL_STRIDE;

			end = min((b + 1) * APPLE_SFTL_STRIDE, (u32)limit);
			if (usage[b].mapped + (end - pos) > APPLE_SFTL_STRIDE)
				return -EBADMSG;
			usage[b].mapped += end - pos;
		}
		start = max(first, run->provider);
		end = min(first + APPLE_SFTL_STRIDE, (u32)limit);
		for (pos = start; pos < end; pos++) {
			if (owners[pos - first] != APPLE_SFTL_NO_ADDRESS)
				return -EBADMSG;
			owners[pos - first] = run->logical + (pos - run->provider);
			owned_count++;
		}
	}
	checkpoint = snapshot->checkpoint_sequence;
	journal = snapshot->journal_sequence;
	run_count = snapshot->nr_runs;
	opaque_count = snapshot->nr_opaque;
	return 0;
}

static int recover(struct device *parent, struct device *child)
{
	struct apple_ppn *live = dev_get_platdata(child);
	struct apple_ppn_ops ops = {};
	struct apple_ppn reader;
	struct apple_sftl_snapshot *snapshot = NULL;
	struct apple_sftl *cold = NULL;
	int ret;

	if (!device_is_registered(child) || !device_is_bound(parent) ||
	    strcmp(dev_driver_string(parent), "apple-h2fmi") ||
	    !device_is_bound(child) || strcmp(dev_driver_string(child), "apple-sftl") ||
	    !live || live->dev != parent || !live->access || !live->ops ||
	    !live->ops->read_page || live->ops->erase_block || live->buses != 2 ||
	    live->caus != 8 || live->blocks != 2088 || live->pages != 128)
		return -ENODEV;
	ret = apple_ppn_begin_raw_write(live->access);
	if (!ret) {
		apple_ppn_end_raw_write(live->access);
		return -EPERM;
	}
	if (ret != -EBUSY)
		return ret;
	ret = check_drain(live->access);
	if (ret)
		return ret;
	ops.read_page = live->ops->read_page;
	reader = *live;
	reader.ops = &ops;
	reader.access = NULL;
	ret = apple_sftl_load(&reader, &cold);
	if (!ret)
		ret = apple_sftl_snapshot(cold, 8 * SZ_1M, &snapshot);
	if (!ret)
		ret = collect(snapshot);
	if (!ret && survey)
		ret = sample_neighborhoods(&reader, cold);
	apple_sftl_snapshot_free(snapshot);
	apple_sftl_free(cold);
	return ret;
}

static int audit_show(struct seq_file *seq, void *ignored)
{
	u32 i;

	seq_printf(seq, "block %u\ncheckpoint %llu\njournal %llu\nruns %u\ncontext_records %u\nlive_chunks %u\n",
		   block, checkpoint, journal, run_count, opaque_count, owned_count);
	seq_puts(seq, "bound_claim_waited_for_raw_semaphore 1\nphysical_erase_callback 0\n");
	seq_puts(seq, "checkpoint_stats_255 ");
	for (i = 0; i < sizeof(saved_vfl); i++)
		seq_printf(seq, "%02x", saved_vfl[i]);
	seq_puts(seq, "\ncheckpoint_stats_254 ");
	for (i = 0; i < sizeof(saved_lower); i++)
		seq_printf(seq, "%02x", saved_lower[i]);
	seq_putc(seq, '\n');
	seq_printf(seq, "saved_plan_landing %u\n", saved_plan.landing);
	for (i = 0; i < saved_plan.nr_erase; i++)
		seq_printf(seq, "saved_plan_erase %u\n", saved_plan.erase[i]);
	for (i = 0; i < APPLE_SFTL_SUBBLOCKS; i++)
		seq_printf(seq, "usage %u %u %u %u %u %u\n", i, usage[i].role,
			   usage[i].flags, usage[i].saved, usage[i].mapped, usage[i].wear);
	for (i = 0; i < APPLE_SFTL_SUBBLOCKS; i++)
		if (samples[i].sampled)
			seq_printf(seq, "sample %u %02x %u %u %02x %u %u\n", i,
				   samples[i].status[0], samples[i].payload[0], samples[i].nonblank[0],
				   samples[i].status[1], samples[i].payload[1], samples[i].nonblank[1]);
	for (i = 0; i < APPLE_SFTL_STRIDE; i++)
		if (owners[i] != APPLE_SFTL_NO_ADDRESS)
			seq_printf(seq, "mapping %u %u\n", i, owners[i]);
	return 0;
}
DEFINE_SHOW_ATTRIBUTE(audit);

static int __init audit_init(void)
{
	struct device *parent, *child;
	int ret = -ENODEV;

	if (!of_machine_is_compatible("apple,p105ap") || !block ||
	    block >= APPLE_SFTL_SUBBLOCKS)
		return -EINVAL;
	parent = bus_find_device(&platform_bus_type, NULL, NULL, match_controller);
	if (!parent)
		return -ENODEV;
	child = bus_find_device(&platform_bus_type, NULL, parent, match_child);
	if (!child)
		goto put_parent;
	owners = kvmalloc_array(APPLE_SFTL_STRIDE, sizeof(*owners), GFP_KERNEL);
	if (!owners) {
		ret = -ENOMEM;
		goto put_child;
	}
	device_lock(parent);
	device_lock(child);
	ret = recover(parent, child);
	device_unlock(child);
	device_unlock(parent);
	if (!ret) {
		report = debugfs_create_file("p105_map_audit", 0400, NULL, NULL,
					     &audit_fops);
		ret = PTR_ERR_OR_ZERO(report);
	}
	if (ret)
		kvfree(owners);
put_child:
	put_device(child);
put_parent:
	put_device(parent);
	return ret;
}

static void __exit audit_exit(void)
{
	debugfs_remove(report);
	kvfree(owners);
}

module_init(audit_init);
module_exit(audit_exit);
MODULE_DESCRIPTION("Read-only audit of current logical ownership of a P105 flash block");
MODULE_LICENSE("GPL");
