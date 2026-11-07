# ARM32 iBEC staging payload

`build --profile devices/p105/cascadia.toml --output DIR` builds a loader
with Orchard's kernel toolchain through `buildenv`. The output directory
contains `loader.bin`, its ELF and a source/profile/toolchain receipt.
The ELF check requires executable ARM code at the upload address and checks
every allocated section against the raw bytes. ARM unwind tables are
discarded so they cannot displace the entry instruction.

`python -m orchard.image.cascadia --profile devices/p105/cascadia.toml
--loader DIR/loader.bin --loader-elf DIR/loader.elf --kernel zImage
--dtb s5l8942x-p105.dtb --initramfs recovery.cpio.gz --bootargs 'rdinit=/init'
--output linux.bin` assembles one device's payload. Kernel bytes remain
unchanged. The DTB editor preserves properties while updating `/chosen`
boot arguments and its initrd addresses, then reserves that destination.

The layout is a checked loader followed by padding to 32 KiB, the zImage,
the edited DTB and the supplied initramfs, with each data address aligned to
eight bytes. The assembly entry disables the profile's watchdog before stack
setup; the C entry also clears it before validation. A rejected image remains
stopped for inspection. Validation checks the configuration checksum, bounds
and relocation alignment while touching only the uploaded low-memory image.
The loader then flushes caches and disables the inherited MMU/cache state
before copying the initramfs to reserved high RAM. Word copies handle the
aligned body and byte copies preserve the exact final tail. A barrier precedes
the Linux handoff with the DTB in `r2`. High RAM therefore needs no inherited
iBEC virtual mapping.
The fixed loader stack uses the gap below offset `0x7000`; code must finish
below `0x6000`, and the configuration starts at `0x7f00`.

The zImage must carry the v6.12 ARM size table and end on an eight-byte
boundary. Its DTB follows immediately so the appended-DTB decompressor can
relocate it. Assembly bounds the inflated kernel together with the relocated
decompressor's workspace below the initramfs destination; unknown table
formats and overlapping layouts are refused.

The receipt describes every byte range and its hash. It records
`architecture = "arm"` and `initramfs_encoding = "cascadia-dtb"`, allowing a
distribution to reject an image assembled with another architecture or
payload format. This external-initramfs extension awaits hardware testing;
the earlier P105 boot embedded its initramfs in the kernel.

See `NOTICE` for source provenance. This producer contacts no device.
