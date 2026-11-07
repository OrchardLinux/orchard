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

The gauge bug is general: on every HDQ gauge (iPad 5, 6, 7, Air 2) the poll
kept running into suspend and touched the suspended 1-Wire UART. The default
poll of 360 s rarely collides; orchard's `30-orchard-battery-poll.conf` sets
10 s, and then every sleep reset the tablet within 5 to 15 seconds.

## Every other SoC

The same parts are in the kernel for S5L8960X, T7000, T7001, S8000, S8003, S8001,
T8011 and T8015: `ps_gpio` always-on in each `-pmgr.dtsi`, the framebuffer's
display domains always-on in each family's `.dtsi`, and the `apple_cpuidle`
entry. Only T8010 has been tested. Their state is `untested`.

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

To report a board: prove it as above, then set `suspend = "working"`.
