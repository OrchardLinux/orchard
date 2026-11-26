# Changelog

Notable changes to orchard: what a user, a device owner or a contributor would
notice. Refactors, CI and fixes to unreleased work are left out; the Git
history has those.

Add an entry under **Unreleased** in the same pull request as the change. When
releasing, rename that section to the version (`## [0.3.0] - YYYY-MM-DD`). The
release workflow uses that section as the GitHub release notes and fails if it
is missing or empty.

## [Unreleased]

### Devices

- Older 32-bit devices (apple-armv7): the kernel is now Project Cascadia at
  d92da7d (it was 091d441). The kernel now builds for the iPad 2 (Wi-Fi) too,
  with drivers for its display, touch, Wi-Fi, Bluetooth, sensors, battery,
  audio and camera; it becomes a board Orchard builds for once its firmware
  recipe exists. The iPad 3 gains drivers for
  its backlight, battery, Bluetooth, speaker and microphones, also untested
  here. (rzxvx, Simone99, Stephen Cox, Alberto Giancarlo Ferrario)
- iPhone 8 Plus: Wi-Fi works (BCM4357). Tested by ryzensnyper.
- A11 devices: PCIe DMA through the DART works. The T8015 DART is driven
  with its own (T8020-like) register layout instead of the S5L8960X one,
  which it ignores. Needed for Wi-Fi on the iPhone 8 Plus; tested there only.
- iPhone 8 Plus: the battery's remaining charge and energy read correctly
  (they were about 13 times too high).
- iPod touch 6: charges at the current the USB host grants (500 mA by
  default, more when the host allows it) instead of the boot loader's 100 mA,
  so it no longer drains while plugged in. Tested by brkosebay (thanks!).
- A7, A8 and A8X devices: the storage driver no longer sleeps while it hands a
  request to the storage controller, which could warn at boot or stall when two
  programs read at once. Found and fixed by brkosebay (thanks!).
- iPad Air 2: the ambient light sensor works. Both of its sensors get their
  own factory calibration, so the screen brightness can follow the room.
  Found and tested on the Wi-Fi model by ricktav (thanks!).
- iPhone 8 Plus: the Always-On Processor starts, with its firmware from the
  recipe, and serves the accelerometer, gyroscope and barometer; the screen
  rotates with the phone. Both models; tested on the Global one by ryzensnyper
  (thanks!).
- iPhone 8 Plus: the compass reads the magnetic field in microtesla, with
  the unit's factory calibration from the ADT.
- iPhone 8 Plus: the phone no longer locks up when the AOP takes over the
  Touch ID sensor; Linux leaves the AOP's GPIO interrupts to the AOP.
- iPad mini 2 (Cellular): display, touch, storage, Wi-Fi, speakers, charging,
  sleep and wake and USB networking work on an installed Omarchy A. Tested by
  perryraskin (thanks!).
- A8 to A11 devices with Apple's NVMe storage: storage no longer stops for
  30 seconds when status queries (such as udisks at boot) run beside reads.
  It stalled every few seconds under that load on an iPad 6 and could darken
  the start before the PIN screen.
- iPad mini 1 (Wi-Fi): verified all four ARM alignment handlers on a RAM boot
  with internal flash disabled, then removed the Cascadia alignment workaround.
- iPad Air (Cellular): Wi-Fi, speakers, charging with charging control, battery
  status and screen on/off work, and a normal install completes with the
  released kernel. Tested by thisiscamk (thanks!).
- iPod touch 6, iPhone 6 and iPhone 6 Plus: the GPU firmware comes from the
  board's own restore image (iOS 12.5.8; 12.5.1 on the iPhone 6) instead of
  the iPod touch 6's iOS 12.4. The firmware code is the same; only its build
  date differs.
- iPad Air (Wi-Fi): display, backlight, buttons, storage, Wi-Fi, battery,
  charging, suspend and resume, USB serial and USB networking are recorded
  as working, from an installed Omarchy A on main. Touch stays untested: it
  stops after 10 to 40 minutes until the driver is rebound. Tested by ricktav
  (thanks!).

### Install & launch

- Linux: a device that is already plugged in when the installer sets up USB
  access gets the new permissions at once, instead of waiting until it is
  reconnected. Found by perryraskin (thanks!).
- The installer waits up to 60 seconds for Pongo to appear instead of 30. On an
  iPad Air (Cellular) it took 40 to 42 seconds, and the install stopped just
  before. Found and fixed by thisiscamk (thanks!).
- The installer no longer stops over a missing index schema when its Python has
  the jsonschema package installed.
- 32-bit boards: the installer carries the code that makes their payload, which
  it was missing.

### Building

- m1n1 is built from its own repository,
  [OrchardLinux/m1n1](https://github.com/OrchardLinux/m1n1), instead of from
  patch files in orchard. m1n1 changes are pull requests there, against the
  real source; orchard names the commit it builds in `boot/m1n1/upstream.lock`.
  The quiet boot screen is part of that m1n1 now. Kernel changes still go into
  orchard as patch files.
- The scripts recipes and distributions run by path moved to `producers/`
  (`buildenv.py`, `initramfs.py`, `assemble.py`, `cascadia.py`). Their old paths
  and module names still work. m1n1, the static tools and the Cascadia loader
  are built once more, because their keys name the new paths.
- `orchard build`, `inputs`, `install`, `launch`, `devices`, `cache` and the build
  step of `run` can go to orchard2, the Rust tool (docs/tooling.md says what
  differs): `ORCHARD2=1`, or `orchard2 = true` in
  `~/.config/orchard/config.toml`. Off by default.
- `orchard build` and `run` no longer take `--hardware`: a checkout's `bin/orchard`
  builds with that checkout's data and recipes.
- A distribution's build entry is told the tool that runs it as `ORCHARD_TOOL`
  (orchard2 only), and resolves its inputs with `$ORCHARD_TOOL inputs`.

## [0.3.8] - 2026-11-25

### Devices

- iPad Air and iPad mini 2 and 3 (A7): the ambient light sensor, and on
  the iPad Air the Smart Cover. Closing the cover reports a cover switch,
  which Omarchy follows to turn the screen off and on; opening the cover
  does not wake the iPad from sleep yet. The light sensor reports lux for
  automatic brightness; on the iPad Air it is uncalibrated, as its device
  tree has no calibration, and on the iPad mini 2 and 3 it uses the
  unit's factory calibration. The iPad mini 2 and 3 get no cover switch:
  the line Apple names for it reads "closed" whatever the cover does.
  Cover and light sensor tested on the iPad Air (Wi-Fi); the light
  sensor reads light on an iPad mini 2 (Wi-Fi), its calibration is
  untested.

### Install & launch

- The installer waits up to ten minutes for its first write to the
  iPad's storage instead of giving up after two. On some iPads the
  storage first spends minutes on its own housekeeping (186 s measured on
  an iPad Air Cellular), and the install used to stop there with an
  error. Thanks to thisiscamk for finding and measuring it.

## [0.3.7] - 2026-11-25

### Devices

- iPhone 8 Plus: the touchscreen works from the first start and every
  time its driver is reloaded, where it used to come up only on some
  starts and could hang the phone on a reload. Fixed by ryzensnyper
  (thanks!). The hang came from how GPIO interrupts were set up, and that
  fix applies to every board.
- iPod touch 6: battery status. With no fuel gauge in the iPod, the
  charge, voltage, temperature and charging state come from its power
  management chip, estimated the way iOS does; USB power shows too. The
  charge estimate has not been checked against a full charge and
  discharge, and the charge current cannot be set yet. Brought up by
  brkosebay (thanks!). The iPhone 6 and 6 Plus, which have the same chip,
  get its voltage and current readings too; their battery stays with
  their gauge.
- iPad Pro 12.9-inch (2017) (A10X): the touchscreen, internal storage,
  Wi-Fi, Bluetooth, the battery gauge, headphones, both microphones, the
  accelerometer, hardware video decoding and sleep, brought up by tompta1
  (thanks!), now also its four speakers (stereo follows the screen
  orientation), Lightning charging up to 1.5 A, both light sensors, the
  120 Hz display mode and the Smart Cover. About half of the boots still
  stop early.
- iPad Pro 12.9-inch (2017, Cellular) and iPad Pro 10.5-inch (A10X): the
  same hardware as the iPad Pro 12.9-inch (2017), with touch and Wi-Fi
  firmware in their recipes, and its speakers, charging and light sensors.
  Untested.
- iPad 6, iPad 7 and the other A9 and A10 devices: fixes found during that
  work for the coprocessor mailbox, SmartIO audio and the Lightning
  charger's interrupt-storm fallback, shared by all of them.
- iPad mini 1 (Wi-Fi, A5, 64 GB): Linux writes to internal storage and reuses
  freed flash, so the disk no longer runs full once its blank flash is used.
  Writes, block erase and iOS's own bookkeeping survived restarts into iOS
  with both filesystems clean. The flash driver erases on any storage chip
  whose layout it can read and check, not one fixed model; the 16 GB iPad mini 1
  stays read-only until its storage format is modelled.
- iPad mini 1 (Wi-Fi, A5): Bluetooth no longer uses one fixed address
  written into Orchard for every unit. Until the boot stage passes each
  unit's own address, it uses the Bluetooth chip's default, which is also
  the same on every iPad mini 1. Untested.
- iPhone 6s Plus (A9): the phone sleeps when idle or on Power, turns its
  screen off, and wakes on Home or Power.
- iPad mini 2 (Wi-Fi, A7): tested on the board. The display, backlight,
  buttons, internal storage reads, battery, charging, suspend and resume,
  USB serial and networking work, along with touch and Wi-Fi.
- iPhone 6s Plus (A9): the accelerometer and gyroscope work, so the screen
  follows the phone's orientation. The motion processor (AOP) now starts
  with the iOS 15 firmware on every A9 iPhone (6s, 6s Plus, SE), with the
  same sensor orientation; untested on the 6s and SE.
- iPhone 6s Plus (A9): Wi-Fi works, and Bluetooth comes up and scans. The
  iPhone 6s has the same module family and gets both too, with its own
  firmware; untested there. The iPhone SE has a different chip and has
  neither yet.
- iPhone 6s Plus (A9): the loudspeaker plays, through a new driver for its
  CS35L21 amplifier, with the output capped at -12 dB because there is no
  speaker protection yet. The iPhone 6s has the same speaker path and gets
  it too, untested. The earpiece, headphones and microphones need the
  CS42L71 codec, which has no driver yet.
- iPhone 6s Plus (A9): the battery gauge works and the phone charges, from
  a computer at the 500 mA it grants and from an Apple wall adapter at up to
  about 1.4 A while awake. The charger lowers or cuts its input when the
  battery is too cold or hot. Asleep it charges at 300 mA. The iPhone 6s and
  SE share the change, and the iPhone 7 and 7 Plus get the same limit at a
  computer port and the same temperature guard, all untested. RAM boots on
  every device now ask the computer for 500 mA, as installed systems
  already do, so devices whose charger follows the computer's limit charge
  faster during a RAM boot.
- iPhone 6s Plus (A9): the desktop draws on the GPU. Omarchy's compositor
  renders through Zink on the A9's PowerVR GPU, which main already supports
  for every A9 board. The iPhone 6s and SE are untested.
- iPad Air and iPad mini 2 and 3 (A7): internal storage keeps working across
  sleep, also with writes in flight. The storage controller finishes its
  commands before sleep and is restarted after it, and its NAND clock stays
  powered: without it every disk command took seconds after wake, and an
  installed system on an iPad Air could hang at the logo. The A8 and A8X
  devices (iPad Air 2, iPad mini 4, iPhone 6 and 6 Plus, iPod touch 6)
  share the storage driver and get the same sleep handling.
- iPad Air and iPad mini 2 and 3 (A7): Lightning charging draws up to
  1.5 A instead of 1.05 A from a charger that offers it, as measured on an
  iPad mini 2; untested on the iPad Air and iPad mini 3.
- iPad mini 2 and 3 (A7): the speakers play, in stereo, through the same
  CS35L19 amplifier driver as the iPhone 6s Plus's loudspeaker, with the
  output capped at -12 dB because there is no speaker protection yet.
  An iPad mini 2 (Wi-Fi) played both speakers with an earlier version of
  the driver; this one is untested on every model. The headphone jack and
  the microphones do not work yet.
- iPad Air (A7): the speakers play, and the audio controller no longer
  floods the kernel with 100000 unhandled interrupts at boot ("irq 62:
  nobody cared"). No A7 iPad's audio controller uses its interrupt any
  more, as the iPad mini 2's already did; playback never relied on it.
  Thanks to ricktav. Tested on the iPad Air (Wi-Fi).
- iPod touch 6, iPhone 6 and 6 Plus and iPad mini 4 (A8): an installed
  system's desktop finds the GPU on every boot, without a kernel option, and
  a GPU app started on the GPU desktop no longer crashes or takes the GPU
  down with it. On the iPod touch 6 two GPU apps ran next to the desktop with
  no faults; the iPhone 6 and 6 Plus and the iPad mini 4 are untested. If the
  GPU does not draw, the login still falls back to the software desktop.
  By brkosebay (thanks!).

### Install & launch

- An installed system starts in the computer's time zone instead of UTC.

## [0.3.6] - 2026-11-24

### Devices

- iPad 6, iPad 5, iPad Air 2 and the other A8, A9 and A10 boards: the boot
  splash stays still until the PIN screen again. When the display driver took
  over, about four seconds into boot, it flashed a test pattern and then
  went black for a moment. Scanning out GPU buffers directly through the
  display DART is now off by default (adbe.dart_scanout=1 turns it on).

## [0.3.5] - 2026-11-24

### Devices

- The touchscreen restart from 0.3.3 is gone again: on a healthy iPad 6 that
  nobody touched, it restarted the touch controller every 30 seconds.

## [0.3.4] - 2026-11-24

### Install & launch

- iPad 6 and iPad 7: starting an installed system works again. In 0.3.3 m1n1
  crashed while starting the internal storage, and the iPad fell back to iOS.

## [0.3.3] - 2026-11-24

### Devices

- iPad 6, iPad 5, iPad Air 2, iPad mini 2 and 4, iPod touch 6, iPhone 6s,
  6s Plus and SE: a touchscreen that stops responding without an error is
  restarted by itself within seconds, instead of staying dead until reboot.
- iPhone 6, iPhone 6 Plus and iPad mini 4 (A8): internal storage on every
  model, as on the iPod touch 6, so they can install. The iPhone 6 and
  6 Plus also get the iPod touch 6's Wi-Fi and Bluetooth, with their own
  firmware in their recipes. Untested.
- iPad Air 2 (Cellular) (A8X): the speakers and Lightning charging of the
  Wi-Fi model; its recipe now carries the SmartIO firmware the speakers
  need. Untested.
- iPhone 8, iPhone 8 Plus and iPhone X (A11): Wi-Fi on every model, not
  only the iPhone X (GSM), with each model's Wi-Fi firmware in its recipe.
  The iPhone X (Global) also gets the touchscreen and OLED brightness of the
  GSM model, and all of them the side and volume buttons. Untested.
- iPhone 6s, iPhone 6s Plus and iPhone SE (A9): battery status and charging
  through the SN2400 charger, the accelerometer, gyroscope and pressure
  sensor (AOP) and hardware video decoding, as on the iPhone 7. Untested.
- iPhone 7 and iPhone 7 Plus (A10): the iPhone 7 gets the touchscreen,
  Wi-Fi, battery gauge, charger and light sensor the 7 Plus had, and all four
  models get Bluetooth, the accelerometer and gyroscope (AOP), hardware video
  decoding and the SN2400 charger driver, which was not built before. Their
  firmware recipes carry the GPU, video decoder and AOP firmware. Untested.
- iPad 6 (Cellular) and iPad 7 (Cellular) (A10): Bluetooth, as on the Wi-Fi
  models; the Patchram follows the module vendor. iPad 7 (A10): charging
  control is offered as on the iPad 6. Untested.
- iPad Air 2 (Wi-Fi): internal storage works, so Omarchy installs with
  `orchard install` and starts from the iPad with `orchard launch`, next to
  iPadOS. The A8X storage controller (ANS1) has a kernel driver now.
- iPad Air 2 (Wi-Fi): the speakers play, in stereo, through PipeWire. The
  headphone jack and the microphones do not work yet.
- iPad Air 2 (Wi-Fi): it charges. The Lightning charger driver supports the
  iPad Air 2's D2207 PMIC, so a computer's charging port or an Apple adapter
  gets up to 1.5 A instead of iBoot's 100 mA.
- iPad Air 2 (Wi-Fi): sleep works. The iPad suspends when idle or on Power,
  and Home or Power wakes it with Wi-Fi, Bluetooth and the USB network back.
- iPad Air 2 (Wi-Fi): Bluetooth headphones play, and a Bluetooth keyboard with
  touchpad works. Bluetooth runs at 3 Mbit/s instead of 115,200 baud, so
  headphones no longer crackle and calls with the CVSD codec have sound.
- iPad Air (Cellular): new, from thisiscamk's work. Touch, the display and
  backlight, the Home button, USB serial and networking and reading its
  internal storage work, and Omarchy installs next to iPadOS with
  `orchard install`.
- iPad mini 2 (Wi-Fi): the touchscreen works, on the A7 SPI and touch clock
  support from thisiscamk's iPad Air work.
- iPad mini 2 (Wi-Fi): Wi-Fi works when started from RAM (it cannot install
  yet). A kernel transport, from thisiscamk's iPad Air work, brings up the
  A7's HSIC link to the BCM43241 radio, and `orchard-a7-wifi.service` starts
  it at boot.
- iPad Air and iPad mini 2 (A7): Wi-Fi on every model, not only the J72 and
  J85 (the iPad mini 3 lacks firmware: iOS 12.5.8 does not ship its files).
- iPad Air (A7): the speakers play in stereo on every model.
- iPad Air and iPad mini 2/3 (A7): Lightning charging at up to 1.05 A
  instead of iBoot's 100 mA.
- iPad Air and iPad mini 2/3 (A7): the battery level shows on every model.
- iPad Air, iPad mini 2/3 and iPhone 5s (A7): internal storage on every
  model, so Omarchy can install next to iOS, not only on the J72.
- iPad Air and iPad mini 2/3 (A7): the touchscreen on every model.
- iPhone 6s Plus (Samsung): Linux and m1n1 read and write its internal
  storage, so Omarchy installs with `orchard install` next to iOS. The
  display, backlight and buttons work. The Samsung A9's PCIe storage
  controller works like the TSMC A9's without its phy-ip block. From
  ricktav's work.
- iPhone 6s, iPhone 6s Plus (TSMC) and iPhone SE: storage, the touchscreen
  and the light sensor are switched on as on the iPhone 6s Plus (Samsung),
  and the iPad 5 (Samsung) gets storage and Wi-Fi like the TSMC model; not
  tried on these devices yet.
- All A9 iPhones and the iPad 5 (Samsung): the GPU driver and the display
  pipe driver are switched on as on the iPad 5 (TSMC), with the GPU firmware
  from iPadOS 16.7.10; not tried on these devices yet.
- iPod touch 6: internal storage works, so Omarchy installs with
  `orchard install` next to iOS. The touchscreen, backlight, buttons and
  Wi-Fi work, and Bluetooth comes up. From Berkay Kösebay's work.
- The compatibility page opens with a "Does my device work?" table: every
  device Orchard installs on, its chip beside it (Samsung or TSMC for the
  A9), and whether it installs, has a GPU desktop, touch, Wi-Fi, Bluetooth,
  sound, charging, sleep and rotation, and who has tested it. The README
  links straight to it.

### Install & launch

- A launcher may quit right after choosing a boot entry. m1n1 used to wait
  for the computer to read its last answer before starting Linux, so the
  device stayed on the boot splash when the launcher had already gone.
- Orchard's Pongo is built from palera1n's PongoOS, at the revision
  palera1n 3.0.0-beta.4 uses, so the same quiet Pongo also boots iPadOS
  through palera1n with its overlay; with the old one iPadOS came up
  without it and half hung.
- On Linux, `orchard run` and `orchard launch` use the released Pongo again
  instead of asking for a Mac with Xcode: the release lookup picked the
  moving `nightly` tag, which publishes no checksums.
- A7 and A8 devices (ANS1 storage) no longer reset back to iOS when
  `orchard launch` starts a system from internal storage: m1n1 freed its
  storage command queue twice while shutting storage down.
- Installers can talk to the device's install system over raw USB, with no
  SSH, network or keys, and the install finishes on the device even if the
  cable drops after the system image is written (docs/install-protocol.md).
  The graphical installer uses it; serial and SSH stay available.
- `orchard ssh-access` lets this computer sign in over SSH to a system
  installed without SSH access: it signs in once on the device's USB serial
  console with the user's password and adds this computer's keys.
- When the Orchard package repository can't be reached, the install stops
  with an error instead of quietly installing a kernel that then never
  updates through the package manager.
- On Linux hosts that need it, the USB port reset after checkm8 now also
  works with palera1n 3.

### Development

- Orchard Simulator: an iPhone 7 alongside the iPad 6 (Device menu), each in
  a realistic frame. Rotate Left and Right (⌃⌘← and ⌃⌘→) turn the device,
  and the desktop follows through the same rotation-sensor path as a real
  tablet. The frame's buttons no longer take the keyboard away from the
  guest, and the screen comes back by itself after the VM restarts.
- Orchard Simulator: a Scale menu shows the guest at 50 %, 75 % or 100 %
  of the Mac's pixels (100 % is pixel for pixel) or fitted to the screen,
  and keeps the scale across device switches and rotation. Cmd+H and
  Cmd+Option+H hide the simulator and other apps, as on any Mac app.
- Orchard Simulator: a tap on the frame no longer touches the screen; a swipe
  from the frame onto the screen still does.

## [0.3.2] - 2026-11-19

### Devices

- Sleep on the iPad 5, 6 and 7: the Smart Cover and the alarm wake the iPad,
  the screen powers off fully in sleep, and video, display and USB come back
  cleanly after waking.
- Charging: full current under heavy load, from a Mac or a charger. Plain USB
  wall chargers no longer loop between charging and discharging.
- USB to a computer: the connection survives sleep and comes back after a
  replug.
- iPad 7: the GPU-accelerated desktop now works end to end, along with sleep,
  hardware video and Bluetooth audio.
- Bluetooth: A10 iPads load the firmware that matches their Bluetooth module.
- Security keys (FIDO) work.
- Apps built with Bun, such as Claude Code, run on A7, A8 and A9 devices.
- iPad mini 4: display, backlight, buttons, Wi-Fi, battery and the USB console
  and network are marked working, as seen during its bring-up.
- A10 iPads: if the video decoder firmware is missing, sound keeps working and
  the decoder just stays off.
- iPad 6 and 7: the headphone jack works; plugging in switches the sound to
  the headphones, unplugging switches back.

### Install & launch

- Kernel updates come as a package: the installer installs the kernel as
  `linux-orchard-<platform>` from the Orchard package repository with the
  system's own package manager (pacman or apk), so the system's ordinary
  update takes each orchard release's kernel, and its boot entry follows. The
  kernel before stays as a second entry to fall back to.
- Several systems on one device boot like a multi-boot PC: `orchard launch`
  no longer asks, m1n1 starts the default system by itself. Installing a
  system makes it the default (systemd-boot's `loader.conf`, which a system
  can also set). `orchard launch --list` picks an entry with the arrow keys,
  `--entry ID` starts one entry once. If the default can't start, the iPad
  shows why and waits for `--list` or `--entry`.
- `/usr/lib/orchard/boot-entries default` makes this system the one that boots
  when nobody picks.

## [0.3.1] - 2026-11-15

### Devices

- iPad 5 (A9): the GPU runs in the same driver as the iPad 6 and 7, ready for
  testing.
- Sleep: storage and Wi-Fi shut down and come back the way iOS does it (iPad 5,
  6 and 7, iPad Air 2, iPad mini 4).
- Smart Cover: opening it wakes the iPad, and folding it back no longer locks
  it.
- iPad 6 and 7: sleep works again, the screen rotates the right way again, and
  the motion sensors use the unit's factory calibration.
- iPad 5, 6 and 7: the clock can set an alarm.
- iPad 6 and iPad 7: the built-in microphones work (stereo, 48 kHz, with a
  volume control).

## [0.3.0] - 2026-11-12

### Highlights

m1n1 now does the boot work: it reads the boot entries from the device's own
storage, paints the splash and starts the system; the host only picks the
entry. The iPad 6 gets speakers and an accelerometer for screen rotation.

### Devices

- Boot entries: `--via checkra1n` boots through standalone checkra1n instead of
  palera1n, and Orchard now fetches and verifies these tools when needed. A palera1n
  of another version is no longer used unless you ask for it.
- Device data updates: published versions of Orchard now download device data, so
  a new board or a corrected feature state no longer needs a CLI update.
- Device catalog: every A5–A13 iPad and iPhone board is now described from public
  sources, with its boot files and a generated compatibility matrix in
  `docs/compatibility.md`. Orchard doesn't build for the new boards yet.
- Install availability: Orchard now works out whether a board can install from
  what it has, and says why if it can't. The iPhone X (GSM) can now install.
  Feature states are more detailed, for example separate storage read and write.
- iPhone X (GSM, d221): Linux boots from RAM, with a device description and a
  Wi-Fi firmware recipe. Experimental. (Eduardo Martins)
- iPhone X (GSM, d221): the touchscreen works, with its firmware converted from
  the IPSW and its calibration from the device; each phone gets the firmware
  variant it asks for. Brightness works over the panel's DSI command, and buttons
  and battery are confirmed working. (Eduardo Martins)
- 32 more iPads and iPhones (A7 to A11) now have a place in Orchard's kernels.
  Experimental: none has booted with Orchard yet. The iPhone 8, 8 Plus and X
  (Global) can try installing, because a storage driver binds; that doesn't mean
  they'll work.
- iPad mini 4 (J96): RAM-boot bring-up record: desktop, touch, battery and
  native Wi-Fi measured on a local kernel tree. Not a device profile yet. (Tom Ballard)
- iPad mini 4 (J96, J97): touchscreen, Wi-Fi and battery, with a firmware recipe.
  iPhone 6 and 6 Plus: battery. Not yet tested on Orchard's build. (Tom Ballard)
- iPad Air (J85): measured RAM-boot status documented. (Christopher M. Smith)
- iPad 6 (J71b): the buttons are marked working. iPad Air 2 (J81): Bluetooth
  uses the sequence tested on a J81.
- iPad Air 2 (J81): native-root boot, desktop and peripheral status recorded.
  (micro23)
- iPad Air 2 (J81): the touchscreen works, with five fingers and no frame
  errors on main's kernel. (Martin Puškáč)
- iPad 3 (Wi-Fi, J1): the display, touchscreen, Wi-Fi and USB serial and
  network work on Orchard's kernel tree, booted from RAM with the J1 boot kit
  on a jailbroken iOS 9.3.5. That kernel was built with GCC, because the kit's
  loader can't start a clang build yet. Device description, device tree and a
  firmware recipe. Orchard's launcher doesn't run this boot chain yet.
  Experimental. (Stephen Cox)

### Install & launch

- m1n1 reads and starts boot entries itself and paints the entry's splash at
  the start of the boot; `orchard launch` only selects the entry, and the
  initrd no longer paints the splash a second time.
- After an iBoot started over USB, m1n1 releases the NVMe boot-from-host
  strap, so storage works on that boot path.
- `orchard shell`, `exec`, `push` and `pull` also reach an installed system,
  not only the recovery.
- Linux hosts: recognize direct installers and grant USB/serial access throughout
  boot and installed-system development. (Anders Haas Franzen)
- The installer picks the internal disk correctly on iPads with several NVMe
  namespaces.
- An install error says whether anything was written, and what to do next.
- m1n1 shows the distribution's boot splash while installing, at its own size.

### Hardware

- USB is about three times faster on Apple devices: SSH over USB reaches 36
  MiB/s, up from 12 on the iPad 6. A process that pauses reading no longer takes
  down the device's USB network and serial.
- Add experimental PMIC power-off handling for iPad (6th generation, Wi-Fi;
  iPad7,5 / J71b).
- iPhone X (GSM, d221): internal storage through the Apple ANS2 NVMe driver
  (`CONFIG_NVME_APPLE`); the recovery's storage fence loads it, and the
  installer's partition writes are tested. No install yet. (Eduardo Martins)
- iPad 6 (J71b): speakers through the MCA audio path, with an ALSA UCM
  profile. (Dalton Alexandre)
- iPad 5: UCM profile for the speakers and internal microphone.
- iPad 7 Wi-Fi (J171): the speakers play. iPad 6 and 7 Cellular (J72b, J172)
  get the same speakers, untested. (Duke Eagleman)
- iPad 6 (J71b): the always-on processor (AOP) runs as an in-kernel driver,
  replacing the userspace DMA owner; its accelerometer is an IIO device with
  the panel's mount matrix, so desktops can rotate the screen. Based on Dalton
  Alexandre's AOP work.
- A10 (T8010): shared IOP mailbox for the GPU and AOP drivers; the NCO clock's
  boot rate is described per board.
- Apple Wi-Fi follows the regulatory country, so 5 GHz networks connect (iPads
  with Broadcom PCIe Wi-Fi).
- iPad 6 (J71b): sleeps with the GPU desktop and wakes instantly with the Power
  button, touch included. A battery-gauge
  poll that rebooted every HDQ-gauge iPad in sleep is fixed; the other A10
  boards carry the same support, untested.
- A plug or unplug now wakes the tablet, and a wall charger keeps its full rate
  during sleep. Seen on the iPad 6; the iPad 7 and iPad 5 use the same path,
  untested.
- Every board can now suspend. Untested boards may reboot or fail to wake
  instead; please report what you see. Distributions decide when to suspend.
  How to bring it up on another board: docs/bring-up/suspend.md.
- Older 32-bit devices (apple-armv7): the kernel is now Project Cascadia at
  091d441 (it was c20b85e), with Cascadia's iPad 3 driver changes and charger
  fixes. The importer follows Cascadia's A5 tree into its new `devices/a5/`
  folder. (Stephen Cox)

### Development

- RAM payloads built in an orchard checkout take the static tools (busybox,
  dropbear, Wi-Fi tools) from the newest nightly when their recipe matches,
  checked against its SHA256SUMS, instead of compiling them;
  `ORCHARD_PUBLISHED=off` builds them locally. (thisiscamk)

### Contributing

- Pull requests are usually reworked to fit the tree before merging, with the
  author's authorship kept, unless the author opts out (see CONTRIBUTING.md).

## [0.2.0] - 2026-10-31

### Highlights

Orchard is now one repository: the CLI and the hardware support merged into a
single `orchard` that runs from any checkout. The kernel is torvalds' tree with
ordered patch layers instead of a fork, built once per platform for every
device. Orchard owns the platform layer (kernel, devicetrees, modules, initrd),
so a distribution builds one device-neutral root for all devices. A new
installer ships with each release.

### Devices

- iPad 6 (Wi-Fi, J71b) remains the qualified device.
- New, experimental: iPad 6 (Cellular), iPad 7, iPhone 7 and 7 Plus,
  including touchscreen firmware and, for the iPhone 7, Wi-Fi and SmartIO
  firmware.
- New, experimental: iPad 5 (J71t, J72t, J71s, J72s), from Berkay Kösebay's
  port.
- New, experimental: iPad Air 2 (J81, J82) with touchscreen and Bluetooth,
  from Martin Puškáč's A8X port; tested on hardware by micro23.
- New, experimental: iPhone 6 (N61) with SPI and touchscreen drivers, from
  Shayne Legault.
- A7, A8 and A8X build as their own platform (`apple-4k`) next to A9–A11
  (`apple-16k`).
- Every device shows its support status and a feature matrix with caveats
  before installing.

### Install & launch

- Installer v2: a guided install that runs on the device, picks targets from
  the partition layout, sets the account password and a sign-in PIN, imports
  this computer's SSH key, and starts the new system right away.
- Each distribution release carries its own installer, so a release installs
  with the tools it was tested with.
- One recovery image for every device that shares a kernel; it only
  bootstraps and reports status.
- `orchard launch` boots an installed system from its boot entry on the
  device. m1n1 reads storage on A9 and A10. Existing installs keep launching
  with the launcher they were installed with.
- Every installed system offers a USB serial login and SSH.
- Every NVMe namespace except the install target is fenced off during an
  install.

### Hardware

- iPad 6: charges from identified Lightning sources, and at 500 mA from
  unidentified ones.
- iPad 6: touchscreen calibration comes from the device at boot.
- Wi-Fi: brcmfmac fixes for receive buffer mapping and the firmware
  handshake; Wi-Fi P2P is disabled for the iPad 5 and 6 firmware. (Dalton
  Alexandre)
- Kernel: Landlock, FUSE, Bluetooth RFCOMM and UHID (Bluetooth LE keyboards
  and mice) enabled. (Dalton Alexandre)

### Development

- `orchard build` and `orchard run` boot any described device from RAM, with
  USB networking, SSH and file transfer.
- Builds share one cache: each file is stored once, `orchard cache` explains
  misses and `--prune` cleans up. The kernel builds incrementally in one
  tree per platform through ccache, and switching patch sets takes half the
  time.
- Firmware recipes are declarative steps (schema 2). Firmware is read from an
  IPSW over HTTP ranges, including LZSS kernelcaches and the board's device
  tree; `orchard` can explore an IPSW during bring-up.
- Orchard Simulator, an iPad-like VM window on macOS, and `tools/qemu-install`
  for testing a release on a virtual disk.
- Builds work on Fedora SELinux hosts and x86_64 hosts, and Linux hosts find
  RAM-boot USB devices. (Anders Haas Franzen, Josh Slaughter, Dalton
  Alexandre)
- One Pongo for every device, built so Xcode 16.3 and later work.
- New guides: contributing, device bring-up, and reading firmware from an
  IPSW.

### Compatibility

- Python 3.11 or later is required.
- Distributions no longer ship a kernel or run kernel-install; orchard writes
  the boot entry and ships the initrd. A distribution release pins one
  orchard release.
- The 0.1 CLI still launches installs made with it.

### Contributors

Berkay Kösebay, Dalton Alexandre, Shayne Legault, micro23, Anders Haas
Franzen, Josh Slaughter, and the ports by Martin Puškáč (A8X) and Pauli1Go
(A10) that this release integrates.
