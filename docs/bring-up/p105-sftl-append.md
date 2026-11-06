# P105 native append writes

The kernel `apple-sftl` writer appends to Apple's shared VFL/SwissPPNFTL
medium on the measured 64 GB P105. Native records carry the updated logical
map across reload. The boot reader must follow VFL and SFTL mapping to find
Linux data. LwVM partitions sit above the recovered `applesftlN` disk.
The [reader note](p105-sftl-reader.md) describes geometry and context recovery.

## Source and format

Kernel branch `p105/07-ftl-append` ends at
`c63274165870417f01cc3e1264067cef71d9c0b2`. It contains the verified PPN
program callback (`ab137f3be`), append/replay path (`cb20cc76a`) and request
allocation guard (`c63274165`), following the read-only reader and program
primitive dependencies. Kernel patch export remains pending integration.

The allocator reserves the boot and package tails, every saved SFTL context,
validated VFL bootstrap members and blocks referenced by the recovered map.
It checks every data and metadata byte across a candidate's active lanes
before programming. An occupied candidate is skipped. A failed storage transaction stops the
instance; exhausted blank space returns `ENOSPC` while reads
remain available. The allocator performs no erase or reclamation.

A program writes a complete 16 KiB page with 64 canonical metadata bytes.
One through four logical 4 KiB entries occupy its chunks; short requests
repeat the last entry with a distinct sequence for each copy. The provider
waits for completion and verifies exact readback before map publication.
Filled blocks receive eight summary pages containing native type-1c runs.
The span field at metadata offset eight is the next sequence minus the block
base; it reserves no additional eight sequence slots.

A fresh loader replays open data records or a completed summary. An
interrupted summary can be ignored only after its entire data prefix
validates. Torn or ambiguous metadata stops recovery. Recovery retains its
contiguous journal rule, so an allocation floor beyond the final visible
record prevents writes before mutation. The saved measured map has equal
allocation and journal sequence 187831212.

The frontend preserves untouched sectors during partial 4 KiB updates and
groups up to four logical pages per physical program. Flush and FUA complete
after synchronous readback. Request processing uses a NOIO allocation scope,
which prevents map or provider allocations from recursing into writeback
while the request lock is held.

## Explicit operating policy

Both `MTD_APPLE_H2FMI_WRITE` and `MTD_APPLE_SFTL_WRITE` default off. Logical
writes also require `write_enable=1` when loading `apple-sftl`, or
`apple_sftl.write_enable=1` for a built-in driver. The parameter defaults to
false and is read-only after load. A supported replay state and a program
callback are required. The installer must fence its intended logical range;
this policy controls the logical disk independently of raw MTD permissions.

`max_page_reads` defaults to 32768 explicit canonical read callbacks across
recovery, candidate scans and runtime reads. Program-internal blank checks
and readback remain inside the program callback and outside that counter.
`max_io_time_ms` defaults to 600000 milliseconds from probe. Programs check
the deadline before and after the synchronous callback. Both limit parameters
are read-only and accept 1 through 4294967295. Exhaustion or a provider error
permanently stops the instance. These defaults bound qualification; an
installed root needs an explicit operating policy and must stay within it.

## Evidence and qualification

The exact source passed these software checks:

- Current ARMv7 core, block, fixture and H2FMI objects compiled in read-only
  and write-enabled variants with `W=1` and warnings as errors. The existing
  `vmstat.h` enum-arithmetic warning remained a warning. Built-in parameter
  prefixes were verified from the objects.
- All 26 tests passed in a Linux KUnit kernel using pinned pristine v6.12,
  exact core/block/test sources and a validation-only Kconfig dependency
  symbol for the mock program provider. This run excludes H2FMI hardware.
  Cases cover all 16 lanes, 8160 separate footer entries and fresh replay.
- Host ASan/UBSan checks passed 26 core cases and 13 read-only cases with
  allocation, mutex and tree-API shims. Extracted request helpers passed 25
  cases plus 2048 varied requests. Queue/policy and provider-wrapper checks
  cover deadline failures and balanced allocation scope.
- The actual C core reconstructed saved private pages and matched 28830 map
  boundary checks against the Python reference, plus native LwVM and System
  prefixes. This issued zero device operations. Public fixtures generate
  synthetic bytes.

The exact kernel writer has no physical-device write qualification. The
earlier unplugged Omarchy desktop used the local Python backend. A fresh
approved boot must establish recovery access and bounded reads before any
approved write work. Native iOS recovery after writes, reboot persistence
and interruption handling need physical evidence. P105 `storage_write`
remains `missing`, with installation gated by the current feature states.
