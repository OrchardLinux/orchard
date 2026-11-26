# m1n1 on the 32-bit boards

m1n1 has a 32-bit build for Apple's ARMv7 chips (A5, A5X, A6, A6X), from the
same source as the 64-bit one ([OrchardLinux/m1n1](https://github.com/OrchardLinux/m1n1)).
`boot/m1n1/build` makes it as `m1n1-armv7.macho` and the nightly publishes it.
The goal is one boot chain: on every board Apple's boot stage starts m1n1 and
m1n1 starts Linux, with the same USB proxy, boot entries and splash. Like the
64-bit m1n1 it is one binary for all its boards; what differs comes from the
Apple device tree.

**Nothing here has run on a tablet.** apple-armv7 therefore still builds
the Cascadia loader too, and that route stays the comparison.

## How it starts

orchard2's `ibss` route (OrchardLinux/tools, `docs/run.md`): from a boot ROM
that checkm8 from a Raspberry Pi Pico entered, or from kDFU, the host starts
Apple's patched iBSS and sends the patched iBEC, which waits in recovery
mode. The host sends Apple's `DeviceTree` and runs `devicetree`, sends m1n1
as an IMG3 kernel and runs `bootx`: the way iBoot starts XNU. That iBEC
accepts this chain is known from booting iOS kernels this way on a P105
(Project Cascadia). m1n1 then has iBoot's boot arguments, framebuffer and
device tree.

The payload is m1n1's usual one (variables, DTBs, zImage, initramfs), behind
the Mach-O. Whether iBEC takes an image of the size of our initramfs in one
upload is not known yet. Without a kernel in the payload m1n1 waits on its
USB proxy.

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
- the proxy over the UART: memory transfer, calls, and recovery from a
  deliberately bad read;
- a payload boot into a stand-in zImage that printed the registers Linux
  would get, with the filled tree dumped and read back.

On the host: the boot-entry tests with an LwVM disk; that the IMG3 orchard2
sends is byte for byte the one the earlier Python producer made; that the
P105's patched iBSS and iBEC are byte for byte Project Cascadia's. The 64-bit
build from the same source boots a J71b.

Not checked anywhere: the real iBEC hand-off, USB, the flash controller and
map loading inside m1n1, timing, A5X and A6 boards.

## First device session (P105)

The tablet in DFU mode from the Pico (`orchard2 devices` shows it as
`pwned checkm8`), or in kDFU. In this order, each step only if the one
before worked:

1. `orchard2 run --bare`: m1n1 alone. Text on the screen and on serial shows
   the entry, the boot arguments and that iBoot leaves the display on.
2. USB: `orchard2 devices` lists the tablet in m1n1 mode, so the proxy
   answers.
3. `orchard2 run` in a payload project: Linux reaches the USB serial login
   and SSH. Shows the hand-off, the filled tree and that iBEC takes the
   payload behind m1n1.
4. Storage from the proxy: `main_storage_init`, time to load the maps, then
   the partition list.
5. A boot entry from flash (needs a boot partition, which no installer makes
   yet).

If a step fails, `orchard2 run --autogo` boots the same project through
Cascadia's loader. Both routes start the same patched iBSS and iBEC in the
same way, so where `--autogo` boots and step 1 does not, the fault is in
m1n1 or its hand-off, and where neither does, it is before them.
