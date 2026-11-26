# m1n1 build

Orchard's m1n1 lives in its own repository,
[OrchardLinux/m1n1](https://github.com/OrchardLinux/m1n1): AsahiLinux/m1n1,
Nick Chan's A7-A11 work from HoolockLinux/m1n1 and Orchard's changes on top.
m1n1 work is a pull request there.

`build` fetches the commit of that repository named in `upstream.lock` and
compiles `m1n1.bin` with chainloading enabled. Next to it go the same build as
`m1n1-idevice.macho` (started by iBoot as the kernelcache),
`monitor-stub.macho` (the monitor A7-A9 iBoot expects) and `m1n1-armv7.macho`
(the 32-bit build for the A5 to A6X chips, which their iBoot starts as its
kernel). Git and Podman are
required.

`orchard build` and `orchard run` run it with their cache directories; don't
call it directly (see [AGENTS.md](../../AGENTS.md)).

## Taking a newer m1n1

Set `commit` in `upstream.lock` to the new commit of OrchardLinux/m1n1 `main`,
in the same pull request as anything in orchard that needs it. Say in the
commit message what the new m1n1 does, not that a line moved. Nothing else
names the commit: the cache key, the nightly and a release follow this file.

To try an m1n1 tree before it is merged, build it there (`make
EXTRA_CFLAGS=-DQUIET_BOOT`, as `build` does) and pass the result as
`--loader FILE`.

## Where the history is

In OrchardLinux/m1n1, `main` starts as the patch layers orchard carried until
November 2026: one commit per patch, a merge commit per layer (`hoolock`:
AsahiLinux c9ef56f plus the 47 commits up to HoolockLinux/m1n1 `idevice`
d5a10ac^, tree-verified; `orchard`: Orchard's first 28 patches). Later work
follows as ordinary commits. The patch files themselves are in orchard's
history up to the tag `m1n1-patches-final`.

Scratch stays in ignored `boot/m1n1/.work/<device>/`. `--resume` reuses it only
for the same commit.
