# Orchard tooling: repositories, development, builds and the installer

Design agreed in October 2026. Sections marked **done** are implemented; the rest is the plan.

## Who uses it

1. **A user installs a distribution** on a supported device, with as little friction as possible.
2. **The same user starts it again.** A tethered boot needs a host tool on every boot.
3. **Someone brings up a device or a driver.** The most common development work. It changes kernel patches, device data, recipes and often host code, and needs no distribution.
4. **Someone works on a distribution** (the desktop, its installer), with mainline orchard or with orchard changes of their own.
5. **Someone creates a distribution.** Rare; minimal-linux is the reference to start from.

Changes are frequent: many commits a day across orchard and the distributions, most of them never released on their own. Nothing in daily work may depend on version numbers or pins.

## Repositories (done)

- **[OrchardLinux/orchard](https://github.com/OrchardLinux/orchard)**: hardware support and the `orchard` tool in one repository (formerly `hardware`, with `cli` merged in as a subtree, its commit IDs unchanged; `cli` is archived). A device change carries its descriptor, firmware recipe, converter steps, kernel patches and tests in one pull request.
- **omarchy-a** and **minimal-linux**: the two maintained distributions.

Inside orchard, code is split by where it runs and how it is used:

- `src/orchard/`: all Python imported on the host, one package.
- `devices/`, `schemas/`, `features.toml`: data.
- `kernel/`, `boot/`: recipes, standalone programs. They run `producers/buildenv.py` by path and need only Python 3.11 and its standard library.
- `producers/`: the standalone scripts recipes and distributions run by path (`buildenv.py`, `initramfs.py`, `assemble.py`, `cascadia.py`): standard library only, no orchard package. Their old paths in `src/orchard/` are small files that run them.
- `runtime/`: device-side code, copied into images, never imported.
- `examples/`: RAM payloads to start from.

There is no wheel: orchard runs from a checkout. One version for the repository (0.2.0); releases publish component bundles, the loaders `m1n1.bin` and `pongo.bin`, sources and checksums.

## Getting started (done)

`git clone`, `./setup`, `orchard run examples/minimal-boot`. `./setup` (and `orchard
setup`) shows every host requirement as ready, todo or yours, asks once, and can be
rerun until everything is ready; `orchard doctor` shows the same and changes
nothing. The requirements are one list shared with the installer's boot tools
(`setup-host`): OpenSSH, libusb, palera1n and Linux USB permissions for booting;
git, Podman (and its machine on macOS), the `orchard` command and PATH for building.
Packages come from glu, then Homebrew on macOS (neither is installed for you), or
apt, dnf or pacman. The `orchard` command is a copy of `tools/launcher`,
which runs the newest nightly, so it does not depend on any clone. Pongo comes from the latest release while its recipe
is unchanged, so most builds need no Xcode.

## Development: which orchard runs (done)

Two launchers, and neither looks at the current directory:

- **`orchard` on PATH** is `tools/launcher` (`./setup` installs it; it replaces itself with the newest nightly's when that differs, and `orchard update` does so for a pinned version). Every command runs the newest nightly: looked up once (`nightly/latest.json`), a shallow clone of its tag in `~/.cache/orchard/tooling/NAME`, so a command runs one version from start to end. Offline: the newest cached nightly. `ORCHARD_VERSION=nightly-YYYYMMDD-COMMIT` (or `vX.Y.Z`) pins one, e.g. to reproduce a bug. A launcher older than the nightly it runs says so.
- **A checkout's `bin/orchard`** runs that checkout, called by its path (`~/src/orchard/bin/orchard build …`), from any directory: that is how to work on orchard itself, in a worktree per branch (`git worktree add ../orchard-gpu -b gpu-a10`). uv installs it editable; an edit counts from the next command.

**orchard2 builds where it is switched on** for the computer: `ORCHARD2=1` (`0`: off), else `orchard2 = true` in `~/.config/orchard/config.toml`. Both launchers then hand `build`, `inputs`, `install`, `launch`, `devices`, `doctor`, `shell`, `exec`, `ssh-access`, `cache` and `run` to the `orchard2` on PATH, with the recipes and Python environment of the version they run; everything else stays in Python. orchard2 has a cache of its own, with its own spelling (`orchard cache ls`, `rm`, `prune`, `clear`); `cache` also says what Python's uses. `doctor` then lists what orchard2 uses of the computer; `setup` stays in Python, which installs the `orchard` command itself. `shell`, `exec` and `ssh-access` use orchard2's own keys: they reach what orchard2 installed or built, and `exec` takes its command after `--`.

What `install` and `launch` do then is described in OrchardLinux/tools, [`crates/cli`](https://github.com/OrchardLinux/tools/blob/main/crates/cli/README.md). Compared with Python's:

- `install` puts the system into the space after iPadOS and erases the partitions there, after a yes (`--yes`); `--split [GB]` resizes iPadOS first. Python asks for a place and never resizes. It takes a name (`omarchy-a`: the newest release of `OrchardLinux/NAME`), a URL, or an index or manifest file, and without one, in a distribution checkout, its last build. `distros` is refused: it has no registered names.
- Both start from DFU mode only, and show the steps of the device that is connected in another mode. A running recovery is not resumed, and a Pongo or m1n1 that waits is not used.
- `launch` starts the one installed system; with several it lets you choose with the arrow keys. `launch --list` prints them and starts nothing; `--entry ID` starts that one.
- Only one iPhone or iPad may be connected. 32-bit boards are refused.
- The boot route is the first in `routes.toml` that fits the board's chip, palera1n today. `--via ROUTE` (and `ORCHARD_VIA`) become `ORCHARD_BOOT=ROUTE`, as orchard2's own `--boot ROUTE`: `checkra1n`, or `ibss` for gaster and Apple's patched iBSS. Python follows the board's `[boot] entries` instead.
- `run DIRECTORY` is `orchard2 run DIRECTORY`, which builds the project and boots what it built, with run's build options. `--via` chooses the boot route as for `launch`; `-y` is not needed, orchard2 does not ask. In a distribution checkout it builds the release and starts its installer system from RAM. A device orchard2 has no boot route for is booted by Python after orchard2 built for it, which it says before touching the device.
- The operation lock of the Python commands is held while orchard2 works on a device.

`orchard python ARGS` runs Python in the running version's environment, for a distribution's own scripts. An `orchard = "PATH"` line in `orchard.local.toml`, which used to choose the checkout, is refused with what to use instead.

## Files (done)

| File | Committed | Contains |
|---|---|---|
| `orchard.toml` beside a payload's `init` or at a distribution's root | yes | the project: `name`, and `[payload]` (`transport`, `modules`, `firmware`) or `[release]` (`build`, `selects-platform`); commented, since the examples teach the system |
| `orchard.local.toml` | never | this checkout's build arguments: `platform`, `device = "BOARD"`, `kernel-tree` |

No repository and no per-person setting names a board: many contributors work on several. A payload serves every board of one platform (`out/PLATFORM/`); `orchard run` takes the platform from the attached board, builds what is missing and adds that board's firmware as its own layer from the private cache; `orchard build` takes it from `--platform`, `--device`, `orchard.local.toml` or the attached board (listing USB devices only). A distribution release serves every board: one root image, and per platform a recovery and orchard's platform layer (kernel, DTBs, modules; see [boot contract](boot-contract.md#kernel-dtbs-and-modules)). Which boards are supported, and how well, is only orchard's device data (`status`, `caveats`), which the installer shows.

## Distributions (done)

A distribution has no submodule, no launcher and no root configuration. `orchard` runs the newest nightly, whose components it then takes too; a checkout's `bin/orchard` runs that checkout. Distribution CI tests against orchard `main`; a release build uses one published orchard release whole, its tools and its per-platform component bundles alike (`ORCHARD_REF` overrides the tools). A release's manifest and build receipt record the exact orchard commit (`orchard.hardware.revision()`).

## Testing an install without a device

`orchard python tools/qemu-install/run RELEASE_DIRECTORY --board BOARD` runs orchard's real
device installer against a 16 GiB loop disk (an APFS-like partition first, as on an
iPad): partitions, root image, the board's platform layer, account and boot entry. Then
QEMU cold-boots that disk as NVMe through orchard's initrd into the system's init. QEMU
cannot run orchard's Apple kernels, so the boot uses Alpine's virt kernel with its storage
modules in orchard's initrd; drivers and the desktop stay for the device. It needs Podman
with a rootful connection (loop devices) and `qemu-system-aarch64`.

## Build caches (done)

All shared by every checkout in `~/.cache/orchard`:

| Layer | |
|---|---|
| Source | shared Git object store (`git/`), one kernel tree per platform (`kernel-work/`) |
| Intermediate | the kernel tree's incremental output; ccache (`ccache/kernel/`); recipe work directories (`work/RECIPE/KEY`) |
| Result | content-addressed components (`components-v2/`), each file stored once; `orchard cache --prune` keeps the most recently used |

Measured on apple-16k (j71b), switching between two patch sets that recompile 109 objects: 120 s without ccache, 83 s with it, 47 s after applying the series in one `git apply`, hashing it in one call and running one `make` job per CPU. The kernel key for a fully cached build went from 16.3 s to 0.8 s. The real cache went from 3.1 GB to 287 MB on disk.

Measuring on the shared tree: hold the tree lock across runs (`ORCHARD_KERNEL_WORK_LOCKED`) and register test series in the shared Git store first, or another session's build switches the tree between runs.

Not used: Docker/BuildKit layers are linear; Nix would rebuild every patch variant of the kernel fully and fits poorly with macOS and Podman; Bazel and Buck2 fight Kbuild.

## Python (done)

Recipes run `$ORCHARD_PYTHON` (default `python3`), which `orchard` sets to its own interpreter. Python 3.11 is the minimum; CI tests 3.11 and 3.14.

## Next: distributions as projects, the device layer and the installer

One step, because all three need a distribution build that does not depend on the
board:

- **Device layer** (done): kernel, initrd, DTBs and modules are orchard's platform
  layer, which the installer writes into the root, and the installer writes the boot
  entry; a distribution builds one root image for every platform, names no board and
  needs no kernel-install. Release format: `schemas/v2`.
- **A distribution as a project** (done): `orchard.toml` (`name`, `[release] build`) names
  its build entry. In a distribution checkout `orchard build` runs it as
  `ENTRY --version VERSION --output out/releases/VERSION` (default version
  `dev-DATE-TIME`), checks the index, its distro and version and every file it
  pins, and records the build in `out/releases/latest.json`. `orchard run`
  builds, then boots that release's recovery for the attached board from RAM,
  installing nothing; `orchard install` without a source installs the last build.
  Entries keep their parts with `orchard.distro.component` (keyed by their files
  under `out/components/`) and take Pongo and the recovery tools from
  `orchard.build.prepare_shared`; the kernels come from the platform layer as before.
- **The installer and the user's orchard,** below.

## The installer and the user's orchard

Done; `src/orchard/installer.py` and `src/orchard/user.py`.

- **Core:** the part of `src/orchard` that install and launch need (everything
  but the development modules `installer.DEVELOPMENT`: building kernels, payloads
  and distributions). Standard library only; `tests/test_core.py` checks every
  import and imports every module without site-packages.
- **Installer per distribution release:** `orchard.installer release DIR` adds
  `orchard-install.pyz` (core, the release's `index.toml` and the `release.toml`
  it pins, the device data of the release's boards, Pongo and the storage-capable
  m1n1) and `install.sh` to a release directory. `install.sh` downloads a
  hash-pinned standalone Python 3.14 into the store (`installer.PYTHON`) and the
  zipapp (`--installer-url`, or the one next to it), checks both by SHA-256 and runs
  `install`, prompting on the terminal even when piped from curl. No system
  Python, no uv. The zipapp unpacks once into `STORE/core/VERSION-HASH`.
- **The user's `orchard`:** `STORE/core/current.json` and `~/.local/bin/orchard`,
  a launcher of the core with `install`, `launch`, `devices` and `update`. An
  installer writes it before installing (so `orchard launch` remains even if the
  install stops) and replaces it only with a newer core; a development orchard on
  PATH (the nightly launcher `./setup` installs) stays. `orchard update` installs the latest
  orchard release's `orchard-core.pyz` if it is newer. Later a self-contained Rust/C
  binary.
- **State:** the store and the build cache record their layout version
  (`cache.STATE_VERSION`, `state.json`); an orchard that finds a newer one refuses
  to touch it.
- **Validation:** jsonschema is the `validate` extra and a development
  dependency. `python -m orchard.manifests` and the tests validate against the
  schemas; release CI installs `.[validate]`. The core checks what it relies on.

## Backlog

- `orchard run` in a distribution boots the locally built system over NFS root (#30) without installing: the desktop's edit-and-run loop.
- Prebuilt kernel layers from CI, first for a newcomer's first build, which otherwise compiles Linux.
- Keep the kernel tree inside the Podman VM (the largest remaining build cost is `make` walking the tree through the host file share).
- A `kernel/build` shortcut when tree, config, image and series match the last build; skip the image check when its inputs match.
- Warn about hand edits in the shared kernel tree; they are built silently and reach every checkout with the same series.
- Prebuilt kernel layers also for upstream pin bumps, which are always full builds.
- Several kernel trees per platform, if checkouts waiting for each other becomes a problem.
- Converter steps next to the device data; IOKit USB on macOS (no libusb); no checkra1n dependency; the iOS app installer with APFS resize.

## Not planned

Checking attestations on the client (releases are attested and `publish.yml` verifies them), Windows hosts, splitting the tablet desktop into upstream-style packages yet.
