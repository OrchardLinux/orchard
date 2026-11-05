# J72 USB Wi-Fi firmware preparation

This is the first host-side component of the J72 Wi-Fi contribution. It
supplies working conversion code, not an Orchard J72 boot profile. The J72
HSIC PHY, radio power/reset ownership and USB driver adaptation still need
integration into Orchard's kernel. No existing device descriptor is changed
or marked working by this contribution.

## Measured configuration

One iPad Air 1 Cellular (`iPad4,2`, J72AP, A7) has working Wi-Fi with the
contributor's Linux 7.0.12 development kernel and RAM payload. The original
prototype is [published with source and tests](https://github.com/thisiscamk/ipad-air-j72-linux/tree/ae9fa9d/code/wifi).
It remains outside Orchard's applied patch series.

| Item | Observed value |
| --- | --- |
| Transport | Internal EHCI / HSIC, USB high speed |
| USB vendor/product | `0a5c:bd1c` in ROM and running-firmware paths |
| ROM chip/revision | `0x4324`, revision 6 (brcmfmac's BCM43241) |
| ROM RAM/remap base | `0x90000` bytes / `0x60000000` |
| Apple firmware family | `4324b5/dureza`, USI board settings `dureza-u-kk.txt` |
| Tested firmware request | `brcm/brcmfmac43241-j72-usb.bin` |
| Firmware image format | Uncompressed TRX, version/flags `0x10020`, appended NVRAM |

The chip revision is a ROM measurement, not inferred from Apple's filename.
Do not substitute BCM43241 SDIO firmware or another board's NVRAM. The
owner's actual unicast MAC must be supplied: no reference or default MAC is
usable for this radio. Keep regulatory and calibration values from the
matching original board file; the converter does not set a country or
change radio limits.

On the development image, private firmware startup/re-enumeration, `wlan0`,
scan, WPA2 association, DHCP, DNS, Wi-Fi-bound HTTPS and pinned-key Wi-Fi SSH
passed. Disconnect/reconnect and DHCP renewal passed. The owner confirmed
Wi-Fi alongside touch, menu and keyboard; RAM-only desktop operation also
survived external USB removal. These are development-image results, not
results from a build of this Orchard branch. Bluetooth, suspend/resume and
other devices' USB radio variants remain unqualified.

## Private conversion

The new command runs offline and accesses no iPad. Supply files captured
or extracted by the owner; it does not download firmware or recover a MAC:

```sh
orchard wifi-firmware \
  --firmware /private/wifi/4324b5/dureza.trx \
  --nvram /private/wifi/4324b5/dureza-u-kk.txt \
  --mac-file /private/owner-wifi-mac.bin \
  --output /private/j72-wifi-mixin
```

`--mac-file` is **six raw bytes**, not colon-separated text. Keeping it in a
file avoids putting the address into shell command history or process
arguments. The output directory must not exist; its parent must already
exist. The command creates the directory with mode 0700 and files with
mode 0600. Do not publish the output or any input, including its per-unit
metadata. On an I/O failure, a partial output directory may remain private;
inspect it and choose a fresh directory before retrying.

Output uses Orchard's existing data-only private mixin format:

- `mixin.tar.gz`: `mixin.json` and
  `lib/firmware/brcm/brcmfmac43241-j72-usb.bin` only.
- `mixin.json`: the local archive hash/size receipt.
- `prepared/firmware/`: the converted private file used to make the archive.

The inner metadata uses profile `j72`; that names this preparation contract,
not a registered device. `orchard run --device j72` and automatic firmware
extraction are not provided by this change. Existing PCIe recipes and their
ADT-based MAC provisioning remain unchanged. This legacy USB protocol needs
the owner's MAC in the appended NVRAM; that per-unit output cannot have a
single public recipe hash.

The converter checks the original TRX magic, exact length, flags, CRC,
firmware extent, jump offset, absence of existing NVRAM and zero-only padding.
It preserves the firmware payload, strips board-file comments, validates
ASCII key/value entries, refuses duplicates and a conflicting MAC, pads to
four bytes, appends the complemented word-count token, then updates the TRX
length/NVRAM extent/CRC. Both input and output fit the observed ROM RAM bound.

## Validation of this contribution

```sh
PYTHONDONTWRITEBYTECODE=1 python -m unittest discover -s tests -v
```

`tests/test_brcm_usb.py` uses synthetic firmware, board data and a synthetic
MAC. It covers malformed headers, CRCs, extents, prepacked input, padding,
NVRAM/token bounds, MAC validation, input file types, overwrite refusal,
private modes, archive contents and the CLI's lack of device discovery.

The submitted converter was also run on the owner's real private inputs.
Its output and the file extracted from its private mixin matched **every
byte** of the firmware used by the working J72 radio. No firmware was
uploaded or driver replaced during that check. The new CLI/mixin wrapper
has not been cold-booted through Orchard. Keep the PR in draft until the
applicable exact-revision integration/device gates are met.

## Remaining kernel integration

The tested prototype establishes requirements for a future native patch:

1. Attach the owning power domains before EHCI binds. Model the HSIC PHY,
   reset, HOST_READY and DEVICE_READY through normal driver/DT ownership,
   retaining exclusive GPIO ownership through firmware re-enumeration.
2. Add the exact USB identity and revision-specific firmware mapping, using
   compatible/match data rather than a machine check or development module
   parameter. Ensure the `brcmfmac-wcc` vendor helper is available before
   the first radio bind.
3. Use `wValue=1` for the measured ROM commands, validate complete transfers
   and ROM identity, include the appended NVRAM extent in the download,
   bound download progress and CRC/length checks, then handle firmware
   startup, HOST_READY cycling and USB reset/re-enumeration. The prototype
   skips the incompatible ROM reset-config path and drains the firmware's
   interrupt notification endpoint.
4. Stop through the owning EHCI driver's synchronized shutdown path and
   verify halt before releasing DMA, IRQ, GPIO or power resources. The
   prototype's initial direct-stop attempt was unsafe and was replaced;
   copying its older observer is not a production integration strategy.
5. Build and cold-test the exact Orchard revision, including the matching
   recovery USB closure, firmware startup, networking, cleanup and error
   paths. Existing shared USB brcmfmac devices need regression testing if
   their behavior changes. Do not enable a device feature based on host
   tests alone.

These steps are intentionally not implemented by installing development
overlay modules. Their source, tests and measured cleanup evidence remain
on the contributor's fork for porting and attribution.

## Provenance

The strict conversion algorithm comes from the device-tested
[J72 packer](https://github.com/thisiscamk/ipad-air-j72-linux/blob/ae9fa9d/code/wifi/usb-firmware/j72_pack_wifi_trx.py).
Its TRX/NVRAM layout follows Project Cascadia's
[`scripts/trx-add-nvram.py` at c20b85e](https://github.com/rzxvx/Project-Cascadia/blob/c20b85ebb951c704d7e3a664c378dde8276e3928/scripts/trx-add-nvram.py),
which packages NUL-separated board settings and the complemented length token
for Broadcom USB download. Cascadia's P105/BCM4334 hardware is a transport
reference; it is not evidence of J72 GPIOs, power addresses or firmware.
