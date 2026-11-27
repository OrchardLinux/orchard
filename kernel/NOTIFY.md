# What to tell contributors

The running list of what other projects need to hear from us: fixes to
offer, deviations from their trees, and findings that affect their devices.
Add each item when the change is made, and remove it once it has been sent
and answered. Fixes are named by the subject of their commit in OrchardLinux/linux.

## Nick Chan (Hoolock)

- DART S5L8960X fault handler NULL dereference. Fix:
  "iommu: apple-dart: Don't dereference a master config in the S5L8960X fault handler".
- The S5L8960X fault message prints the IOVA after the window mask, so a
  fault at 0xbffff170 reads as 0x3ffff170. The older driver printed
  `addr | dma_min`. This is only an observation; it misled our Wi-Fi
  debugging.

## Paul Praschl (pauli1go: J171/J172, D10/D11/D101/D111)

- **Affects all his T8010 devices on the current Hoolock base:**
  `t8010.dtsi` gives the PCIe DART windows with `apple,dma-range`. Hoolock
  6831bc70's DART driver no longer reads that property; it expects the
  window in the five-cell specifier. Without it Wi-Fi faults on its first
  DMA (seen on J71b), and the NVMe DART loses its window too. Fix:
  "arm64: dts: apple: t8010: Describe PCIe DART windows in the IOMMU specifier".
- H9P `apple,manual-availability-iommu` was parsed with `#iommu-cells` and
  broke on five-cell DARTs. It now parses one fixed stream cell, and the
  binding describes the cells. Fix: "PCI: apple-h9p: Parse manual-availability DART with a fixed stream cell".
- ANS: release the FlatDMA slot before unmap ("nvme-pci: apple-ans: Release the FlatDMA slot before unmapping its pages").
- SmartIO: take RX interrupts only while the coprocessor is up
  ("soc: apple: sio: Only take RX interrupts while the coprocessor runs").
- H9P: the System-RAM overlap check.
- bq27540 entry ("power: supply: bq27xxx_hdq: Add the BQ27540 found in iPad 5 and iPad 6"), and the question of whether
  values are served from cache only.
- apple_z2: J71b is a new variant on his J172 paths. Like J172, it signals
  firmware-ready twice. ("Input: apple_z2: Add iPad 6 (J71b) touchscreen support", `0002`)
- The iPad DT layout now follows mainline. `t8010-ipad6.dtsi` describes the
  iPad 6 board and `t8010-ipad7.dtsi` overrides it. The WLAN UART
  platform-control block moved into `t8010.dtsi` as a disabled SoC node.
  J171/J172 DTBs are unchanged apart from label names and the new
  `wifi0`/`bluetooth0` aliases. ("arm64: dts: apple: t8010: Describe the iPad 6 board; derive the iPad 7")
- We are reworking his layer toward mainline practice (see REVIEW.md,
  "Mainline-practice fixes for the imported pauli1go layer"). This is for
  information; we maintain it, nothing is expected from him.
- brcmfmac RX buffers are now mapped bidirectionally for everyone
  ("wifi: brcmfmac: msgbuf: Map RX buffers for device reads too"): on J71b the BCM4355C1 firmware reads them
  during association. It is likely the same on J172 (same chip).
- Planned changes to his code, once done: the board-named
  `apple,j172-pcie` compatible, the `"XX"` antenna SKU, one Wi-Fi
  calibration mechanism, and the apple_z2 DT tuning properties.

## Berkay Kösebay (J71t: hardware PR #6, PR #8, cli PR #2)

- Nothing to send yet. The PRs are under our own review and are not adopted
  as they are (see REVIEW.md, "Unverified sources").

## linux-wireless (brcmfmac)

- Don't cancel reset work that was never initialized
  ("wifi: brcmfmac: Don't cancel reset work that was never initialized").
- Select Apple NVRAM by OTP when there is no antenna SKU
  ("wifi: brcmfmac: Select Apple NVRAM by OTP when there is no antenna SKU").
- msgbuf: map RX buffers for device reads too ("wifi: brcmfmac: msgbuf: Map RX buffers for device reads too").

## linux-bluetooth (hci_bcm)

- iPad 6 BCM4355C1 data and quirks ("Bluetooth: hci_bcm: Add iPad 6 (J71b) BCM4355C1").
