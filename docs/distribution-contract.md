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

`orchard build` passes a developer's checkouts the same way, as paths, and
does nothing with them itself: `ORCHARD_KERNEL_TREE` (`--kernel-tree`, or
`kernel-tree` in `orchard.local.toml`), which `orchard.inputs` builds the
kernels from, and `ORCHARD_PACKAGES` (`--packages`, or `packages`, else a
`packages` checkout beside the distribution's; `packages = false`: none), a
checkout of the Orchard package repository (OrchardLinux/packages). With
`ORCHARD_PACKAGES` set, the distribution builds the `[orchard]` packages its
image installs whose recipe there is not the published one, with that
repository's own commands (`tools/pkgrepo local --arch ARCH NAME…` says which
and in what order; `ci/build-package` with `OUT` and `LOCAL_REPO` builds one),
and installs them in place of `[orchard]`'s. Unset, both change nothing.

## Commands

| Command | Does | Files |
|---|---|---|
| `orchard.inputs --output FILE [--platform P …] [--kernel-dir P=DIR …] [--m1n1 FILE] [--pongo FILE] [--tools DIR] [--loader BOARD=FILE …] [--kernel-tree DIR]` (`ORCHARD_KERNEL_TREE` too) | resolves what the build takes finished | `FILE`: per platform its kernel bundle and boards (board → DTB), the shared m1n1, Pongo and static tools, orchard's revision, and under `produced` how each was got (built: its commands; reused from the cache; downloaded from a nightly: tag, asset, sha256); paths into orchard's cache. With 32-bit platforms (`--platform apple-armv7`): `"architecture": "arm"`, ARMv7 tools under `shared`, and per platform `loaders` (board → its Cascadia loader) instead of m1n1 and Pongo; one document never mixes architectures |
| `orchard.image.platform P --output DIR --kernel-dir BUNDLE --busybox FILE [--receipt FILE]` | the platform layer of installed systems | `DIR/platform-P.tar.gz`, its receipt |
| `orchard.image.platforms_index DIR (--release TAG [--platform P …] \| --inputs FILE \| --kernels DIR --tools DIR --commit SHA) [--architecture arm64\|arm] [--loader BOARD=FILE …]` | what a build takes from orchard: every arm64 platform layer, Pongo, m1n1, `install-tools.tar.gz`, and `platforms.toml` listing them; from a published orchard build (`--release`, which `orchard.image.publish` then links instead of copying), or built here. For 32-bit roots (`--architecture arm`, or an inputs document of them): `platforms-armv7.toml` next to it, with the ARMv7 layers, `install-tools-armv7.tar.gz` and per board `cascadia-BOARD.bin` | `DIR/*` |
| `orchard.image.root_image ROOT.tar.* --output DIR` | a root as the block image the installer writes; needs Podman, or root on a native build | `DIR/root.blocks.zst`, `DIR/root.map.json`, `DIR/root-image.json` (the manifest's `[root]`) |
| `orchard.image.recovery --dtb FILE … --loader FILE [--logo FILE] --kernel FILE --modules-dir DIR --busybox --dropbear --dropbearkey --wpa-supplicant --wpa-cli --iw FILE --base FILE --base-receipt FILE --root-receipt FILE --output FILE [--verbose]` | a platform's RAM recovery | the image and `IMAGE.json` |
| `orchard.installer release DIR [--installer-url URL] [--pongo FILE] [--m1n1 FILE]` | the installer of a release | `DIR/install.sh`, `DIR/orchard-install.pyz` |
| `orchard.manifests FILE … [--verify-local]` | checks release manifests | exit status |
| `orchard.image.publish --distro NAME --tag TAG --release-dir DIR [--release-dir DIR] --output DIR` | the hosted release; one `--release-dir` per root architecture ([two roots](#two-roots-in-one-release)) | `DIR` |
| `$ORCHARD/src/orchard/image/assemble.py` (by path) | an m1n1 payload | see [ram-development.md](ram-development.md#assembling-by-hand) |
| `$ORCHARD/src/orchard/image/cascadia.py --profile devices/BOARD/cascadia.toml --loader FILE [--loader-elf FILE] --kernel zImage --dtb FILE --initramfs FILE --output FILE` (by path) | a 32-bit board's Cascadia payload | the image and `IMAGE.json` |
| `$ORCHARD/src/orchard/buildenv.py` (by path; standard library only) | the build container or native build of a recipe: `build`, `run`, and as a module `native()`, `build()`, `run()`, `identity()` | — |

Changing one of these is a contract change: say so in the changelog, and
update both distributions in the same push set.

## 32-bit roots

A distribution that builds an ARMv7 root takes `platforms-armv7.toml`
instead of `platforms.toml` and writes a release manifest for it: the same
`root`, `install_tools` (the ARMv7 one) and `platforms`, with
`boot_method = "recovery-autogo"`, no `pongo`, `m1n1` or `logo`, and a
`cascadia` table naming each board's loader (`[cascadia] p105 = {…}`, the
index's `loaders.cascadia`). orchard assembles a board's payload from that
loader, the platform layer and its own device data, so the release says
nothing about boot addresses.

## Two roots in one release

A release can carry a 64-bit and a 32-bit root, one tag for both. The
distribution builds each into its own release directory (each with its own
`index.toml` and `release.toml`) and publishes both together:

```sh
python3 -m orchard.installer release out/aarch64      # the installer, once
python3 -m orchard.image.publish --distro NAME --tag TAG \
  --release-dir out/aarch64 --release-dir out/armv7 --output out/publication
```

The hosted index pins one manifest per root architecture: `[release]` is the
64-bit (aarch64) root, `[releases.armv7]` the 32-bit one.

```toml
schema = 2
kind = "orchard.index"
distro = "minimal-linux"

[release]
url = "https://github.com/OrchardLinux/minimal-linux/releases/download/v0.4.0/release.toml"
bytes = 3006
sha256 = "…"

[releases.armv7]
url = "https://github.com/OrchardLinux/minimal-linux/releases/download/v0.4.0/release-armv7.toml"
bytes = 2710
sha256 = "…"
```

The installer and `orchard install` take the manifest of the attached board's
root architecture: `armv7` for a board with a 32-bit chip (the `apple-armv7`
platform), `aarch64` otherwise, falling back to `[release]`. An index of one
release (`[release]` alone, as every release before this) serves whatever that
manifest covers. A board whose architecture has no root in the release gets an
error saying so. The 64-bit root's files keep their names; a file of the other
root whose name is already taken by a different file gets the architecture
added (`release-armv7.toml`, `root-armv7.blocks.zst`); files orchard published
are linked, not copied, as before. The installer's `orchard-install.pyz`
covers the boards of both roots.
