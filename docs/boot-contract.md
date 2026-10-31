# Boot contract

What an installed system has to put on a device so Orchard can boot it. It is
two standards, applied with as few additions as possible:

- [Boot Loader Specification](https://uapi-group.org/specifications/specs/boot_loader_specification/) (BLS), Type #1 entries;
- [Discoverable Partitions Specification](https://uapi-group.org/specifications/specs/discoverable_partitions_specification/) (DPS), for partition types.

Status (2026-10-29): agreed direction, not yet implemented by the CLI or an
installer. It replaces the old layout (root directory on an "OmarchySystems"
ext4 partition, launcher kernel, `switch_root`). Old installs are not migrated.

## Who does what

| Part | Does | Never does |
|---|---|---|
| Bootloader (m1n1 + host) | Reads the boot partition, lists the entries, loads the chosen entry's kernel, initrd and DTB, applies the ADT fill-ins, boots | Mount or read a root filesystem, write to storage |
| Installer | Creates the boot partition once; creates a root partition per system; unpacks the system's root image and orchard's platform layer into it; writes the system's entry and its files from the layer | Touch APFS beyond the agreed resize; touch another system's partition or files |
| Installed system | Owns its root partition | Install a kernel, initrd or boot entry; touch another system's files |

## Kernel, DTBs and modules

They are orchard's, not the distribution's. A distribution's root image holds no
`/usr/lib/modules`; one image serves every platform. orchard's platform layer
(`orchard.image.platform`, one per platform) holds `/usr/lib/modules/RELEASE/`
with `vmlinuz`, the DTB of every board of the platform under `dtb/` and the
modules, plus the regulatory database and hardware defaults under `/usr/lib`
(orchard's `system/`). The installer writes it into the new root after the root
image and refuses a file the image already has. Then it writes the entry below
itself: the kernel, orchard's initrd and the board's DTB from the layer. So no
distribution needs `kernel-install`, mkinitcpio or dracut, and every init system
works the same way.

orchard's initrd (`boot/initramfs/root`) holds BusyBox and the platform's storage
modules only. It loads them, waits up to 30 s for the partition that
`root=PARTUUID=` names, mounts it (`rootfstype=`, `rootflags=`, `ro`/`rw` as
given) and starts its `/sbin/init` (or `init=`). If any step fails, it says
which one and opens a shell on the console.

## Partitions (GPT)

| Partition | Type GUID | Filesystem | Count |
|---|---|---|---|
| Apple container | (as Apple left it) | APFS | 1, untouched |
| Boot | XBOOTLDR `bc13c2ff-59e6-4262-a352-b275fd6f7172` | FAT32 | Exactly 1, shared by all systems, at least 1 GiB |
| Root | DPS arm64 root `b921b045-1df0-41c3-af44-4c6f280d3fae` | The system's choice | 1 per system |

- **XBOOTLDR, not ESP.** There is no EFI firmware. XBOOTLDR is the BLS/DPS type for a boot partition that a non-EFI loader reads. More than one XBOOTLDR partition is an error, never a choice.
- **Partition identity only.** Roots are named by PARTUUID in their entries, never by partition number or position.
- **Inside its root partition, a system uses its native layout,** for example upstream Omarchy's btrfs subvolumes.

## Entries

The layout follows BLS:
- one file per entry under `/loader/entries/<entry-id>.conf` on the boot partition;
- the entry's files under `/<entry-token>/<version>/`.

The entry token is the system's id and the first group of its root's PARTUUID, for example `omarchy-4d6c1a2b`. It keeps systems apart, two installs of one distribution too. The entry is `/loader/entries/<entry-token>-<version>.conf`.

```
title      Omarchy
version    6.18.2-orchard
sort-key   omarchy
linux      /6a1f…/6.18.2-orchard/linux
initrd     /6a1f…/6.18.2-orchard/initrd
devicetree /6a1f…/6.18.2-orchard/t8010-j71b.dtb
options    root=PARTUUID=4d6c…-… rw quiet
```

| Key | Orchard rule |
|---|---|
| `linux` | Required. arm64 `Image`, raw or gzip-compressed |
| `initrd` | Optional and repeatable. Several are concatenated in order, as BLS specifies |
| `devicetree` | **Required.** The DTB for this board. m1n1 applies the ADT fill-ins (MAC addresses, calibration, memory, CPUs) to it, so the plain DTB from the kernel build is used. The installer copies the board's DTB from the platform layer. |
| `options` | Required. It must contain `root=PARTUUID=…` of the system's own root partition. Root discovery without `root=` is not used, because one device can hold several roots. |
| `title`, `version`, `sort-key` | As BLS: shown in the host's list and used for its order. |
| `machine-id`, `architecture` | Optional. If `architecture` is present, it must be `aa64`. |
| `devicetree-overlay`, `efi`, `uki` | Not supported. Such entries are listed as unbootable. |

**Boot counting** (`+tries` in the file name) is not supported yet. Such entries boot as if they had no counter.

## Splash

A system's boot splash ships in its root as `/usr/share/orchard/splash`; the
installer copies it next to the entry's kernel (`/TOKEN/VERSION/splash`).
When an entry starts, orchard's initrd paints it centred on the framebuffer
as soon as it has mounted the root, before the system starts; it stays until
the system clears it. Entries start quiet (`fbcon=map:9`), as the recovery
does, so no console text covers it. A missing or malformed splash only costs
the picture, never the boot.

The format is `OSPL`, then width and height as little-endian u32, then
width × height pixels as the bytes b, g, r, 0 (the framebuffer's XRGB8888), top row first. Distributions
write it from the same artwork their own m1n1 draws, so install and launch
show the same picture.

## Selection

The host lists every valid entry in BLS order and boots the one the user picks. Invalid entries are listed with the reason and never hidden. Reasons include:
- missing files;
- a `devicetree` whose name doesn't match the board;
- a missing `root=PARTUUID`.

Without a choice, the host boots the first entry in BLS order. A remembered default, like systemd-boot's `loader.conf`, can come later.

## Host and m1n1

m1n1 does the storage boot and the host only chooses (bootloader decision of
2026-10-29). This section is the interface between them; the Rust launcher and
`transports/m1n1.py` implement the host side against it. Status per item:
**done** (in `boot/m1n1/patches/orchard`) or **open**.

### Transport

m1n1's USB proxy (CDC-ACM `1209:316d`, interface 0) with m1n1's own request and
reply framing and checksums. The host speaks it with its own small client, not
Asahi's proxyclient.

**Open:** m1n1 puts the device's identity into its USB serial string, as
Orchard's gadgets do (`ECID:… CPID:… BDID:… BOARD:j71b`), so `orchard devices`
recognises the m1n1 stage as the same device. First check what Hoolock's DWC2
sends today.

### Who does what

| m1n1 | Host |
|---|---|
| Reads storage (read-only), finds XBOOTLDR, parses and checks the entries, sorts them | Chooses an entry, or takes the first one |
| Paints the splash, loads the entry, applies the DT fill-ins, shuts storage down, boots | Shows entries, problems and progress |
| Reports problems as text | Writes nothing, parses no disk structures, doesn't touch the framebuffer |

Nothing here needs a host: m1n1 can later boot the first (or a remembered)
entry by itself, as in Asahi's model.

### Operations

The `0x15xx` ops are Orchard-local (Asahi and Hoolock end at `0x14xx`) and get
proper numbers when they go upstream.

| Op | Status | Arguments | Result |
|---|---|---|---|
| `P_MAIN_STORAGE_INIT`, `_SHUTDOWN` (`0xf08`, `0xf09`) | exists | – | ok or error |
| `P_BOOT_ENTRIES` (`0x1500`) | **done** (0007) | `args[0]`: a 16-byte buffer; `args[1]`: the board's DTB file name, or 0 | m1n1 writes the text's address and size into the buffer |
| `P_BOOT_ENTRIES_FREE` (`0x1501`) | **done** (0007) | `args[0]`: the same buffer | – |
| `P_BOOT_START` | **open** | the entry id; a splash flag | replies OK, then starts the kernel; on failure an error text and the proxy stays |

`P_BOOT_ENTRIES` needs `P_MAIN_STORAGE_INIT` first. Its text is BLS syntax:

```
contract 1
entry omarchy-4d6c1a2b-6.18.2-orchard
title Omarchy
version 6.18.2-orchard
sort-key omarchy
linux /omarchy-4d6c1a2b/6.18.2-orchard/linux
initrd /omarchy-4d6c1a2b/6.18.2-orchard/initrd
devicetree /omarchy-4d6c1a2b/6.18.2-orchard/t8010-j71b.dtb
options root=PARTUUID=4d6c…-… rw quiet
root 4d6c…-…
problem devicetree is not t8010-j71b.dtb

```

- The first line is `contract N`. A host refuses a contract it doesn't know.
- Then one block per entry, in boot order, each ended by an empty line:
  - the `entry` id: the file name without `.conf` and without a boot counter;
  - the entry's BLS keys in file order (keys BLS doesn't define are left out, so they can't pose as the lines below);
  - `root` if the options name a partition;
  - a `problem` line per reason the entry can't boot, using the rules of [Entries](#entries) and [Selection](#selection). An entry without problems is bootable.
- If the disk itself can't be read (no GPT, not exactly one XBOOTLDR, no FAT), a single `error TEXT` line follows the contract line instead.
- Order is systemd-boot's: entries with a sort-key first, by sort-key, machine-id and newest version (UAPI version comparison), then by id, newest first.

`P_BOOT_START` (open), in this order:
1. Paint the entry's splash (`splash` in the directory of its `linux`, see [Splash](#splash)) unless the flag says not to. Painting in m1n1 gets its own device test first: the host painting over the proxy (`P_FB_BLIT`) crashed m1n1 on J71b.
2. Load linux (gunzipped if needed), the initrds (concatenated) and the devicetree. m1n1 makes room for the kernel slot (512 MiB) itself. A heap allocation past the limit panics m1n1, so a host that uses the boot manager doesn't set a heap limit (`P_HEAPBLOCK_SET_LIMIT`); today's client sets 128 MiB as soon as it connects.
3. Shut storage down. Only now, so a failure before it leaves storage up for another entry: a second `main_storage_init` on A10 has never been tested. The NVMe strap release (0006) stays the backend's job.
4. Set the entry's options as bootargs, start cpufreq and the secondary CPUs, prepare the DT, reply OK, boot.

The entry's own `options` decide quiet or verbose; the host has no flag for that.

Boots from RAM keep the existing ops (memory writes, `P_GZDEC`, `P_KBOOT_SET_CHOSEN`,
`_SET_INITRD`, `_PREPARE_DT`, `_BOOT`), as `linux.py` uses them. **Open:** one
`P_BOOT_SPEC` op (kernel, DTB, initrd, arguments) later, so both kinds of boot end in
the same m1n1 code, splash included.

### Versions

- An op m1n1 doesn't know gets its "bad command" status. Then the host falls back to its own reader (`bootdisk.py`) until that is deleted.
- Changes to the text change the `contract` line, not the op numbers.

### Guarantees

- No op here writes to storage.
- Every op returns in bounded time, except a successful `P_BOOT_START` after its reply.
- After a failure m1n1 stays in the proxy, so the host can report it or offer another entry. The host doesn't retry on its own.
- Every change must be tested on both ways into m1n1: Pongo, and iBoot (`launcher-kit`, `./launch boot --bare`, then the client over the proxy). Device time goes through the user.

Which SoCs have an m1n1 storage backend is in [bring-up/bootloader/experiments.md](bring-up/bootloader/experiments.md#per-device-menu).
