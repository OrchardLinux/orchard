# Kernel configuration for A7, A8 and A8X

`config` is the `apple-16k` configuration with 4 KiB pages instead of 16 KiB.
Apple's A7 (S5L8960X), A8 (T7000) and A8X (T7001) CPUs have no 16 KiB
translation granule, so their devices cannot run the `apple-16k` kernel. The
patch layers are the same (`../../series/apple-4k`); only the page size and
what Kconfig derives from it differ (page shift, contiguous-PTE and
max-order sizes, mmap randomization bits, THP swap).

Both platform configs build the legacy composite USB gadget and its mass-storage
function as modules. RAM and recovery images can bind their configfs gadget at
startup; users of the legacy composite gadget can load it when needed. FUSE
remains a module.

## How the file is generated

From `../apple-16k/config`, with `CONFIG_ARM64_4K_PAGES=y` and
`CONFIG_ARM64_16K_PAGES` unset, resolved with `olddefconfig` on the pinned
source as described in `../apple-16k/README.md`. Keep the two files in step: a
change to `apple-16k` is made here too, then resolved again.

## J71t system sleep preparation (0.4)

Resolve with Orchard Clang 19. Enable KEYBOARD_GPIO_POLLED=y so the
existing polled cover switches can probe; keep GPIO_APPLE_D2365=y and
SUSPEND=y for PMU event wake and s2idle. Those latter options were already
enabled. Disable DWARF debug information (DEBUG_INFO_NONE=y) to keep the
local build footprint bounded. Kconfig also refreshes current derived options.
These config choices do not qualify cover wake or suspend on hardware.

## USB host device classes (0.4)

With USB host through the Lightning camera adapter, common USB devices need
their class drivers. Enable as modules: USB Ethernet (USB_NET_DRIVERS=y,
USB_USBNET, USB_RTL8152, USB_NET_AX88179_178A, USB_NET_CDCETHER,
USB_NET_CDC_NCM, USB_NET_CDC_EEM, USB_NET_RNDIS_HOST, USB_NET_AX8817X for
ASIX AX88772 adapters) and USB audio (SND_USB_AUDIO). Kconfig selects MII,
PHYLIB, PHYLINK, AX88796B_PHY (the AX88772's internal PHY), the MDIO helpers,
NET_SELFTESTS, SND_HWDEP and SND_RAWMIDI, and enables USB_RTL8153_ECM. The
default-on minidrivers nobody asked for (USB_NET_NET1080,
USB_NET_CDC_SUBSET, USB_NET_ZAURUS) stay off. RTL8152 needs no firmware; RTL8153/8156 load optional `rtl_nic`
firmware and work without it. Debug information stays off.
