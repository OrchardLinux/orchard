# Platforms, kernel trees, builds and boot

Design agreed in November 2026, continuing [tooling.md](tooling.md). Sections
marked **done** are implemented; **planned** is agreed but not built; **open**
still needs a decision.

## Platforms (done)

A platform is one kernel build shared by every board that names it in
`devices/<board>/device.toml` `[kernel]`. It is described in one file,
`platforms/<name>.toml`, and nowhere else:

| Field | Meaning | apple-16k / apple-4k | apple-armv7 |
|---|---|---|---|
| `arch` | `make ARCH=` | `arm64` | `arm` |
| `image` | make target and payload file | `Image.gz` | `zImage` |
| `dtbs` | where `make dtbs` leaves the boards' DTBs | `arch/arm64/boot/dts/apple` | `arch/arm/boot/dts/apple` |
| `upstream` | lock of the base the series applies to | `kernel/upstream.lock` (v7.3-rc1) | `kernel/upstream-v6.12.lock` |
| `series` | ordered patch layers | `kernel/series/<name>` | `kernel/series/apple-armv7` |
| `config` | full config | `kernel/configs/<name>/config` | `kernel/configs/apple-armv7/config` |
| `defconfig` | the same config in OrchardLinux/linux | `orchard_apple16k_defconfig`, … | `orchard_applearmv7_defconfig` |
| `branch` | its branch in OrchardLinux/linux | `main` | `armv7` |
| `loader` | the payload format its kernel boots in | `m1n1` | `cascadia` |

`kernel/series.py --platform NAME` prints the checked descriptor; `kernel/build`
and orchard's kernel cache read it. A new platform is a new descriptor, not a
script. The platform layer of installed systems goes into the one arm64 root,
so it and its CI job cover only `arch = "arm64"` platforms; apple-armv7 builds
and caches, but has no platform layer until a root of its architecture exists.

**Done:** `loader`, the payload format of the platform (validated and printed; nothing uses it yet): `m1n1` (m1n1 +
`Image.gz` + DTBs + initramfs) or `cascadia` (trampoline + `zImage` with
appended DTB + built-in root). It follows from the kernel: m1n1 cannot run
32-bit code. How a board reaches that loader (checkm8 and Pongo, an iBoot
chain, kDFU from a jailbroken iOS) is device data and stays in
`device.toml` `[boot] methods`; whether a unit is jailbroken is detected at
runtime. The launcher takes the chain from the device and the payload format
from the platform.

## Kernel and m1n1 trees

**Done:** [OrchardLinux/linux](https://github.com/OrchardLinux/linux) (fork of
torvalds/linux) and [OrchardLinux/m1n1](https://github.com/OrchardLinux/m1n1)
(fork of AsahiLinux/m1n1). During the transition the patch layers here stay
authoritative and the trees are generated from them by
[tools/kernel-tree](../tools/kernel-tree/README.md): one commit per patch,
one merge per patch directory, the defconfigs on top, one branch per
platform base. Regeneration is stable: unchanged topics keep their SHAs.
`export.py` turns a branch into patch files for an orchard PR.

**Planned, at the switch:** the trees become authoritative and the patch
directories a generated export for one release. The kernel stays a patch
queue on top of upstream, not a fork:

- an upstream bump is a rebase onto the new tag, never a merge of upstream
  into a branch; commits that landed upstream drop out by patch-id;
- the state before a rebase stays as a tag per base (`orchard/v7.3`);
- the delta is exportable as a plain series at any time.

Configs live in the tree as defconfigs (`arch/<arch>/configs/`); orchard only
names them. Until the switch, `kernel/configs` is authoritative: regenerate
makes the tree's defconfigs from it and checks they reproduce it byte for
byte. **Done:** what differs from the source of truth is refused, never
corrected silently. `export.py` carries a branch's defconfig change into
`kernel/configs/<platform>/config` (`make olddefconfig`), so it reaches orchard
the way any change does; regenerate refuses to move a branch whose
defconfig holds a change orchard does not have, and says to export it.

## Imported work (done)

Contributor work comes in as a layer with the original authorship and a
`kernel/sources.toml` entry, organised by subsystem, not by contributor.
Project Cascadia's A5 port is the first import from a build project rather
than a kernel tree: `tools/kernel-tree/import-cascadia.py` rebuilds the tree
its build assembles and splits it into commits whose authors come from
Cascadia's history per file. Bring-up safeguards stay out of the kernel:
storage protection is the installer's job (`runtime/recovery/storage-fence`),
and Cascadia's NAND driver is read-only by construction.

## Build arguments (done)

1. Primitives take explicit arguments (`make PLATFORM=apple-16k`,
   `PLATFORM=all`, a producer's `--platform`) and work without orchard.
2. `orchard build` passes the same arguments through: `--platform NAME|all`
   (repeatable), `--device BOARD`.
3. `orchard.local.toml` only holds sticky defaults for those arguments
   (`platform`, `device`, `kernel-tree`).
4. Precedence: argument, then `--device`, then `orchard.local.toml`, then the
   attached board, else an error. A local build never means all platforms
   implicitly; CI passes `--platform all`.

**Done:** `--kernel-tree PATH` builds a kernel tree of your own (a branch of
OrchardLinux/linux) with its tree's defconfig instead of orchard's patched
tree, cached apart from orchard's kernels (`kernel-tree-*`) and recorded in
`build.json`. It builds inside the tree, in `.orchard-out/PLATFORM` (which
orchard adds to the tree's `.git/info/exclude`): the build output lives and
goes with its tree instead of piling up in orchard's cache. Kernel work happens on such a branch; `export.py` turns it into
patch files at the end. It takes RAM payloads only, until distributions are
projects like any other (below).

## Projects, producers and boot (planned)

**Principles.** orchard is a tooling layer, not a distribution framework: it
provides producers, platform data and a CLI that makes using them easy;
projects compose them. Every fact and every step exists in one place; where
a second copy exists for a while (`kernel/configs` and the tree's
defconfigs), a check guards it and the source of truth wins, everything else
is refused. Doing by hand what the CLI does is slower, never broken:
producers check their own inputs and fail loudly, and no step depends on
state only the CLI has.

Agreed November 2026. It replaces the separate paths for RAM payloads
(`payload.toml`) and distributions (`distro.toml`): before, a distribution's
build entry calls orchard's internals itself (`platform.kernel()`,
`prepare_shared()`, its own m1n1), so no orchard option (`--kernel-tree`, the
platform, the m1n1) reaches it without its own wiring.

### Three layers, each usable without the orchard CLI

1. **Producers** are plain commands, files in and files out. The kernel is
   `kernel/build` here; after the switch it is `make
   orchard_apple16k_defconfig && make` in OrchardLinux/linux. m1n1 is `make`
   in OrchardLinux/m1n1. A root is the project's own command. Instead of
   compiling, a platform's components (kernel, modules, DTBs, m1n1) can come
   from a published build with its `SHA256SUMS`. A producer may use a
   container, but never imports orchard's Python; producers exchange files
   and a small receipt (JSON) of what was built from what.
2. **Assembly** is a standalone script: m1n1, kernel, DTBs, initramfs and
   splash in, `linux.bin` out. At its core this is m1n1's payload format
   (m1n1 with DTB, `Image.gz` and initramfs appended, as Asahi documents it);
   what orchard's assembly adds beyond that (`boot.json`, several DTBs) is
   written down so it can be redone by hand.
3. **Boot** by any route: palera1n/Pongo, the m1n1 proxy, the orchard
   launcher. The commands for each are documented.

So a user can build a project's root, take or build the platform's
components, assemble and boot without orchard.

**Composition stays with the project.** A distribution's release (root,
per-platform recovery and platform layer, manifest, installer package) is
assembled by the distribution's own script, which calls orchard's producers
by path or takes their published outputs, and imports none of orchard's
Python. The orchard CLI does not compose a second time: it resolves the
inputs (platform and device; the kernel from a published build, a local
build or `--kernel-tree`; the shared components), hands them to that script
as files and arguments, caches, and boots. `--explain` records what ran
(**done** for RAM payloads: `out/PLATFORM/commands.sh`), so the standalone
path is documented by the tool and cannot drift from it. The two
distributions' scripts stay short because the producers do the work;
contract tests in orchard check how they call them.

### Two stages: initramfs and root

| Stage | Job | Supplied by |
|---|---|---|
| 1: initramfs | find the root, switch to it | orchard by default: today the initrd of the platform layer (`image/platform.py` `initrd()`: BusyBox and the storage modules) for installed systems. A project may bring its own; this boundary is expected to move as the tooling does. |
| 2: root | the system | the project (a distribution's root), or orchard (the recovery/installer) |

A RAM example (`examples/minimal-boot`) is the case without a stage 2: its
`init` and `rootfs/` are packed into the initramfs (`orchard.image.initramfs`)
and nothing switches root.

The boot mode is where stage 1 gets the root from, chosen when booting, not
declared by the project:

| Mode | Root from | Chosen by |
|---|---|---|
| RAM | the whole root image, loaded with the kernel | `orchard run --root ram`; refused early if it does not fit the device's memory (device data) |
| Recovery / installer | orchard's installer system as stage 2, the release as data | the default of `orchard run` for an installable project |
| Installed | a partition on the device (BLS, read by m1n1) | `orchard launch` |
| Live | the host over USB (NBD or NFS over the gadget network), later USB storage; a tmpfs overlay makes it writable | `--root host` |

Working in an existing system (`orchard launch`, then `push`, `pull`,
`exec`) is a session with a device, not a build, and is not declared.

One root image format for every mode (ext4 or erofs, to decide) is what
makes this work: RAM, live and installed boots use the same file, so what is
tested is what is installed. A root directory remains for RAM examples and
NFS.

### The project file

**Done:** one file for every project, `orchard.toml` (`orchard.project`),
replacing `payload.toml` and `distro.toml`, which are still read with a
notice to rename them. It declares what the project builds and never which
board it is for; the kernel is the platform's, and a developer's choices
(`platform`, `device`, `kernel-tree`) stay in arguments and
`orchard.local.toml`.

```toml
# A RAM example                  # A distribution
name = "minimal-boot"            name = "minimal-linux"
[payload]                        [release]
transport = false                build = "tools/build.py"   # its own script composes the release
                                 selects-platform = true
```

In a distribution `orchard build` runs the script with `ORCHARD_CHECKOUT` (the
snapshot whose producers it calls) and, with `--kernel-tree`,
`ORCHARD_KERNEL_TREE`, which `orchard.inputs` builds the kernels from; it
records that call as `out/releases/VERSION.commands.sh`, which `--explain`
prints. The script's own steps are the distribution's
([distribution-contract.md](distribution-contract.md)).

### Carried over

- The platform layer (kernel, modules, DTBs, firmware recipes, initrd) is
  orchard's artifact. A distribution build takes it finished for its orchard
  revision and compiles it locally only on request (`--kernel-tree`).
- A local test build covers one platform; releases cover all.
- **Done:** one m1n1, orchard's. A distribution supplies its artwork as
  data: the boot splash, which m1n1 paints per entry for an installed boot,
  and m1n1's custom logo payload (`m1n1_logo_256128`, at most 256 px), which
  the recovery's assembly puts directly after m1n1 (`assemble.py --logo`) and
  the quiet m1n1 draws on black. minimal-linux and omarchy-a build no m1n1
  any more (J71b, November 2026).
- The A5 (`cascadia` loader) packs stage 1 into the zImage because iBEC
  passes no initrd. As its launcher is rebuilt from first principles, the
  aim is that it differs from arm64 only in having no m1n1; how far that
  goes is open.

### Open

- Recovery semantics: whether a distribution can bring its own recovery, or
  only data (name, splash) for orchard's.
- The root image format.
- Live boot is its own later item: the stage-1 sources (host over USB, then
  USB storage) and the launcher side.

### Order

1. **Done:** assembly as a standalone script (`assemble.py`, by hand in
   docs/ram-development.md), `--explain` for RAM payloads, one m1n1.
2. Published components (nightlies), so a distribution build needs neither a
   kernel compile nor the CLI.
3. The orchard internals the distributions import become producers with file
   contracts (`platform.kernel()`, `prepare_shared()`, `orchard.image.*`),
   and the distributions call those; then `orchard.toml`.

## Build environment (planned)

On macOS every build runs in the Podman VM, and `orchard setup` creates it
with Podman's defaults (`podman machine init`): 4 CPUs and 4 GB on a
14-core, 48 GB Mac, so one kernel build starved every other. It was raised
to 10 CPUs and 16 GB by hand (November 2026). Instead:

- `orchard setup` creates the VM with explicit resources derived from the
  host (most of its cores, a good share of its memory, a disk large enough
  for a kernel tree, a root and the builder images), and says what it chose;
- `orchard doctor` reports the VM against the host and warns when the VM is
  the bottleneck;
- the values are a visible, adjustable setting, not a default in a script;
- builds that share the VM take a job count (`ORCHARD_KERNEL_JOBS`) so a
  second build does not take every core.
- **done:** builds and launches share the component cache's lock
  (`~/.cache/orchard/.lock`); only `orchard cache` takes it exclusively, and
  an entry being built is locked on its own. Before, a build held it
  exclusively for a whole kernel compile, so an `orchard launch` of an
  already-built entry waited for an unrelated platform's build while an
  iPad sat in DFU (November 2026);
- builds check free space on the host and in the VM before they start, and
  the VM gives deleted space back (fstrim) instead of growing until a build
  dies with "no space left on device".

## Updating orchard (done)

`orchard` used to run a hidden mainline clone that fetched `main` before
use, so any command could change the code a running build in another session
reads. Now the `orchard` on PATH runs one nightly from start to end (see
"Nightlies"), and a checkout's `bin/orchard` runs that checkout, which is
yours to update with git. A fix for a running build means restarting it.

## Device catalog

Device data (`devices/`, `chips/`, `methods.toml`, `features.toml`) reaches a CLI
separately from the CLI's own code, so a data change needs no CLI update:

- Every change on main publishes `catalog-v1/catalog.json` and the archive it names
  (`.github/workflows/catalog.yml`), after the data tests pass.
- A published orchard (a nightly, a release, an installed core) checks for a newer
  catalog at most once an hour and keeps it in `catalog/` in the Orchard cache. Without
  network it uses the cached one. It bundles no device data and pins none.
- A checkout's `bin/orchard` reads its own files, so a change can be tested before
  it is merged. `ORCHARD_DATA=DIR` selects a directory with any orchard.
- A build reads its own checkout's data, because it has to match that checkout's kernel.
- `platforms/` isn't in the catalog: it is the kernel's build recipe, read by builds.

The schema is the only contract. Additions stay in `catalog-v1`; a breaking change
publishes `catalog-v2`, and older CLIs keep what they understand. New boot methods
need code: `hardware.IMPLEMENTED_METHODS` says what this version implements, so data
can name a method a CLI doesn't have, and that CLI says it has no boot method for the board.

A distro release pins the platform layer it was tested with, so a board that needs
a newer kernel arrives with the next distro release or nightly.

## Boot methods and entries

A **method** (`methods.toml`, `[boot] methods`) is the chain that runs once a device
is entered: `checkm8-pongo` is Pongo, then m1n1, then Linux. An **entry** is how the
device gets there: `palera1n` and `checkra1n` both reach `checkm8-pongo`, and an iBSS
chain would have its own. `methods.toml` says which entries reach which method, and
`apple/entries.py` says how to run each one, because device data updates on its own
and must not choose what gets executed.

A board can name preferred entries in `[boot] entries`; `--via NAME` (or `ORCHARD_VIA`)
overrides that on `run`, `install`, `launch` and `boot`. The default is palera1n. It
isn't known yet what makes an entry work on a board (the chip, the host, the arguments
or the tool's own patches), so nothing refuses an entry by chip or host, and a failed
attempt isn't retried with another one.

`tools.ensure_tool` fetches an entry's tool on first use into `tools/` in the Orchard
cache, pinned by SHA-256 per host. A binary elsewhere counts only if it matches the
pin, so an unexpected version never boots a device silently; `ORCHARD_PALERA1N` or
`ORCHARD_CHECKRA1N` selects another binary on purpose. palera1n v2.4 embeds checkra1n
0.1337.3, patched on Linux; the `checkra1n` entry runs the unpatched official build.

## Nightlies

**Done (CI):** a scheduled run (03:17 UTC) builds every published component
of orchard main when main moved since the last nightly, or on dispatch with
`nightly: true`. Every nightly is its own immutable pre-release,
`nightly-YYYYMMDD-COMMIT`, with the same assets as a release (the tooling as
`orchard-source.tar.gz`, per platform a component bundle, m1n1, Pongo,
tools, `component-keys.json`, `SHA256SUMS`, attestation) plus `nightly.json`. The
release `nightly` only points at the newest (`latest.json`); nightlies older
than 14 days go, the newest always stays. GitHub's latest release, which
`orchard update` and the installer read, never points at a pre-release.
Distribution CI takes one with `HARDWARE_TAG=nightly` (resolved once through
the pointer) or `HARDWARE_TAG=nightly-YYYYMMDD-COMMIT`. apple-armv7 is in it
too, with its kernel, modules and device trees alone, because the boot tools
and loaders in the other bundles are arm64. CI building it every nightly is
also what catches a broken Cascadia re-import.

**Planned: orchard as versioned tooling** (agreed November 2026, after
Flutter's SDK and channels). The hidden mainline clone (`orchard-main`) is
gone (November 2026).

- Channels: **nightly** for developers, **stable** (releases vX.Y.Z) for
  installing. A stable distribution release pins, and for now bundles, a
  stable orchard release; until one exists, distribution releases are
  previews on a nightly.
- **Done, first part:** the `orchard` on PATH (`tools/launcher`) runs the
  newest nightly (looked up per command through `latest.json`, a shallow
  clone of its tag in `tooling/NAME`), offline the newest cached one, and
  says so when neither exists; `ORCHARD_VERSION` pins one. Nothing looks at
  the current directory: a checkout's `bin/orchard` runs that checkout. Its
  kernels, m1n1 and tools then come from that nightly (same files, same
  source keys) and its Pongo too (`git describe` finds the nightly's tag).
  `orchard update` installs the newest nightly's launcher, and a launcher
  older than the nightly it runs says so.
  The user core an installer leaves (install, launch, devices) stays on
  stable releases; the two meet in the Rust launcher.
- The installed CLI is thin, like `bin/flutter`: in a project checkout
  (`git clone omarchy-a && cd omarchy-a && orchard run`) it resolves the
  newest nightly once at the start of the build, fetches its tooling
  (`orchard-source.tar.gz`) and the components the build needs into the cache
  per nightly, runs that tooling, and records the nightly in the release.
  The project builds only its own parts; nothing of orchard is compiled. A
  nightly published meanwhile counts from the next build on, so nothing
  changes under a running build. Offline, the newest cached nightly, said so.
- Pinning stays: `ORCHARD_VERSION` names a nightly or a release, a
  checkout's `bin/orchard` runs that checkout, and `--kernel-tree` a kernel
  tree; a checkout of your own compiles, or takes a published component
  whose source key matches.
  `--kernel-tree` stays a path: a branch without a clone of one's own is
  `git clone --depth 1 -b BRANCH https://github.com/OrchardLinux/linux`.
- **Later, for testers:** a pull request's or branch's kernel built by CI
  like a nightly's and taken finished (`orchard run --kernel pr:N` or its
  artifact), so testing a kernel change needs no compile at all.

**Done:** a distribution build takes the nightly's kernel instead of
compiling it when the platform's *source key* matches (`orchard.published`):
a hash of the files that decide the kernel (upstream lock, series and every
patch, config, toolchain recipes, build fields, `KERNEL_PRODUCER`), which any
checkout computes without a kernel tree. CI publishes the keys
(`component-keys.json`, in `SHA256SUMS`); the bundle is checked against
`SHA256SUMS` before use and cached (`kernel-published-PLATFORM`). Another
key, no network or `ORCHARD_KERNEL=local`: it compiles as before. Builds in
an orchard checkout (kernel work) always compile their kernel; their static
tools come from the nightly when the tool recipes match (orchard#58), and
`ORCHARD_PUBLISHED=off` builds everything locally. The published kernel is
CI's build of that kernel state; CI installs the toolchain natively, so it
is not claimed to be byte-identical to a local compile.

**Planned, at the switch** (trees authoritative; decided November 2026): a
nightly is a tuple of commits, recorded in `nightly.json`: orchard
(descriptors, device data, producers, tools), OrchardLinux/linux per platform
branch, OrchardLinux/m1n1 main, the Pongo pin.

- The kernel job clones OrchardLinux/linux at the descriptor's branch and
  runs `kernel/build PLATFORM --kernel-tree DIR`, the path that gives a
  kernel byte for byte identical to the series build today; m1n1 likewise
  from its tree.
- A nightly runs when any of those repositories moved since the last one.
- A platform is rebuilt only when its source key moved; otherwise the last
  nightly's bundle is reused, so apple-armv7 rebuilds only when its branch or
  the shared files move.
- **Nightlies follow the branch heads; releases pin them.** A kernel change
  needs no second commit in orchard to reach the nightly.
- **Changes land in dependency order:** m1n1, then the kernel, then orchard,
  and each step works with the current state of the others. One commit can no
  longer change a driver and its descriptor together. A nightly may catch a
  half-landed change, which is why only a device-tested one becomes a release.

### Source key at the switch

The series, the configs and the upstream lock leave orchard, so the key can no
longer be hashed from a checkout's patch files. It has two parts:

- **The source,** from the tuple: the Git tree hash of the platform's linux
  commit, which holds the upstream base, the patches and the defconfig. A tree
  hash, so a rebase with the same result rebuilds nothing. m1n1 likewise, with
  the brand patch still among its recipes.
- **The recipe,** from the running orchard: the descriptor's build fields, the
  defconfig's name, the toolchain recipes and `KERNEL_PRODUCER`.

`nightly.json` records each branch's commit and tree hash. CI writes
`component-keys.json` from both parts. A build reads the tuple of the version
it runs (its own nightly, the lock in a release, the newest nightly in a
checkout), adds its own recipe part and compares, so it needs no clone of
linux to decide. A match downloads the bundle; a mismatch compiles that
commit from a clone. `--kernel-tree` keeps its key of the tree's content, and
distributions keep going through `orchard.inputs` with nothing to change.

An orchard checkout then takes its kernel from the nightly too, because orchard
no longer holds kernel sources: kernel work means `--kernel-tree`. A checkout
that changes a toolchain recipe compiles.

### Releases

There is one version users see, orchard's `vX.Y.Z`, plus each distribution's
own. Kernel, m1n1 and platforms have no version numbers of their own.

| Layer | Between releases | At a release | Identified by |
|---|---|---|---|
| Kernel (OrchardLinux/linux) | PRs merge to its branches; nightlies follow | tag `orchard-vX.Y.Z` per branch | commit and upstream base |
| m1n1 | PRs merge to `main`; nightlies follow | tag `orchard-vX.Y.Z` | commit |
| Platform (descriptor, device data, its kernel bundle and m1n1) | rebuilt when its inputs move | its bundle is a release asset | its entry in the lock |
| CLI and tooling (orchard) | nightly channel | `vX.Y.Z`: the tooling and the lock | tag |
| Distribution | previews on the newest nightly | its own version, pinning one stable orchard | its version and that orchard release |

**A release is a promoted nightly.** Pick a nightly that went through a device
round, write its tuple into a lock in orchard, tag that commit `vX.Y.Z`, tag
the linux and m1n1 commits, and publish the nightly's assets unchanged. Users
get the files that were tested, nothing is rebuilt, and the lock does not
change a source key. A nightly is kept 14 days, so promote within that time.

- **The tags keep a release rebuildable:** an upstream bump rebases the
  branches, and a commit only a lock names would no longer be on any of them.
- **A patch release** is the same tuple with one commit changed; a
  `release/0.3` branch is made only when one is needed.
- **Release notes:** a linux or m1n1 PR carries its changelog entry in its
  description; a release collects them into `CHANGELOG.md`. CI's check for the
  version section stays.
- **A distribution** bumps its pin and releases on its own schedule.

**Later: the platform layer separate from the distribution.** A stable
distribution release bundles its platform layer for now, so a kernel fix or a
new board waits for a distribution release. The installer and an update step
then combine a distribution's root with the newest compatible platform bundle.
That needs a compatibility check between root and platform, mainly modules and
firmware, and comes after the switch.

## Build caching (planned, last)

- Cache keys follow what determines the output (source tree, config,
  toolchain image, build flags), not the text of the scripts that run it; a
  producer change that alters output bumps an explicit producer version.
  **Done for kernels** (`KERNEL_PRODUCER`); m1n1, Pongo and the recovery tools
  still hash their scripts.
- Root and desktop as separate layers: the package base is reused while the
  package list is unchanged; a desktop change does not reinstall it.
- Package caches (pacman, apk, apt) are host directories mounted into image
  and root builds, so builder images are disposable and an interrupted build
  resumes from local disk.
- Mirrors are measured and ranked by the tooling, not hard-coded.
- `orchard cache` distinguishes what is precious (packages, downloads, git
  mirrors, firmware and private inputs), rebuildable (trees, outputs, images)
  and garbage (temporary directories), reports real sizes, and cleans only
  the last two.

## A5 boot (planned; record of how it works)

Project Cascadia's chain, as shaynelegault ran it on a P105:

1. **Pwned DFU.** checkm8 works on the A5 but needs tighter USB timing than a
   Mac can do (Cascadia uses a Raspberry Pi Pico); or kDFU: a jailbroken iOS
   (EverPwnage, iOS 7 to 9.3.6) boots a patched iBSS with `kloader`.
2. **Patched iBSS** from the user's own IPSW. For kDFU it must be
   unencrypted: after iOS has booted, the GID key is unavailable.
3. **iBEC with an auto-go hook** at fixed offsets of the iOS 8.4.1 (12H321)
   iBEC: when a Recovery-mode upload is complete, iBEC jumps into it.
4. **Staging bundle** uploaded to `0x80000000`: the `linux_boot` trampoline
   (paints status, turns MMU and caches off), padding to `0x8000`, then
   `zImage` with the DTB appended, entered at `0x80008000`. The root is built
   into the kernel: iBEC passes no initrd pointer.
5. **Linux** with a USB gadget; the host takes `10.55.0.1`, then SSH and NFS.

In orchard this is an iBoot-chain variant whose payload is the trampoline
instead of m1n1, and belongs in the launcher with the other chains:

| Cascadia | Orchard |
|---|---|
| decrypt and patch iBSS/iBEC, auto-go patch | firmware recipe steps of the device |
| `linux_boot` trampoline | built from source like m1n1, for ARM32 |
| bundle packing | the `cascadia` payload assembly of the platform |
| `irecovery -f` | the launcher's USB transport |
| DFU entry | kDFU, or an external checkm8 device |

Also needed: ARMv7 builds of the static recovery tools, the recovery
protocol for armhf, touch calibration captured once from the unit's iOS
(like `orchard capture-adt`) and handed to the boot assembly, and
shaynelegault's runtime services as a platform layer while their work moves
into drivers.

## Open

- **A5 userspace.** Distributions build one arm64 root. Recommendation:
  minimal-linux additionally builds armv7 (Alpine, also Cascadia's base) as
  the root for platforms without an arm64 root; an armv7 Omarchy (Arch Linux
  ARM `armv7h`, software rendering) stays a separate, later track.

## Later

Ideas carried over from an earlier architecture plan (October 2026), not yet
designed in detail:

- **Deploy as an update to a running system.** `orchard deploy` to an iPad
  already running Linux, without DFU or recovery: transfer only what
  changed, stage a new system generation beside the active one, keep
  device-local firmware, identity and user data, switch the boot selection
  only after staging and verification, and keep the previous generation for
  rollback. Transfer, activation and reboot stay separate steps. A
  non-transactional `--live` mode for desktop iteration updates a bounded set
  of owned files and restarts services, in seconds. Today there are only
  `orchard push/pull/exec` for RAM sessions and a full install.
- **Per-distribution minimum requirements.** Installation follows from what a
  board has: a kernel and device tree, a boot method this Orchard implements
  (with its `boot.json` if it needs one), a firmware recipe and writable
  internal storage (`hardware.install_blockers`). While this is experimental
  that deliberately allows boards where other things don't work yet. A
  distribution could later require more, such as working Wi-Fi or touch.
- **The simulator as a target of the same workflow** (`--target simulator`):
  the same userspace and packaging as the devices with a VM kernel and
  virtual peripherals, so contributors without Apple hardware can work on
  the desktop and on update logic. It does not validate drivers, touch,
  charging or suspend.
- **The recovery not sealed to one root.** A distribution's recovery is
  still built against its root's exact receipt (`--root-receipt`), so every
  root update rebuilds it.
