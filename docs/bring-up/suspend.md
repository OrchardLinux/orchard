# Suspend (s2idle): what a board needs and how to prove it

How Linux suspend-to-idle was made to work on the iPad 6 (J71b, T8010) on
2026-11-04, and how to repeat it on another board. The evidence log is
`examples/suspend-check/RESULT.md` (local, not committed).

## What suspend needs on these SoCs

Linux suspends to idle (`/sys/power/mem_sleep` = `[s2idle]`): user space
freezes, drivers suspend, the CPUs idle until a wake interrupt. There is no
firmware sleep state. Four things must hold on every board:

1. **A wake source that survives.** The Power and Home buttons wake through
   the AP GPIO controller (`gpio-keys`, wake enabled). If PMGR powers the GPIO
   domain down, no wake interrupt arrives.
2. **Nothing that cannot come back loses power.** Today the display pipe
   (adopted from iBoot: timings and panel link the driver cannot program from
   scratch) must keep its domains; without that, the deeper PM tests leave the
   screen blank. A domain can go off once its driver restores what it held: the
   A10 GPU driver stops and restarts its firmware over sleep, so `ps_gfx` has
   no entry.
3. **No driver touches suspended hardware.** Periodic work must stop between
   the freezer and resume: queue it on `system_freezable_wq`, or cancel it in
   the driver's suspend callback. Buses without `dev_pm_ops` for their
   devices (1-Wire/HDQ, for instance) only have the first option.
4. **A cpuidle driver with an `enter_s2idle` state.** Without one the idle loop
   never reaches the s2idle path: it looks like a working suspend, but the tick
   keeps running and timekeeping is not suspended, so `CLOCK_MONOTONIC` counts
   the sleep. On wake, after any sleep longer than 3 minutes, systemd kills
   logind, userdbd and timesyncd for missing their watchdogs; the end of the
   sleep dies with logind, and NetworkManager stays asleep with Wi-Fi off.

## T8010 (A10) as done

All in the kernel, nothing in user space:

| What | Where | Why |
|---|---|---|
| `ps_gpio` always-on, manual PM | `t8010-pmgr.dtsi` | button wake |
| `ps_disp0_fe`, `ps_disp0_be` always-on, manual PM | `t8010-pmgr.dtsi` | adopted display pipe |
| `ps_dp` (iPads) / `ps_mipi_dsi` (iPhones, iPod) | `t8010-ipad6.dtsi` / `t8010-7.dtsi`, `t8010-n112.dts` | the panel link each family drives |
| `apple_cpuidle` lists every Apple SoC here | `drivers/cpuidle/cpuidle-apple.c` | tick and timekeeping stop in s2idle |
| bq27xxx poll on `system_freezable_wq` | `drivers/power/supply/bq27xxx_battery.c` | the HDQ gauge poll reset the SoC in sleep |

`apple,always-on` keeps a domain on while awake too, so this keeps state
instead of saving power in suspend. Each entry goes once its driver saves and
restores its own state. Sleep current has not been measured.

Storage and Wi-Fi were tested here with the PCIe ports left up over the sleep.
That has since been replaced, see
[Storage and PCIe over sleep](#storage-and-pcie-over-sleep).

The gauge bug is general: on every HDQ gauge (iPad 5, 6, 7, Air 2) the poll
kept running into suspend and touched the suspended 1-Wire UART. The default
poll of 360 s rarely collides; orchard's `30-orchard-battery-poll.conf` sets
10 s, and then every sleep reset the tablet within 5 to 15 seconds.

## Every other SoC

The same parts are in the kernel for S5L8960X, T7000, T7001, S8000, S8003, S8001,
T8011 and T8015: `ps_gpio` always-on in each `-pmgr.dtsi`, the framebuffer's
display domains always-on in each family's `.dtsi`, and the `apple_cpuidle`
entry. Only T8010 has been tested. Their state is `untested`.

## Storage and PCIe over sleep

The NVMe controller (ANS) and Wi-Fi sit behind Apple's PCIe root complex
(`pcie-apple-h9p`). Over a sleep the ports are powered off and rebuilt, as iOS
does it. Nothing behind the root complex stays powered, with one exception
named below, and nothing is expected to survive.

Not run on any board yet: the code was written on 2026-11-15 without a device.
What a test has to show is at the end of this section.

### What iOS does

Read from the iPad 5 kernelcache (iPad6,11, iOS 16.7.x):

- **Storage** (`AppleEmbeddedNVMeController`): on sleep it freezes its queue
  and shuts the controller down, keeping no state. On wake it initializes the
  controller as at boot and unfreezes.
- **Root complex** (`AppleS8003PCIe`, `AppleT8010PCIe`): registers a platform
  quiesce and a platform active action. Quiesce disables every port. Active
  applies the common tunables again, runs the common bring-up again (on S8003
  the lane configuration, the ready wait and the common-done write), resumes
  the ports and waits for their links.
- **Taking a port down** (`disableGated`): save the endpoint's configuration,
  send PME_Turn_Off and wait for the acknowledge, wait for L2 (both waits may
  time out, iOS goes on), assert PERST#, then turn the port hardware off:
  reference clock, then port enable. iOS panics with "Can't assert NVMe reset
  while port is enabled" if the order is violated.
- The Apple device tree gives `perst-to-refclk-delay-us = 60` on the root
  complex and `t-refclk-to-perst = 100` per bridge, on J71t and J71b alike.
  No property asks for a port to stay powered in sleep.

### What the kernel does

| Step | Where | What |
|---|---|---|
| NVMe stop | `nvme_suspend()`, ANS glue | I/O drained, NVMMU idle, shutdown notification completed (`CSTS.SHST`), then `CC.EN` cleared and `CSTS.RDY` gone, bus mastering off, every NVMMU translation retired. Only then is the root complex told that the controller is stopped. |
| Wi-Fi | `brcmfmac` | its normal D3 entry |
| DARTs | `apple-dart` | saved in the suspend phase, while their port is still up |
| Ports down | `pcie-apple-h9p`, `suspend_noirq` | Wi-Fi first, storage last: PME_Turn_Off and a wait of at most 10 ms for L2 (S8003), PERST#, 60 us, reference clock off, port off |
| Power domains | genpd | the PCIe domains go off after the ports |
| Ports up | `pcie-apple-h9p`, `resume_noirq` | the bring-up of the probe: common block and tunables, storage port with its power cycle and NVMe mode, the other ports, link training, 100 ms, NVMMU on zeroed tables |
| Configuration space | PCI core | restored for root ports and endpoints |
| DARTs | `apple-dart` | rebuilt in the resume phase, after the ports are up |
| NVMe start | `nvme_resume()` | the controller reset that follows any suspend, as after boot |
| Wi-Fi | `brcmfmac` | finds a chip that was reset and loads its firmware again |

Why the NVMe stop matters: a controller that was shut down but still has
`CC.EN` set hangs the SoC when its port is power cycled (found by Berkay
Kösebay on J71t, see [fast-restart.md](fast-restart.md)). The stop is his, from
orchard #105 and #117. If it fails, the controller is reset and the sleep is
refused with `-EBUSY`; it never continues into a power-off. If no NVMe driver
is bound, nobody stopped the controller, and the root complex leaves all its
ports as they are (kernel log: `storage controller not stopped, ports stay up
over sleep`).

**The exception: the Wi-Fi chip's supply.** It is switched by a PMIC GPIO
behind I2C, which can't be reached in the phase where the ports go down and
come up. The chip keeps its supply over the sleep and is held in reset, with
its port, link and reference clock off.

**A Wi-Fi port that does not come back** in the resume itself gets a second
attempt as soon as the system is up, with the chip's power cycle as at boot;
the Wi-Fi device is then removed and found again. Kernel log: `no link after
sleep ... power cycling its endpoint`, then `back after an endpoint power
cycle`. **A storage port that does not come back** is cut off: its devices
are marked disconnected, the NVMe controller is marked dead and I/O fails
with errors. On a tablet that runs from that disk this ends the session.

USB is not kept: the gadget disconnects and enumerates again after the wake.

### Per SoC

The sequences are per SoC in the driver's match data, never per board.

| SoC | Boards with PCIe described | Take-down | State |
|---|---|---|---|
| S8003 (A9, TSMC) | iPad 5: J71t, J72t | iOS's, as decoded | untested |
| S8000 (A9, Samsung), S8001 (A9X) | none: the root complex is not described for them | | nothing to do |
| T8010 (A10) | iPad 6, iPad 7, iPhone 7 and 7 Plus (the boards that enable it) | PERST#, link off, 60 us, then the same two root complex bits as on S8003 (same register layout, and the bits the bring-up sets). No PME_Turn_Off: the request register of this port block is not known. | untested; replaces "ports left up", which is what was tested on J71b on 2026-11-04 |
| T8011 (A10X) | none | | nothing to do |
| T7000, T7001 (A8, A8X) | iPad mini 4, iPad Air 2: Wi-Fi only | PERST#, link off, then the link enable and reference clock bits the bring-up sets; not decoded from iOS | untested |
| T8015 (A11) | the boards that enable it | none | left as before: ports stay up |

On the A9 iPhones with an S8003 the root complex is described but not enabled;
they get the S8003 path once it is.

### Testing it

One sleep of more than 3 minutes on the installed system, then:

- the same boot ID, and no `nvme`, `pcie-apple-h9p` or `brcmfmac` error in the
  kernel log;
- a read that bypasses the page cache works:
  `dd if=/dev/nvme0n1 of=/dev/null bs=16k count=64 skip=$RANDOM iflag=direct`;
- Wi-Fi reconnects by itself (allow a few seconds: the firmware is loaded
  again);
- a second sleep right after works as well.

Kernel log after each wake: `nvme nvme0: N/0/0 default/read/poll queues`
(the controller came up from reset) and `brcmfmac: brcmf_c_preinit_dcmds:
Firmware: ...` (the chip loaded its firmware again). On S8003, `port N: no L2
after PME_Turn_Off` is a finding to report, not a failure. `ports stay up over
sleep`, `did not come back after sleep`, `controller not stopped for sleep`
and `Disabling device after reset failure` are failures.

## Waking on plug

A plug or unplug wakes the tablet, as on any tablet. The D2365 latches event 72
on both, and the charger drivers arm it as a wake source
(`apple_tristar_charger` on the iPad 5 and 6, `apple_d2365_charger` on the
iPad 7), so during sleep it is the only charger event left unmasked. Because no
source change can pass unnoticed, an identified charger keeps its limit across
sleep. A USB host may be asleep as well and parks at the handoff.

Seen on the iPad 6 (J71b) on 2026-11-07: a sleep of about 5 minutes on a wall
charger, then waking on unplug and on plug, with the screen lighting by itself.
The charge rate during sleep was not recorded, and neither was USB data coming
back on a Mac after the test.

## Bringing up another board

1. **Domains.** In the board's DT, find the domain of the GPIO controller the
   buttons use and the `power-domains` of the display node (front end, back end,
   panel link); the GPU's too, if its driver cannot restart its firmware over
   sleep. Same SoC family: the T8010 entries above apply
   as they are; another SoC needs its own, in its `-pmgr.dtsi` (shared) or the
   family `.dtsi` (panel link).
2. **Pollers.** Check the board's drivers for delayed work that requeues itself
   on a non-freezable workqueue (`system_wq`, `system_percpu_wq`,
   `schedule_delayed_work`). Gauges, chargers and sensors are the usual ones.
3. **Idle.** A new SoC's compatible goes into `cpuidle-apple.c`.
4. **Test in RAM first**, with `examples/suspend-check` or an equivalent
   `/init`: `pm_test=devices`, then `pm_test=platform`, then one real sleep.
   Load the board's drivers one at a time between real sleeps.
5. **Then the installed system**, with its default services and settings.
6. **Mark the feature**: `[features] suspend = "working"` in the board's
   `device.toml`, with the evidence (boot ID, sleep length, wake IRQ) in the
   change.

## Wake time

No user-space code runs before the kernel's device resume ends, so any driver or
console that blocks resume delays the screen.

- **The console.** Everything logged while consoles are suspended is written out
  at resume, with the CPU held, at 115200 baud on the device tree's `stdout-path`
  UART: 90 KB took about 7.8 s on the iPad 6. orchard's platforms default to console
  log level 4, so only errors go there; the kernel log itself stays complete. For a
  serial cable, boot with `loglevel=7`. Do not measure wake time with
  `pm_print_times` on: it logs every device.
- **Drivers.** Resume callbacks must not wait for hardware: `apple_z2` booting the
  controller inline cost 3.4 s. Read the times from the callbacks, or from key
  timestamps: printk times inside resume do not show it.

## Testing rules that mattered

- **Wait at least 45 s before pressing Power**, timed. Failures can take 5 to
  15 s; a sleep that is woken early proves nothing.
- **Launch detached** (`setsid … &` with a short delay): USB disappears in
  sleep, and a dropped SSH session must not look like a failed command.
- **A pass** is the same boot ID after wake, `pm_wakeup_irq` naming the button
  interrupt, `suspend_stats/fail` unchanged, and the user seeing the screen
  return, with `CLOCK_MONOTONIC` not counting the sleep: it falls behind
  `CLOCK_BOOTTIME` by the sleep's length, and
  `/sys/devices/system/cpu/cpu0/cpuidle/state1/s2idle/usage` went up. Sleep
  once for more than 3 minutes: no watchdog lines, and NetworkManager logs
  "wake requested".
- **Split, don't toggle.** When one configuration passes and another fails,
  swap halves (boot path, command line, user space, udev, kernel settings)
  instead of disabling services one at a time. On J71b, six such splits found
  the gauge poll after a day of single-service toggles had not.
- **Keep helpers and their output apart:** a helper copied to the path of its
  own output directory fails before any test.

## In an installed system

Every board may suspend. Nothing in orchard turns it off: an untested board
can only be reported on if its owner can try it. On a board where it does not
work yet the likely failure is a reboot, or no wake, instead of a wake.
`[features] suspend` says what is known about a board and decides nothing.

Distributions keep the policy: when to suspend (Power key, idle) and locking
first. omarchy-a asks logind (`CanSuspend`) before it suspends.

### Testing sleep as a tester

Sleep and suspend are the same thing: in Linux, sleeping is called suspend and
waking up is resume (`suspend` and `resume` in `device.toml`, "Sleep" in the
compatibility table). Screen off (`screen_off`) is different: the display is
dark, but the device keeps running.

In Omarchy A, Power turns the screen off. About 10 seconds later the device
goes to sleep, unless something is playing.

1. Press Power, wait at least 45 seconds, then press Power or Home. Do it a few
   times, and once for more than 3 minutes.
2. Run `journalctl -b | grep sleep-ended`. `result=slept` with the time you
   waited is a real sleep. `result=aborted` means it was still entering sleep.
   No line since boot means it never slept, or it restarted instead of waking.
3. Check that the screen, touch, Wi-Fi, sound and charging still work.

Report the log lines and what worked after waking. That is enough for
`suspend` and `resume`; for another system, use the checks under
[Testing rules that mattered](#testing-rules-that-mattered).

To report a board in `device.toml`: prove it as above, then set
`suspend = "working"`, and `resume = "working"` if everything came back.
