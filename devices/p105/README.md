# iPad mini 1 (Wi-Fi), P105

The first board of the apple-armv7 platform (Apple A5, Project Cascadia's
port on Linux 6.12). Device data from Shayne Legault's P105 bring-up
(OrchardLinux/orchard#10, shaynelegault/hardware 407c910).

## Firmware

`firmware.json` pins the iOS 8.4.1 (12H321) IPSW and makes four outputs.
All of them read encrypted images, with keys the launcher caches from the
device ([key file](../../docs/firmware-recipes.md#legacy-image-keys)). The
pinned hashes are Cascadia's measurements; no extraction with real keys has
run yet ([P105 firmware](../../docs/bring-up/p105-firmware.md)).

| Destination | Made by | SHA-256 |
|---|---|---|
| `lib/firmware/P105.mtprops` | copy of `P105.mtprops` | `516b8db5f6144ad64698867b8f6e6209a49010fcff1cb103539fe5ea4ffb1390` |
| `lib/firmware/brcm/brcmfmac4334.bin` | `trx-add-nvram@1` (after Cascadia's scripts/trx-add-nvram.py) from `borg.trx`, `borg-t-st.txt`, placeholder address `02:00:00:00:00:00` | pending verification |
| `lib/firmware/brcm/BCM4334B0.hcd` | `bluetool-hcd@1` from `usr/sbin/BlueTool` | `b6791a67141f1a16447e916237180a049b89dfaab9c38451b33864dbb6a50dda` |
| `lib/firmware/apple/sgx543.fw` | `sgx543-from-kernelcache@1` (Cascadia's container format) from the IPSW member `kernelcache.release.p105` | `5f929f351459b169ed91a68dce086dbc682b639c4a31846ef5b12837562e9a77` |

Cascadia's hashes were prepared with its own workflow (https://github.com/rzxvx/Project-Cascadia.git at
`c20b85ebb951`, `scripts/build-firmware.sh`; the root filesystem is
`058-24036-023.dmg` of the IPSW, read by `scripts/rootfs-extract.py`), from
the IPSW members `058-24036-023.dmg`, `kernelcache.release.p105`, `Firmware/dfu/iBSS.p105.RELEASE.dfu`, `Firmware/dfu/iBEC.p105.RELEASE.dfu`.

The touch calibration is per unit and private: `lib/firmware/mtcal.bin`, the
unit's syscfg `MtCl`, captured from its jailbroken iOS and supplied through
`orchard mixin`. It is never part of a public recipe or release.

The Wi-Fi firmware carries a placeholder address; the platform layer gives
each iPad its own at boot ([Wi-Fi address](../../docs/bring-up/p105-firmware.md#wi-fi-address)).

## Open work

- Storage: the NAND driver (OrchardLinux/orchard#86, #87), the iOS disk
  translation layer and its append writes (#89, #91) are in
  `kernel/patches/armv7/mtd`; the disk is `applesftlN`, on the 64 GB model
  only. Reclamation (#93) is unfinished: until it lands, writes stop once
  the blank flash blocks are used up.
- A Linux root on LwVM: the kernel reads LwVM partitions
  (`kernel/patches/armv7/block`), but the shared initrd finds its root by
  reading a GPT itself and needs to learn LwVM partitions.
- USB: the cold-handoff topic (#90) awaits a device boot of its exact
  revision, and the iPad 3 (J1) a regression boot of the shared PHY code;
  see [USB handoff](../../docs/bring-up/p105-usb-handoff.md).

## Unmerged code

- #85's per-patch platform scopes (007a95b, also carried by #86/#87):
  reverted; 32-bit patches live in `kernel/patches/armv7`.
- #87's `MTD_APPLE_H2FMI_WRITE` build option (fa59001): removed, programming
  is part of the driver.
- #90's gadget config change (39b60b1, built-in CDC gadget off): not taken
  yet; it waits until 32-bit boots bind their own configfs gadget.
- #89's read budget and lifetime module parameters (ffa74e8, source
  e7d118292) and its module build (a607325): dropped; the reader is built in.
- #91's `MTD_APPLE_SFTL_WRITE` option and `write_enable` parameter (e049bb4):
  removed, writing is part of the driver.
