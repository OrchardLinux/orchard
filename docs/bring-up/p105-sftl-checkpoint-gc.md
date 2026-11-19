# P105 SwissPPNFTL checkpoint encoder and reclamation planner

The 64 GB P105 shares one flash translation layer across the native iOS and
Linux partitions. The append writer returns `ENOSPC` after it exhausts
verified blank blocks. Orchard's installer warns about that limit and allows
installation. See the [append writer](p105-sftl-append.md) and
[P105 device profile](../../devices/p105/device.toml).

## Kernel source

The port uses the merged append writer on
[OrchardLinux/linux armv7 at 0c650327](https://github.com/OrchardLinux/linux/commit/0c6503279c702b44a340c37f072cd9170d6bf4a4).
The [ported kernel source](https://github.com/shaynelegault/linux/commit/e823a434a766b09c5e5b2ad4586b7a27085b1828)
contains the planner and its fixtures. The kernel patches live in `kernel/patches/armv7/mtd/`, selected by
`kernel/series/apple-armv7`. The original planner and native rotation
notes are in [the source draft at c504eae2](https://github.com/shaynelegault/linux/commit/c504eae2a214915dcf5d2c4856c52e7a6ac79d2d).

The snapshot covers all 15,625,016 logical keys, including the sixteen
internal keys beyond the exported disk. The encoder preserves the opaque
accounting records, updates the context-block list and serializes range
fragments with 48-bit sequence numbers. The planner budgets destination
space for live data and the block footer. It checks copy and checkpoint
readback results supplied by its caller, then compares the whole map from
a fresh recovery against the planned map. These helpers operate offline.

## Open work

- The runtime writer needs authoritative VFL and context ownership data to
  select blocks and lanes, verify relocated data, and return an erased block
  to the allocator.
- Installing a checkpoint requires native updates to accounting fields.
  The reader needs to select the previous complete checkpoint and replay
  interrupted saves when the newest checkpoint is incomplete.
- The native VFL rotation switches banks in two persisted steps. An initial
  checkpoint retains the old table references while the tables are copied.
  A final checkpoint publishes the new references. Recovery after a torn
  copy or a block retirement still needs qualification.
- A controller erase and reuse cycle still needs a test on the measured
  hardware, including the physical erase extent, the relationship between
  normal and SLC views, and recovery after partial completion. The P105
  profile records both storage reads and writes as `untested`.

The native format evidence comes from IOFlashStorage-730 on build 13G36
and the [pinned Cascadia NAND research](https://github.com/rzxvx/Project-Cascadia/blob/c20b85ebb951c704d7e3a664c378dde8276e3928/docs/research/p105-nand.md).
On the ported source, the host sanitizer run passed 22 reader/writer cases
and eight planner cases. The differential tests matched 24 native metadata
vectors and round-tripped 209 encoded maps. Five ARMv7 objects compiled
with warnings treated as errors, including both KUnit fixtures. These checks
used synthetic pages and covered encoding, receipt validation and cold map
recovery. Physical erase and reboot recovery remain untested.
