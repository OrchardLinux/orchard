# PR #93 on the 64 GB iPad mini

On 19 November 2026 the P105 ran the ARMv7 kernel built for PR head
`bd8916c` from integration commit `8b7f4cbb`. Its kernel notes matched
the build's SHA-256, `bdad8ec70b7f63a04bd1b33002ca89767b3550ef59eb4bcc8a74530efbac1e92`.
Linux ran from the Mac over USB. The internal disk and its three partitions
were read-only and unmounted during the measurements.
[ARMv7 measurement build](https://github.com/OrchardLinux/orchard/actions/runs/44556846039)

## Raw writes and live ownership

All 16 raw MTD devices returned `EBUSY` for a complete page request while
SFTL owned the disk. Each request named block 2087, which this driver
rejects before issuing a controller command even when SFTL releases the disk.
The contention test held the semaphore that protects an in-flight raw program
on the real controller. A competing SFTL claim waited for release and then
returned `EBUSY` because the disk was still bound. The test submitted no
physical program.

An independent load recovered 12,793 map runs and 32 native context records.
Block 896 held 29 current 4 KiB pages across eight lanes in the iOS Data
partition. Every logical read matched the data at its recovered physical
address. The primary and backup LwVM headers passed their CRC and matched the pair from
an earlier native iOS boot. An erase trial on block 896 would put live iOS
data at risk.

## Spare areas

Block 1472 had the native dirty-free role and zero saved or current mappings.
Its complete diagnostic capture covered all 16 lanes and one lane on each
neighbor. On 1472, all 2,048 normal rows contained data. Each of the 1,024
SLC reads returned `0x61` with no data phase. Block 1471 had ten live
mappings across the block. The sampled lane on block 1473 was already blank
in both views.

The survey sampled page zero on bus 0, CAU 0 in 1,085 further candidate
locations. Each candidate and both neighbors had the dirty-free role, zero
flags and zero saved or current mappings. The survey excluded the recovered
VFL bank members and their neighbors. Of those samples, 1,072 were blank
in both views and 13 returned retired status without data in both views.
These results cover the specified page and lane. The erase footprint and
recovery from a partial erase still need a physical trial with observable
controls.

## Native counters

Checkpoint sequence 189791520 contained the 88-byte VFL record (ID 255)
and the 464-byte lower-provider record (ID 254). The live journal reached
189808092. The saved allocator selected block 1380 as its landing block.
By this boot it already held 6,625 live pages.

Four reads through `/dev/mtd0ro` returned 65,536 bytes matching the
independent physical capture. Reading the checkpoint again yielded identical
counter records and the same journal sequence. A live accounting provider
needs an authoritative sample with a continuous record of operations and
completion flags across the handoff into Linux, including its initial
media scan. Reclamation remains disabled pending that handoff and the
physical erase and cold-boot tests.

On iOS 9.3.5, the paired USB service returned both live records in
`com.apple.disk_usage` under `NANDInfo`. The collector verified 40 saved
responses and four fresh ones. Each contained matching copies of the 11 VFL
and 58 lower-provider counters. The geometry record
`0x03000f00` contained the measured 64 GB signature at byte offset 48.
Starting there, five little-endian words read `2, 2088, 8, 64, 128`.

| Read in native iOS | Additional 4 KiB pages in VFL | Change in lower-provider word 1 |
| --- | ---: | ---: |
| 1 MiB on System | 256 | 94 |
| 4 MiB on Data | 1,024 | 433 |

The first System interval also recorded a program covering 32
pages. A repeat showed the same read counts with no program delta. In two
comparisons, reversing the read order produced identical changes in all 69
counters. The paired system log yielded no per-command flash records during
the controlled read. Between the last iOS sample and Linux's first request,
an ordered firmware trace or a verified interval of flash inactivity is still
needed.

After returning to iOS, both copies of the LwVM partition header passed
their CRC and matched the Linux measurements. All 29 sampled Data pages
from block 896 also matched. These are measurements taken before any
erase trial.

## Repeating the checks

Verify the running kernel notes against the measurement artifact and check
the internal volumes before collecting data. Keep the full map and captures
private. [P105 capture procedure](../../tools/p105/)

On a P105 session with Python 3, stage `tools/p105/check_raw_exclusion.py`
in RAM. The script pins the kernel whose tail-block guard was reviewed.

```sh
timeout -k 5 45 python3 /run/check_raw_exclusion.py
```

The optional `p105_map_audit` module recovers a second map through the
driver's read callback. Its report includes provider offsets and logical page
numbers for the selected block. Run these commands on the matching P105,
with `debugfs` available at `/sys/kernel/debug`. Direct the report to the
host or to tmpfs. Adding `survey=1` at load also samples eligible spare areas.

```sh
timeout -k 5 180 modprobe p105_map_audit block=1472
timeout -k 5 30 cat /sys/kernel/debug/p105_map_audit > /run/p105-map-report
timeout -k 5 15 modprobe -r p105_map_audit
```
