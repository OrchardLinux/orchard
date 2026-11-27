# kernel-tree

Orchard's kernel is [OrchardLinux/linux](https://github.com/OrchardLinux/linux)
and its m1n1 [OrchardLinux/m1n1](https://github.com/OrchardLinux/m1n1). Both
were patch layers in orchard until November 2026; these tools are what remains
of that, and the way back.

## Kernel work

```sh
git clone https://github.com/OrchardLinux/linux && cd linux
git switch -c my-work
make ARCH=arm64 LLVM=1 orchard_apple16k_defconfig   # orchard_apple4k_defconfig for A7-A8X
make ARCH=arm64 LLVM=1 -j"$(nproc)" Image.gz modules dtbs
# commit as usual and open a pull request against main (armv7 for the 32-bit boards)
```

`orchard build … --kernel-tree .` builds the tree with orchard's recipe. On
macOS the kernel tree needs a case-sensitive volume, and the kernel builds in
a Linux container.

## Taking a newer kernel into orchard

```sh
orchard python tools/kernel-tree/take [--linux ../linux] [BRANCH[=COMMIT] ...]
```

Moves the locks (`kernel/upstream.lock`, `kernel/upstream-armv7.lock`) to the
branch heads, or to the commits named, and writes
`kernel/configs/<platform>/config` again from the tree's defconfigs. Commit
the result with the change that needs it.

## Importing other trees

`import-cascadia.py` rebuilds Project Cascadia's A5 kernel as commits on
v6.12 (it leaves them at `refs/orchard/cascadia-import` in the linux clone)
and can still write them as patch files. A refresh replays the `armv7`
series on the new import and adds the result on top of the branch, as
`docs/platforms-and-builds.md` describes for any re-base.
`kernel/tools/linearize-merges.py` and `replay-commits.py` did the same for
the Hoolock and Pauli1Go layers.

## Going back to patch files

The patch files are in orchard's history up to the tags `kernel-patches-final`
and `m1n1-patches-final`. To return to them: restore `kernel/patches`,
`kernel/series`, the locks and `kernel/build` from the tag, then turn what
was merged in OrchardLinux/linux since into patch files with `export.py`
(`--orchard`, `--linux`, the range's branch). `convert.py` is the other
direction: it turns orchard's patch layers at a commit into a tree.
