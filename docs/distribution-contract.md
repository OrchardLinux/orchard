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
| `orchard.inputs --output FILE [--platform P …] [--kernel-dir P=DIR …] [--m1n1 FILE] [--pongo FILE] [--tools DIR] [--kernel-tree DIR]` (`ORCHARD_KERNEL_TREE` too) | resolves what the build takes finished | `FILE`: per platform its kernel bundle and boards (board → DTB), the shared m1n1, Pongo and static tools, orchard's revision, and under `produced` how each was got (built: its commands; reused from the cache; downloaded from a nightly: tag, asset, sha256); paths into orchard's cache |
| `orchard.image.platform P --output DIR --kernel-dir BUNDLE --busybox FILE [--receipt FILE]` | the platform layer of installed systems | `DIR/platform-P.tar.gz`, its receipt |
| `orchard.image.platforms_index DIR --kernels DIR --tools DIR --commit SHA` | every arm64 platform layer, `install-tools.tar.gz` and `platforms.toml`, the index of what a build offers; orchard's nightly and releases publish these |
| `orchard.image.recovery --dtb FILE … --loader FILE [--logo FILE] --kernel FILE --modules-dir DIR --busybox --dropbear --dropbearkey --wpa-supplicant --wpa-cli --iw FILE --base FILE --base-receipt FILE --root-receipt FILE --output FILE [--verbose]` | a platform's RAM recovery | the image and `IMAGE.json` |
| `orchard.installer release DIR [--installer-url URL] [--pongo FILE] [--m1n1 FILE]` | the installer of a release | `DIR/install.sh`, `DIR/orchard-install.pyz` |
| `orchard.manifests FILE … [--verify-local]` | checks release manifests | exit status |
| `orchard.image.publish --distro NAME --tag TAG --release-dir DIR --output DIR` | the hosted release | `DIR` |
| `$ORCHARD/src/orchard/image/assemble.py` (by path) | an m1n1 payload | see [ram-development.md](ram-development.md#assembling-by-hand) |
| `$ORCHARD/src/orchard/buildenv.py` (by path; standard library only) | the build container or native build of a recipe: `build`, `run`, and as a module `native()`, `build()`, `run()`, `identity()` | — |

Changing one of these is a contract change: say so in the changelog, and
update both distributions in the same push set.
