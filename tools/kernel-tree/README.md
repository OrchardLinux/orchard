# kernel-tree

Orchard's kernel and m1n1 also exist as git trees:
[OrchardLinux/linux](https://github.com/OrchardLinux/linux) and
[OrchardLinux/m1n1](https://github.com/OrchardLinux/m1n1). During the
transition the patch layers here stay authoritative and `main` of both trees
is generated from them. These tools do both directions.

## New kernel work in the tree

```sh
git clone https://github.com/OrchardLinux/linux && cd linux
git switch -c my-work
make ARCH=arm64 LLVM=1 orchard_apple16k_defconfig   # orchard_apple4k_defconfig for A7-A8X
make ARCH=arm64 LLVM=1 -j"$(nproc)" Image.gz modules dtbs
# commit as usual, then turn the branch into orchard patch files:
~/orchard/tools/kernel-tree/export.py --orchard ~/orchard --linux . my-work
```

`export.py` writes each commit into `kernel/patches/<topic>/` with the next
free number, in `git format-patch --zero-commit` format, and checks that the
files reproduce the branch. The topic comes from the files a commit touches;
if they point to several topics it asks for `--topic SHA=TOPIC`. Changes to
an Orchard defconfig belong in `kernel/configs/<platform>/config` instead.

When a topic applies to one platform's kernel base, pass `--platform` after
testing the branch on hardware:

```sh
~/orchard/tools/kernel-tree/export.py --orchard ~/orchard --linux . \
    --base armv7 --platform apple-armv7 my-work
```

Each exported patch receives an adjacent `.patch.toml` with schema 1 and the
selected platform names. Export adds `scoped patches/<topic>` to the selected
series when no existing layer covers that directory. A scoped layer selects
only explicitly matching patches, which lets ARMv7 share subsystem directories
with ARM64. Ordinary layers keep including unscoped patches and skip patches
scoped to another platform. Existing patch files keep their names and bytes.
`--dry-run` prints the plan without writing files or reserving numbers.

Repeat `--platform` for every platform on the same generated branch, such as
`--platform apple-4k --platform apple-16k`. Regeneration requires those
platforms to select the same ordered patches. Without `--platform`, export
keeps writing unscoped patches and leaves the series files alone.

On macOS the kernel tree needs a case-sensitive volume, and the kernel builds
in a Linux container.

## Regenerating the trees (maintainers)

```sh
orchard python tools/kernel-tree/regenerate --linux ../linux --m1n1 ../m1n1 [ORCHARD_COMMIT]
```

Every platform descriptor names its branch in OrchardLinux/linux (`main` for
apple-16k and apple-4k, `armv7` for apple-armv7). For each branch it applies that
series onto its upstream base (fetched if missing) in build order, one commit
per patch and one merge per patch directory, adds the platforms' defconfigs
(checked to reproduce `kernel/configs`), and moves the branch; m1n1 `main`
likewise. It pushes nothing.

Regeneration uses the committed platform selector, including patch scopes.
A commit keeps the committer and date of the orchard commit that last changed
its patch file or adjacent scope, and a merge cites the latest change to its
selected patch inputs. Topics before the first changed one keep their SHAs.
Branches based on an older `main` rebase cleanly onto a new one.
