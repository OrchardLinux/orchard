# Install protocol

How an installer on the computer drives the install system on the device over
raw USB: no network, no SSH, no keys, no drivers. The receiver on the device
is `orchard-install-usb` from `install-tools.tar.gz`; hosts are orchard2's
`crates/usb` (the graphical installer) and, later, the Python CLI.

Status: draft, protocol version 1. Covers GPT disks (64-bit boards). LwVM
disks (32-bit boards) follow as version 2 with their own plan shape.

## The boot it talks to

The host boots the release's kernel with the install add-on appended to its
initrd, as today (`install_direct.add_on`). The add-on needs only:

- `install/ecid`: the device the host means, checked by the receiver;
- the programs and files of `install-tools.tar.gz`, unchanged.

`authorized_keys` and `host_key` are optional. The Python CLI still passes
them while it drives installs over SSH; the graphical installer doesn't.

The install system starts, in this order (`boot/initramfs/install/run`):

1. the receiver, on a FunctionFS instance: it writes its interface's
   descriptors before the gadget binds;
2. the USB gadget with the receiver's interface, serial console and USB
   network, in one configuration. If the receiver isn't up within 5 seconds,
   the gadget binds without it; serial and SSH still come up. The receiver
   answers `Info` as soon as the host has selected its interface;
3. in the background, Dropbear for debugging: with the add-on's `host_key`
   when there is one, else `-R` (host key made on the first SSH connection,
   not at boot). Without `authorized_keys` it lets no one in; the serial
   console always works.

A host waits for nothing but the receiver: no SSH, no DHCP, no host key.

## Finding the receiver

| | |
|---|---|
| USB ID | `05ac:4146` (product string `Orchard Install`) |
| Serial number string | `ECID:<hex> BOOT:<boot id> BOARD:<board>`, as today |
| Interface | class `0xff`, subclass `0x4f`, protocol `0x01`, one bulk OUT and one bulk IN endpoint |

The interface is vendor-specific, so macOS, Linux and Windows (WinUSB) let a
user-space host claim it without a driver. The host checks the ECID in the
serial number before claiming it, and again in `Info`.

The serial number repeats the add-on's ECID; `Info.ecid` is the device's own,
which the receiver reads from the live Apple device tree (the MTD named
`adt`, `/chosen/unique-chip-id`, as the recovery's `identity.py`). If it
can't be read or differs from `install/ecid`, the receiver is `failed` with
`wrong-device` from the start and answers every request but `Info` with
`wrong-device`.

### Sessions

After claiming the interface, the host selects its alternate setting 0
(`SET_INTERFACE`). That, like a new configuration or a replug, starts a fresh
session on the device: anything half-sent before is dropped, a transfer in
progress is cut (a cut stream becomes `stream-incomplete`), and events
queued for the old session are discarded. The phase and staged files stay.
A host that re-attaches after a lost link does the same.

The device reads exactly each header and each payload, so the host may split
its writes into transfers however it likes and needs no zero-length packets.
Every message the device sends is one transfer that ends in a short packet
(a JSON payload is padded with one space when header and payload would fill
whole 64-byte packets), so a host reading into a large buffer gets each
message without waiting for the next one.

## Framing

Every message, in both directions, is an 8-byte header and a payload:

| Offset | Size | Field |
|---|---|---|
| 0 | 1 | type |
| 1 | 1 | flags, 0 |
| 2 | 2 | reserved, 0 |
| 4 | 4 | payload length, little-endian, at most 1 MiB (1,048,576) |

Payloads are UTF-8 JSON objects, except `Data`, which is raw bytes.

- A header with a non-zero flag or reserved field, a longer payload, type
  `0x00` or a device-to-host type (`0x80` and up) is refused with `protocol`;
  the framing is lost, so the receiver drops what it was doing and ignores
  everything until a fresh session (see Sessions).
- A host-to-device type this version lacks (`0x07` to `0x7f`) is answered with
  `unknown-request`; its payload is skipped and the framing kept.
- A payload that isn't a JSON object, or `Data` outside a transfer, is
  refused with `protocol`; the framing is kept.
- Any message but `Data` inside a transfer is refused with `protocol` and
  cuts the transfer (one answer for both).

The host sends one request at a time and reads until its answer (`Ok` or
`Error`). `Event`s may arrive at any time, before and between answers; the
host reads the IN endpoint continuously.

| Type | Direction | Name | Payload |
|---|---|---|---|
| `0x01` | host → device | `Info` | `{}` |
| `0x02` | host → device | `Inspect` | `{}` |
| `0x03` | host → device | `Put` | `{"name", "bytes", "sha256"}`, then `Data` |
| `0x04` | host → device | `Plan` | the install plan |
| `0x05` | host → device | `Stream` | `{"bytes", "sha256"}`, then `Data` |
| `0x06` | host → device | `Data` | raw bytes |
| `0x81` | device → host | `Ok` | the request's result |
| `0x82` | device → host | `Error` | `{"code", "message"}` |
| `0x83` | device → host | `Event` | `{"event", ...}` |

`Put` and `Stream` announce a transfer and are answered with `Ok` (`{}`) once
the device is ready; the host then sends exactly `bytes` bytes as `Data`
messages, and the device answers once more, with `Ok` after the last byte has
been written and its SHA-256 checked, or `Error`. A transfer of 0 bytes gets
both answers at once. `Data` beyond the announced size is refused with
`protocol` and cuts the transfer.

## Requests

### Info

Answered in every state, also while the device is finishing:

```json
{"protocol": 1, "ecid": "1a2b3c4d5e6f", "board": "j71b", "boot_id": "…",
 "phase": "ready", "staged": ["platform.tar.gz"], "stream": null, "record": null, "error": null}
```

`board` is the board as in the serial number (the device tree's first
`compatible` without `apple,`); `ecid` is lowercase hex without padding, or
`null` when it couldn't be read. `staged` lists the files `Put` staged that
are still there (the finishing program deletes the ones it consumes).

The host checks `protocol` first and stops on a version it doesn't speak. A
device that receives a request it doesn't know answers `unknown-request`.

`phase` is one of:

| Phase | Meaning | Host may send |
|---|---|---|
| `ready` | nothing written yet | `Inspect`, `Put`, `Plan` |
| `planned` | the partition table is written | `Put`, `Stream` |
| `stream-incomplete` | a stream was cut off or didn't verify; nothing of it counts | `Stream`, from the start |
| `finishing` | the image verified; the device finishes by itself | `Info` |
| `done` | installed; `record` is set | `Info` |
| `failed` | stopped; `error` is `{"code", "message"}` | `Info`, `Inspect` |

`Info` is allowed in every phase. Anything not in the table is answered
with `bad-state`. `stream` is `{"written", "bytes"}` while streaming, else
`null`.

### Inspect

Read-only. The disk and every partition on it, as the device reads them:

```json
{"disk": {"path": "/dev/nvme0n1", "sector": 4096, "first": 6, "last": 31249994, "bytes": 128000000000,
          "table_sha256": "…"},
 "partitions": [{"number": 1, "start": 6, "blocks": 30000000, "name": "Container",
                 "type": "…", "partuuid": "…", "fs": "apfs", "fs_uuid": null, "label": null}],
 "free": [{"start": 30000006, "blocks": 1249989}]}
```

This is `install_device.py inspect`'s `layout`, plus `disk.bytes`; `fs`,
`fs_uuid` and `label` come from each partition's first 4 KiB as
`install_direct.probe` reads them (`ext4`, `vfat`, `apfs` or `null`). The
disk is the internal one (`nvme<N>n1`, or `asp<N>n1` on A7-A8X), waited for
up to 30 seconds; a disk without a GPT, or a GPT whose checksums are wrong,
answers `disk`. Whether
there is room, and for what, is `install_layout.py`'s decision on the host,
which `orchard2` mirrors. iOS's used space inside APFS is not visible here.

### Put

Stages one file in `/run/orchard` under `name`: a plain file name (letters,
digits, `.`, `-`, `_`, at most 100 characters, not starting with `.`), never
a path. Names ending in `.part` and the receiver's own files (`done`,
`plan.json`, `timeline`, `os-release`, `root.map`, `write.log`,
`finish.log`, `install.log`, `install-usb.log`, `install_device.py`,
`boot-entries`) are refused with `bad-name`. Files are written `0600`,
root's; a second `Put` of the same name replaces it. A file that doesn't fit
in the device's memory is refused with `too-large`. The second answer is
`{"name", "bytes"}`. Allowed when `ready` or `planned`.

The names are the CLI's: `platform.tar.gz` (the plan's `layer`),
`firmware.tar.gz` (`firmware`), the kernel package under its own file name
(`kernel_package`), and `secrets.json`. `secrets.json`
(`{"password", "pin"}`) must be staged whenever the plan has an `account`:
`Stream` refuses with `missing-file` without it. The finishing program reads
and deletes it, the receiver deletes it after the finishing step in any case,
the receiver itself never reads it, and neither secret ever appears in an
event, an error or a log.

### Plan

The install plan, version 2, exactly as `install_layout.plan` makes it:
`disk` (path, sector, `table_sha256`), `remove`, `boot`, `root`, `image`,
`layer`, `entry`, `firmware`, `account`, and optionally `kernel_package`.
`account` may carry `ssh_keys`, the public keys the new user may sign in
with (`ssh-ed25519 …` lines); without them the installed system takes no SSH
logins until access is added later. The whole plan is one message, so at
most 1 MiB.

Files are named as the CLI names them, each staged by `Put` under the plan
path's file name:

```json
"layer": {"path": "/run/orchard/platform.tar.gz", "bytes": 0, "sha256": "…", "expanded_bytes": 0},
"firmware": {"path": "/run/orchard/firmware.tar.gz", "bytes": 0, "sha256": "…"},
"kernel_package": {"path": "/run/orchard/linux-orchard-….pkg.tar.zst", "base": "pacman",
                   "bytes": 0, "sha256": "…", "version": "…"}
```

`firmware` may be `null`. `kernel_package` is the shape `install_direct.kernel_package`
builds (`base` is `pacman` or `apk`, the root's package manager); without it
the finishing program installs the platform layer's files, as today. Every
`path` must be `/run/orchard/<plain name>`, else `plan-mismatch`.

`image` is the root image's `{"bytes", "sha256", "expanded_bytes"}` plus
`map`, the content of the release's `root.map.json` as it is:

```json
"map": {"format": 1, "block_size": 4096, "blocks": 2000000, "restored_bytes": 3000000000,
        "ranges": [[0, 1024], [32768, 2048]]}
```

`ranges` are `[start, count]` runs of 4 KiB blocks of the partition, in
stream order and ascending. The device turns them into `orchard-write`'s map
itself (`/run/orchard/root.map`, as `service.py` does); a map without
`block_size` 4096 and `ranges` is a `plan-mismatch`.

Allowed when `ready`. The device checks the plan against the live table
before it changes anything (`install_device.check_plan`): the table must
still match `table_sha256` and `sector`, `disk.path` must be the device's
disk, new partitions lie only in free space or the one replaced Linux
partition, and APFS and every non-Linux partition stay untouched. A plan for
an LwVM disk (`boot` `null` or `disk.table` `lwvm`) answers `plan-version`.
Then it writes both copies of the new table as `install_direct.new_tables`
computes them, waits for the partition nodes, reads the table back, writes
the empty FAT32 boot filesystem when the plan creates the boot partition,
answers `Ok` with the new partitions' nodes (`{"root": "/dev/nvme0n1p5",
"boot": "/dev/nvme0n1p4"}`), and is `planned`. A mismatch answers
`layout-changed`, `plan-mismatch` or `plan-version` and changes nothing; the
host inspects again. A disk error while writing makes the device `failed`
(`disk`): the table may be half written, and only a fresh boot and inspect
are safe.

The plan is kept in `/run/orchard/plan.json`, `0600`, with `present` added:
the PARTUUIDs that stay on the disk, which the finishing program needs.

### Stream

The root image (`root.blocks.zst` with the map from the plan's `image`) into
the new root partition, verified while it is written, through `orchard-write`
as today. Allowed when `planned` or `stream-incomplete`. `bytes` and
`sha256` must be the plan's `image`, else `plan-mismatch`. Before anything is
written, every file the plan names (`layer`, `firmware`, `kernel_package`)
must be staged, and `secrets.json` too when the plan has an `account`, else
`missing-file` naming the file; a map that reaches past the root partition
answers `too-large`.

If the link drops, a fresh session starts, or the host stops before the last
byte, the phase becomes `stream-incomplete`: a zstd stream can't resume
mid-way, so the host sends `Stream` again from byte 0. Bytes that don't match
`sha256` also leave it `stream-incomplete` (answer `sha-mismatch`); a writer
that fails on good bytes makes it `failed` (`disk`).

After the last byte the device checks the image's SHA-256, answers `Ok`
(`{"bytes", "sha256"}`), and from then on finishes by itself (`finishing`),
whether or not the host stays connected: filesystem identity and size
(`tune2fs`, `resize2fs`), the mounts, then the finishing program of today's
installs inside the new root (`install_device.py finish`, from the add-on):
firmware, account, kernel package or platform layer, boot entry; then unmount
and sync. No command from the host runs on the device. When it is done the
phase is `done`; after the `done` event has gone out (at most 2 seconds) the
receiver writes `/run/orchard/done`, and the device boots into the installed
system, which shows up under its own USB identity (`05ac:4145`).

## Events

```json
{"event": "heartbeat", "phase": "planned"}
{"event": "stream", "written": 104857600, "bytes": 745000000}
{"event": "step", "step": "account"}
{"event": "done", "record": {…}}
{"event": "failed", "code": "…", "message": "…"}
```

- `heartbeat`: every second, in every phase. A host that hears nothing for 5
  seconds treats the link as lost and re-attaches; the device keeps going.
- `stream`: at least every 64 MiB and every second while streaming.
- `step`: when a finishing step starts, in this order: `filesystem`,
  `firmware` (only with `firmware`), `account` (only with `account`),
  `kernel` (the kernel package, or the platform layer's files), `entry`,
  `sync`.
- `done` and `failed`: once, at the end; `Info` keeps reporting them.

## The completion record

`Info.record` and the `done` event, what the host keeps about the install:

```json
{"root_partuuid": "…", "boot_partuuid": "…", "entry": "omarchy-a-1a2b3c4d-7.0.0",
 "os": {"id": "omarchy-a", "name": "Omarchy", "version": "0.4.0"},
 "image_sha256": "…", "layer_sha256": "…"}
```

`entry` is the boot entry the finishing program wrote (its file name in
`/loader/entries` without `.conf`); `os` is the new root's `os-release`
`ID`, `NAME` and `VERSION_ID`, each `null` when missing; the hashes are the
plan's, which the device verified.

## Errors

| Code | When |
|---|---|
| `protocol` | malformed header or JSON, payload too large, `Data` without a transfer or beyond it, a request inside a transfer |
| `unknown-request` | a request type this version doesn't have |
| `wrong-device` | the add-on's ECID is not this device's |
| `bad-state` | the request isn't allowed in this phase |
| `bad-name` | a `Put` name that isn't a plain file name |
| `sha-mismatch` | a transfer's bytes don't match its SHA-256 |
| `too-large` | a transfer doesn't fit where it goes |
| `missing-file` | `Stream` before the plan's files, or `secrets.json` for its account, are staged |
| `plan-version` | a plan version the device doesn't know |
| `layout-changed` | the table changed since `Inspect` |
| `plan-mismatch` | the plan would touch something it may not |
| `disk` | reading or writing the disk failed |
| `finish` | a finishing step failed; `message` names it |

Messages are for people; hosts act on codes.

## SSH access later

The install never needs SSH. A user who wants it afterwards gets it two ways:

- at install time, the plan's `account.ssh_keys` (the CLI asks this as its
  last question; the graphical installer may offer the computer's existing
  `~/.ssh/*.pub`);
- after the install, an `orchard` command that signs in once on the installed
  system's USB serial console with the user's password and adds the
  computer's public key. Planned, not built yet.
