# m1n1 on the 32-bit boards

m1n1 has a 32-bit build for Apple's ARMv7 chips (A5, A5X, A6, A6X):
`boot/m1n1/build` makes `m1n1-armv7.macho` next to the 64-bit files, from the
same source (`boot/m1n1/patches/orchard` 0029-0031). The goal is one boot
chain: on every board Apple's boot stage starts m1n1 and m1n1 starts Linux,
with the same USB proxy, boot entries and splash. Like the 64-bit m1n1 it is
one binary for all its boards; what differs comes from the Apple device tree.

**Nothing here has run on a tablet.** apple-armv7 therefore still builds and
boots the Cascadia loader (`loader = "cascadia"`, `recovery-autogo`); the
switch to m1n1, and the removal of that loader, follow the first device boot.

## How it starts

A patched iBEC starts it the way iBoot starts XNU (method `recovery-bootx`):
the host sends Apple's `DeviceTree` and runs `devicetree`, sends the image
`producers/m1n1_armv7.py` makes and runs `bootx`. That iBEC accepts this chain
is known from booting iOS kernels this way on a P105 (Project Cascadia). m1n1
then has iBoot's boot arguments, framebuffer and device tree.

The payload is m1n1's usual one (variables, DTBs, zImage, initramfs), appended
to the Mach-O or, with `--ramdisk-output`, sent as iBoot's ramdisk. Which of
the two iBEC takes at the size of our initramfs is not known yet. Without a
kernel in the payload, `autoboot=1` starts the default boot entry from flash,
and otherwise m1n1 waits on its USB proxy, where `orchard`'s client
(`transports/m1n1.py`) works unchanged.

No host code runs this chain yet: `orchard-launcher` only knows
`recovery-autogo`.

## What is in it

- Processor layer: entry and self-relocation, exceptions with the proxy's
  guards, MMU (1 MiB sections, identity) and caches, time from the interrupt
  controller's 24 MHz timebase, watchdog.
- Linux hand-off: the board's static device tree gets memory, framebuffer,
  serial number, Wi-Fi and Bluetooth addresses, random seed, boot arguments
  and initrd; zImage entered with `r0 = 0`, `r1 = ~0`, `r2 = tree`.
- USB: the DWC2 controller behind the S5L PHY, started after iBoot powered it
  down (the sequence of `kernel/patches/armv7/usb`; A5 family power gates
  only, other chips keep what iBoot left).
- Flash: the read half of our Linux H2FMI/VFL/SFTL drivers, unchanged
  (`src/arm32/sftl`, GPL-2.0-only), as m1n1's main storage; LwVM partition
  table; boot entries from an XBOOTLDR partition as on GPT disks.

## What was checked, and how

In QEMU (`smdkc210`: Cortex-A9, Samsung UART) with a synthetic Apple device
tree and a stand-in for iBoot that loads the Mach-O and passes boot arguments:

- start-up, relocation, MMU and caches, framebuffer console and logo;
- the proxy with `orchard`'s own client over the UART: memory transfer,
  calls, and recovery from a deliberately bad read;
- a payload boot of the image `producers/m1n1_armv7.py` made, into a stand-in
  zImage that printed the registers Linux would get, with the filled tree
  dumped and read back; the same with the payload as ramdisk.

On the host: the boot-entry tests with an LwVM disk (`cargo test`), the
producer's tests, and a comparison of the 64-bit build with the one before
these patches: its C code compiles to the same instructions (line numbers in
assertions aside); only the boot-entry reader's partition listing was
restructured.

Not checked anywhere: the real iBEC hand-off, USB, the flash controller and
map loading inside m1n1, timing, A5X and A6 boards.

## First device session (P105)

In this order, each step only if the one before worked:

1. `bootx` of m1n1 alone: text on the screen and on serial. Shows the entry,
   the boot arguments and that iBoot leaves the display on.
2. USB: the proxy answers (`orchard`'s client connects).
3. A RAM payload: Linux reaches the USB serial login and SSH. Shows the
   hand-off, the filled tree and which payload route iBEC accepts.
4. Storage from the proxy: `main_storage_init`, time to load the maps, then
   the partition list.
5. A boot entry from flash (needs a boot partition, which no installer makes
   yet).
