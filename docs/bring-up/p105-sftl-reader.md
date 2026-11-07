# P105 native VFL and SwissPPNFTL reader

H2FMI exposes physical PPN pages. Apple's VFL and SwissPPNFTL records supply
the logical medium above them. The proposed `apple-sftl` kernel driver reads
those records and publishes a read-only `applesftlN` disk. LwVM partition
mapping belongs above that disk, where encrypted iOS contents retain their
encryption.

The measured profile has two buses and eight CAUs per bus, each with 2,088
blocks of 128 normal-view pages. H2FMI supplies each 16 KiB data page with its
64 metadata bytes through a canonical provider. Context reads select the
separate SLC address view. This provider offers no program or erase callback.

Recovery requires agreeing VFL context replicas and lane tables. It validates
the saved SFTL context fragments, then replays later data and trim records in
sequence order. Completed-block summaries preserve allocation floors; open
blocks are scanned fully so an erased lane cannot hide a later completed
stripe. Malformed or incomplete maps prevent disk registration. Unsupported
geometry returns an error.

The map contains 15,625,016 entries, including sixteen internal keys. The
exported medium contains 15,625,000 logical 4 KiB pages, or 64,000,000,000 bytes,
and accepts 512-byte sector reads. The frontend serializes access to its
preallocated request buffer and drains requests before releasing the provider.

## Bounded qualification

The latest reader candidate is
[`e7d118292d828b9796d85d19872653f7d4a8cb67`](https://github.com/shaynelegault/linux/commit/e7d118292d828b9796d85d19872653f7d4a8cb67).
It follows the canonical H2FMI provider and native-map reader commits
`9b494217444fc61adae2f95bc2ec05e461d45049` and
`e5960c537455b9c40c57deb53823e80ca9b13bb4`.

Orchard carries this reader in `kernel/patches/armv7/mtd` without the read
budget and lifetime module parameters of that candidate: a mounted root reads
for as long as the system runs. A provider error still stops the reader.

## Software checks and limits

The unchanged mapping core reconstructed a saved private capture through
sequence 187831212. An independent Python reference agreed at all 28,830
checked endpoints of the map union. Both LwVM headers and the System prefix
matched previously captured native iOS logical reads. This comparison used
saved pages and performed zero device operations. Captured pages remain
private; public fixtures generate synthetic media.

At `e5960c537`, eight tests passed in an ARM64 QEMU KUnit kernel built from
pristine pinned Linux v6.12 plus the exact reader/test sources and required
Kbuild entries. This ran the mapping code without Cascadia or an H2FMI
hardware backend. The host version of those fixtures also passed under
ASan/UBSan with allocation, mutex and tree-API shims.

The canonical H2FMI provider shares controller code with the imported
two-CAU profile. That profile needs a regression check; native-map recovery
currently accepts only the measured 64 GB geometry.

The exact candidate still needs a complete qualification image and an
approved P105 boot with working USB recovery, followed by bounded native-map
reads. Host reconstruction and an ARM64 KUnit kernel do not qualify ARMv7
controller timing. P105 `storage_read` stays `untested` until then. Sustained
writes and interruption recovery belong to the dependent writer work.
