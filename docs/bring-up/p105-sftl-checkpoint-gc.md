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
generations and blocks that may still be needed for journal replay.

A writer with an atomic checkpoint installer can also compact a partially
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

## Source and verification

The ARMv7 patches in `kernel/patches/armv7/mtd/` build on the merged
[ARMv7 append writer](https://github.com/OrchardLinux/linux/commit/0c6503279c702b44a340c37f072cd9170d6bf4a4).
The [kernel source](https://github.com/shaynelegault/linux/commit/ccd56d3c440c0e84d1581667d43a0437b3e221f8)
contains KUnit fixtures for both the ordinary write path and the planner.

The mixed-block fixture leaves five live 4 KiB records in a closed block.
A normal write triggers relocation and a synthetic two-slot checkpoint
installation. The test reloads that checkpoint through the real reader,
checks all five records and the triggering write, then writes into the
reclaimed block and reloads again. Other fixtures exercise incomplete
footers, corruption, failed programs, partial erase and a checkpoint that
was never published. A simulated controller transaction checks every byte of
the erased lane, including the last byte of the last page.

On this revision, 31 reader/writer cases and nine planner cases passed under
ASan and UBSan. Differential checks matched 24 native metadata vectors,
round-tripped 209 maps and verified 66 relocation sizes. Eleven ARMv7 object
configurations compiled with warnings treated as errors.

The checkpoint metadata and footer evidence comes from IOFlashStorage-730 on
build 13G36 and the [pinned Cascadia NAND research](https://github.com/rzxvx/Project-Cascadia/blob/c20b85ebb951c704d7e3a664c378dde8276e3928/docs/research/p105-nand.md).
The original [planner and rotation notes](https://github.com/shaynelegault/linux/commit/c504eae2a214915dcf5d2c4856c52e7a6ac79d2d)
document the starting evidence.

## Remaining before hardware enablement

The checkpoint installer used by the tests models an atomic switch. A native
P105 installer must update the accounting fields, keep the old table
references during the first VFL save, publish the new references in the
second save, and recover correctly if power fails during either save or
during table copying. The current encoder preserves the opaque native
accounting records.

Physical qualification must establish the exact erase footprint in both
normal and SLC views, exclusive ownership across raw MTD and SFTL operations,
and recovery after a partial erase or block retirement. End-to-end testing
on the measured P105 must verify cold-boot recovery and preservation of
existing iOS data before the device can advertise reclamation.
