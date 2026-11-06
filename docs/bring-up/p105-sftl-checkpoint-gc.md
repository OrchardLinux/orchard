# P105 offline checkpoint encoder and GC planner

The kernel draft on
[`p105/08-ftl-reclamation-integration`](https://github.com/shaynelegault/linux/tree/p105/08-ftl-reclamation-integration)
at `c504eae2a214915dcf5d2c4856c52e7a6ac79d2d` stacks on the append writer at
`c63274165870417f01cc3e1264067cef71d9c0b2`. It supplies an offline checkpoint
encoder and GC transaction planner. Kernel patches follow Orchard's
platform-scoped export workflow. MTD patches 0009 and 0010 select only
`apple-armv7`; applying them to the append-writer source reproduces this
branch exactly. Exact-revision hardware qualification remains pending.

The snapshot contains all 15,625,016 logical keys, including the sixteen
internal keys beyond the exported disk. The encoder preserves opaque native
accounting payloads, updates the decoded context-block list and serializes
range fragments with 48-bit sequence metadata. The planner budgets its
working buffers and destination capacity, retains footer space, checks copy
and checkpoint readback receipts, then compares the entire cold-recovered
map. Supplied blank, reserved and lane descriptions need authoritative VFL
and context ownership proof before future destructive I/O uses them.

Media GC remains disconnected. Opaque accounting fields still need native
updates before checkpoint installation. The current reader rejects an
incomplete newest SFTL context and requires contiguous replay; native
previous-checkpoint selection and interrupted-save replay need further work.
The earlier installed desktop used the Python backend and supplies no
qualification for these kernel changes.

Native format work follows IOFlashStorage-730/13G36 and the pinned
[Cascadia NAND research](https://github.com/rzxvx/Project-Cascadia/blob/c20b85ebb951c704d7e3a664c378dde8276e3928/docs/research/p105-nand.md).
The native metadata formatter is at `0x81092408`. Saved 64 GB parameters and
the native row builder agree on normal erase rows
`(cau << 19) | (block << 7)`. Native VFL rotation erases an unreferenced target
bank, writes an initial checkpoint retaining old table references, copies the
tables and writes a final checkpoint. The source bank survives the healthy
path. Physical erase effects, SLC aliasing and retirement/torn-copy recovery
remain unqualified. Firmware, captures and device identifiers stay private;
public tests generate synthetic media.

Host ASan/UBSan checks passed 27 writer-enabled recovery cases, 14 read-only
cases and eight checkpoint/planner cases. Twenty-four metadata records
matched the pinned native formatter running in synthetic RAM, and 209 C
checkpoint images round-tripped through the Python map decoder. Exact-source
core/frontend/checkpoint/provider and both fixture objects compiled on the
current ARMv7 tree in read-only and experimental write configurations, with
`W=1` and warnings-as-errors apart from the existing `vmstat.h` enum warning.
An ARM64 QEMU KUnit kernel passed 35/35 tests using pinned pristine Linux
v6.12 plus the exact integrated C sources and a validation-only dependency
symbol for synthetic program callbacks. These checks exercise no controller
timing. P105 storage features remain `missing`, with installation gated
while exact-revision device qualification is pending.
