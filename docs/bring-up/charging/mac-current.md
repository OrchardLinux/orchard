# Full charging current from a Mac

Installed Orchard systems present USB product ID `05ac:1014` with the
interface-association device class `ef/02/01`, so a Mac grants them up to
2400 mA (`docs/device-protocol.md`, USB identity). Found and first measured by
Berkay Kösebay on a J71t (orchard PR #108); the default for every board since
2026-11-13. The kernel side is `drivers/usb/0013` (no extra current while the
bus is suspended) and `drivers/power/0030` (the grant capped at the board's
maximum).

## How macOS grants the current

macOS's AppleUSBHostMergeProperties personality lists `1014` in its 2400 mA
`UsbPowerSinkCapability` table; Orchard's own `4145` is absent. The generic
AppleUSBHostCompositeDevice power negotiation then allocates the extra power
and sends vendor device request `40/40`, zero length, with the signed suspend
mA in `wValue` and the signed extra active mA in `wIndex`. No usbmux
interface or iOS preferred-configuration protocol is needed for this PID.

The device class matters: IAD class `ef/02/01` matches Apple's generic
composite personality. AppleUSBCDC's matching IAD personality has
`IOMatchDefer=True`, while its non-deferred vendor personality requires class
`0/0/0`; with `ef/02/01` the composite driver binds first and the CDC drivers
(ACM serial, NCM network) still bind to their interfaces. ACM and NCM already
emit IADs. The strings still identify Orchard and the distribution. This is a
compatibility identity, not an assigned Orchard PID.

A new identity needs one macOS accessory approval; with the stable USB serial
(`ID:`) macOS keeps it across boots. Keep the Mac unlocked and reconnect the
cable if the device stays unregistered. Never change the Mac's security
settings silently. Hosts treat `1014` as an Orchard system only with an
Orchard serial (`BOARD:` with `ID:` or `BOOT:`), because real Apple hardware
uses that ID too.

## What the tablet does with it

The identity alone is never current on the tablet. Only the host's real
request reaches `composite_vbus_draw` -> DWC2 -> the Lightning charger. The
charger caps the allowance at its board maximum and keeps its pack guards,
stepped ramp, readback verification and source-epoch withdrawal. No request
means the 500 mA fallback. BC1.2 detection exists separately: an SDP Mac port
must not be relabelled CDP to obtain 1.5 A.

A grant arriving while suspended cannot select the active allowance. The
policy drops the active grant on suspend; resuming needs a fresh grant. Reset
and disconnect clear it too. The suspend field is logged (debug) but never
authorises an elevated suspend limit.

`current_max=0` on a configured link can have three causes: a missing report,
a report gated by the charger (not present, data off), or a report from an
earlier source epoch. The charger's debug messages show reported and accepted
current, present, data and the report epoch. Never replay an old allowance
after a PMU event to make that number nonzero.

## Device acceptance check

On the unlocked Mac, approve the accessory and capture `ioreg -p IOUSB -l -w0`
and `ioreg -l -w0 -r -c IOUSBHostDevice`. Expect PID 0x1014, MaxPower 500,
`UsbPowerSinkCapability` 2400 and `UsbPowerSinkAllocation` above 500 (2400 if
available), AppleUSBHostCompositeDevice bound, and
`kUSBConfigurationCurrentOverride`. The host's available 3000 mA alone is not a
grant. ACM serial and NCM network must keep working.

On the tablet, enable dynamic debug for `composite.c`, `dwc2/gadget.c` and
`apple_tristar_charger.c`, and capture dmesg with the `lightning` supply's
`online`, `usb_type`, `current_max` and `input_current_limit`, the battery's
status, `current_now`, capacity, temperature and voltage, and the UDC state. A
usual full grant is extra=1900: accepted/`current_max`=2400000, then a guarded
ramp to `input_current_limit`=2400000. Never more than min(500 + extra, board
maximum); a smaller grant gives a correspondingly smaller limit, rounded down.

At a moderate state of charge and normal pack temperature, hold brightness and
workload fixed for at least 60 s: the battery must report Charging with a
positive `current_now`. A full or thermally limited pack is not a valid test.
Also try a plain 500 mA host, a reset, unplugging to another source, a smaller
or revoked grant, and Mac sleep and wake: losing a grant must stop the elevated
draw. A Mac allocation without a request and an accepted allowance is a failed
test, even if the battery happens to charge with the panel off.

## USB network stalls seen during the bring-up

The J71t runner saw two NCM stalls with carrier up while ACM kept working,
recovered by cycling only `usb0`. `drivers/usb/0010` fixes one reproducible
cause: a `NETDEV_TX_BUSY` during the timer flush abandoned the partial NTB; it
now retries at the usual 300 us interval. The runner's endpoint state was not
captured, so it is not proof that both stalls had this cause.

A 16 KiB NTB need not fit a FIFO at once: DWC2 splits transfers and streams
packets through the FIFO, and the 2056-entry total is in 32-bit words. No
FIFO, DMA, ZLP or NTB-size change is justified by those totals alone. ACM
surviving also differs from the shared OUT FIFO starvation fixed by
`drivers/usb/0008`. Capture endpoint state and request completions during a
stall before changing any of those.
