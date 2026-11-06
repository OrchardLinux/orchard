# Installing into an LwVM Data tail

The installer can prepare a Linux root above the kernel's logical
`/dev/applesftlN` provider. `orchard-lwvm` supplies a checked device-mapper
view of the Linux descriptor. NAND addressing, ECC, FTL replay and page
programming belong to the kernel storage driver.

The public P105 install capability remains gated. This source has synthetic
host coverage. The current ARMv7 candidate still needs device qualification
of the complete writable backend, reclamation and recovery from an
interrupted write. Preparing System boot assets through native iOS also
remains a separate step.

## Native HFS preparation

Allocation requires an owner-private proof from the existing native shrink
workflow. That workflow must shrink HFS, complete an unmounted filesystem
check and capture its primary and alternate 512-byte volume headers. A
later native read confirms the filesystem size and volume identity after
returning to iOS. Its current LwVM pair must still describe the same Data
partition. Mounted timestamps and journal updates can change normally.
Every later native resize invalidates this proof.

The proof records the device profile and ECID, Data's LwVM UUID, the saved
LwVM header hash, HFS volume identity, exact filesystem size, both checked
native headers and the confirming native header. The filesystem check
report has `unmounted: true` and integer `exit_code: 0`. These are trusted
results supplied by the native preparation workflow. The host code checks
their consistency; it cannot attest that an external filesystem checker
ran.

Data remains encrypted in the whole logical provider view. Recovery binds
the native proof to its pinned device identity and rechecks the live LwVM
pair. It has no plaintext HFS view, so it relies on the owner supplying the
current native preparation. It never compares encrypted provider bytes
with a plaintext volume header.

Use saved regular files to create the proof:

```
python -m orchard.lwvm proof \
  --primary lwvm-primary.bin --backup lwvm-backup.bin \
  --data-uuid DATA_UUID --profile PROFILE --ecid ECID \
  --native-primary hfs-checked-primary.bin \
  --native-alternate hfs-checked-alternate.bin \
  --native-confirmation hfs-native-confirmation.bin \
  --fsck-report fsck.json --output shrink-proof.json
```

This command opens saved files and creates a new private JSON file. A
separate saved allocation can be reviewed with `python -m orchard.lwvm plan
--primary ... --backup ... --proof shrink-proof.json --linux-uuid UUID
--output allocation.json`. Each output is created with mode 0600; existing
files are refused. Captures and generated proofs stay private.

## Allocation and installation

`orchard install SOURCE --hfs-shrink-proof shrink-proof.json` adds the
checked tail as a target when the device's capabilities permit installation.
The owner types `allocate Data tail` before any allocation. Existing Linux
descriptors are offered only as replacements, with the usual typed
`erase NUMBER` confirmation. Inspecting or launching a previous installation
performs no formatting.

Planning preserves each existing descriptor's identity, flags and unknown
bytes. Data's new length rounds the checked HFS length up to a complete
allocation chunk. Its later chunks become an unencrypted Linux descriptor,
with their logical indexes rebased to zero. Fragmented physical chunks
retain their order. The last extent stops at the original Data endpoint.
An existing Linux descriptor causes new allocation to stop.

The plan carries both saved headers, their hashes and the exact Linux
extents. Recovery rechecks the live pair, CRC, media size, device numbers,
ECID and recovery boot ID. It refuses a mounted or held provider before
enabling its logical-write policy. For a modular `apple-sftl`, it closes
the provider, reloads it with `write_enable=1` and checks the complete
snapshot again. The two default-off NAND write configuration prerequisites
must already be present in the approved kernel. A built-in read-only
provider refuses installation before writes.

The fresh driver's initial read-only flag must also report that its replay
state and upstream program callback permit writes. The installer checks
this before clearing a block fence, which preserves the driver's refusal
when recovered journal sequences prevent a qualified write.

The plan explicitly bounds the provider to one hour and at most
`131072 + 8 * ceil(root_bytes / 4096)` read callbacks, capped at the
driver's unsigned 32-bit limit. The budget gives headroom based on the
planned root size. It predicts no completion time or physical write count.
Read-budget or lifetime exhaustion stops the provider and requires a new
inspection. Recovery's default 32,768-read policy remains read-only.

Metadata commit writes the backup first. Each header write has a private
intent record, a driver flush, block-buffer invalidation and complete
readback before proceeding. The primary is written only while its old
bytes and the new backup remain exact. A failure after an intent reports
an uncertain outcome and stops. The installer performs no automatic
retry, rollback or header repair. `/run/orchard/lwvm-write.json` retains the
original and proposed headers with the last stage. Save this record before
leaving RAM recovery when inspection is needed.

Only the verified Linux DM mapping is passed to `mke2fs`, mounted as the
target and populated through the existing bounded root tar extractor.
The mapper receipt must match the plan's provider, header CRC, Linux UUID,
complete extent table, size and writable mode. System and Data mappings
are never opened for filesystem writes. Cleanup unmounts the target,
removes the mapping, fences the provider and restores its default read-only
policy. A cleanup failure is reported and requires inspection.
Mapping removal uses the same static helper with the validated extent
receipt. It verifies the complete DM table and provider identity without
reading the medium, so a spent read budget still permits cleanup. Failure
to unmount or remove a mapping leaves it for inspection while the raw
read-only fence is still attempted.

The finished result names the installed Linux UUID and filesystem UUID.
`/etc/orchard/installed-root.json` and the host's private `last-install.json`
record an `orchard.ios-local-file-root` handoff with `boot_assets_prepared:
false`. Native iOS must prepare its launcher and System boot assets from
the matching release before startup. This path never writes a FAT boot
partition or changes Apple's System filesystem from Linux.

## Recovery producer and validation

Build `orchard-lwvm` through `orchard.build.prepare_lwvm` and pass its path
as optional `--lwvm FILE` to `orchard.image.recovery`. It is staged at
`/usr/sbin/orchard-lwvm`. The static executable must match the recovery's
ELF architecture. A recovery intended for LwVM installation needs the
kernel's device-mapper support and `apple-sftl` module tree. Recovery's
storage fence recognizes the logical provider and sets it read-only before
publishing storage readiness.

`tests/test_lwvm_install.py` checks geometry and corruption refusal,
noncontiguous maps, final extent clipping, proof consistency, preservation
of existing partition identities, explicit replacement, changed live
headers and failure after either header submission or flush. Root tests
reject mapper receipts that expose another range and verify that streamed
files and formatting receive the Linux mapping. These tests use synthetic
bytes and fake device calls. They establish no hardware write result.
