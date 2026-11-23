# Changelog

Notable changes to orchard: what a user, a device owner or a contributor would
notice. Refactors, CI and fixes to unreleased work are left out; the Git
history has those.

Add an entry under **Unreleased** in the same pull request as the change. When
releasing, rename that section to the version (`## [0.3.0] - YYYY-MM-DD`). The
release workflow uses that section as the GitHub release notes and fails if it
is missing or empty.

## Unreleased

### Devices

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
