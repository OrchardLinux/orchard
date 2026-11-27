# P105 NAND: two drivers, one proposal

State: ports 1, 5, 3, 4 and 6 written on branch `p105-nand-fusion` as
`kernel/patches/armv7/mtd` 0017-0023 on top of #93 (now at 59bdee56); port 2
compared offline only (below). The measured 64 GB P105 has since qualified
#93's write, checkpoint, erase and reuse path. The Cascadia ports remain
without P105 device runs.

The iPad mini 1 (P105) stores iOS's disk on PPN NAND behind two H2FMI buses,
through Apple's VFL and SwissPPNFTL, with LwVM partitions on top. Two
independent Linux implementations exist for it:

- **Orchard's stack**, by Shayne Legault: `kernel/patches/armv7/mtd` 0001-0005
  (H2FMI geometry, verified page programming, the provider interface, the
  VFL/SwissPPNFTL reader and append writes) and `armv7/block` 0001 (LwVM
  partitions), merged on orchard main. His open draft
  [#93](https://github.com/OrchardLinux/orchard/pull/93)
  (`shaynelegault/hardware` `p105/08-ftl-reclamation`, bd8916c876) adds
  0006-0016: checkpoint encoding and GC planning, live-page copy, VFL bank
  rotation, native context pages, recovery of interrupted saves, provider
  event accounting, native allocation order, H2FMI erase with verification
  and raw-program exclusion. Kernel branch: `shaynelegault/linux`
  `p105/08-ftl-reclamation-armv7` (46a828ddef).
- **Cascadia's driver**, by rzxvx: `drivers/mtd/nand/apple` at Cascadia
  ad28ba4 (`apple_ppn`, a PIO controller driver, and `apple_ftl` with
  `sftl_*`, the FTL reader and writer), written from Cascadia's behavioural
  spec (`devices/a5/docs/research/p105-nand-spec.md`); its format findings
  and first write path credit crystal-lized's iPhone 5 work. Orchard imports
  it as `kernel/patches/import/cascadia/0018`, with `APPLE_PPN` off.

## The headline

The two drivers so far cover **different P105s**. Cascadia's runs on the
16 GB package (2 CAUs per CE, 1064 blocks per CAU, 256 pages per block) with
iOS 8.4.1 (12H321) on the media; its `check_params` refuses anything else.
Orchard's SFTL layer runs on the 64 GB package (8 CAUs per bus, 2088 blocks
per CAU, 128 pages per block) and models iOS 9.3.5 (13G36); its H2FMI layer
reads both package profiles, but the FTL registers only for the 64 GB one.
Neither alone serves every P105.

**Proposal: Orchard's stack as the base (H2FMI provider, apple-sftl,
LwVM partitions, then #93), with Cascadia's 16 GB / iOS 8.4.1 support and its
device-proven write-path failure handling ported in as separate commits under
rzxvx's authorship.** Details below.

## Feature comparison

| | Orchard: merged (0001-0005, block 0001) | Orchard: #93 (0006-0016, draft) | Cascadia ad28ba4 |
|---|---|---|---|
| Controller | Cascadia's earlier `mtd-apple-h2fmi`, extended: geometry from both packages' parameters, 4 row cycles, page status before data, bus stops on transport errors | adds block erase with status checks and readback of all 128 pages and their OOB; drains and refuses raw MTD programs while SFTL owns the map | own PIO driver `apple_ppn`: power-state feature `0x180` after every reset, 4-byte rows, burst transmit config, bad-block tables; claims its register windows (`request_mem_region`) |
| Package | 16 GB and 64 GB at the H2FMI level; FTL 64 GB only | same | 16 GB only |
| iOS on the media | 13G36 (iOS 9.3.5) is the model | 13G36 storage code is the reference, byte for byte | 12H321 (iOS 8.4.1) only; the NAND install refuses other builds |
| Read | VFL contexts and lane tables, saved SFTL ranges, journal replay incl. summaries and open stripes with holes; whole disk `applesftlN` | unchanged; incomplete native batches give a read-only disk | VFL context, context superblock, tree, replay; `ftl0` plus one block device per LwVM volume (`ftl0pN`) |
| Partitions | generic `block/partitions/lwvm.c` (contiguous partitions, PARTUUID/PARTNAME) | unchanged | LwVM parsed inside the driver; Cascadia's `nand-volume` tool resizes HFS and writes a new header, checked by iOS's own validator in Unicorn |
| Append | blank VFL blocks, native record metadata and summary pages, page blank-checked before use and read back after; mappings published only after verification; flush/FUA wait for verified writes | unchanged | one frontier superblock, BTOC at close, partial pages and siblings |
| In-place overwrite (rewriting an LBA) | yes, out of place by appending; read-modify-write for partial sectors, up to four 4 KiB pages per program | unchanged | yes, remap to the frontier |
| Reclamation / GC | none: freed space is never reused, `ENOSPC` once blank blocks run out | yes: victim selection by native rules, live-page copy with readback, donor erase after checkpoint readback and a fresh reload; H2FMI offers erase for every package whose discovered geometry validates; qualified on the measured 64 GB P105 | yes: up to `SFTL_MAX_VICTIMS` closed superblocks, fewest valid first; live LBAs moved, commit, then erase; tree compaction when the map fragments |
| Checkpoint / context write-back in iOS's format | none: Apple's map, contexts and VFL bootstrap left in place; recovery replays the appended records | native context and range records, 24-page checkpoint, statistics records sampled in native order; context publication and accounting are qualified on the measured 64 GB P105; VFL SLC-bank erase still lacks a physical callback | context written to a fresh superblock and verified page by page, older contexts and victims erased afterwards; "Linux writes the normal form" of the tree; the spec-gaps doc lists where iOS's own output differs (e.g. a BTOC `user` field) |
| iOS boots after Linux wrote | untested | yes: iOS 9.3.5 booted twice after a 64 MiB Linux write; System and Data passed `fsck_hfs`, and the witness retained its SHA-256 | yes, "checked many times" on 8.4.1 (Cascadia's `p105-nand-other-ios.md`), incl. the full volume cycle below |
| Failure handling | a failed program, uncertain completion or readback mismatch stops all I/O on that bus; disk read-only if the replayed journal does not end at the allocation floor | ambiguous erase or failed verification stops the writer instance; recovery of interrupted saves and partial lane retirement, read-only when the newer mapping is not provable | program/erase failure: the superblock is given up for the session (type 1, nothing valid, VFL untouched) and its live data moved to a new frontier; failed spare erases try the next spare; torn page found at mount: superblock given up (`give_up_torn`); a stopped writer refuses writes and the checkpoint |
| Power-loss recovery | fresh recovery replays appended records, including a data prefix whose summary was cut off | KUnit power cuts at all 25 save boundaries, between the old context's lane erases, after each of 76 rotation steps; 95 incomplete save prefixes and 15 partial retirements on a RAM overlay of captured pages | replay of an unclosed frontier; offline crash-replay tests on an overlay of the `w0c` dump; the 2026-11-16 incident (below) recovered on the device |
| Fault injection | KUnit mock provider | KUnit synthetic media with readback faults, ambiguous erase, reservation failure, sequence exhaustion | `CONFIG_APPLE_PPN_FAULT_INJECT`: program/erase failures injected on the device under ext4 writes (`tools/nand-faults/faults.sh`) |
| Write gating | none in the kernel; the installer decides (`runtime/recovery/storage-fence`) | raw MTD programs refused while SFTL owns the disk | `apple_ppn.allow_write`, `apple_ftl.enable_write`, `write_volume=Linux`; System, Data and effaceable volumes always refuse |
| Tests | KUnit: H2FMI parameter parsing and row encoding, SFTL recovery, journal/trim ordering, replica disagreement, torn summaries, append writer (26 tests in a v6.12 KUnit kernel) | 72 sanitizer cases, 209 map round trips, 66 relocation differentials; firmware differential on 600 block tables, 341 captured donors, 552 provider bytes | offline `nandtest` on the `w0c` dump (spec section 8.1 read checks; writer on an overlay that enforces erase-before-program and page order: close, commit, crash replay, reclaim, tree compaction, remount); device ladder rungs 1-5 |
| Device evidence | whole-disk reads and earlier bounded programming established the 64 GB geometry; this merged base remains append-only | on 2026-11-23, a 64 MiB SFTL write survived Linux recovery and two clean iOS boots; all 16 lanes of an obsolete block erased, native accounting reused it, iOS reclaimed it again, and a cold Linux scan recovered wear 9 with both views blank | one 16 GB iPad mini on 8.4.1: whole-device read with 0 errors; on 2026-11-16 backup, LwVM remove, HFS grow, shrink, create, ext4 fill through the FTL and a daily PC-less boot from the NAND volume, iOS and `fsck_hfs` clean after each step |

The 2026-11-16 incident in Cascadia's record: an old userspace tool reset the
controller through `/dev/mem` while `apple_ppn` was programming; a page tore,
the writer gave the superblock up, four spare erases timed out and the writer
stopped before any commit. Recovery (`tools/nand-recover`) gave the torn
superblock up and erased the spares; iOS and `e2fsck` were clean afterwards.
The fixes were `give_up_torn` at mount, `request_mem_region`, and
`IO_STRICT_DEVMEM` in Cascadia's config. Orchard's #93 0016 closes the
in-kernel half of the same hazard (raw MTD programs refused while SFTL owns
the map); `/dev/mem` access stays open in Orchard's config
(`STRICT_DEVMEM` is off).

## Why Orchard's stack as the base

- It is already the platform's NAND path, in upstream form: one controller
  driver (H2FMI) with a provider interface, the FTL as a separate MTD user,
  LwVM as an ordinary partition parser, no command-line gates. That matches
  the final-stack rules (no gates in shared drivers, storage protection in
  the installer).
- Its controller and reader already handle both packages' geometry from
  their parameters; Cascadia's would need the 64 GB layout added everywhere
  (`NCAU`, `check_params`, bank order, 256 vs 128 pages per block).
- #93 models iOS's own allocation, wear, statistics and context layout and
  checks them against Apple's code. Cascadia's writer produces contexts iOS
  8.4.1 accepts, with documented differences from iOS's own output.
- Two controller drivers for one block cannot share the hardware; keeping one
  avoids a second register-level implementation to maintain.

Orchard's measured 64 GB writer now has device evidence. Cascadia still
supplies the 16 GB write-path and failure-handling record that guides the
ports below.

## What to port from Cascadia (authored by rzxvx, one commit each)

1. **16 GB geometry in apple-sftl**: the four-bank order `(bus 0, cau 0),
   (bus 1, cau 0), (bus 0, cau 1), (bus 1, cau 1)`, 256 pages per block, nb 4
   or 8 BTOC sizes as Cascadia reads them, so the reader registers on both
   packages. Source: `sftl_mount.c`, `sftl.h`, spec-gaps corrections.
2. **iOS 8.4.1 (12H321) context compatibility**: run the reader against
   Cascadia's `w0c` dump (private, like Orchard's captures) and compare the
   native context encoder of #93 with Cascadia's `sftl_context_write`/
   `sftl_format.c` output, which iOS 8.4.1 is known to boot from. Port any
   field 12H321 needs that 13G36 does not.
3. **Session give-up of a failed superblock** (`mark_bad`, `frontier_fail`,
   `take_spare` trying the next spare) into the append writer, instead of
   stopping the whole bus on the first program failure. The VFL stays
   untouched; iOS retires a really bad block itself.
4. **Torn open superblock at mount** (`give_up_torn`): live data moved, the
   superblock left with nothing valid and listed for erase, so neither Linux
   nor iOS programs into it before an erase.
5. **Controller facts from `apple_ppn`** where H2FMI lacks them: the power
   state set after every reset, the transmit burst configuration, the
   bad-block table reads, and claiming the register windows.
6. **On-device fault injection** as a test-only provider option (Kconfig,
   default n), so the failure paths above can be exercised on a tablet, not
   only on synthetic media.

Not ported: the command-line write gates (the installer is the gate), the
in-driver LwVM parser (block/partitions/lwvm.c already does it; Cascadia's
`nand-volume` resizing belongs with the installer's LwVM writer in
`tests/lwvm.py`), and `apple_ppn` as a second controller driver.

## What stays from Orchard's side (authored by Shayne Legault)

All of `armv7/mtd` 0001-0005 and `armv7/block` 0001 as merged, and #93's
0006-0016 as he lands them: checkpoint encoding, live-page copy, reclamation
in the write path, VFL rotation, native context pages, interrupted-save
recovery, provider accounting, native allocation order, verified erase and
raw-program exclusion.

## What the ports became

All patches leave #93's 0001-0016 untouched. Port patches are authored by
rzxvx and cite the Cascadia commits they come from; the two follow-ups that
change behaviour of Shayne's code are ours and say why.

| patch | port | author | what it does |
|---|---|---|---|
| 0017 | (for 1) | ours | geometry descriptor instead of #93's fixed 64 GB constants; no functional change |
| 0018 | 1 | rzxvx | 16 GB layout (4 lanes, 1064 × 256, 1055 superblocks, VFL in 1062/1063, 3,906,250 pages); 12H321 tree batch 16, "+nb" summary spans, PAD records; H2FMI registers the provider for both profiles; the 16 GB disk is read-only |
| 0019 | 5 | rzxvx | SDR bring-up and SDR transfers in H2FMI when iBoot left no DDR state: NAND reset, power state 0x180 = 2 after it, timing 0x23210, firmware and bad-block table reads, one start per 4112-byte unit; the armv7 config gets `STRICT_DEVMEM` + `IO_STRICT_DEVMEM` (H2FMI already claims its windows) |
| 0020 | (for 3) | ours | H2FMI resets the packages after a failed transaction instead of refusing all later I/O (at most 8 resets) |
| 0021 | 3 | rzxvx | a failed data or summary program gives the block up: live pages move, the request follows, blank failed pages reuse their sequences; 8 failures per session stop the writer |
| 0022 | 4 | rzxvx | one torn last page per open block is skipped at load and its four sequences accounted; the first write moves the block's live pages out |
| 0023 | 6 | rzxvx | `CONFIG_MTD_APPLE_H2FMI_FAULT_INJECT` (default n): debugfs `apple-h2fmi/fail_program`, `fail_program_landed`, `fail_erase` |

Not ported: Cascadia's "try the next spare" after a failed spare erase (#93's
verified erase stops on failure), and marking a
given-up or torn block as awaiting erase in the next context (the append
writer leaves the block open until its records are superseded).

Where the ports change what #93's tests asserted: a failed program no longer
stops the writer (four cases rewritten to assert the give-up), a torn data page
no longer makes the medium unreadable, and the relocation case that corrupted
the landing block's last page now corrupts the page before it. Unknown read
statuses and refresh still stop recovery, as #93 decided; Cascadia's walk
would skip them.

## Port 2: 12H321 against #93's native encoder (offline)

Cascadia's `w0c` dump is private, so the reader did not run against it. The
comparison is between #93's native context encoder (`apple-sftl-reclaim.c`
`sftl_checkpoint_build`, `apple-sftl-account.c`) and Cascadia's
`sftl_ctx_build` / `sftl_context_write` / `frontier_close`, which iOS 8.4.1
boots from. Same in both: context metadata (`0x1f`, section, sequence,
entry offset and count, `0xffffffff`), section order 1, 2, 3, 5, 6, 4, each
section padded to whole pages, 511 range pairs per record, the unmapped
sentinel, summary entry layout and the "+0" span a Linux close writes.

| field | #93 (13G36 model) | Cascadia (12H321, iOS-accepted) |
|---|---|---|
| base record names | 2 (new context group and the reserved next site) | 1 (its own superblock) |
| range records | multiple of 64 (`cxtNextBatch`) | `max(32, roundup(used, 16))` |
| unused range record bytes | all `0xff` | two words `0xffffffff`, rest zero |
| statistics section | refreshed: ids 9-13, 37, 38 and live records 254 and 255 from the provider | copied unchanged from the replaced context |
| block roles 1 and 9 | 1 clean free, 9 dirty free | read as 1 awaiting erase, 9 erased; its own recovery found type-9 blocks with old data, which fits #93's reading |
| summary size | 32 chunks (flags `0x40`) | 4 or 8 chunks (flags 8 or 16), the only sizes on 12H321 media |
| summary padding | repeats the last page | PAD records (`0xffff0000`, type 1) up to the summary, no hole |
| user sequence tuple | per footer run, `U32_MAX` users skipped, count = samples | over all entries, count = entries |
| read counts | zeroed for a retired block | carried unchanged |
| context site | two-block group, any lanes | one superblock with all four banks |
| previous context | erased after the new one reloads | kept as the fallback, at most two |
| VFL table reference (reader) | u16, bit 15 = bank, low bits = page | u32 log position |

Each row is a candidate for the 16 GB writer. Only the dump (or a 16 GB P105)
can say which 13G36 behaviours 12H321 tolerates; until then the 16 GB profile
stays read-only.

## Order and evidence before anything is enabled

1. Land #93 through its own review, followed by the geometry-derived H2FMI
   erase enable whose 64 GB device qualification is now complete.
2. Ports 1 and 5 (0017-0019), then read-only device runs on both a 16 GB and
   a 64 GB P105: the whole disk read and compared with a raw dump.
   **Needs hardware:** a 16 GB P105 for the 16 GB reader at all (VFL in
   either bank, the u16 table reference reading Cascadia's log positions,
   statistics record tags, "+nb" spans and PAD replay are untested on real
   12H321 media); the SDR path of 0019 on either package (today's boot chain
   leaves DDR, so it only runs after a failure, 0020).
3. Ports 3, 4 and 6 (0020-0023); fault-injection runs on a 16 GB P105
   against a disposable Linux volume, as Cascadia did. KUnit covers the
   give-up and torn paths on synthetic 64 GB media only.
   **Needs hardware:** the reset after a failed transaction (0020) and the
   SDR transfers it switches to; what a cut-short PPN program reads back as
   (0022 only recognises malformed metadata); the debugfs injection (0023)
   on a tablet; iOS meeting a given-up or torn open block (never observed,
   Cascadia's spec section 9).
4. Port 2, then write with reclamation on the 16 GB P105 (8.4.1). Follow each
   step with a cold iOS boot and `fsck_hfs`, using Cascadia's 2026-11-16 cycle.
   The measured 64 GB P105 (9.3.5) qualification is complete.
   **Needs the `w0c` dump or hardware:** every row of the port 2 table; the
   16 GB writer is not enabled until they are settled.

Cascadia's import stays in the series meanwhile, so its files remain
available; `APPLE_PPN` stays off. Drop 0018 from the platform only when the
ports are in.
