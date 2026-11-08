# J71t Mac extra-current candidate

Host-tested on 2026-11-08; not yet qualified on an iPad. The matching root
initrd reads `/etc/orchard/mac-usb-current` from the mounted system. Exactly
`1` on `apple,j71t` selects the compatibility identity `05ac:1014`, explicit
IAD device class ef/02/01, and the existing ACM+NCM configuration at
500 mA MaxPower. Omarchy A carries that opt-in as a separate system change.
Removing the file selects the normal `05ac:4145` identity on the next boot.
Recovery, installer and RAM/NFS gadgets retain their existing identities.

This Mac's AppleUSBHostMergeProperties personality lists `1014` in its
2400 mA UsbPowerSinkCapability table; `4145` is absent. The generic
AppleUSBHostCompositeDevice power-negotiation path can therefore allocate
extra power and send vendor device request `40/40`, zero length, signed
suspend mA in wValue and signed extra active mA in wIndex. No usbmux interface
or iOS preferred-configuration protocol is needed for this PID. Its IAD
class ef/02/01 matches Apple's generic composite personality. AppleUSBCDC's
matching IAD personality has IOMatchDefer=True; its non-deferred vendor
personality instead requires 0/0/0. Both ACM and NCM already emit IADs. The strings
continue to identify Orchard and Omarchy. This is a compatibility identity,
not an assigned Orchard PID or a claim to implement an Apple product.

A new identity requires macOS accessory authorization, possibly on every boot
because the existing BOOT serial changes. Keep the Mac unlocked and reconnect
the cable if the device remains unregistered/unmatched. Do not silently change
the Mac's security settings. Orchard discovery recognizes this PID as a
system only when the existing serial contains a boot UUID and BOARD:j71t.

The identity is never current evidence on the tablet. Only the host's real
request reaches composite_vbus_draw -> DWC2 -> lightning. The charger clamps
the allowance at its board maximum and retains pack guards, stepped ramp,
readback verification and source-epoch withdrawal. No request means the
existing 500 mA fallback. BC1.2 primary/secondary detection already exists;
SDP on this Mac must not be relabeled CDP just to obtain 1.5 A.

A grant arriving while suspended cannot select its active allowance. The
existing policy discards the preceding active grant on suspend; resuming
needs a fresh grant. Reset and disconnect also clear it. The supplied suspend
field is logged but does not authorize an elevated suspend limit. This work
does not certify the pre-existing charger's USB suspend/preconfiguration
compliance: its unknown-source hardware fallback remains 500 mA.

`current_max=0` on a configured link is a separate possible blocker. A report
is accepted only while present, with data enabled, in the current PMU source
epoch. Logs now show reported/accepted current, present/data and report epoch;
dynamic debug on the charger shows stale report/source epochs. Do not replay
an old allowance after a PMU event to make this number nonzero. A packet trace
and these logs must establish whether the live zero was a missing report, a
gated report, or epoch invalidation. That observation is still required.

## Host verification

Run `ORCHARD_KERNEL_SOURCE=/path/to/prepared/kernel PYTHONPATH=src python3
-m unittest discover -s tests -p test_j71t_mac_current.py -v`. The C test
extracts the actual production functions and executes them with ASan/UBSan:
all request-type/request/short-length/vendor combinations, all signed extra
values, suspend, board caps, ramp/revoke, attachment and stale epochs.
The other tests run the initrd gadget function in a mock filesystem and test
both host discovery implementations. They do not contact USB hardware.

## Device acceptance gate (orchestrator only)

Boot the new Image, DTB, matching initrd and modules together, with the
Omarchy A opt-in present. Do not rebind a live gadget to edit its identity.

On the unlocked Mac, approve the new accessory and capture `ioreg -p IOUSB
-l -w0` plus `ioreg -l -w0 -r -c IOUSBHostDevice`. The device should be
registered/matched/configured, have PID 0x1014, MaxPower 500,
UsbPowerSinkCapability 2400 and UsbPowerSinkAllocation above 500 (2400 if
available). Check AppleUSBHostCompositeDevice binding and
kUSBConfigurationCurrentOverride. The host's available 3000 mA alone is not
a grant. Preserve ACM serial and NCM network connectivity.

On the tablet, enable dynamic debug for composite.c, dwc2/gadget.c and
apple_tristar_charger.c. Capture dmesg and lightning's online, usb_type,
current_max and input_current_limit alongside battery's status/current_now,
capacity, temperature, voltage and the UDC state. A usual full grant is
extra=1900, giving accepted/current_max=2400000, followed by a guarded ramp
to input_current_limit=2400000. Never exceed min(500+signed extra, 2400).
A smaller grant must produce a correspondingly smaller limit, rounded down.
SDP may remain correct; it does not imply that a software grant failed.

At a moderate state of charge and normal pack temperature, hold brightness
and desktop workload fixed for at least 60 seconds: battery status must be
Charging and signed current_now positive. Full/thermal-limited packs are not
a valid charging-current acceptance test. If current_max stays zero, preserve
the report/epoch logs and EP0 trace; do not force a PMIC limit.

Also test: a plain 500 mA host (no extra request), reset, unplug/replug to a
different source, a smaller/revoked grant, and Mac sleep/wake. Any loss of a
valid grant must stop elevated draw and return to the fallback. Suspend may
lose extra current until macOS issues a new grant; record that limitation.
Mac allocation without an actual request and accepted allowance is a failed
test, even if battery current happens to be positive with the panel off.

## Sustained NCM transfers

The runner reported two carrier-up stalls with ACM still working, recovered
by cycling only usb0. USB patch 0012 fixes a reproducible NCM timer bug:
NETDEV_TX_BUSY during a timer flush used to abandon the partial NTB. Retry
at the usual 300 us interval, advancing the timer expiry before restart.
This is a supported cause of silent NCM stalls; the runner's exact endpoint
state was not captured, so it is not proof that both incidents had this cause.

A 16 KiB NTB does not need to fit in a dedicated FIFO at once: DWC2 splits
transfers and streams USB packets through the FIFO. The 2056-entry total is
in 32-bit words. No FIFO, DMA, ZLP or negotiated NTB-size change is justified
by those totals alone. ACM surviving also differs from the known shared-OUT
FIFO starvation fixed by USB patch 0008. Do not change those controls without
capturing endpoint state and request completions during a stall.

Run test_ncm_flush.py with ORCHARD_KERNEL_SOURCE to execute the production
callback under repeated request exhaustion and subsequent recovery. On the
next device run count every `usb0-stall-bounce` marker, including the runner's
transient oi-usb0-watchdog. Use one watchdog at a time so bounces are attributable.
The Omarchy service's new carrier-up fallback cycles only usb0 after 15 seconds
without replies from the previously reachable 172.16.42.2 peer. It leaves
UDC, ACM and host charging-grant ownership intact. A clean sustained-transfer
acceptance run requires zero watchdog bounces, not merely successful retries.
