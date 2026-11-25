# arcade

Three small games on the device's own screen, in a RAM-only system: a picker,
ARCADE, and VOIDMOTHS (a fixed shooter), HEDGEWICK (a maze chase) and GLINT (a
tilt maze). The same payload runs on the N66 (iPhone 6s Plus) and on the J71
(iPad Air), J85 (iPad mini 2) and J81 (iPad Air 2). Every program looks for
what the device has (screen size, buttons, touchscreen, motion sensor,
speaker) and does without what is missing; nothing depends on the board's
name. Nothing mounts storage, and NVMe, if anything binds it, is set read-only.

## Run it

```sh
cd examples/arcade
src/build.sh                             # builds rootfs/usr/bin/{arcade,hedgewick,voidmoths,glint}
orchard run                              # builds what is missing, then boots
orchard shell                            # the USB root shell stays up
```

`src/build.sh` needs Docker and the `localhost/orchard-hardware-kernel-toolchain:bookworm`
image; it makes static aarch64 binaries, which run on both kernels. To build
the payload without booting:

```sh
orchard build examples/arcade --platform apple-4k    # J71, J85, J81
orchard build examples/arcade --platform apple-16k   # N66
```

`orchard run` adds the booted board's firmware as a second layer
([`orchard.toml`](orchard.toml), `firmware = true`).

## What each board has

From the device trees of the Orchard kernel (`arch/arm64/boot/dts/apple/`),
the kernel configs (`kernel/configs/<platform>/config`) and each board's
`devices/<board>/device.toml`, where the status of each part is kept.

| | J71 iPad Air | J85 iPad mini 2 | J81 iPad Air 2 | N66 iPhone 6s Plus |
|---|---|---|---|---|
| Platform | apple-4k | apple-4k | apple-4k | apple-16k |
| Screen | boot framebuffer, 1536x2048 | boot framebuffer, 1536x2048 | boot framebuffer, 1536x2048 | boot framebuffer, 1080x1920 |
| Buttons | gpio-keys Home, Power, Vol-, Vol+, mute switch | gpio-keys Home, Power (the volume keys are not reported) | gpio-keys Home, Power, Vol+, Vol-; Smart Cover switch (ignored) | gpio-keys Home, Power, Vol-, Vol+, mute switch |
| Touch | apple_z2 on SPI2, its own units 14621x19539, `apple/j72-fwstart.z2fw`; `untested` | apple_z2 on SPI1, 1536x2048, `apple/j85-fwstart.z2fw` | apple_z2, 1536x2048, `apple/j81-fwstart.z2fw` | apple_z2 on SPI2, 1080x1920, `apple/n66-fwstart.z2fw` |
| Sound | "iPad Air Speakers": not used | "iPad mini Speakers": not used | "J81 Speakers": not used | "iPhone Speaker": used, -20 dB |
| Accelerometer | none (no AOP modules on apple-4k) | none | none | AOP, `apple_aop_accel` |
| GLINT | grey, "needs a motion sensor" | grey | grey | playable |

The touchscreen driver is a module on both platforms (`CONFIG_TOUCHSCREEN_APPLE_Z2=m`,
apple-4k `config:2363`, apple-16k `config:2361`); gpio-keys, evdev, simpledrm and
the SPI controller are built in (apple-4k `config:2335`, `:2321`, `:4138`, `:2660`).
The driver applies the tree's axis inversion itself and reports multitouch
protocol B with `INPUT_PROP_DIRECT`.

## Controls

The programs find the buttons by the keys a device reports and the
touchscreen by its axes (`ABS_MT_POSITION_X/Y` and `INPUT_PROP_DIRECT`), never
by name, and map the touchscreen's range onto the picture, letterbox
included. The mute switch is ignored.

| | Buttons | Touch |
|---|---|---|
| ARCADE | Vol+/Vol- choose, Home plays. Without volume keys: a Power tap chooses, Home plays. With one button: a tap chooses, a 0.6 s hold plays. | Tap a game to choose it, tap it again to play. |
| HEDGEWICK | Vol+ turns left, Vol- right (at the next opening), Home: U-turn, Vol+ and Vol- together: pause. Without volume keys it needs the touchscreen. | Swipe: go that way at the next opening; swipe back: U-turn; tap the top bar: pause. |
| VOIDMOTHS | Vol+/Vol- move, Home fires (Power where there is no Home, or a Power tap on a board without volume keys). | Drag: the ship follows the finger and fires; tap: fire. |
| GLINT | Tilt; on the title Vol+/Vol- choose the board, Home levels and starts; Home pauses. Without a sensor Vol+/Vol- turn gravity. | Tap: level the board and start; swipe left/right on the title: choose the board; tap the top: pause. Without a sensor the marble rolls towards the finger. |
| Screen off (all four) | Power: the screen goes dark. Home or Power lights it again. Without volume keys: hold Power 0.8 s (a shorter press keeps its meaning above). | Ignored while the screen is dark. |
| Back | Hold Home 2 s: to the title; again on the title: to ARCADE. | Hold the top left corner (the small wedge) 2 s: the same. |

The first touch picks the touchscreen, and the title screens show touch hints
once one is present. Without a touchscreen nothing changes.

### Screen off

Power darkens the screen the way it does in Omarchy, in the picker and in
every game, through one piece of code (`src/backlight.h`):

- Off: `4` into `bl_power` of every device under `/sys/class/backlight/`
  (found by listing the directory, whatever its name). A device without
  `bl_power` gets `brightness` 0; its old value is kept in memory and in
  `/run/arcade-backlight`, and written back when the screen comes on.
  Nothing else in sysfs is touched (never `max_brightness`).
- While dark: no game tick (no deaths, the clock stands), nothing drawn, the
  sound card closed (the picker's idle card closes too), the motion sensor
  not read; the touchscreen is read and thrown away; the program sleeps on
  the buttons and looks at `/run/notice` twice a second. All other presses
  are swallowed, so nothing moves in the dark.
- Home or Power lights it again (that press does nothing else), and so does
  a new notice. A game that was in play stays paused until a press (or a
  tap): HEDGEWICK and GLINT show their pause screen, VOIDMOTHS "PAUSED".
- On every way out (end, SIGTERM, SIGINT, a crash signal) the backlight goes
  back on; each program also lights it when it starts, the picker after each
  game returns, and `arcade --restore-tty`, which init runs after every run
  of the picker, after a SIGKILL. The picker's footer names the key.

Which key, per board (decided at run time by the keys the device reports,
not by the board's name):

| Keys found | Boards | Screen off | Power otherwise |
|---|---|---|---|
| Home, Power, volume | N66, J81; J71 if its volume keys report | a Power press | nothing else (VOIDMOTHS no longer fires with Power) |
| Home, Power, no volume | J85 (volume not reported) | Power held 0.8 s | a tap (released before 0.8 s): ARCADE next game, VOIDMOTHS fire, HEDGEWICK as before |
| Only one of Home and Power, or no backlight device | none known | none | unchanged (Power stands in for Home) |

The log (`/run/arcade.log` and the games' logs) has a line per change:
`screen: off (Power): <device> bl_power=4`, `screen: on (Home) after 12.3 s
dark, 0.004 s CPU (0.03 % of one core)`, and at the end a count
(`screen: offs= ons= longs= taps= eaten= dark_s= dark_cpu=`).

## Firmware

`firmware = true` adds the booted board's firmware files as a second
initramfs. It starts nothing by itself: the kernels have no uevent helper
(`CONFIG_UEVENT_HELPER_PATH=""`), [`init`](init) does no coldplug, and every
firmware user is a module. Only the modules init loads read any of it:

| Board | Files in the layer | Used |
|---|---|---|
| J71 | `apple/j72-fwstart.z2fw`, `brcm/brcmfmac43241-j71-usb{,.murata}.bin`, `apple/s5l8960x-smartio.bin` | the touchscreen's |
| J85 | `apple/j85-fwstart.z2fw`, `brcm/brcmfmac43241-j85-usb{,.murata}.bin`, `apple/s5l8960x-smartio.bin` | the touchscreen's |
| J81 | `apple/j81-fwstart.z2fw`, `brcm/brcmfmac4350c2-pcie.apple,rieslinga*` (16 files), `brcm/BCM.apple,j81.hcd`, `apple/t7000-smartio.bin` | the touchscreen's |
| N66 | `apple/n66-fwstart.z2fw`, `apple/aop-s8000{,-ext}.bin`, `apple/s8000-smartio.bin` (the speaker's DMA), `apple/gfx-s8003.bin`, `apple/vxd-deva.bin`, `brcm/brcmfmac4350c2-pcie.apple,pinotnoir*`, Bluetooth | touchscreen, AOP, SmartIO |

Wi-Fi, Bluetooth, the GPU and the video decoder stay off; their files only
cost initramfs size. The firmware comes from Apple's restore image, is kept in
Orchard's private cache and never lands in this directory.

## What init does

- Buttons: gpio-keys is built in; if no `gpio-keys` device shows up, it loads
  `gpio_keys` and `evdev`.
- Touchscreen: if the tree has a `touchscreen0` alias whose node is enabled,
  `modprobe apple_z2`. Log: `/run/touch.log`.
- Motion sensor: only with an enabled `apple,s8000-aop` platform device (the
  N66), `modprobe apple_aop apple_aop_accel`. Log: `/run/motion.log`.
- Sound: only when `/sound-speaker` is the card "iPhone Speaker" (the N66), the
  loudspeaker's modules. The iPads' cards are never brought up, and the
  programs open no card but "iPhone Speaker", at most -6 dB (-20 dB by
  default). Log: `/run/sound.log`.
- Storage: every 5 s for a minute, any `nvme*` block device is set read-only.
- USB serial shell, USB networking and SSH (`/usr/libexec/orchard/usb-session`).
- The picker in a respawn loop on tty1; without a framebuffer, VOIDMOTHS in
  text mode. `touch /run/game.stop; killall arcade` frees the console.
- `reboot`, `poweroff` and `halt` work from the USB shell.

`notice TEXT` (e.g. `orchard exec -- notice "step 3 done"`) shows a line on
the screen for 6 s.

## Tests

`src/test.sh` runs every headless test in the toolchain image, under
AddressSanitizer and UBSan, and ends with `ALL TESTS PASSED`:

- `test-maze.sh`: ARCADE and HEDGEWICK on a 1080x1920 framebuffer;
- `test-voidmoths.sh`, `test-glint.sh`: the two other games, with sound on a
  fake ALSA card;
- `test-ipad.sh`: the touch code (`touch_test.c`), recorded touch streams
  (`touchrec.py`: tap, swipe, drag, long press, in the J71's units and in
  pixels) in all four programs, 1536x2048, 1080x1920 and 2048x1536 screens,
  no motion sensor, missing buttons, the iPads' speaker cards, and `init` on
  fake device trees of all four boards.

Screen off is tested in all four suites on a fake sysfs (`$FAKE_BL_ROOT`,
with `$FAKE_KEYS` scripting the buttons in time; test builds only, the phone
builds are checked to carry neither): Power off and on in each program, Home
wakes, touch ignored while dark, the game frozen while dark and paused
after, Power held 0.8 s on a board without volume keys, the sound card
closed and reopened, nothing drawn and near-idle CPU while dark in real time,
a notice lighting it, SIGTERM, fatal signals and SIGKILL plus
`--restore-tty` or a restart, a backlight with `brightness` only, the picker
lighting a screen a game left dark, `max_brightness` left alone.

## Tested

| Board | Result |
|---|---|
| N66 iPhone 6s Plus | RAM boot (apple-16k): all four programs with the buttons, the touchscreen and the tilt sensor; sound at -20 dB; screen off and on in each program. |
| J81 iPad Air 2 | RAM boot (apple-4k): picker and games with the buttons and the touchscreen, GLINT grey, the Smart Cover ignored, `/run/sound.log` "silent", screen off and on in each program. |
| J71 iPad Air, J85 iPad mini 2 | Not booted yet; the tests cover their trees, keys and touch units headless. |

## First boot

On every board: `src/build.sh` first, then `orchard run` from this directory.
After the boot, from the host: `orchard exec -- cat /run/touch.log /run/arcade.log`.

1. Put the device in DFU mode and connect it to the host. On the iPads, enter
   DFU from Recovery rather than from a running iPadOS.
2. `orchard run`.
3. ARCADE shows three cards; GLINT is grey without a motion sensor (on the
   N66 it turns live by itself within about 10 s, once the AOP is up). The
   footer names the buttons and the touchscreen that were found:
   `VOL+/- CHOOSE ... POWER SCREEN OFF` with volume keys, `POWER NEXT ...
   HOLD POWER SCREEN OFF` without them, and a `TOUCH:` line.
4. Buttons: Vol-/Vol+ move the selection and skip GLINT where it is grey;
   Home on a card starts the game. The mute switch and the Smart Cover do
   nothing.
5. Touch: tap a card, tap it again; in VOIDMOTHS drag the ship (it must
   follow the finger: a wrong axis direction shows up here); in HEDGEWICK
   swipe in all four directions and tap the top bar. Hold the top left
   corner 2 s twice: back to ARCADE.
6. Screen off: press Power (hold it on a board without volume keys) in the
   picker and in each game: the screen goes dark and taps do nothing. Home
   or Power lights it again, the game paused until a press. On the N66 the
   sound stops while dark, and in GLINT the marble must not have moved.
7. `orchard exec -- cat /run/touch.log /run/sound.log /run/motion.log` and
   `orchard exec -- grep screen: /run/*.log`: the touchscreen and firmware
   found, "silent" on the iPads, the backlight device and the CPU share
   while dark.

On the J71 the touchscreen is `untested`; report whether the ship follows
the finger.
