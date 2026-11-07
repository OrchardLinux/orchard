# iPad mini 1 (Wi-Fi), P105

The first board of the apple-armv7 platform (Apple A5, Project Cascadia's
port on Linux 6.12). Device data from Shayne Legault's P105 bring-up
(OrchardLinux/orchard#10, shaynelegault/hardware 407c910).

## Firmware

`firmware.json` pins the iOS 8.4.1 (12H321) IPSW and the measured inputs, and
produces nothing yet (`outputs` is empty): orchard cannot read this IPSW's
encrypted root filesystem, and the steps below are not in its recipe
vocabulary yet. What the board needs, as Shayne's recipe recorded it:

| Destination | Made by | SHA-256 |
|---|---|---|
| `lib/firmware/P105.mtprops` | copy of `P105.mtprops` | `516b8db5f6144ad64698867b8f6e6209a49010fcff1cb103539fe5ea4ffb1390` |
| `lib/firmware/brcm/brcmfmac4334.bin` | `cascadia-trx-add-nvram` (scripts/trx-add-nvram.py) from `borg.trx`, `borg-t-st.txt` | `c02d3308434b7e2251bb2e235362e31a6033a4bc909c030888e8f9085afec5f4` |
| `lib/firmware/brcm/BCM4334B0.hcd` | `cascadia-bluetool-borg-tdk` (scripts/extract-bt-firmware.py) from `usr/sbin/BlueTool` | `b6791a67141f1a16447e916237180a049b89dfaab9c38451b33864dbb6a50dda` |
| `lib/firmware/apple/sgx543.fw` | `cascadia-sgx543-firmware` (scripts/extract-sgx-firmware.py) from the IPSW member `kernelcache.release.p105` | `5f929f351459b169ed91a68dce086dbc682b639c4a31846ef5b12837562e9a77` |

These were prepared with Cascadia's own workflow (https://github.com/rzxvx/Project-Cascadia.git at
`c20b85ebb951`, `scripts/build-firmware.sh`; the root filesystem is
`058-24036-023.dmg` of the IPSW, read by `scripts/rootfs-extract.py`), from
the IPSW members `058-24036-023.dmg`, `kernelcache.release.p105`, `Firmware/dfu/iBSS.p105.RELEASE.dfu`, `Firmware/dfu/iBEC.p105.RELEASE.dfu`.

The touch calibration is per unit and private: `lib/firmware/mtcal.bin`, the
unit's syscfg `MtCl`, captured from its jailbroken iOS. It is never part of a
public recipe or release.

## Open work

- Storage: the NAND driver (OrchardLinux/orchard#86, #87) is in
  `kernel/patches/armv7/mtd`. The iOS disk translation layer and its append
  writes (#89, #91) follow on top of it. Reclamation (#93) is unfinished:
  until it lands, writes stop once the blank flash blocks are used up.
- A Linux root on LwVM: the kernel reads LwVM partitions
  (`kernel/patches/armv7/block`), but the shared initrd finds its root by
  reading a GPT itself and needs to learn LwVM partitions.

## Unmerged code

- #85's per-patch platform scopes (007a95b, also carried by #86/#87):
  reverted; 32-bit patches live in `kernel/patches/armv7`.
- #87's `MTD_APPLE_H2FMI_WRITE` build option (fa59001): removed, programming
  is part of the driver.
