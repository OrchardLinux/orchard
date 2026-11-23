# P105 complete-page PPN programming

The H2FMI program path accepts one complete physical page: 16 KiB of data and
64 metadata bytes, programmed through the measured PPN command path. It is
part of the driver (kernel/patches/armv7/mtd); there is no build option for it.

The caller supplies every data and metadata byte. The driver serializes the
transaction with the bus lock, checks the destination's erased status and
every raw byte, then programs the page and validates completion. Success
requires a fresh read with good status and an exact comparison of the whole
page. MTD reports progress only after that readback succeeds. An uncertain
completion or mismatched readback stops further I/O on the bus.

The request must align to a physical page and include a full data/OOB pair.
Block zero and the two reserved package-tail blocks are excluded. This
raw MTD interface exposes no erase operation and owns no free-space map. The
aggregate PPN provider exposes verified normal-block erase to SFTL for every
package whose reported geometry validates (`armv7/mtd/0026`): the erase row
and the pages read back after it follow from that geometry, and the bootstrap
window at the top of each CAU is never erased. A package whose geometry does
not validate gets no erase, and the driver logs it. SFTL owns every lane of
the block and preserves Apple's native mapping records.

SFTL models the writer, reclamation and checkpoints for the 64 GB layout only.
The 16 GB package's provider offers erase as well, but its disk stays
read-only until its 12H321 accounting and checkpoint formats are modelled.

## Source and evidence

The candidate is
[`cef7165b078f1d52c36fd9a38467e999ab2fefdd`](https://github.com/shaynelegault/linux/commit/cef7165b078f1d52c36fd9a38467e999ab2fefdd),
on the geometry/read topic
`fe8c2f6960bd9d9fa0b051560598069497b31d82`. The program fixture exercised
the actual driver functions with fake MMIO and passed 134,181 checks under
ASan/UBSan. Cases cover blank detection, request bounds, completion statuses,
transport failures and full readback mismatches. The read fixture retained
its 140,146 checks. Strict checkpatch reported no errors or warnings.

Focused ARMv7 object builds passed with programming enabled and disabled;
the KUnit fixture also compiled. Those builds used the existing qualified
ARMv7 generated headers. Their source hashes match this candidate, but they
were object checks rather than a complete current-base kernel build or a
kernel KUnit run.

On 23 November 2026, Linux wrote a 64 MiB file through SFTL. Full readback and
`e2fsck` passed before restart. A fresh Linux load recovered the mapping and
the file. Two later iOS boots reported clean System and Data filesystems, while
the file retained SHA-256
`d41b5b52b5a8b4f5329a413c2edcd6de1f575bac32e70fd76fafcf1dfa6c6421`.

The same trial erased all 16 lanes of one obsolete block. Native accounting
was saved before Linux reused it, and a fresh load recovered the incremented
wear plus the written page. iOS later relocated that page and reclaimed the
block. A cold Linux boot recovered iOS's state and found every normal and SLC
page in the block blank. P105 `storage_write` is `working` for the measured
64 GB package. The 16 GB package stays read-only: its SFTL writer is not
modelled.
