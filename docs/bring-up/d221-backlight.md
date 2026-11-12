# D221 (iPhone X) brightness, buttons and battery

Measured on one d221 in RAM boots, 2026-11-05.

## Brightness

The iPhone X panel is OLED: there is no DWI backlight controller, which is why
`apple_dwi_bl` writes were accepted without effect. The ADT's `backlight` node
points its `backlight_update` and `backlight_enable` functions at the `lcd`
node (`lcd,summit`) under `mipi-dsim`, with `backlight-level-encoding = 11`,
`DBVMin = 139`, `DBVMax = 2047` and `override-boot-brightness = 740`. iOS sets
the level with the MIPI DCS command `0x51` sent to the panel.

The DSI host at `0x218400000` is a Synopsys DesignWare MIPI DSI 1.31a
(`VERSION = 0x3133312a`). iBoot leaves it in video mode (`MODE_CFG = 0`) with
low-power commands enabled (`VID_MODE_CFG = 0x0100bf03`); its last command was a
three-byte long write (`GEN_HDR = 0x329`). A DCS long write through the generic
command FIFO reaches the panel between frames: the payload into `GEN_PLD_DATA`
(0x70), then `0x339` into `GEN_HDR` (0x6c), with `CMD_PKT_STATUS` (0x74) showing
the FIFO empty again.

**The panel takes the level low byte first**: `51 DBV[7:0] DBV[10:8]`. Sent in
the big-endian order of the DCS specification, 2047 reads as 1799 and 139 as
768, close to the boot level, so only the maximum seems to work. Tested
variants that change nothing: `53 20` and `53 2c` (write control display) before
the level, the generic long write type `0x29` in the same order, and the short
write `15 51 xx`.

`apple_dsi_bl` sends the level this way and registers a backlight from 0 to
2047. Levels below `apple,min-dbv` are raised to it, so 0 is the dimmest, not
off; blanking the backlight (`bl_power` 4) sends 0, which darkens the panel until
it is unblanked. On a white screen, stepping through the driver: 139 is well below the boot
level, 2047 well above, and 400, 740 and 1400 fall in between.

## Buttons

The ADT's `buttons` node names SMC keys (`bVUP`, `bVDN`, `bHLD`, `bRIN`), and
`macsmc-input` on `main` already reports them: volume up and down, the side
button as `KEY_POWER` and the ring/silent switch as `KEY_MUTE`, one press per
change of position.

## Battery

`macsmc-power` on `main` reports the battery and the charger: status,
capacity, voltage, current, temperature, health and cycle count (this unit: 1031
cycles, 2355 mAh full charge). `charge_now` reads negative.

## Not working

- 3D Touch: the touch controller's `luna` interface sends nothing, even under
  firm presses; iOS must enable it.
- Suspend: `s2idle` is entered (USB drops) but the side button does not wake
  the phone.
