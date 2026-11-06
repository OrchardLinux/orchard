# D221 (iPhone X) touchscreen

The iPhone X touch controller is not a Z2 controller. iOS drives it with
`AppleHIDTransportSPI` (ADT `hid-transport,k1`), not `AppleMultitouchSPI`: it
has an HBPP bootloader, like the iPad 7's, then speaks Apple's HIDSPI, the
transport of the M1 MacBook keyboards. `apple_hidspi_ts` drives it. Everything
below was measured on one d221 (personality `C1R1FC0,2`) in RAM boots,
2026-11-05, first in userspace (spidev, i2c-dev, uinput), then in the kernel.

## Hardware

| Part | Where | Notes |
| --- | --- | --- |
| SPI | `spi1`, `0x22e404000`, IRQ 292 | ADT `spi-1,samsung`, spi-version 1, as T8010; iBoot leaves its pins muxed |
| Chip select | GPIO `0x8a` (138) | a GPIO, high when idle |
| Interrupt | AOP GPIO 21 | low while a packet is pending |
| Reset | GPIO `0x21` (33) | active low; iBoot holds the controller in reset |
| Clock | PMGR `0x78000` | the T8010 touch clock block; ADT `TCLK(8, 100, 24000000)`, so divider 1 from the 24 MHz reference |
| Supply | Acorn display PMU, I2C 0x2a on `i2c3`, register `0x05` | see below |

The ADT's multi-touch node gives `function-power_ana = dpLE(0x6)` and
`function-power_pulldown = dpLE(0x40)` on the Acorn, and a `power-sequence` of
4-byte steps whose third byte is the value at power-on: pulldown 0, power_ana 1,
then `clock_enable` with 10 ms. iOS's `AppleAcornDisplayPMU` also writes the
ADT's init value `0x09` to register 0x05 when it starts. Without those bits the
controller stays silent: `0x05 = 0x0f` brings its bootloader up (status register
0x04 reads 0x0f and the interrupt falls), `0x46` and `0x06` do not.

## Firmware load (HBPP 0x105, SPI mode 3)

The firmware is `Firmware/D221_Multitouch.im4p` from the IPSW: an MTFW with
five personalities; the unit's ADT `hid-merge-personality` picks one (m1n1
passes it on, see "Firmware variants" in [firmware-recipes.md](../firmware-recipes.md)). Its
items are replayed in order (`hidspi-from-mtfw@1` converts them into
`lib/firmware/apple/d221-touch-<personality>.bin`, an op list the driver reads): the
bootloader, vectors and boot configuration, three DCS images through the
controller's four FIFOs, the multi-touch calibration, a poll, the command list,
the 230 KB `d22_racer.bin`, ten 3D Touch calibrations, a final poll and the core
reset release. The packets are the iPad 7's (`z2fw.encode_blob`,
`z2fw.register_packet`); blobs answer `4bc1`, masked writes `4ad1`.

- The NOP probe (`1a a1` then `18 e1` seven times) answers HBPP words
  (`1f01 4879 …`); errors read `4f81`.
- Status words are `0x4879 ^ ((m | m << 4) << 3)`: m=5 write ACK, 7 blob ACK,
  8 read reply (`4c39`), 15 error.
- A register read (`1c 73`, address, sum) is answered with `4c39`, the value
  (BE16 low half, then the high bytes) and the BE16 sum of those four bytes
  (iOS `0xfffffff0059216b8`). Without iOS's CS-to-clock delay the reply can
  arrive shifted by a word and a bit; the driver looks for it at every offset.
- Reads in mode 0 come out shifted by one bit; the bootloader wants mode 3.

## HIDSPI (SPI mode 0)

The firmware's config says mode 0, CS Delay 200 µs, Inter Transaction 250 µs,
Transfer Length 512, Padding Length 4. Each transfer is 4 bytes of padding
(`ef be ad de`) and a 512-byte packet framed as in Asahi's `spi-hid-apple`:
flags (0x20 read, 0x40 write), device, offset, remain, length, data, and a
CRC-16 (ARC) of the first 510 bytes. Writes are padded the same way, then
followed, 200 µs later, by an 8-byte read whose last four bytes are the
status `ac 27 68 d5`.

- After the load the controller sends device 0xd0, data `a0 80 00 00` (Asahi's
  "booted").
- Asahi's enumeration requests (`20 01 d0`, `20 02 i`, `20 10 i`) are answered
  empty; the HID descriptors of the two interfaces (0 `grape`, multitouch; 1
  `luna`, 3D Touch) are in the MTFW's config instead.
- A get-report (`32 d9 00`) on interface 0 returns the surface descriptor:
  LE32 width and height in 0.01 mm (6232 × 13495) and the sensor's edges
  (x −218..6003, y −220..13270).
- Touches come unasked on interface 0 as input reports `10 44` (report 0x44,
  the iPhone 7 Plus' touch report) with `apple_z2`'s finger records: the
  count at byte 16 of the payload, the fingers from byte 24, states 3 and 4
  for a finger down. About 120 reports a second.
- Back-to-back transfers without the 250 µs gap make the controller drop back
  to its bootloader within a second (every read then returns `4f81`); the
  driver keeps the gap and reloads the firmware if it happens anyway.

## Calibration

The calibrations are per unit and live in the ADT's multi-touch node.
m1n1 copies them through the `touchscreen0` alias: `multi-touch-calibration`
as `apple,z2-cal-blob` and the ten `luna-*` under their ADT names; the op list
names the property, so the firmware file is the same for every unit. This
unit's ADT has no `luna-shape-graffix-calibration`; the controller keeps its
default.

## Tested

- Kernel driver, payload with the firmware in `/lib/firmware`: the load
  succeeds on the first try, the surface descriptor is read, and taps over
  the whole panel report with the right orientation (corners near
  (128, 170), (125, 2365), (1099, 2413), (1105, 82) on 1125 × 2436).
- No CRC errors over minutes of use.

## Open

- The `luna` (3D Touch, interface 1) reports are ignored.
- Only `C1R1FC0,2` has run on a device. The recipe also installs `C1R1FC0,1`
  (which the IPSW's device tree names) and `C1R1FC0,2-W3`, converted the same
  way but untested. The two `-AdcDump` personalities, diagnostic builds going by
  their name, aren't installed: a unit asking for one has its touchscreen off,
  and the log names the variant.
- No suspend or resume: the controller stays powered while the driver is bound.
