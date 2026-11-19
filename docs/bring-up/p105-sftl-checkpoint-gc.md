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

The two-block SwissPPNFTL save and VFL bank rotation operate on different
parts of the medium.

| Operation | Write order | Recovery evidence |
| --- | --- | --- |
| SwissPPNFTL checkpoint | Write the named blank alternate in normal view, verify each page, reload with both generations visible, retire the old context, reload again, then erase the data donor. | The native base and accounting prefix contains 32 records. The map is padded in batches of 64 records. Coverage of the last logical key can occur many pages before the save finishes. |
| Healthy VFL rotation | Erase the four target SLC members, save a checkpoint referencing the source tables, copy all 16 lane tables, then save the final checkpoint with the target references. | Each page has four replicas. The source bank can contain a table update after its last complete checkpoint. The executor checks that tail before changing the target. |

Recovery from an incomplete native batch preserves confirmed journal writes
and exposes the disk read-only when the next safe sequence is uncertain.
If retirement stops between lanes, the new generation must name the old
context site and every surviving record there must predate the selected
generation. Conflicting references, missing live records or unsupported
geometry can still prevent a mount.

The encoder derives per-superblock valid logical-page counts and the global
valid-page statistic from the whole map. These values matched every one of
the 2,086 superblocks in the archived native checkpoint. The writer also
rebuilds the per-block user sequence statistics from closed footers. A live
page copy receives a new physical write sequence and keeps the high 32 bits
of the original user sequence in its tag. The tests supply the remaining
allocation policy for synthetic media. The P105 driver keeps native
checkpoint writes disabled.

## Source and verification

The patches in `kernel/patches/armv7/mtd/` apply to the merged
[ARMv7 append writer](https://github.com/OrchardLinux/linux/commit/0c6503279c702b44a340c37f072cd9170d6bf4a4).
The [kernel source](https://github.com/shaynelegault/linux/commit/64e248acc722bbf6a1171caf07d3858853de6ad3)
contains the synthetic KUnit fixtures.

- A normal write moved five live records, programmed a 24-page native-format
  checkpoint and reused the donor. Cold loads verified the map with both
  contexts visible and again after retirement. The fixtures cover all 25
  save boundaries, readback faults, and power cuts between the old context's
  lane erases.
- The C reader replayed captured device pages through journal sequence
  187831212. All 15,625,016 keys agreed with the archived map. All 95
  incomplete prefixes of a 96-record second generation preserved the map,
  including interruptions inside pages and during map padding.
  Erasing 1–15 of the 16 old lanes preserved the new map read-only. A fully
  retired old context permitted healthy recovery.
- Replaying the recorded healthy VFL rotation reproduced all 72 target
  replica pages, including their out-of-band metadata. All 256 source pages
  remained intact. Synthetic tests also interrupted the operation after
  each of its 76 member erases or page programs.
- The reader rejects a historical 283-FTL-superblock checkpoint for this
  2,086-superblock volume. The native loader and setter treat this field as
  the size of the FTL address space.
- All 59 host sanitizer cases passed, along with 209 range-map round trips,
  66 relocation cases and 24 native metadata vectors. The ARMv7 object
  checks covered 17 configurations.

The checkpoint metadata and footer evidence comes from IOFlashStorage-730 on
build 13G36 and the [pinned Cascadia NAND research](https://github.com/rzxvx/Project-Cascadia/blob/c20b85ebb951c704d7e3a664c378dde8276e3928/docs/research/p105-nand.md).
The original [planner and rotation notes](https://github.com/shaynelegault/linux/commit/c504eae2a214915dcf5d2c4856c52e7a6ac79d2d)
document the starting evidence.

## Remaining before hardware enablement

The remaining software work is to account for allocation of the landing
block, open write streams, wear and the timing of native counter updates.
On all 341 qualifying captured closed donors, native finalization changes
class 3 to class 9 and clears the read count. After a statistics refresh,
the free count increases and the data count decreases. The native writer
can save the previously cached counters before that refresh. Applying the
donor delta directly to the old checkpoint could therefore publish the
wrong counts. Each save also requests two statistics payloads from the
native provider. ID 255 reads 88 bytes of VFL state (13G36 handler
`0x810a048c`), and ID 254 reads 464 bytes of lower-provider state
(`0x8108ac18`). The archive contains one version of this pair. A pair of
checkpoints bracketing a known native relocation, or an independently
validated replay of those provider events, would allow a byte-for-byte
comparison of the resulting state. Scrub and bad-member replacement also
need qualification.

Physical qualification must establish the exact erase footprint in both
normal and SLC views, exclusive ownership across raw MTD and SFTL operations,
and recovery after a partial erase or block retirement. Before a physical
donor can be returned for writing, the driver must also reconcile the
reported erase outcome with native wear and free-block state. End-to-end testing
on the measured P105 must verify cold-boot recovery and preservation of
existing iOS data before the device can advertise reclamation.
