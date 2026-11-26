# m1n1 build

`build` fetches the pinned Asahi m1n1 revision (`upstream.lock`), applies the
ordered patch layers in `series` and compiles `m1n1.bin` with chainloading
enabled. Next to it go the same build as `m1n1-idevice.macho` (started by iBoot
as the kernelcache) and `monitor-stub.macho` (the monitor A7-A9 iBoot expects). Git and Podman are required.

It also builds the 32-bit m1n1 for Apple's ARMv7 chips (A5-A6X) from the same
source, `m1n1-armv7.macho`: one file for every 32-bit board, as `m1n1.bin` is for
the 64-bit ones. Their iBoot starts it as its kernel;
`producers/m1n1_armv7.py` puts it and a payload into the image iBoot takes
([bring-up notes](../../docs/bring-up/armv7-m1n1.md)).

`orchard build` and `orchard run` run it with their cache directories; don't
call it directly (see [AGENTS.md](../../AGENTS.md)).

Layers, in order:

- `patches/hoolock/`: Nick Chan's A7-A11 staging tree for upstream m1n1,
  exported verbatim. The base is Hoolock's merge base with Asahi, so base plus
  this layer equals Hoolock's tree (see `sources.toml`).
- `patches/orchard/`: Orchard's loader changes. Board data comes from the ADT,
  never from board compatibles.

When Hoolock lands a commit in Asahi, its patch leaves `patches/hoolock/` with
the next base move. To refresh the layer from a newer Hoolock revision:

```sh
git format-patch --zero-commit --no-signature -o boot/m1n1/patches/hoolock BASE..TIP
```

Leave out CI-only `[DOWNSTREAM]` commits and check that base plus the layer
gives the same tree as Hoolock's tip before updating `sources.toml`.

Scratch stays in ignored `boot/m1n1/.work/<device>/`. `--resume` reuses it only
if the recorded patch set is unchanged.
