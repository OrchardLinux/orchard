# Adaptive charging in Orchard: a guide for D2365 boards (iPad 7, J171/J172)

Reference implementation: iPad 6 Wi-Fi (J71b), `apple_tristar_charger`.
Target: the imported `apple_d2365_charger` with `apple_bellatrix_power` on
the iPad 7, enabled by PR #46. Any new charger port follows the same rules.

Paths are in this repository. Patch numbers refer to `kernel/patches/drivers/power/`, unless stated.
"Unknown" marks facts nobody has measured yet. Do not fill those gaps with
guesses; measure them on the device or read them from the ADT or kernelcache.

---

## 1. What "adaptive" means here

The PMIC input-current limit (D2365 `0xa03`) caps what the board draws from
VBUS. The hardware does not negotiate this limit with the source, and VBUS
loss does not reset it. On J71b, `0x1c` stayed in place for 4.9 s after an
unplug, until software restored it (`docs/bring-up/charging/README.md` §1,
`pmu-events.md`). So software has to do three things
(`docs/bring-up/charging/README.md` §1):

1. find out what the attached source can supply;
2. program at most that, and never more than the board maximum;
3. drop the limit again as soon as the source may have changed.

In Orchard, "adaptive" means the limit follows evidence about the source:

| Evidence | Grant (J71b) | Where |
|---|---|---|
| Nothing identified (computer, hub, generic charger, unconfigured host) | 500 mA fallback (`0x22`) | 0003 |
| Apple USB-A brick (BrickID divider on D+/D−) | 500–3000 mA per ID, capped at board max | 0003 `tristar_brick_ma()` |
| USB-C Rp on CC | 1.5 A or 3 A, capped | 0003 `tristar_identify_usbc()` |
| BC1.2 CDP/DCP | 1.5 A | 0009 `tristar_identify_bc12()` |
| Apple host current grant (vendor request, through `vbus_draw`) | 500 mA + the extra the host grants, capped | `drivers/usb/0005`, `drivers/usb/0004`, 0008 |
| No input | 100 mA handoff (`0x02`) | 0003 `tristar_poll()` |

The project policy behind this table: there is no undifferentiated 2.4 A. Each source gets the highest grant it
states itself or that a spec covers, up to the board maximum. Anything
unidentified gets 500 mA.

Three more properties make a grant adaptive and not just a lookup:

- **Start conservative.** Every attachment starts at the fallback. A higher
  grant comes only after identification.
- **Ramp with guards.** The limit rises in steps, and only inside a battery
  window.
- **Withdraw first.** Any sign that the source changed drops the grant
  before anything else happens. Attribution comes afterwards.

**Why a fixed 1425 mA is not acceptable.** A USB 2.0 port only has to
supply 500 mA to a configured device, and 100 mA before configuration.
1425 mA is almost three times that. The iPad 7 then tries to draw more than
a plain computer port or a bus-powered hub is obliged to deliver. Experiment
CH-01 (`docs/bring-up/charging/experiments.md`) already flags this as
*inferred* and advises against long sessions on computer ports. The same
mistake has been rejected before. PR #8 mapped any USB session to 2.4 A and
PR #6 granted 500 mA to a suspended host, and neither was merged (`README.md` §2.4 and §8, `experiments.md` CH-06). The PR #46 test
does not settle the question either way. It shows +751 mA net battery
current on a ThinkPad USB-A port with the limit at 1425 mA. Net battery
current is input minus system load, so the input was above 751 mA. Whether
that port is a 500 mA SDP, a 900 mA USB 3 port, a CDP, or just tolerant is
not known. A battery that charges proves nothing about the source contract
(`measurements.md`, "Lessons").

---

## 2. How J71b does it, end to end

### 2.1 Pieces

```
source ─ Lightning ─ CBTL1610 (Tristar, i2c0 0x1a) ─ mux ─▶ D2365 ADC ch 1 (0x4000)
VBUS ─────────────────────────────────────────────▶ D2365 charger: status 0x18a, limit 0xa03
D2365 event block 0x140 ─ PMU line (AP GPIO 38) ─▶ pmu_gpio irq_chip ─▶ charger IRQs
USB host ─ dwc2 gadget ─ composite (vendor req 0x40) ─ vbus_draw ─▶ "lightning" psy
BQ27540 gauge ("battery" psy) ─▶ pack guards
```

All of the policy lives in one driver, `apple_tristar_charger.c` (0003,
extended by 0005, 0008, 0009 and 0013). DT: `devices/t8010/0003`, `0004`,
`0005`. The board maximum comes from `input-current-limit-microamp` (2400 mA
on J71b, from the ADT `usb-input-limit-max`). The encoding is
mA = code × 12.5 + 75, and `tristar_ma_code()` rounds down so a grant never
exceeds what is allowed.

### 2.2 States and timing (`tristar_poll()`, every 100 ms on `system_highpri_wq`)

| Phase | Entry | What happens |
|---|---|---|
| EMPTY | `0x18a` = 0 | Writes the handoff `0x02` and forgets the source, `usb_ma` included |
| (transitional) | `0x18a` ≠ 0 and ≠ `0x42` (for example `0x40` during attach, `0x62` in PR #51) | No grant: withdraws to the fallback |
| SETTLING | `0x18a` = `0x42` with no source present → `tristar_new_attachment()` | Writes the fallback. Early look at 300 ms, before USB enumerates (0009). Identifies at 1000 ms (`tristar_run_identification()`) |
| RAMPING | target above the fallback | `tristar_ramp_step()`: +500 mA every 500 ms. Before each step it reads the pack and falls back if the pack is outside the window |
| CHARGING | target reached | `tristar_guard()` once a second: `tristar_check_limit()` (0013), then the pack window |
| FAULT | mux restore failed, limit write failed, event storm | Holds the fallback until reboot |

Retries: an unidentified source is retried twice, 3 s apart (a half-inserted
plug reads as no signature). After more than 3 identifications spoiled by
events, the source counts as "unstable" and stays at the fallback.

### 2.3 Source detection (0003, 0009)

`tristar_identify()` does the following:

1. It checks that the mux is idle.
2. It reads the CBTL1610 capability frame (`cbtl_capability()`: status
   stable before and after, opcode `0x75`, CRC-8) to tell a USB-C cable from
   a USB-A cable and to get the orientation.
3. On USB-C, it reads CC on the oriented lane. Below 660 mV means default
   power, 660–1229 mV means 1.5 A, and 1230 mV or more means 3 A. With
   default power, it tries BC1.2 next.
4. On USB-A, it reads the BrickID divider on both mux banks. The thresholds
   1500/2184/2880 mV come from J71b firmware, and when the two banks
   disagree, the lower advertisement wins. With no signature, it tries
   BC1.2.

Every mux sample is restored and verified (`tristar_mux_sample()`). A
failed restore is a fault. The mux is never sampled during a USB session,
because mux changes stalled USB data on J71b (0008). That is why the early
look at 300 ms exists: it catches a host's own CDP or CC advertisement
before enumeration. Background: `tristar-cbtl1610.md`, `brickid.md`,
`usb-c.md`.

### 2.4 Host grant path (`drivers/usb/0005`, `drivers/usb/0004`, 0008, `devices/t8010/0005`)

1. `composite.c` answers the Apple vendor request (`bmRequestType` 0x40,
   `bRequest` 0x40, zero length), but only when the gadget's idVendor is
   `0x05ac`. The allowance is 500 mA plus `(s16)wIndex`. A bus reset, a
   disconnect or a suspend ends it. `composite_vbus_draw()` reports it
   through the normal `usb_gadget_vbus_draw()` path.
2. `dwc2_hsotg_vbus_draw()`: when there is no `usb_phy` and `usb-psy-name`
   is set, a work item calls `power_supply_set_property(INPUT_CURRENT_LIMIT)`
   on the named supply. The supply is looked up each time, so probe order
   does not matter.
3. The J71b DT sets `usb-psy-name = "lightning"`. The iPad 7 DT deletes the
   property.
4. `tristar_set_property()` stores `usb_ma` only while a source is present.
   It also records the event epoch of the report. `tristar_usb_ma()` counts
   the allowance only within that epoch, because the UDC does not always
   notice the host going away. A PMU source event therefore ends it.
5. `tristar_target_code()` takes max(source, host allowance), capped at the
   board maximum. Anything at or below 500 mA maps to the fallback. A lower
   allowance withdraws at once; a higher one ramps
   (`tristar_update_target()`).

Status: implemented, but the grant itself has not been observed. macOS sent
only 100 mA in the J71b RAM test (`experiments.md` CH-16). PR #51 drove the
grant from a Fedora host with a private tool. A 1000 mA allowance held, and
1500 mA held at dim brightness. At the requested 2400 mA, and at 1500 mA
with a bright screen, the PMIC status read `0x62` and the driver fell back.
The meaning of `0x62` is not established.

### 2.5 Withdrawal and PMU events (0005, `devices/t8010/0004`)

- The `pmu_gpio` driver owns the read-to-clear event block `0x140` and
  hands out each event as an IRQ. This is the one-owner rule
  (`pmu-events.md`). The charger requests the line (240), ADC done (176)
  and the events the iPadOS charger subscribes to: 16, 40–43, 72 and 77.
- `tristar_line_irq()` runs **before** the block is read. It writes the
  fallback and reads it back without taking the policy lock. The block is
  read only after that.
- `tristar_source_irq()` withdraws again, bumps the epoch and kicks the
  poll. A new epoch invalidates any ADC result, identification or host
  allowance in flight. An ADC completion nobody asked for counts as a
  source event.
- When the line asserted but no source event followed, the poll ramps back
  after 200 ms (`TRISTAR_LINE_SETTLE_MS`).
- More than 200 source events in one second means something is stuck. The
  driver then goes to FAULT and disables the source IRQs.
- Event 73 is left out because it latches every few seconds near 2 A with
  the same source attached (`devices/t8010/0004`).
- Measured: 981–1006 µs from the PMU interrupt to a verified readback, on
  four unplugs (`measurements.md`). PR #51 (`fedora-usb-a.md`) got 4.3 ms from a software
  callback, which is not a cable-edge measurement. The time from the
  physical edge to GPIO 38 is unmeasured (CH-03).

### 2.6 Restoring limits after a PMIC reset

On the iPad 5, the D2365 sets `0xa03` back to its attach default `0x02`
about 26 ms after a write, or 160–380 ms into the attach. The battery then
drained at about 1.1 A while sysfs still said 500 mA (0013, Berkay
Kösebay). `tristar_check_limit()` therefore reads the limit back once a
second:

- If the hardware holds less than was written, the driver restores the
  fallback and ramps again under the pack checks.
- If the hardware holds more, the driver puts it back down.

Each correction is logged. Paul's D2365 driver got the same fix as 0010.

### 2.7 Lifecycle

- **Probe:** a limit above the fallback is lowered, not adopted ("never adopt
  a limit we cannot explain").
- **Suspend:** parks at the handoff and forgets the source, because sleep
  can hide a swap. Resume identifies again.
- **Shutdown and remove:** restore the handoff.
- **Reporting:** `lightning` supply with `online`, `usb_type`
  (BrickID/C/CDP/DCP/SDP/unknown), `input_current_limit` (verified code) and
  `current_max` (what the source or host allows). The pack itself is
  guarded by bq27xxx, with 0011 ("Discharging" at full charge).

---

## 3. How the D2365 driver works today

Files: `kernel/patches/import/pauli1go/0029` (driver), `0028` (binding),
`0030` (DT), `kernel/patches/drivers/power/0006` (binding fix) and `0010`
(attach-default restore). Bellatrix: `import/pauli1go/0026`, `0027`.

**Configuration.** Board facts sit in match data on a board-named
compatible. `apple,j172-d2365-charger` sets handoff `0x02`, charging limit
`0x6c` and `max_charge` `0x34`. 0x6c × 12.5 + 75 = **1425 mA**, and that is
where the fixed value comes from. The generic `apple,d2365-charger` has no
policy. The J171 binds the J172 compatible, because the node is in the
shared `t8010-ipad7.dtsi` (PR #46 test output:
`OF apple,j172-d2365-charger`).

**Supply.** `online` comes from the supplier, the Bellatrix `usb` supply,
through `power_supply_get_property_from_supplier()`. Bellatrix polls once a
second over I2C:

- it checks the chip ID (`0x73` = `0x74`);
- it reads `0x16` bit 1 (external VBUS);
- it reads `0x17`: bit 0 (provider) forces offline, bit 1 (consumer) means
  online.

Bellatrix reports presence only. It writes nothing and reports no source
current. Its DT node has an interrupt (AOP GPIO 13, level low) that the
driver does not use.

**Policy** (`apple_d2365_charge_policy_work()`) runs every 5 s, and also
right away on `external_power_changed`. It steps through these checks:

1. Supplier offline → restore the saved limit (`0x02`).
2. Battery guard (`apple_d2365_battery_is_safe()`): pack present,
   10–40 °C, 3.0–4.35 V. Outside → restore.
3. Read `0x18a`, `0xa00`, `0xa03` and `0xa0c`. Charge-timeout bit 4 →
   restore.
4. Integrity: `0xa00` must read `0x00` and the `0xa0c` field must equal
   `0x34`. Otherwise → restore.
5. If the policy is enabled and `0xa03` ≠ `0x6c` → restore. The exception
   is 0010: within 15 s of enabling, a reading of `0x02` counts as the
   attach default and `0x6c` is written again.
6. If the policy is not enabled, `0xa03` must equal `0x02`, or the driver
   refuses. It then writes `0x6c` with readback and checks again after
   100 ms.

Restore writes the saved value with readback, up to 3 times, 20 ms apart.
Remove and shutdown retry that 3 times, 100 ms apart. The `charger` supply
exports `online`, `status`, `charge_type` and `input_current_limit`.

**Compared with J71b:**

| Mechanism | J71b `apple_tristar_charger` | `apple_d2365_charger` (J172 data) |
|---|---|---|
| Source identification | BrickID, CC, BC1.2, host grant | None |
| Grant | Per source, capped at the DT maximum | Fixed 1425 mA from match data |
| Start | Fallback 500 mA, then ramp | Jumps 100 → 1425 mA |
| Ramp | 500 mA every 500 ms, pack check before each step | None |
| Pack guards | 5–40 °C, 3.2–4.35 V, \|I\| ≤ 3 A; before each step and every 1 s | 10–40 °C, 3.0–4.35 V; no current check; every 5 s |
| Limit read-back | Every 1 s; restore or lower (0013) | Every 5 s; `0x02` within 15 s is rewritten (0010), anything else stops the policy |
| Withdrawal trigger | PMU line IRQ, write before attribution, about 1 ms | Bellatrix poll (≤ 1 s) → `power_supply_changed` → workqueue |
| Swap without a VBUS gap | Event epoch catches it | Not seen (Bellatrix stays online) |
| Host grant | `usb-psy-name` → `set_property` | Not wired (`usb-psy-name` deleted on iPad 7) |
| Unexplained limit at start | Lowered to the fallback | Policy refuses; limit left as found |
| Suspend | Parks at the handoff | No PM ops |

Paul's guards, readback and verified restore are a good base. What is
missing is everything that makes the grant depend on the source.

---

## 4. Bringing J171 to the same standard

### 4.1 Rules (from the J71b work)

- **Do not exceed the default before withdrawal is measured on that board**
  (`experiments.md`, rule 1). Until §4.2 step 4 is done, J171 grants at most
  the 500 mA fallback.
- **No fixed limits.** A grant is the minimum of a source's evidence and the
  board maximum. A constant in match data is neither.
- **No machine checks in shared code.** No `of_machine_is_compatible()` and
  no board constants chosen by a board-named compatible. Put the board
  maximum in DT, as `input-current-limit-microamp`, the way the Tristar
  binding does. Take its value from the J171/J172 ADT `usb-input-limit-max`;
  do not copy J71b's 2400 mA or guess.
- **No unbounded raw PMIC writes.** Write only `0xa03`, always with
  readback, rounded down, clamped between the handoff and the DT maximum.
  Do not write `0xa00` (iPadOS toggles bit 1; Linux does not need it), the
  pack limit `0xa08`/`0xa0c`, or the event mask `0x1c0`. Do not dump
  registers or scan buses. Do not create a writable sysfs limit for
  userspace.
- **One owner** of `0xa03` and of the event block.

### 4.2 Steps, mapped to J71b

| # | Step | J71b counterpart | iPad 7: known / to measure |
|---|---|---|---|
| 1 | **Baseline** (CH-00): `0xa03`, `0xa0c`, `0x18a`, the Bellatrix `0x16`/`0x17` and the gauge, for no cable, a computer and a wall charger, plus a cold boot with the cable attached | J71b reference values in CH-00 | Known: handoff `0x02` and cap field `0x34` (match data). To measure: `0x18a` values. The J172 charge-state field can stay set while Bellatrix says offline (driver comment, `pmic-d2365.md`) |
| 2 | **Fallback first.** Replace the 1425 mA grant with the 500 mA fallback while a source is online and the handoff while offline. Lower an unexplained limit at probe instead of refusing. Keep 0010's attach-default restore, and read back every second as in 0013 | `TRISTAR_CHG_FALLBACK_CODE`, `tristar_check_limit()`, probe | Whether the PMIC resets `0xa03` after attach on J171, and when (CH-09; 0010 was carried over from the iPad 5 and is "to be checked there") |
| 3 | **Board maximum from DT** (`input-current-limit-microamp`) | J71b binding | Value from the iPad 7 ADT |
| 4 | **A source-change signal and measured withdrawal.** Withdraw before attribution, like `tristar_line_irq()` | PMU line through `pmu_gpio`, events 72/176, about 1 ms | Unknown. The iPad 7 PMU line is deliberately left out (`devices/t8010/0004` removes `interrupt-controller` from `pmu_gpio` on iPad 7). Candidates: the PMU line from the J171 ADT, or the unused Bellatrix interrupt (AOP GPIO 13). Do one ADC-only conversion and one plain unplug/replug, and record which events appear (`README.md` §7 step 4). Then measure edge → verified readback over many unplugs and fast swaps (CH-03) |
| 5 | **Host grant.** Name the D2365 charger supply in `usb-psy-name` on iPad 7 and add `set_property(INPUT_CURRENT_LIMIT)` with `tristar_set_property()` semantics: only while online, valid only until the next source event, ≤ 500 mA changes nothing, lower withdraws, higher ramps | 0008, `devices/t8010/0005` | No driver code needed in dwc2 or composite. To check: that the J171 gadget enumerates and gets the request (`devices/j171/device.toml` says `usb_serial = false`). The grant needs step 4's epoch. Without it, an allowance can outlive a swap |
| 6 | **Source identification** (CH-01). Find where the iPad 7 learns the source current, from the iOS IORegistry, the Bellatrix kext in the kernelcache, then read-only Linux prints. Expose results from the Bellatrix supply (`usb_type`, `current_max`) and consume them in the charger through the supplier link | BrickID / CC / BC1.2 in 0003, 0009 | Unknown. That Bellatrix replaces Tristar is *inferred* (CH-01). No register meanings are known beyond `0x16`, `0x17`, `0x73` |
| 7 | **Ramp and guards.** 500 mA steps, pack window including \|I\| ≤ 3 A before each step and every second; FAULT on write or readback failure | `tristar_ramp_step()`, `tristar_guard()` | Status bytes that should block a grant: like `0x62` on J71b, unknown |
| 8 | **Lifecycle.** Park at the handoff on suspend and forget the source; handoff on shutdown and remove | `tristar_suspend()`, `tristar_stop()` | Suspend on iPad 7 untested (CH-06) |

**Driver shape** is the maintainer's call. Do not restructure without
agreeing it first. Paul's split (a port-controller supply reporting the
source, the D2365 charger granting) is the design `README.md` §5 says was
originally planned. It became workable once the PMU event controller
existed (0005). One option is a shared D2365 policy core with a Tristar
backend and a Bellatrix backend. Another is to keep two drivers with the
same rules. The iPad Air 2's D2207 raises the same question ("one Dialog
charger driver with per-PMIC match data"). Propose a shape in the PR
before writing it.

**Status with PR #46:** PR #46 enables the config together with step 2's
first part, `drivers/power/0014` (500 mA fallback instead of the fixed
1425 mA). Lowering an unexplained limit at probe and the one-second
read-back are still open. `devices/j171/README.md` tracks what is tested.

### 4.3 Test plan

Use a RAM boot and log over Wi-Fi if the gadget is in use (`experiments.md`
rules). Battery at 20–90 %, pack below 40 °C. Fix the screen brightness and
record it. Label the source, cable, plug orientation and which end you
moved. Use an inline USB meter when you have one (CH-14), because gauge
current is net of system load.

| Case | Expected after steps 2–5 | Record |
|---|---|---|
| Plain computer USB-A port (SDP) | Fallback 500 mA; never above it without a grant | `input_current_limit`, `current_max`, `usb_type`, `0x18a`, gauge current, meter mA/V |
| Host that grants (macOS, or the PR #51 Fedora method) | 500 + extra, capped, ramping; drops to 500 when revoked | Vendor request values, ramp timestamps, any non-online status (like `0x62`) and fallback events |
| Apple USB-A brick, USB-C charger, generic charger | 500 mA until step 6 identifies them; then per source | Same as above, plus Bellatrix raw values per source and orientation (CH-01 C) |
| Unplug while granted, and fast swap (brick → computer, under 1 s) | Withdrawal before attribution; handoff when offline; no grant carried over | Edge → verified readback in µs, at least 20 repetitions, max and histogram; a poller counting missed detaches (CH-03) |
| Replug after 1 s and after 10 s | Starts at the fallback; host allowance cleared | `0xa03` at attach, +10 ms, +100 ms, +1 s (CH-09: attach-default reset) |
| Near full (≥ 95 %) and at termination | Limit stays within the source; PMIC terminates; `status` and the battery report it truthfully | `0x18a` bits 0–2 and 4, gauge current and status, `charger` status |
| Pack guard | Grant drops to the fallback outside the window | Log line and timestamp (simulate by lowering the guard bound in a test build, not by heating the pack) |

---

## 5. Open questions

| # | Question | Marked as |
|---|---|---|
| 1 | Where does the iPad 7 get source current from: Bellatrix registers, the PMIC ADC, BC1.2, PD? | Unknown (CH-01) |
| 2 | Which pin is the iPad 7 PMU line, and do events 72/176 behave as on J71b? | Unknown; DT leaves it out on purpose |
| 3 | Does the Bellatrix interrupt fire on attach, detach or swap, and how fast? | Unknown |
| 4 | J171/J172 `usb-input-limit-max` | Read from the ADT; not in the repo |
| 5 | Does the D2365 reset `0xa03` after attach on J171? (0010 was ported from the iPad 5) | To check |
| 6 | What does 1425 mA actually draw on a computer port? What is the ThinkPad port in PR #46? | Unmeasured (CH-14) |
| 7 | Does the J171 gadget receive the Apple host request? Does macOS send it to this gadget? | Unknown (CH-16: macOS sent 100 mA to the J71b gadget) |
| 8 | J71b status `0x62` (bit 5): what it means, and whether J171 shows it | Unresolved (PR #51) |
| 9 | Real input current per code; iPadOS uses a per-unit `usb-input-limit-calibration` | Unmeasured; never publish calibration bytes |
| 10 | Suspend and wake with charging; the event mask `0x1c0` cannot be written yet on J71b | Blocked (CH-06, CH-15) |

**Where notes and code disagree, the code on main wins:**

- `README.md` §2.4 and §3 and `pmu-events.md` still say 425 mA (`0x1c`) for
  unidentified sources and "BC1.2 not implemented". Main uses 500 mA
  (`0x22`) and implements BC1.2 (0009).
- Keep four things apart, as the early handoff notes did: available
  current ≠ host allocation ≠ programmed limit ≠ net battery current.
- The `pmu-events.md` dispatch pseudo-code skips the write while an ADC is
  pending. Main no longer does (0005).

---

## Sources

Code (orchard main):

- `kernel/patches/drivers/power/0003`, `0005`, `0008`, `0009`, `0013`
  (Tristar charger), `0010` and `0006` (D2365), `0011` (bq27xxx), `0012`
  (J71t binding);
- `kernel/patches/drivers/usb/0003`, `0004`, `0005`;
- `kernel/patches/devices/t8010/0003`, `0004`, `0005`;
- `kernel/patches/import/pauli1go/0026`–`0030`;
- `devices/j171/device.toml`.

Docs:

- `docs/bring-up/charging/` (README, experiments, measurements, pmu-events,
  pmic-d2365, tristar-cbtl1610, brickid, usb-c);
- `docs/bring-up/charging.md`;
- `docs/integration/j71t.md`.

PRs:

- OrchardLinux/orchard#46 (description and diff);
- OrchardLinux/orchard#51 (`docs/bring-up/charging/fedora-usb-a.md`).
