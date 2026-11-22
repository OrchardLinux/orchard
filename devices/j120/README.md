# iPad Pro 12.9-inch (2017), Wi-Fi

J120 (A1670, iPad7,1) boots from internal storage through a J120-capable
m1n1 binary. The loader must be able to read the internal FAT boot partition,
supply the NVMe handoff in the Linux device tree and start the saved Linux entry.

Supply the loader separately as `m1n1.bin`. When assembling release inputs,
pass the file to Orchard's `orchard.inputs` module with `--m1n1 FILE`.
The generic Orchard `v0.3.2` loader has not been qualified for J120 internal boot.

## Loader source

The J120 build used AsahiLinux/m1n1 at
`c9ef56f7356e93e6f6cac116c529d31c277ef6a6` with local J120 changes. Orchard
carries its standard loader changes in [boot/m1n1/patches](../../boot/m1n1/patches/).
The additional J120 source patch is pending a separate export from the loader
maintainer.
