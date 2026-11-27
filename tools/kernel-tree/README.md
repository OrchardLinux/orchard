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

## A new base under the series

The branches are never force-pushed. A move to a newer upstream, a re-import
of a layer or a re-ordering is added on top of the branch:

```sh
orchard python tools/kernel-tree/replay --linux ../linux --branch armv7 --from OLD --onto NEW
```

`replay` makes a marker commit with NEW's content (first parent NEW, second
parent the branch as it is) and replays the commits of `OLD..armv7` after it,
with their authors and messages; a commit NEW already has is dropped. The
result, `armv7-replayed`, descends from the branch, so pushing it is a
fast-forward and nothing before is lost. Everything after the newest marker
is the current series on its base. It needs no checkout, so the clone can be
bare and on any filesystem; a commit that does not apply stops it, to be
resolved by hand and continued with `--continue`.

## Importing other trees

`import-cascadia.py` rebuilds Project Cascadia's A5 kernel as commits on
v6.12 and leaves them at `refs/orchard/cascadia-import` in the linux clone
(`--out DIR` also writes them as patch files). A Cascadia refresh is then:

```sh
orchard python tools/kernel-tree/import-cascadia.py --cascadia ../Project-Cascadia --commit REV \
    --linux ../linux --base adc218676eef25575469234709c2d87185ca223a
orchard python tools/kernel-tree/replay --linux ../linux --branch armv7 \
    --from LAST --onto refs/orchard/cascadia-import
```

LAST is the last commit of the import that `armv7` is on: the board device
tree commit before Orchard's own first commit (after the newest marker, or in
the original `import/cascadia` layer). Record the new Cascadia commit in
`kernel/sources.toml`, push the result as a pull request's worth of review,
and take it into orchard with `take`.
`kernel/tools/linearize-merges.py` and `replay-commits.py` made the Hoolock
and Pauli1Go layers.

## Going back to patch files

The patch files are in orchard's history up to the tags `kernel-patches-final`
and `m1n1-patches-final`. To return to them: restore `kernel/patches`,
`kernel/series`, the locks and `kernel/build` from the tag, then turn what
was merged in OrchardLinux/linux since into patch files with `export.py`
(`--orchard`, `--linux`, the range's branch). `convert.py` is the other
direction: it turns orchard's patch layers at a commit into a tree.
