# LwVM installed-root mapper

`orchard-lwvm` reads Apple's logical volume map from a whole logical storage
provider. The kernel backend supplies flash geometry, ECC and the current FTL
mapping. This helper handles the LwVM volume layer above that provider.

Build a static executable with `boot/lwvm/build --arch arm --output DIRECTORY`.
The same producer accepts `arm64`. `orchard.build.prepare_lwvm` caches the
executable by its source inputs and target architecture.

```
orchard-lwvm inspect --disk image.raw --root UUID --json
orchard-lwvm map --disk /dev/applesftl0 --root UUID --mode ro
orchard-lwvm map --disk /dev/applesftl0 --root UUID --mode rw --json
```

`inspect` accepts a regular image or block device. `map` requires a block
device and root privileges. Its default output is the verified `/dev/dm-N`
path. A JSON receipt has schema 1, kind `orchard-lwvm-root`, canonical
`root_uuid`, zero-based `partition_index`, `header_crc32`, `media_bytes`,
`chunk_bytes`, root `bytes`, `disk` major/minor and ordered `extents`.
Each extent contains `start_sector`, `sectors` and `physical_sector` in
512-byte sectors. A mapped receipt adds `mapping` with `name`, `uuid`,
`device` and `mode`. Regular images have null disk numbers.

The helper never programs flash or changes either LwVM header. A writable DM
table also requires a writable kernel provider; it grants no additional FTL
write capability. Existing mappings are reused only when their complete
linear table, provider identity, DM UUID and access mode match. Failure while
creating a new map removes that map. An existing mismatch remains untouched.

## Accepted format

The format derives from native LwVM header validation and geometry observed
in the iOS 9.3.5 storage provider, with the prior Orchard image tooling used
as a crosscheck. The tests construct independent synthetic headers.

The accepted signature is `6a9088cf8afd630ae351e24887e0b98b`. Both 4096-byte
header copies must pass their CRC32 and agree byte for byte. CRC32 uses the
whole header with bytes `0x2c..0x2f` zeroed, matching the standard zlib CRC32.
The media size at `0x20` must equal the provider's actual byte length.
The nonzero media UUID occupies `0x10..0x1f`.

To derive chunk size, take `(media_bytes - 1) >> 10`, then choose the power of
two whose exponent is that value's bit length. The accepted value fits 32
bits and gives a chunk of at least 4096 bytes. The primary header starts at
zero; the backup starts at `chunk_bytes - 4096`. No measured device size or
owner identity selects the geometry.

Partition count at `0x28` is between 1 and 8. Each 128-byte descriptor starts
at `0x200 + index * 0x80`: type UUID, identity UUID, then little-endian 64-bit
virtual start, virtual end and attributes. UUID bytes use canonical order.
Virtual bounds determine volume length. They do not locate physical data.
The requested root must carry the standard Linux filesystem type UUID
`0fc63daf-8483-4772-8e79-3d69d8477de4` and zero attributes. This excludes
the encrypted Data volume from the mapper.

The 1024 little-endian words at `0x800` locate physical chunks. A word's high
nibble selects its descriptor; its low 12 bits give that descriptor's logical
chunk index. Every descriptor requires exactly one chunk for each logical
index covering its length. Owned chunks must fit wholly within the provider.
Chunk zero belongs to metadata. The accepted special markers are `0xf000`
at index zero, `0xf111` for free complete chunks, and `0xf3ff` beyond the last
complete chunk. Unknown markers, missing ownership, duplicate logical chunks
and overlapping virtual descriptors stop mapping.

The helper orders extents by logical chunk index and merges physically
adjacent runs. It clips the final run to the descriptor's exact byte length.
Consequently a fragmented volume does not expose gaps or neighbouring data.
It refuses a damaged or divergent header pair without attempting repair.

## Validation boundary

`tests/test_lwvm.py` compiles and executes the C parser against sparse images.
These tests establish format checks and extent generation. Kernel compilation,
synthetic Linux mapping checks and device boot qualification are separate
results. Device feature flags remain gated on hardware qualification.

`boot/lwvm/check-linux --helper PATH` runs the actual helper against a
temporary image on a disposable Linux worker as root. It allocates its own
loop device and map, checks fragmented reads and writes, tests exact reuse
and mismatch refusal, then removes its map and loop association. The ARMv7
static helper has passed this check under Linux's compat ioctl path.
