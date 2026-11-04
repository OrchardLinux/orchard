# J72 firmware inputs

The J72 recipe identifies iPad Air Cellular (`iPad4,2`, J72AP) inputs from
iOS 12.5.7 build 16H81. Firmware and per-device calibration remain private.

`outputs` is deliberately empty. The initial native console boot needs no
firmware output; it must not install the experimental RAM worker's private
firmware bundle as if it were a qualified native driver input. Orchard's
firmware producer skips unused inputs, so this recipe does not download an
IPSW for a console build.

The recipe's complete 2,961,469,678-byte IPSW was downloaded from the stated
Apple CDN URL and its SHA-256 verified. Its BuildManifest identifies iOS
12.5.7/16H81 and J72AP with CPID `0x8960`, BDID `0x12`, and product `iPad4,2`.

## Touch input and remaining conversion work

`J72.mtprops` is the decoded logical file, not its empty on-disk data fork.
Its SHA-256 is
`69f27fe207cc5810a748dac3dfe640b1c5a755709f49513b4b5125660784f0fc`
and its logical size is 80,301 bytes. It uses a `com.apple.decmpfs` type-4
attribute and a `com.apple.ResourceFork` containing zlib blocks. The existing
Orchard resource-fork decoder reproduces the privately extracted file exactly.

The iOS 12.5.7 system image is APFS. Orchard's existing UDIF reader, APFS volume
lookup, extended-attribute lookup and type-4 decompression read this path
without changes, producing bytes equal to the private logical file. This is
the system image; recovery RAM images are separate inputs and may use another
filesystem. No HFS+ reader is needed for this firmware recipe.

The normal `Ipsw.system_file()` path was also checked against the Apple CDN:
it fetched 2,067,491 bytes by range and returned the same 80,301-byte logical
file. That check covers ZIP member selection, UDIF, APFS and decompression.
It does not convert the file or qualify touch hardware.

A native touch output must not be added until J72 firmware conversion and its
native driver are qualified together on hardware. Successful extraction alone
does not qualify a boot packet or the per-device calibration sequence.

The existing Gen1 converter targets a different board's firmware protocol.
Its presence is not evidence that it can convert J72's N1 personality. No
native J72 touch firmware format or device calibration output is claimed here.
