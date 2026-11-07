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
primitive exposes no erase callback and owns no free-space map. A dependent
VFL/SFTL writer must decide which blank pages it can allocate while preserving
Apple's native mapping records.

The driver binds the shared `apple,s5l8940x-fmi` compatible, which P105 uses
in this source tree. Its imported two-CAU parameter profile also needs a
regression check before merge; the measured 64 GB profile does not qualify
that hardware.

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

Earlier bounded device programming supplied protocol evidence. This exact
revision still needs an approved physical test, including the failure and
recovery paths. It has no new physical-programming result. Raw page readback
does not establish logical-map persistence after restart or recovery after
interrupted writes. P105 `storage_write` is `untested` until a device test.
