# P105 SwissPPNFTL checkpoint and reclamation

The measured 64 GB P105 shares one flash translation layer across iOS and
Linux. Its H2FMI profile still appends to verified blank blocks and returns
`ENOSPC` when they run out. The installer continues to warn about this
limit. See the [append writer](p105-sftl-append.md) and
[P105 device profile](../../devices/p105/device.toml).

## Write path

The allocator checks the map for all 15,625,016 logical keys, including the
16 internal keys beyond the exported disk. On a provider that supports
verified erase, it can recycle a fully obsolete block. The block must have a
valid native footer, predate the saved checkpoint, and be absent from both
the checkpoint map and the current map. The allocator verifies each record
against its footer and compares the active map with a fresh load. It excludes
bootstrap and context blocks, unknown lane flags, ambiguous checkpoint
generations and blocks that may still be needed for journal replay. To
erase an obsolete native donor directly, the saved block role must already
be dirty-free with zero live pages.

For the native layout, a new append block must also have the clean-free
class and zero saved mappings. Two of the five such blocks in the captured
checkpoint were consumed by later writes. The other three had erased heads
and no live mappings. Allocation still verifies the entire block. The
native role table also protects a third context block that the two-entry
checkpoint header does not name.

A writer with a qualified checkpoint accounting policy can also compact a partially
live block. The allocator keeps a verified blank landing block while space
is available. Under space pressure it copies live records into that block,
checks the data and out-of-band metadata after each program, and allows room
for the pending write and block footer. It installs the new checkpoint,
compares the checkpoint readback, then reloads the medium and verifies the
entire mapping. Only then can it erase the old block. The waiting write
uses the free tail of the landing block. A subsequent write can claim
the erased block. Without a checkpoint installer, foreground writes retain
the available blank capacity.

The H2FMI erase operation checks both controller status replies and reads
all data and out-of-band bytes from all 128 pages of each affected lane.
The allocator also verifies the whole logical block before reuse. An
indeterminate erase or failed verification stops the writer instance.

## Checkpoint publication and recovery

SwissPPNFTL saves allocate a fresh normal-view context group. The separate
VFL rotation uses two replicated SLC banks.

| Operation | Write order | Recovery evidence |
| --- | --- | --- |
| SwissPPNFTL checkpoint | Reserve fresh sites, copy live data, write the base page, sample the two providers, then write the statistics and remaining context pages. Reload with the old generation present, retire its occupied site, release unused new capacity, reload and verify the donor erase. | The base names the new generation's capacity. Old occupied sites are discovered independently. The native accounting prefix has 32 records and map batches have 64. |
| Healthy VFL rotation | Erase the four target SLC members, save a checkpoint referencing the source tables, copy all 16 lane tables, then save the final checkpoint with the target references. | Each page has four replicas. The source bank can contain a table update after its last complete checkpoint. The executor checks that tail before changing the target. |

Recovery from an incomplete native batch preserves confirmed journal writes
and exposes the disk read-only when the next safe sequence is uncertain.
For a saved context-role site outside the selected group, recovery checks
the physical pages, lane order, record tags, sequence and map ownership.
Incomplete evidence can yield a read-only mount. Conflicting metadata or
references into a reserved site can prevent recovery.

The writer derives the valid-page count of each of the 2,086 superblocks
from the whole map. A native save preserves the cached global counts until
the observed firmware statistics refresh. The writer also
rebuilds the per-block user sequence statistics from closed footers. A live
page copy receives a new physical write sequence and keeps the high 32 bits
of the original user sequence in its tag. The tests supply the remaining
allocation policy for synthetic media. The P105 driver keeps native
checkpoint writes disabled.

## Native allocation, wear and counters

The healthy 13G36 allocator compares wear across clean and dirty free
blocks. When the clean pool is below six, an allocation can also erase
other low-wear dirty blocks to replenish it. After the last valid page
moves, the closed donor changes from class 3 to dirty-free class 9 and its
read count clears. A successful later erase changes it to clean-free
class 1 and increments its 16-bit wear value. Native firmware also
increments wear when it releases an unused new context reservation,
without issuing an erase for that site. The driver declines an ambiguous
or overflowing transition before reuse.

The native base program completes before the two getters run. ID 255
contains 88 bytes of VFL statistics. ID 254 contains 464 bytes from
lower-provider requests and completions. Both are sampled before the
statistics page is programmed. The number of physical rows, command
batching and completion flags all matter. A post-save statistics refresh
updates cached global counts separately.

## Source and verification

The patches in `kernel/patches/armv7/mtd/` apply to the merged
[ARMv7 append writer](https://github.com/OrchardLinux/linux/commit/0c6503279c702b44a340c37f072cd9170d6bf4a4).
The [kernel source](https://github.com/shaynelegault/linux/commit/e3684d2ab4c71fbef0907f3e584e07af09b59744)
contains the synthetic KUnit fixtures.

- A normal write moved five live records, programmed a 24-page native-format
  checkpoint and reused the donor. Cold loads verified the map with both
  contexts visible and again after retirement. The fixtures cover all 25
  save boundaries, readback faults, and power cuts between the old context's
  lane erases.
- The C reader replayed captured device pages through journal sequence
  187831212. All 15,625,016 keys agreed with the archived map. Of the 2,048
  pages at unnamed context-role block 601, only two are in the archive.
  The reader exposes that unmodified capture read-only.
- A separate RAM overlay supplied erased values for the 2,046 uncaptured
  pages on each of blocks 594, 601 and 636. With that assumption, all 95
  incomplete prefixes of a new 96-record save and all 15 partial
  retirements preserved the full map. The overlay started with all
  captured pages intact.
- Replaying the recorded healthy VFL rotation reproduced all 72 target
  replica pages, including their out-of-band metadata. All 256 source pages
  remained intact. Synthetic tests also interrupted the operation after
  each of its 76 member erases or page programs.
- The reader rejects a historical 283-FTL-superblock checkpoint for this
  2,086-superblock volume. The native loader and setter treat this field as
  the size of the FTL address space.
- Independent execution of the storage firmware matched the C allocation
  planner on 600 generated block tables. On 341 captured closed donors the
  two implementations agreed on donor state, wear and the separate cached
  statistics refresh.
- The firmware and C implementation agreed on all 552 provider bytes at
  426 comparison points, covering the archived initial counters, a
  synthetic initial state and integer rollover.
- In a separate end-to-end replay, the original 13G36 foreground GC chose
  donor 3 from two eligible blocks, read its footer, and moved keys
  101–104 to block 32. The verifier compared all 16 KiB of data and 64
  out-of-band bytes at the lower-provider write boundary. Native firmware
  updated the map, finalized the donor, allocated context sites 33 and 34,
  and serialized the statistics after writing the base page. It erased old
  context 17, released unused site 34, and subsequently erased the donor.
  The Linux event accounting matched the native getters byte-for-byte.
  The initial state and successful NAND responses were supplied in RAM.
- The replay used the original ARM storage code from build 13G36, SHA-256
  `fcc7fa01fa80c03ca4de026ce01e8fb2b131abd60fffa1f498e5b901d34e78be`.
  It entered foreground GC at `0x81090320`; the checkpoint getters at
  `0x810a048c` and `0x8108ac18` supplied the independent reference bytes.
- All 72 host sanitizer cases, 209 map round trips and 66 relocation
  differentials passed. Twenty ARMv7 object configurations compiled.

The checkpoint metadata and footer evidence comes from IOFlashStorage-730 on
build 13G36 and the [pinned Cascadia NAND research](https://github.com/rzxvx/Project-Cascadia/blob/c20b85ebb951c704d7e3a664c378dde8276e3928/docs/research/p105-nand.md).
The original [planner and rotation notes](https://github.com/shaynelegault/linux/commit/c504eae2a214915dcf5d2c4856c52e7a6ac79d2d)
document the starting evidence.

## Remaining before hardware enablement

The physical provider needs a trustworthy starting state and a continuous
record of native-equivalent reads, programs, erases, synchronizations and
their completion flags. One saved counter sample plus the logical journal
cannot reconstruct that history. Replaying two plausible clean-pool
histories selected the same observed landing blocks, 654 and 661, and
issued different maintenance erases. Losing that history invalidates the
counter model. Scrub and bad-member replacement also need qualification.

Physical qualification must establish the exact erase footprint in both
normal and SLC views, exclusive ownership across raw MTD and SFTL operations,
and recovery after a partial erase or block retirement. Before a physical
donor can be returned for writing, the driver must also reconcile the
reported erase outcome with native wear and free-block state. End-to-end testing
on the measured P105 must verify cold-boot recovery and preservation of
existing iOS data before the device can advertise reclamation.
