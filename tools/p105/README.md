# Capturing the P105 flash erase footprint

A P105 device build stages the optional `p105_capture.ko` module in
`lib/modules/<release>/extra/`. It has no device alias and must be loaded
explicitly on a running matching kernel. It reads through the bound H2FMI
driver on the measured 64 GB P105. The module accepts the physical bus, CAU
and block as read-only parameters at load. It samples that block index
across all 16 lanes and the adjacent blocks on the selected lane, in both
normal and SLC views. Each open of `/dev/p105-capture` starts a new scan.

Use the PR's `p105-read-only-measurement` artifact as the kernel directory for
the current [hardware-check RAM project](../../examples/hardware-check/).
Set `KERNEL_DIR` to the directory containing `zImage`, `dtbs/`, `lib/` and
`orchard.commit`. From the Orchard checkout, verify the files and the live
kernel before taking a baseline. `bin/orchard exec` uses the running session's
SSH transport. The kernel notes digest distinguishes builds with the same
`uname -r`.

```sh
(cd "$KERNEL_DIR" && shasum -a 256 -c SHA256SUMS) || exit 1
notes=$(bin/orchard exec -- timeout -k 5 10 sha256sum /sys/kernel/notes) || exit 1
test "${notes%% *}" = "$(cat "$KERNEL_DIR/kernel.notes.sha256")" || exit 1
bin/orchard exec -- timeout -k 5 10 cat /proc/self/mountinfo || exit 1
bin/orchard exec -- timeout -k 5 90 hardware-check storage || exit 1
```

Confirm that `/` is the RAM root. The storage check uses mountinfo device
numbers to reject a mounted internal disk or partition, including read-only
mounts through aliases. Record the check's 4 KiB read before the baseline as
part of the trial. Record the board, `orchard.commit`, target
coordinates and the issued operation. With the system quiescent, load the
module using the measured bus, CAU and block for this trial.

```sh
bin/orchard exec -- timeout -k 5 30 modprobe p105_capture bus="$BUS" cau="$CAU" block="$BLOCK"
```

Stream the flash data and out-of-band metadata directly into a private host
file. Use a fresh directory for each trial. A failed read leaves a partial file.

```sh
umask 077
bin/orchard exec -- timeout -k 5 600 cat /dev/p105-capture > before.ppns.partial &&
    mv before.ppns.partial before.ppns
```

Repeat the read into `after.ppns` after the independently recorded operation.
An interior target produces 3,456 PPNS records (56,899,584 bytes) per phase.
The driver supplies the exact managed status, four 4 KiB chunks and their
out-of-band bytes. An I/O error or a row without a data phase terminates the
stream. The comparator reports an inconclusive result for a refresh-status
row, a missing record or an already blank control.

Create the sibling `before.json` and `after.json` manifests using the fields
documented in [compare_erase.py](compare_erase.py), the actual target, the
view of the issued operation and the SHA-256 of each complete PPNS file.
Then run:

```sh
python3 tools/p105/compare_erase.py before.json after.json
```

The capture calls the provider's page reader and leaves SFTL ownership in
place. A scan spans many transactions. Other activity may change the medium
or its read counters during the scan.

## Read live counters in iOS

On a paired Mac or Linux host with `ideviceinfo` installed, select the
64 GB iPad mini running build 13G36. Use its identifier from `idevice_id -l`.
The paired USB service can answer while the iPad runs iOS.

```sh
python3 tools/p105/capture_ios_counters.py --udid "$P105_UDID" --samples 4 ./p105-ios-counters
```

The collector checks the board, build and flash geometry. For each sample
it compares the two provider records against their copies in the native
statistics table. It creates a private directory for the responses and
records the time window for each query. A disconnect ends the run. Keep
the raw responses on the measurement host.

To compare two controlled intervals, bracket each with a pair of saved
samples from the same iPad. Record which reads completed in each interval
and their order. Pass the four binary responses to the host comparator.

```sh
python3 tools/p105/compare_ios_intervals.py a-before.bin a-after.bin b-before.bin b-after.bin
```

The comparator checks every byte of each ending response. A match means
the same 64-bit counter increments explain both intervals, including any
wraparound. On the measured iPad two pairs of opposite read orders matched.
[64 GB P105 measurements](../../docs/bring-up/p105-sftl-device-qualification.md)
