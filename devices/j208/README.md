# iPad Pro 10.5 (Cellular), J208

Hardware identity is in [`device.toml`](device.toml); measured bring-up
facts are in [`docs/bring-up/j208.md`](../../docs/bring-up/j208.md). This
file tracks what needs testing, open work, and contributed code that is not
in the tree yet.

## Needs testing

Nothing J208-specific is in the kernel series yet. The mainline
`t8011-j208` device tree (SoC, framebuffer, buttons) is what Orchard would
build; a RAM boot of `examples/hardware-check` with it would confirm
display and buttons.

## Open work

From the review of #42. Each needs a test on a J208:

1. Wi-Fi: `pcie-apple-h9p` T8011 support through match data and the
   device tree (port 2 and its tunables), without machine checks or DMA test
   gates.
2. Wi-Fi: the BCM4355 rev 10 (`14e4:43dc`) Apple profile in brcmfmac
   through the existing OTP and board selection, without J208 checks in
   shared code.
3. A J208 device tree beyond mainline's (`arm64: dts: apple: t8011-j208`):
   the PMICs, touch SPI device, PCIe Wi-Fi, UART5/HDQ battery and I2C3
   light sensors used in the RAM sessions.
4. Touch: an `apple_z2` J208 variant that reports input (the RAM driver
   only captured frames), plus its binding.
5. Backlight: a WLED cell in the Dialog PMU driver (`040-dialog`) for the
   D2400 registers, instead of direct writes.
6. Power: the D2400 LDO13 and D2401 WLAN GPIO1 in the existing PMU
   regulator driver. (The RAM module used mask `0x19` while writing 0 or 1,
   which clears bits 3–4; check that.)
7. Battery: a bq27xxx chip/profile entry for the BQ27540 behaviour seen
   here (remaining capacity at `0x10` answers, NAC at `0x0c` does not),
   instead of a runtime table swap.
8. Light sensor: an IIO driver for the I2C3 parts (`0x29`, `0x39`; the
   register map looks TCS34xx-like, check against `tcs3472`).
9. SmartIO: an `apple,t8011-sio` compatible, without the firmware-size gate.
10. A firmware recipe from iPadOS 17.7.11 (21H461).
11. Shared SPI word-size setting: the RAM patch changed
    `APPLE_SPI_T8010_DESC_WORD_BITS << word_index` to
    `word_index << APPLE_SPI_T8010_DESC_WORD_BITS` in `spi-apple`. That
    also changes J71b touch (8-bit setting `0x0f` → `0`). Check on J71b
    before any change.

## Unmerged code

PR #42 brought the drivers from the J208 RAM desktop sessions under
`devices/j208/drivers/`. They are not in the tree: the patches are bare diffs
against Hoolock `6831bc701` that don't apply to the current series, they
gate shared drivers on `apple,j208`, and several modules write PMIC
registers directly. Everything is preserved in the contributor's commit
`a8d54e3`, tagged [`contrib/vividmuse-j208`](https://github.com/OrchardLinux/orchard/tree/contrib/vividmuse-j208/devices/j208):

| Path at the tag | Contents |
| --- | --- |
| `drivers/patches/0001` | SmartIO `t8011-sio` compatible, firmware-size gate |
| `drivers/patches/0002`, `0003` | SPI word-size shift (shared), J208 PIO timing |
| `drivers/patches/0004`, `0005` | PCIe port 2 for Wi-Fi with T8011 tunables (21H461), WLAN UART provider |
| `drivers/patches/0006` | brcmfmac BCM4355 profile (worked: WPA2 association) |
| `drivers/patches/0007` | `apple_z2` J208 frame capture (diagnostic, no input device) |
| `drivers/modules/backlight/` | WLED gate and level (D2400 `0x307`–`0x309`, `0xb00`/`0xb01`) |
| `drivers/modules/battery/` | UART5/HDQ overlay, bq27xxx register-table swap |
| `drivers/modules/als/` | I2C3 enable overlay, identity, config and sample readers |
| `drivers/modules/pmic/` | D2400 LDO13 and D2401 GPIO1 regulator stubs |
| `ram-kernel.config` | the RAM kernel's resolved configuration |
| `tests/test_j208_drivers.py` (repo root) | checks of the layout above |

Get a file with `git show contrib/vividmuse-j208:devices/j208/drivers/patches/0006-j208-brcmfmac-profile.patch`.
