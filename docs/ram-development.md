# Local RAM-boot development

```sh
cd examples/minimal-boot
orchard run
```

`orchard build` and `orchard run` take the payload directory, by default the current
one. See the [README](../README.md#develop) for which orchard checkout runs.

## Platforms and boards

A payload serves every board of one platform: the platform's kernel, each board's
DTB (m1n1 picks the booted board's), one m1n1, Pongo and the tools. It goes to
`out/PLATFORM/`. `orchard run` builds what is missing or outdated for the attached
board, then boots it; `--no-build` boots the last build. `orchard build` takes the
platform from `--platform`, `--device BOARD`, `device` in the nearest
`orchard.local.toml` (searched upwards to the repository root; this checkout only,
never committed) or the attached board, and otherwise lists the platforms and
their boards. No repository and no per-person setting names a board. Distro-owned
producers can use `orchard.build.configured_device(directory)`. Installation does
not read it and still rejects incompatible devices.

## orchard.toml

An optional `orchard.toml` beside `init` names the payload (`name = "..."`) and
describes it under `[payload]`, never its board (the older `payload.toml` is
still read, with a notice):

- `transport = false` leaves the USB session helper (network, SSH) out;
- `modules = true` stages the kernel's modules without loading them;
- `firmware = true` adds the booted board's firmware: `orchard run` reads it from
  Apple's restore image once, keeps it in Orchard's private cache and adds it as its
  own initramfs layer. Firmware never lands in the project's `out/`.

## Directory content

A build directory contains an executable startup script named `init` and optional
`rootfs/` content. `init` becomes `/init`; files beneath `rootfs/` appear at the same
relative paths inside the RAM filesystem and retain their permissions. No
`build.py`, `run.py`, registration or manifest language is needed. Symlinks in the
CLI's `rootfs/` input are currently rejected, not silently dereferenced.

The CLI supplies BusyBox, the hardware's other static tools (Dropbear, and
`wpa_supplicant`, `wpa_cli` and `iw` for Wi-Fi), the device kernel and boot
framing, then writes artifacts to `DIRECTORY/out/PLATFORM/`; `orchard.toml` decides
about modules and firmware (see above). Pongo is the latest release's when this
checkout's Pongo recipe (`boot/quiet`) is unchanged since, so building needs no Xcode;
a changed recipe is built on macOS. Component override flags: `--cache`, `--kernel-dir`, `--busybox`, `--loader`, `--pongo`.
No parent-directory layout is required: `examples/`, `use_cases/`, a deeply nested
folder or an unrelated absolute path all work. Hardware resources come from the
hardware data and recipes of the orchard checkout it runs from,
not a guessed sibling directory: run a checkout's `bin/orchard` to build with its.
The default shared cache is `~/.cache/orchard/`, respecting `XDG_CACHE_HOME`;
`ORCHARD_CACHE_DIR` or `--cache DIR` overrides its root. Outputs stay in the supplied
directory's `out/`, independently of its name or location.

`orchard run DIRECTORY` verifies the last successful payload, shows the sole
connected device, asks `Boot this device? [Y/n]`, and uploads once via Pongo.
A different chip/board or an unknown payload profile produces a plain-language
warning, not a rejection. The prompt then becomes `Try booting anyway? [y/N]`;
Enter cancels, while `y` or `yes` attempts the boot. `--yes` prints the warning
and proceeds without prompting. Upload checks track the connected device's actual
identity, not the payload's expected chip/board. Exactly one device is still required.
It never installs a rootfs, calls a device RPC or claims Linux readiness. It requires
an interactive terminal by default. Use `orchard run DIRECTORY --yes` (or `-y`)
to explicitly authorize a non-interactive boot without the prompt. This skips
only confirmation: payload verification, sole-device/identity checks, operation
locking, refusal to reboot running Linux and the single-attempt policy remain.
A custom `/init` owns device behavior; the host tool cannot infer its safety.

## Assembling by hand

`linux.bin` is m1n1's own payload format; orchard adds nothing to it. With a
kernel bundle (`Image.gz`, DTBs), an initramfs and m1n1, from a published
build or your own, assemble it without orchard:

```sh
python3 producers/assemble.py --bootargs 'console=tty0 loglevel=7 rdinit=/init' \
    --loader m1n1.bin --dtb t8010-j71b.dtb --kernel Image.gz \
    --initramfs initramfs.cpio.gz --output linux.bin
```

The script uses only Python's standard library (its older path,
`src/orchard/image/assemble.py`, still runs it). The same bytes by hand:

```sh
{ cat m1n1.bin
  cat logo.bin                    # optional, and only here: m1n1's custom logo
  printf 'chosen.bootargs=%s\n' 'console=tty0 loglevel=7 rdinit=/init'
  cat t8010-*.dtb                 # every DTB; m1n1 picks the one matching the device
  cat Image.gz
  printf 'm1n1_initramfs'
  python3 -c 'import os,struct,sys; sys.stdout.buffer.write(struct.pack("<I", os.path.getsize("initramfs.cpio.gz")))'
  cat initramfs.cpio.gz
} > linux.bin
```

A logo is m1n1's custom logo payload: `m1n1_logo_256128`, then RGBA
flattened on black, 256×256 and then 128×128 (`--logo`; m1n1 picks the size
for the screen). m1n1 looks for it only directly after itself. To make one:

```sh
{ printf 'm1n1_logo_256128'
  magick logo.png -resize 256x256 -background black -flatten -depth 8 rgba:-
  magick logo.png -resize 128x128 -background black -flatten -depth 8 rgba:-
} > logo.bin
```

m1n1 reads the `chosen.bootargs` line as configuration, takes the DTB whose
root compatible names the device, and hands the framed initramfs to Linux
unexpanded. Boot `linux.bin` like any m1n1 payload: through Pongo (`orchard
run` does this), the m1n1 proxy, or an installed m1n1.

## Lower-level Python functions

The Python functions are separated by responsibility:

- `orchard.build.prepare_kernel(hardware=None, cache=None, device='j71b')`:
  build/reuse a coherent kernel/DTB/module bundle only. Defaults to the active
  hardware package and normal shared cache; does not build loaders, Pongo or tools.
  A distro-owned producer can consume this bundle without any distro setup or
  assembly knowledge in CLI. Uses exactly the same kernel keys and artifacts as
  `prepare_components`, including when called from a fresh hardware checkout.
- `orchard.build.prepare_components(hardware, cache, ...)`: shared component
  building and cache reuse. Invokes hardware-owned kernel/tool/loader producers;
  does not compose RAM roots, install systems or access a device.
- `orchard.ram.assemble_ram(hardware, output, *, kernel, busybox, loader,
  pongo, init, ...)`: RAM-root composition and boot-payload assembly from explicit
  components. Supports `files={destination: source}`, custom boot arguments,
  hostname, device DTB filename and optional modules.
- `orchard.ram.build_directory(directory, ...)`: composes directory content,
  delegating component preparation to `orchard.build`.
- `orchard.boot.boot_ram(output)`: verification, confirmation and tethered
  upload through existing USB/Pongo transport.
- `orchard.boot.run_directory(directory)`: CLI run operation.

Cache and acquisition policy is CLI-owned (`sources.py`, `build.py`). CLI also
owns module-SDK export and reuse (`kernel.py`, `kernel_sdk.py`). Hardware supplies
unchanged kernel/config/patch, module, DTB, staging and toolchain recipes. No new
hardware API, package version or hardware commit is needed for these workflows.
The broader hardware/CLI boundary remains open; example content and build/run
commands do not depend on where those internals eventually settle.

The CLI does not supply a startup script. Callers and examples own `/init`.

There is no required Orchard device agent, installer protocol or device API.

## Cache behavior

The shared cache contains retained upstream Git objects and immutable input-keyed
components. A missing local source tree has the same key as the declared patched
kernel tree. Consequently, a fresh checkout with matching inputs reuses both the
kernel base and completed device layer without invoking a compiler or materializing
Linux. Local tracked/untracked source edits are snapshotted without changing the
user's Git index and select different kernel bases.

Retained kernel bases exclude external modules, final-DTB overlays, firmware
recipes, documentation and unrelated device inputs. CLI exports an SDK with
matching configuration, generated headers, Module.symvers and host build tools.
To avoid extending the hardware API, the initial base fill invokes its existing
full producer, then retains only the kernel/in-tree modules and SDK. That first
fill also builds external modules and overlays as discarded byproducts.

Subsequent module/DTB edits bypass the full producer. CLI restores a private SDK
copy and in-tree module paths, then calls the original hardware module/staging/DTB
recipes. No make command writes into the shared cached SDK.

Each component records the inputs of its key in `key-inputs.json`. On a miss the
build prints which inputs differ from the newest cached entry of that component,
e.g. `differs from newest cached 0bd1204e3f1a in: tree`. The device layer depends
on the device's DTB selection only, not on the rest of `device.toml`.
`orchard cache` lists the shared components with their size, last use and inputs;
`orchard cache kernel-j71b/0bd1204e` removes one entry and `orchard cache kernel-j71b`
all of a component's entries. `orchard cache --prune` keeps the five most recently
used entries of each component (`--keep N` for another number) and removes
unfinished builds.

Builds also keep Orchard's directories, the shared cache and the store, under a
size limit: 20 GiB, `ORCHARD_CACHE_BUDGET_GB` for another, 0 for none. Above
it they delete the least recently used of what Orchard can make again: older
component entries and work directories, downloaded release assets, and kernel
trees without local edits. The newest entry of each component, work recipe and
the newest kernel tree stay, a tree with local edits is never deleted, and
nothing outside these two directories (your `out/` builds, a simulator disk) is
touched. The compiler cache keeps itself under 2 GB.

Entries store each file once: a file whose content and mode another entry already
has becomes a hard link to the single copy in `components-v2/.objects/`. Kernel
entries of one device mostly differ in a few files, so sharing makes about 90% of
their size disappear from disk. The listing shows both the entries' size and what
they use on disk.

Cached artifacts are hash-checked, inputs are rechecked before publication, and a
cache lock makes a second build into the same cache wait for the first. Editable Linux source
and incremental compiler output stay under each hardware checkout's
`kernel/.work/`. On a cache miss, source acquisition borrows upstream objects
from the shared store; it never resets an existing checkout. Do not prune/delete
that store while source trees borrow its objects. Existing sources can seed the
store locally rather than redownloading Linux.

`init` and rootfs files are assembly inputs, not kernel inputs. Toolchain containers
still use the host's configured Podman connection; no VM management is introduced.
When patches change, CLI checks the shared cache against the newly declared
source tree before changing the local checkout. On a miss, it applies the verified
delta from a recorded clean baseline, retaining the index, unchanged source
mtimes and compiler output. Local edits or an unknown baseline stop that transition
without resetting the checkout. Hardware receives the matching patch marker.

Kernel-base keys fingerprint the base producer and SDK exporter, not unrelated
source-management functions. Verified caches from the two earlier equivalent
base producers remain reusable, including the charging-worker cache. A changed
base producer does not inherit that compatibility. Cache reuse still checks the
complete file inventory and hashes.

Developer receipts are not publisher signatures or hardware qualification.
Explicit component paths are trusted inputs: callers must keep kernel, DTB and
modules coherent. Never automatically retry uncertain boot or storage mutations.
