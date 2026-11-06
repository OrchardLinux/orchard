# The distribution contract

A distribution builds with orchard's producers, called as commands, and
imports none of orchard's Python. orchard is a tooling layer
([platforms-and-builds.md](platforms-and-builds.md#projects-producers-and-boot-planned)):
these commands, their arguments and their files are what it keeps stable;
everything else in orchard may change. `tests/test_distribution_contract.py`
holds them to it.

## The snapshot

Every command runs from an orchard snapshot `ORCHARD`: a checkout, the
newest nightly's tooling (what the `orchard` launcher runs), or a release's
`orchard-source.tar.gz` unpacked. Its Python modules run as
`PYTHONPATH=$ORCHARD/src python3 -m orchard.NAME`; its data (device trees,
recovery runtime, recipes) are files under `$ORCHARD`. A build that runs
under `orchard build` gets `ORCHARD_CHECKOUT` set to the snapshot; a build
without orchard takes it as an argument.

## Commands

| Command | Does | Files |
|---|---|---|
| `orchard.inputs --output FILE [--platform P …] [--device BOARD] [--platform-loader P=FILE] [--platform-tools P=DIR] [--platform-lwvm P=FILE] [--kernel-dir P=DIR …] [--m1n1 FILE] [--pongo FILE] [--tools DIR] [--kernel-tree DIR]` (`ORCHARD_KERNEL_TREE` too) | resolves what the build takes finished | `FILE`: per platform its kernel bundle and boards (board → DTB), the shared m1n1, Pongo and static tools, orchard's revision, and under `produced` how each was got (built: its commands; reused from the cache; downloaded from a nightly: tag, asset, sha256); paths into orchard's cache |
| `orchard.image.platform P --output DIR --kernel-dir BUNDLE --busybox FILE [--receipt FILE] [--root-architecture arm64|arm] [--device BOARD] [--lwvm FILE] [--rescue-tools DIR]` | the platform layer of installed systems | `DIR/platform-P.tar.gz`, its receipt |
| `orchard.image.platforms_index DIR (--release TAG [--platform P …] \| --inputs FILE \| --kernels DIR --tools DIR --commit SHA)` | what a build takes from orchard: every arm64 platform layer, Pongo, m1n1, `install-tools.tar.gz`, and `platforms.toml` listing them; from a published orchard build (`--release`, which `orchard.image.publish` then links instead of copying), or built here | `DIR/*` |
| `orchard.image.root_image ROOT.tar.* --output DIR` | a root as the block image the installer writes; needs Podman, or root on a native build | `DIR/root.blocks.zst`, `DIR/root.map.json`, `DIR/root-image.json` (the manifest's `[root]`) |
| `orchard.image.recovery --dtb FILE … --loader FILE [--logo FILE] --kernel FILE --modules-dir DIR --busybox --dropbear --dropbearkey --wpa-supplicant --wpa-cli --iw FILE --base FILE --base-receipt FILE --root-receipt FILE --output FILE [--verbose] [--platform P] [--device BOARD]` | a platform's RAM recovery | the image and `IMAGE.json` |
| `orchard.installer release DIR [--installer-url URL] [--pongo FILE] [--m1n1 FILE]` | the installer of a release | `DIR/install.sh`, `DIR/orchard-install.pyz` |
| `orchard.manifests FILE … [--verify-local]` | checks release manifests | exit status |
| `orchard.image.publish --distro NAME --tag TAG --release-dir DIR --output DIR` | the hosted release | `DIR` |
| `$ORCHARD/src/orchard/image/assemble.py` (by path) | an m1n1 payload | see [ram-development.md](ram-development.md#assembling-by-hand) |
| `$ORCHARD/src/orchard/buildenv.py` (by path; standard library only) | the build container or native build of a recipe: `build`, `run`, and as a module `native()`, `build()`, `run()`, `identity()` | — |

Changing one of these is a contract change: say so in the changelog, and
update both distributions in the same push set.

## Architecture and board selection

The default input document still selects every ARM64 platform and preserves
its `shared` m1n1/Pongo/tool record. An explicit ARMv7 build selects a board
with `orchard.inputs --device p105`. Each selected platform record includes
`architecture` (`arm64` or `arm`), `image`, `loader` (`kind` and `file`) and
its own `tools` paths. `boards` contains the selected board when `--device`
is given. A document has one root architecture; mixing ARM32 and ARM64
platforms is refused before producing components. The top-level
`architecture` repeats that target for the distribution's root builder.

`--platform-loader PLATFORM=FILE` and `--platform-tools PLATFORM=DIR` supply
finished components for offline builds. ARMv7 uses these per-platform inputs
and has an empty `shared` record. A Cascadia payload uses one board's handoff
profile; a platform with several boards requires explicit board selection.

`orchard.image.platform --root-architecture arm --device p105` checks the
root ISA and writes a layer for that board. Its kernel image comes from the
platform descriptor. Optional `[root] storage_modules` in that descriptor
names the early-root modules; an empty list requires built-in storage.

An LwVM platform sets `[root] format = "lwvm"`. Its schema-1 input record
adds `platforms[P].tools.lwvm`, the absolute path of the static mapper for
the selected architecture. `--platform-lwvm P=FILE` supplies that finished
component for an offline build. The existing six tool paths and `shared`
record retain their meaning. Consumers forward this path as `--lwvm FILE`
to the recovery and platform producers. The platform producer also accepts
`--rescue-tools DIR` for the matching static Dropbear tools; otherwise it
resolves those tools through Orchard's cache. LwVM installed roots require
built-in `CONFIG_BLK_DEV_DM=y` and its linear target alongside the logical
storage backend. P105 remains `install = false` until the exact path passes
physical qualification.
Every platform record also identifies `root_format` as `gpt` or `lwvm`.
Consumers require `tools.lwvm` for `lwvm` before building a root, and may
default an older snapshot's absent `root_format` to `gpt`.

`orchard.image.recovery --platform apple-armv7 --device p105` stages an
ARM EABI5 hard-float runtime and invokes the source-built Cascadia loader.
The ELF dependency reader follows ELF32 libraries as well as ELF64, and
recovery refuses libraries or executables from another ISA. Cascadia puts
the external recovery initramfs in a reserved RAM range and passes its
addresses through the DTB. Its image receipt has
`initramfs_encoding = "cascadia-dtb"` and `architecture = "arm"`; the component
records describe contiguous `loader`, `kernel`, `dtb`, and `initramfs` ranges
with padding records where needed. This new handoff awaits physical testing.

A distribution that accepts a board also declares `selects-device = true`
under `[release]` in `orchard.toml`, alongside `selects-platform = true`.
Orchard then forwards its selected `--device` to that distribution's build
entry. Existing distribution entries retain their previous arguments.
