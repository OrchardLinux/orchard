# P105 PPN geometry and reads

The measured 64 GB P105 package exposes two H2FMI buses. Each bus has eight
CAUs with 2,088 blocks per CAU and 128 normal-view pages per block. A page
contains 16 KiB of data and 64 bytes of metadata. The reference two-CAU
package uses a different address layout, so the driver queries both PPN
parameter copies and accepts only the measured profiles.

The 64 GB row places the page in bits 0 through 6, the block in bits 7
through 18 and the CAU from bit 19. Four row cycles carry the address.
Context reads use the separately selected SLC view. The MTD byte offset
remains 64 bits throughout, including addresses above 4 GiB.

The proposed read path validates completion status before asking for payload,
restores inherited controller state on exit, and stops a bus after a
transport or cleanup failure. A failed zero-byte read returns a terminal
error, which prevents MEMREAD from repeating the same page indefinitely.
Page programming belongs to a separate topic.

## Evidence and scope

The source branch starts at current ARMv7 base
`97d6283e22e8f989a9d1027d99dc519b5f1f7f81` and ends at
[`fe8c2f6960bd9d9fa0b051560598069497b31d82`](https://github.com/shaynelegault/linux/commit/fe8c2f6960bd9d9fa0b051560598069497b31d82).
The actual read functions passed 140,146 assertions under ASan and UBSan.
Focused ARMv7 driver and KUnit objects compiled. Synthetic public KUnit
fixtures check both parameter profiles, address uniqueness, context-view
bounds and malformed responses.

Earlier device captures supplied the geometry and native logical-page
crosschecks. Exact-revision hardware qualification remains pending. The new
candidate must establish USB recovery before a bounded read-only session;
its source omits the program and erase paths.

Raw PPN reads expose physical flash pages. Apple's VFL and SwissPPNFTL maps
still determine the logical medium, with LwVM describing its partitions.
The dependent kernel translation reader follows those native maps.
Encrypted iOS volume contents retain their encryption at this layer.

P105 `storage_read` is `untested` until a device test.
The read topic also changes the imported reference profile's shared
controller code; that profile needs a regression check before merge.
