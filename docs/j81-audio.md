# J81 (iPad Air 2) audio bring-up

Working notes for speaker and headphone audio on the iPad Air 2 (J81, A8X /
T7001). Status: **both speakers play from a RAM boot**; the installed system
and the headphone codec are not tested yet. `devices/j81/device.toml` keeps
`audio_output` and `audio_input` at `missing` until the installed system plays.

Sources:
- Apple's device tree `Firmware/all_flash/DeviceTree.j81ap.im4p`
  (sha256 `3021ec21…a48e`, as pinned in `devices/j81/boot.json`).
- The kernelcache `kernelcache.release.ipad5b` (19H422, sha256 `69120a77…9f59`;
  matches the BuildManifest digest). Both are read from the pinned public
  IPSW by byte range.

## Hardware (from the ADT)

| Block | ADT | Notes |
| --- | --- | --- |
| SmartIO | `/arm-io/sio`, `iop,s5l8960x`, reg `0x20ae00000` | DMA parent of every MCA and of spi1 |
| MCA0 | `0x20a0a0000`, SIO endpoints 0x28/0x29 | headphone/mic codec |
| MCA1 | `0x20a0a4000` | voice |
| MCA3 | `0x20a0ac000`, SIO endpoints 0x2e/0x2f | **speakers**; TDM, 8 slots (like J71b) |
| MCA4 | `0x20a0b0000` | Bluetooth |
| I2S switch | `0x20a003000`, `i2s-switch,t7001` | same class as S8000 |
| Speaker amps | 2× MAX98721 on i2c1, 0x31 and 0x34 | IRQ GPIO 8 / 9; `function-alive` GPIO 101; calibration in syscfg `SpCl` |
| Headset codec | CS42L81 on spi1 (`0x20a084000`) | CS GPIO 157, IRQ GPIO 116 |

The MCAs report `mca-version` 1. The J81 Linux DT already has the PMGR
power states (`ps_sio`, `ps_sio_p`, `ps_mca0`–`4`, `ps_spi1`, `ps_i2c1`) and a
disabled i2c1 node; SIO, the I2S switch, spi1, the MCAs, the codec, the amps
and the sound card are missing.

## What the A9/A10 audio path gives us, and what is new

The J81 is closest to the S8000/T8010 path (SmartIO DMA + MCA), not to the A5
(Cascadia) path.

**SmartIO firmware.** `com.apple.driver.AppleT7000SmartIO` carries it in
`__DATA,__firmware` (0x788e0 bytes, sha256 `5fc96ee7…4bd3`, a 32-bit ARM
image, `KF-SIO`). `orchard.apple.smartio.extract()` already extracts it; the
recipe needs the bundle in `smartio.BUNDLES` and a
`smartio-from-kernelcache@2` step in `devices/j81/firmware.json`. The message
protocol matches A9/A10 (ready message op 0x6d, protocol version 3).

**SmartIO registers (new work).** The J81's SIO is an A7 IOP version 1 (iOS:
AppleA7IOPV1), while S8000/T8010 use version 2. `apple-sio-core` needs a V1
register backend. The J81 ANS (also V1, already working through `asp`) shows
part of the layout (remap registers 0x08–0x1c, CPU control 0x28, mailbox at
+0x1000); the queue, interrupt-status and timebase registers are not known
yet.

**MCA v1 (new work, small).** iOS's `AppleAE2Audio` handles MCA versions 0, 1,
4 and 5 in one driver. Its v4 register offsets match `s8000-mca.c`; its v0/v1
offsets match the A5 Cascadia `s5l-audio.c` map (for example TX_CFG 0x18,
RX_CFG 0x2c, FRAME 0x0c; FIFO size register 0x04 reads 0x80). Plan: a v1
register map inside the `s8000-mca.c` framework.

**MAX98721 amps (new work).** iOS's `AppleMAX98721Amp` uses 8-bit registers
that match the upstream MAX98925 map. Init, as iOS does it:
- FORMAT `0x20` = 0x57 / 0x97 / 0xd7 (16/24/32-bit, TDM, one bit of delay).
- MAP `0x11`–`0x19` = `c0 00 00 f0 00 ab 89 00 00`; `0x22`–`0x26` = 0; `0x2b` = 2.
- Per amp from the ADT `speaker-config`: `0x2c`, `0x2e`, `0x2f`, `0x30`–`0x35`, `0x37`.
- Enable: `0x36` = `0xb0` | per-amp bits, then `0x38` = 0x80, wait 1.5 ms.

Base: upstream `max98925.c` with a new compatible, the MAX98721 revision ID
(unknown yet; upstream accepts 0x51/0x80), the ADT values and TDM slots.

**NCO (new work, small).** The ADT's `/arm-io/nco` (`nco,t7001`,
`nco,s5l8960x`) is the block the T8010 driver variant already handles (iOS:
`AppleS5L8960XNCO`, same control bits). The only difference is the channel
stride: iOS reads `pmgr-nco-page-size` and falls back to 0x1000, and the J81
ADT has no such property. Its window is 0x5000 bytes, so five channels 0x1000
apart; MCA0, 1, 3 and 4 use channels 0, 1, 3 and 4 (`function-mclk_frequency`).
The reference rate is in `/arm-io` `clock-frequencies`, which the IPSW ADT
leaves zero for iBoot to fill in; it has to be read from the live ADT (m1n1
exposes it as MTD `adt`). A running J81 reports 342857142 Hz (2.4 GHz / 7)
at index 10 (clock-id 0x10a minus 0x100), the rate iOS special-cases for
precision.

**I2S switch.** iOS binds `AppleAE2I2SSwitch3` to `i2s-switch,s5l8960x`, the
layout `s8000-mca.c` programs. MCA3's route is `aud3` to `pin3`.

**Headset codec.** CS42L81 has a driver on the A9/A10 path; the J81 MCA0
record starts like J71t's.

## Plan

1. **Read-only probe** (no audio driver): enable i2c1 with no clients, read the
   amps' revision register `0xff` and dump `0x00`–`0x3a` at 0x31 and 0x34.
   Read PMGR power states for MCA0/MCA3/SIO, and only for powered blocks the
   MCA version/FIFO registers and SIO CPU control. No writes.
2. SmartIO firmware recipe for the J81.
3. SmartIO V1 register backend; SIO reaches "ready".
4. MCA v1 and the DT nodes; validate the TDM stream with the amp outputs off.
5. MAX98721 driver; low-gain `speaker-test`, one amp at a time.
6. Headphones and microphones through CS42L81.

## Status

| Piece | State |
| --- | --- |
| SmartIO firmware (`smartio-from-kernelcache@2`, `AppleT7000SmartIO`) | done |
| SmartIO V1 register backend (`apple,t7000-sio`) | works on a J81: firmware boots, setup completes, 4 DMA channels |
| MCA v1 (`apple,t7001-mca`, MCA3 speaker link) | plays on a J81: TDM 8×16 in a 256-bit frame, bit clock from NCO channel 3 (24.576 MHz at 48 kHz) |
| MAX98721 driver | both amplifiers play on a J81 (0x31 slot 0, 0x34 slot 1) |
| NCO (`apple,t7001-nco`, 0x1000 channel stride) | written, builds |
| DT: SIO, I2S switch, MCA3, NCO in `t7001.dtsi` (disabled); SmartIO enabled on the Air 2 | written |
| NCO reference rate | 342857142 Hz (live ADT `clock-frequencies[10]`); NCO, I2S switch and MCA3 enabled on the Air 2 |
| DT: amplifiers and sound card (`audio-graph-card2`, two codecs on one link) | card "J81 Speakers" registers and plays; parsing the `multi` node logs OF refcount warnings in audio-graph-card2, to be fixed |

## Speaker safety

iOS protects the speakers in software (amp current/voltage feedback plus the
`SpCl` calibration); Linux will have neither at first. Use the ADT's hardware
limiter and boost settings, start with low gain, test one amp at a time, and
check the stream with the speaker outputs disabled first: a wrong TDM slot or
format can produce full-scale noise (J71b's plain-I2S attempt gave loud
static).

## Test log

| Date | Step | Result |
| --- | --- | --- |
| 2026-11-19 | ADT and kernelcache analysis (offline) | hardware above identified; no device access |
| 2026-11-19 | SmartIO firmware recipe | `t7000-smartio.bin` extracted, sha256 matches |
| 2026-11-19 | SmartIO V1 backend, MCA v1, MAX98721 driver (offline) | build as modules for apple-4k; patch audit passes for apple-4k and apple-16k; not run on hardware |
| 2026-11-20 | Live ADT read from Omarchy (read-only) | NCO reference 342857142 Hz |
| 2026-11-20 | RAM probe: amplifiers (read-only) | both answer at 0x31/0x34, revision 0x80; registers hold iOS's MAP values, global enable (0x38) and channel enables (0x36) off |
| 2026-11-20 | RAM probe: SmartIO | firmware boots; first build failed setup with -EBUSY: the send FIFO stays disabled on V1 (TX status 0x00020000) until the AP enables it; fixed in the V1 backend |
| 2026-11-20 | RAM probe: NCO and MCA3 | both bind; MCA version register reads 0x00010002 (version in the upper half on v1), check fixed |
| 2026-11-20 | RAM boots with the speaker DT | 4 of 5 freeze within ~1-12 s of /init (no console output, no USB), before any audio driver loads (they are blacklisted). Bisected by enabling one node at a time, one boot each: main kernel, +I2C1+SIO, +NCO, +I2S switch all boot; +MCA3 freezes. On a running system MCA3 can be powered (PMGR 0x20e020148) and its registers read (0x00010002) without a hang, so the suspect is how the boot treats the enabled MCA3 node: with a consumer that never probes, genpd keeps the domain in its boot state (sync_state pending) |
| 2026-11-20 | NCO variant and DT nodes (offline) | NCO layout from iOS's `AppleS5L8960XNCO`; patch audit passes for apple-4k and apple-16k; not run on hardware |
| 2026-11-20 | RAM boots with the speaker DT, DFU entered from Recovery | 2 of 2 boot normally; PMGR at handoff: MCA0-4 `0x200` (off), SIO on. The earlier freezes all followed DFU entered straight from iPadOS, so they may be the same bootchain problem rather than MCA3; open until more boots confirm it |
| 2026-11-20 | First playback (SmartIO firmware loaded by hand, modules loaded in order) | 440 Hz at -20 dBFS (-32 dBFS after the driver's attenuation) heard from amp0 at gain 8; 660 Hz in channel 1 heard from amp1. During playback: hw_ptr advances at 48000 frames/s, MCA3 TX_CFG 0x109 and FRAME 0x0f107007 (running), no underflow; i2s-switch aud3/pin3 routed with output enables; both amps global enable 0x80, blocks 0xb0, no status flags. A -30 dBFS tone at gain 4 was inaudible |
| 2026-11-20 | Stereo test (portrait, Home button down) | channel 0 / amp0 (0x31, slot 0) is the bottom-left speaker, channel 1 / amp1 (0x34, slot 1) the bottom-right; both together play cleanly |
| 2026-11-20 | Lesson | reading MCA3 offset 0x64 (not a v1 register) resets the SoC; read only the registers the driver names |
