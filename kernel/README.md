# Kernel build

Orchard's kernel lives in its own repository,
[OrchardLinux/linux](https://github.com/OrchardLinux/linux): upstream Linux
with the Apple A-series work on top. Kernel work is a pull request there,
against the source (see [CONTRIBUTING.md](../CONTRIBUTING.md)).

A platform is described in one file, `platforms/<platform>.toml`: its
architecture, kernel image, DTB directory, its branch in OrchardLinux/linux,
the lock that names the commit to build, and its config. `build` reads it,
fetches that commit and compiles one kernel, its modules and every DTB of the
platform. A device name resolves to its platform through
`devices/<device>/device.toml` `[kernel]` and additionally copies its DTB to
`DIRECTORY/<device>.dtb`. Git and Podman are required. `series.py --platform
NAME` prints the checked descriptor of a platform or device.

| Platform | Branch | Lock |
|---|---|---|
| `apple-16k` (A9 and later), `apple-4k` (A7 to A8X) | `main`, on upstream v7.3-rc1 | `kernel/upstream.lock` |
| `apple-armv7` (A5 to A6X) | `armv7`, on upstream v6.12 | `kernel/upstream-armv7.lock` |

## Taking a newer kernel

`tools/kernel-tree/take` moves a lock to its branch's head (or to a commit you
name) and writes the platform configs again from the tree's defconfigs. Commit
the result in the pull request that needs the new kernel, and say in the
message what the kernel gained, not that a line moved. Nothing else names the
commit: cache keys, CI, the nightly and a release follow the lock.

`kernel/configs/<platform>/config` is the expanded copy of the tree's
`arch/<arch>/configs/<defconfig>`. They are one config and the tree's is the
source: change an option in the defconfig, in the kernel pull request. The
build refuses a config that is not what the commit's defconfig gives. The
copy stays: tests read option values from it.

## How it builds

`orchard build` runs it; build with `orchard build --device j71b` (platform
`apple-16k` plus the j71b DTB) or `orchard build --platform apple-16k`, not
with `kernel/build` directly: it holds the shared tree's lock (see
[AGENTS.md](../AGENTS.md)). `--kernel-tree DIR` builds a kernel tree of your
own instead, as it is, with its own defconfig.

The source and compiler output live in one tree per platform, shared by
every checkout:
`${ORCHARD_CACHE_DIR:-${XDG_CACHE_HOME:-~/.cache}/orchard}/kernel-work/<platform>/`
(`--work-dir` chooses another). When the lock names another commit, a clean
tree is checked out to it in place: only the files that differ are written,
so only those compile again. A tree with local edits is refused, never
reset: save the edits, or build a tree of your own with `--kernel-tree`.
Checkouts whose locks name different commits move the tree back and forth.
A second build of the same tree waits for the first. The cache key of a built kernel is what
determines it: the source tree, the config, the descriptor's arch, image and
DTB directory, the toolchain image and `KERNEL_PRODUCER` in
`src/orchard/build.py`, not the text of these scripts; raise that version when
a change alters what they produce. Compiled objects also go
into a compiler cache (ccache) next to the trees, `.../orchard/ccache/kernel/`,
so a new commit, in any worktree, compiles only the files it changed;
it keeps itself under 2 GB. ccache stands in for `clang-19` on the recipe's PATH, so the
recorded compiler commands are the same with and without it; `ORCHARD_CCACHE=0`
builds without it and rebuilds nothing by itself. `make` runs one job per CPU of
the build environment (the Podman machine's CPUs); `ORCHARD_KERNEL_JOBS=N`
chooses another number. `--output` must name a new
directory; it receives the image the descriptor names (`Image.gz` for arm64), `<device>.dtb`, `dtbs/`, `kernel.release` and
`lib/modules/`.

## Where the history is

The branches of OrchardLinux/linux start as the patch layers orchard carried
until November 2026: one commit per patch and one merge commit per layer, in
the order they applied (`hoolock`, `import/pauli1go`, `drivers/<subsystem>`,
`devices/<soc>` on `main`; `import/cascadia`, `armv7/<subsystem>` on `armv7`),
then the platform defconfigs. `kernel/sources.toml` records where each layer
came from. Documents written before then name a patch by its old path
(`kernel/patches/<layer>/NNNN-subject.patch`): find it in OrchardLinux/linux by
its subject, or as a file in orchard's history up to the tag
`kernel-patches-final`. `tools/kernel-tree/README.md` says how to go back to
patch files.

The kernel has no device-specific NVMe fences. Protecting Apple's data is
the installer's job: the recovery keeps every NVMe partition and every
namespace but the main one read-only (`runtime/recovery/storage-fence`), and
the install script makes writable only what its plan creates, while writing.
