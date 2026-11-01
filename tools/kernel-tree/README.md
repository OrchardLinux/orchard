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

On macOS the kernel tree needs a case-sensitive volume, and the kernel builds
in a Linux container.

## Regenerating the trees (maintainers)

```sh
tools/kernel-tree/regenerate --linux ../linux --m1n1 ../m1n1 [ORCHARD_COMMIT]
```

It applies the series in build order, one commit per patch and one merge per
patch directory, adds the platform defconfigs (checked to reproduce
`kernel/configs`), and moves `main` of both clones. It pushes nothing.

Regeneration is stable: a commit keeps the committer and date of the orchard
commit that last changed its patch file, and a merge cites the orchard commit
that last changed its directory, so topics before the first changed one keep
their SHAs. Branches based on an older `main` rebase cleanly onto a new one.
