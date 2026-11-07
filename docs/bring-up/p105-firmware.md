# P105 firmware

## What the recipe makes

`devices/p105/firmware.json` (iOS 8.4.1, 12H321) has four outputs:

| Output | Reads | Needs keys | State |
|---|---|---|---|
| `P105.mtprops` | root image | root image | hash pinned (Cascadia's) |
| `brcm/brcmfmac4334.bin` | root image | root image | hash pending: the placeholder address changed |
| `brcm/BCM4334B0.hcd` | root image (`BlueTool`) | root image | hash pinned (Cascadia's) |
| `apple/sgx543.fw` | IMG3 kernelcache | kernelcache | hash pinned (Cascadia's) |

Every output reads an encrypted image, so none can be made before the
launcher has written the keys ([key file](../firmware-recipes.md#legacy-image-keys)).
With the keys, `bin/orchard build` for P105 verifies the three pinned hashes;
`firmware.measure('p105', …)` gives the Wi-Fi hash to pin
([pending outputs](../firmware-recipes.md#structure)). Touch calibration
(`mtcal.bin`) is per unit and travels through `orchard mixin`.

The reader handles the IPSW's compressed ZIP prefix followed by stored
DEFLATE blocks, encrcdsa v2, UDIF, HFS+ and IMG3 (AES, LZSS), with no new
dependency. Code adapted from rzxvx's Project Cascadia is credited in the
modules and `NOTICE`. Generated fixtures cover each layer; no real keys or
Apple files are in the repository.

A bounded live read of the pinned IPSW (2026-11-07) reached byte 4,000,000
of the root image after fetching 198,284 bytes. A cold index walks about
89,000 stored-block headers; the full extraction's time and traffic are not
measured yet.

## Wi-Fi address

The Wi-Fi firmware carries the placeholder `02:00:00:00:00:00`. The platform
layer's udev rule replaces it at boot, before the interface comes up, with a
stable locally administered address derived from the device tree's
`serial-number` (`system/defaults/usr/lib/orchard/wifi-address`). That is a
userspace step because Linux 6.12's brcmfmac reads no address from the
device tree for this USB-attached chip.

P105's boot stage does not pass a serial number yet (the Cascadia loader
hands over Orchard's static DTB), so until it sets `/serial-number` as m1n1
does on 64-bit boards, P105 gets a random address on every boot. Untested on
hardware.
