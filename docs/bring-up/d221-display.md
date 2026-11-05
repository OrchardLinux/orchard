# iPhone X (d221) display

What is known about the A11 (`t8015`) display on the iPhone X (`d221`): what
works on main, what breaks the screen, and what the display block looks like
for whoever writes the driver. Measured on one `iPhone10,6`, RAM boots over
USB, November 2026.

## State

| Piece | State |
| --- | --- |
| Console on the panel | **works** on main through simpledrm on the boot framebuffer |
| ADBE (`drivers/gpu/drm/adbe`) on T8015 | not in main; adopting the pipe works, flips do not (below) |
| Backlight (`dwi_bl`) | writes are accepted and read back, the panel does not change |

### How it was tested

Main at `993751e`, a RAM payload with a USB serial shell and the default
bootargs (`console=tty0 loglevel=7 rdinit=/init`), booted with
`orchard run --no-build <payload> --device d221 --yes` from an x86_64 Arch host
on a USB-A port:

- `fb0` is `simpledrmdrmfb` (`Initialized simpledrm … for 8b1657d80.framebuffer`),
  1125x2346, 30-bit `x2r10g10b10`.
- The kernel log stays on the panel, and text written to `/dev/tty0` from the
  USB serial shell shows up on it.
- `pm_genpd_summary`: `mipi_dsi`, `disp0_hilo`, `disp0_ppp`, `disp0_be` on;
  `dp` off.

## Do not give `disp0` the DisplayPort domain

A `disp0` node modelled on the T8010 iPads (power domains `ps_disp0_fe`,
`ps_disp0_be`, `ps_dp`) and enabled on the d221 turns the screen black a moment
after boot:

- the panel shows the early console, then goes dark;
- `pm_genpd_summary` shows `mipi_dsi off` (also `disp0_hilo`, `disp0_ppp`);
- white written straight into the boot framebuffer (`devmem`, outside System
  RAM) does not appear, and backlight writes change nothing: the panel link is
  down, not the buffer empty.

Why: `framebuffer0` in `t8015-common.dtsi` lists `ps_disp0_be`, `ps_mipi_dsi`,
`ps_disp0_hilo` and `ps_disp0_ppp`, and simpledrm holds them. A driver that
adopts the pipe retires simpledrm through the aperture helpers, which releases
those domains. If the adopting node asks for `ps_dp` instead of `ps_mipi_dsi`,
nothing holds the DSI link and genpd powers it off. The iPhone X panel (`lcd`,
`summit`) sits under the ADT's `mipi-dsim` (Synopsys, 2 lanes), not a
DisplayPort link. This is the same class of problem `devices/t8010/0015` deals
with on the A10 iPhones.

Proof boots on the d221 with the ADBE series below (same kernel, bootargs only):

| Bootargs added | `mipi_dsi` | Panel |
| --- | --- | --- |
| none (ADBE adopts the pipe) | off | early text, then black |
| `pd_ignore_unused clk_ignore_unused initcall_blacklist=adbe_platform_driver_init` | on | console stays |
| `initcall_blacklist=adbe_platform_driver_init` | on, held by simpledrm | console stays |

A future `disp0` node for T8015 should therefore take
`<&ps_disp0_fe>, <&ps_disp0_be>, <&ps_mipi_dsi>, <&ps_disp0_hilo>, <&ps_disp0_ppp>`
and stay disabled on the d221 until the driver can flip.

## The T8015 display block

The A11 display is not the H8/H10 pipe of S8000/T8010. It is a newer block with
a Cortex-M3 firmware, driven in iOS by the kext `AppleMobileDispH10P`
(personality `AppleADBE0-8970X`, `IONameMatch disp0,t8015`). The A9/A10
offsets (`ADP_SPDS` 0x1014, `ADP_FORMAT` 0x402c) read unrelated values on
T8015.

Registers decoded from that kext (offsets in the `adp` window at
`0x218000000`, size `0x2a6000`):

| Offset | Meaning |
| --- | --- |
| `0x45004` | pipe state; bits 6:4 clear when a swap completed |
| `0x45818` | interrupt status (write-1-to-clear) |
| `0x4581c` | interrupt enable (`0xc0180c08` when armed) |
| `0x46020` | version; the kext expects `0x42` (accepts `0x41`) |
| `0x4602c` | frame ID, `0x1000 \| id << 8` (M3 run modes only) |
| `0x50000 + L*0x8000` | layer L block |
| `0x50004` | layer control, bit 31 = enabled |
| `0x5001c` | layer format word |
| `0x50020 + 4p` | plane p base (bus address), masked with `~0x3f` |
| `0x50028 + 4p` | plane p stride, written before the base |

The format word decodes as
`(v[27:26]<<16) | (v[25:24]<<12) | ((v & 0x6000)!=0)<<8 | (v[23]<<4) + 0x10`,
then a table lookup: ARGB `0x11010`, r03w `0x1010`, a04w `0x2010`, 8a3b
`0x3020`, BGRA `0x11110`.

Read on the device as iBoot leaves it: `0x46020 = 0x42`,
`0x50004 = 0x80000001`, `0x5001c = 0x03dc8001` (code `0x3020`, 8a3b: 30-bit
2:10:10:10 plus an 8-bit alpha plane, matching `x2r10g10b10`),
`0x50020 = 0xb15f4000`, `0x50024 = 0xb2084000`, `0x50028 = 0x11c0`. The plane
base is a bus address: DRAM sits at bus 0, so the boot framebuffer at physical
`0x8b15f4000` reads as `0xb15f4000`.

## ADBE on T8015: adoption works, flips do not

An experimental ADBE series (binding compatible `apple,t8015-display-pipe`,
adoption from `0x46020`/`0x5001c`/`0x50004`, a flip path writing
`0x50028`/`0x50020` with a software vblank, an XRGB8888 format word
`0x055c8001` for the console) adopts the pipe and registers `adbedrmfb`. But
neither register holds a CPU write:

```
T8015 layer format 0x3dc8001 -> 0x55c8001 (reads 0x3dc8001)
T8015 first flip: base 0xb15f4000 -> 0xe39c280 (register does not hold it, reads 0xb15f4000),
  stride 0x11c0 -> 0x11c0, status 0x300600 -> 0x300600, state 0x1 -> 0x200
```

while the pipe state word moves (`0x45004`: `0x1 -> 0x200 -> 0x400`). The
likely reason is that the layer registers are shadowed and take effect only on
a commit the driver does not issue, or that the M3 firmware owns them. Finding
that commit (or the M3 interface) in the kext is the next step for a T8015
driver; the console does not need it.

## Backlight

`dwi_bl` probes on the d221 and exposes `/sys/class/backlight/232200080.backlight`
(0..2047). A write reads back (511 -> 511), but with a white framebuffer,
writing 0 and then 2047 changes nothing, and a later boot left at
`brightness = 0` kept the panel lit. The register is the DWI command register,
not the OLED's level; the real control is elsewhere.
