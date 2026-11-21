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

The install system starts, in this order:

1. the USB gadget with the receiver's interface, serial console and USB
   network;
2. the receiver, which answers `Info` as soon as its endpoints are up;
3. in the background, Dropbear for debugging, with `-R` (host key made on the
   first SSH connection, not at boot). Without `authorized_keys` it lets no
   one in; the serial console always works.

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

## Framing

Every message, in both directions, is an 8-byte header and a payload:

| Offset | Size | Field |
|---|---|---|
| 0 | 1 | type |
| 1 | 1 | flags, 0 |
| 2 | 2 | reserved, 0 |
| 4 | 4 | payload length, little-endian, at most 1 MiB (1,048,576) |

Payloads are UTF-8 JSON objects, except `Data`, which is raw bytes. A message
with an unknown type, a non-zero flag or reserved field, or a longer payload
is refused with `protocol` and the receiver drops what it was doing.

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

`Put` and `Stream` announce a transfer and are answered with `Ok` once the
device is ready; the host then sends exactly `bytes` bytes as `Data`
messages, and the device answers once more, with `Ok` after the last byte has
been written and its SHA-256 checked, or `Error`.

## Requests

### Info

Answered in every state, also while the device is finishing:

```json
{"protocol": 1, "ecid": "1a2b3c4d5e6f", "board": "t8010-j71b", "boot_id": "…",
 "phase": "ready", "staged": ["layer.tar.zst"], "stream": null, "record": null, "error": null}
```

The host checks `protocol` first and stops on a version it doesn't speak. A
device that receives a request it doesn't know answers `unknown-request`.

`phase` is one of:

| Phase | Meaning | Host may send |
|---|---|---|
| `ready` | nothing done yet | `Inspect`, `Put` |
| `planned` | the partition table is written | `Put`, `Stream` |
| `stream-incomplete` | a stream was cut off; nothing of it counts | `Stream`, from the start |
| `finishing` | the image verified; the device finishes by itself | `Info` |
| `done` | installed; `record` is set | `Info` |
| `failed` | stopped; `error` is `{"code", "message"}` | `Info`, `Inspect` |

`stream` is `{"written", "bytes"}` while streaming, else `null`.

### Inspect

Read-only. The disk and every partition on it, as the device reads them:

```json
{"disk": {"path": "/dev/nvme0n1", "sector": 4096, "first": 6, "last": 31249994, "bytes": 128000000000,
          "table_sha256": "…"},
 "partitions": [{"number": 1, "start": 6, "blocks": 30000000, "name": "Container",
                 "type": "…", "partuuid": "…", "filesystem": "apfs"}],
 "free": [{"start": 30000006, "blocks": 1249989}]}
```

This is `install_device.py inspect`'s `layout`, plus `disk.bytes`. Whether
there is room, and for what, is `install_layout.py`'s decision on the host,
which `orchard2` mirrors. iOS's used space inside APFS is not visible here.

### Put

Stages one file in `/run/orchard` under `name`: a plain file name (letters,
digits, `.`, `-`, `_`), never a path. Files are written `0600`, root's. A
second `Put` of the same name replaces it.

The plan names the files the install needs: `layer` (orchard's platform
layer), optionally `firmware` and a kernel package. `secrets.json`
(`{"password", "pin"}`, when the plan has an account) is staged the same way;
the device deletes it as soon as the finishing step has read it, and neither
secret ever appears in an event, an error or a log.

### Plan

The install plan, version 2, exactly as `install_layout.plan` makes it:
`disk` (path, sector, `table_sha256`), `remove`, `boot`, `root`, `image`,
`layer`, `entry`, `firmware`, `account`. `account` may carry `ssh_keys`, the
public keys the new user may sign in with (`ssh-ed25519 …` lines); without
them the installed system takes no SSH logins until access is added later.

The device checks the plan against the live table before it changes anything
(`install_device.check_plan`): the table must still match `table_sha256`, new
partitions lie only in free space or the one replaced Linux partition, and
APFS and every non-Linux partition stay untouched. Then it writes the new
table, waits for the partition nodes, answers `Ok`, and is `planned`. A
mismatch answers `layout-changed` or `plan-mismatch` and changes nothing; the
host inspects again.

The plan is kept in `/run/orchard/plan.json`, `0600`.

### Stream

The root image (`root.blocks.zst` with the map from the plan's `image`) into
the new root partition, verified while it is written, as `orchard-write`
does today. Allowed when `planned` or `stream-incomplete`; every file the
plan names must be staged first, else `missing-file`.

If the link drops or the host stops before the last byte, the phase becomes
`stream-incomplete`: a zstd stream can't resume mid-way, so the host sends
`Stream` again from byte 0.

After the last byte the device checks the image's SHA-256 and answers `Ok`,
and from then on finishes by itself (`finishing`), whether or not the host
stays connected: filesystem identity and size, the platform layer, kernel
package, firmware, account, boot entry, sync. It runs the finishing program
of today's installs inside the new root; no command from the host runs on
the device. When it is done the phase is `done`, and the device boots into
the installed system, which shows up under its own USB identity
(`05ac:4145`).

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
- `step`: when a finishing step starts, in this order: `filesystem`, `layer`,
  `kernel`, `firmware`, `account`, `entry`, `sync`.
- `done` and `failed`: once, at the end; `Info` keeps reporting them.

## The completion record

`Info.record` and the `done` event, what the host keeps about the install:

```json
{"root_partuuid": "…", "boot_partuuid": "…", "entry": "omarchy-a-…",
 "os": {"id": "omarchy-a", "name": "Omarchy", "version": "0.4.0"},
 "image_sha256": "…", "layer_sha256": "…"}
```

## Errors

| Code | When |
|---|---|
| `protocol` | malformed header or JSON, payload too large, `Data` without a transfer |
| `unknown-request` | a request type this version doesn't have |
| `wrong-device` | the add-on's ECID is not this device's |
| `bad-state` | the request isn't allowed in this phase |
| `bad-name` | a `Put` name that isn't a plain file name |
| `sha-mismatch` | a transfer's bytes don't match its SHA-256 |
| `too-large` | a transfer doesn't fit where it goes |
| `missing-file` | `Stream` before the plan's files are staged |
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
