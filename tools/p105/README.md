# Capturing the P105 flash erase footprint

A P105 device build stages the optional `p105_capture.ko` module in
`lib/modules/<release>/extra/`. It has no device alias and must be loaded
explicitly on a running matching kernel. It reads through the bound H2FMI
driver on the measured 64 GB P105. The module accepts the physical bus, CAU
and block as read-only parameters at load. It samples that block index
across all 16 lanes and the adjacent blocks on the selected lane, in both
normal and SLC views. Each open of `/dev/p105-capture` starts a new scan.

For an authorized observation, use a quiescent system and record its target,
device, trial, driver revision and issued operation separately. On the device,
load the matching module with the actual target values:

```sh
timeout -k 5 30 modprobe p105_capture bus="$BUS" cau="$CAU" block="$BLOCK"
```

Stream directly into a private host file from the already accessible device.
This example uses an existing SSH connection with permission to read the
capture node. The stream contains full flash data and out-of-band metadata.

```sh
umask 077
ssh "$DEVICE" 'timeout -k 5 600 cat /dev/p105-capture' > before.ppns.partial &&
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
