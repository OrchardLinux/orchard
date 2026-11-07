# P105 firmware

## What the recipe makes

`devices/p105/firmware.json` (iOS 8.4.1, 12H321) has four outputs:

| Output | Reads | Fingerprint |
|---|---|---|
| `P105.mtprops` | root image | Cascadia's, matched |
| `brcm/brcmfmac4334.bin` | root image | new (placeholder address), measured |
| `brcm/BCM4334B0.hcd` | root image (`BlueTool`) | Cascadia's, matched |
| `apple/sgx543.fw` | IMG3 kernelcache | Cascadia's, matched |

Every output reads an encrypted image, so none can be made before the
launcher has written the keys ([key file](../firmware-recipes.md#legacy-image-keys)).
Touch calibration (`mtcal.bin`) is per unit and travels through
`orchard mixin`.

## Verification (2026-11-07)

One extraction of all four outputs by byte range from Apple's server, with
a stand-in key file in the launcher's place (filled from Project Cascadia's
published keys, kept only on the host):

- touch, Bluetooth and GPU firmware matched Cascadia's fingerprints;
- the Wi-Fi firmware made with Cascadia's old address reproduced
  Cascadia's fingerprint exactly, so the step is right; with the placeholder
  `02:00:00:00:00:00` it is the pinned `f1fb3a66…`;
- traffic: 35.8 MB of the 1.5 GB IPSW, in 384 s (one cold run, no cache).

That run found one step bug: Apple's `borg.trx` header length counts 4,012
bytes past firmware and NVRAM slot; the step now takes `offsets[0]` bytes of
firmware, as Cascadia does.

The reader handles the IPSW's compressed ZIP prefix followed by stored
DEFLATE blocks, encrcdsa v2, UDIF, HFS+ and IMG3 (AES, LZSS), with no new
dependency. Code adapted from rzxvx's Project Cascadia is credited in the
modules and `NOTICE`. Generated fixtures cover each layer; no keys or Apple
files are in the repository.

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
