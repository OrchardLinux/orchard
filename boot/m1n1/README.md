# m1n1 build

`build` fetches the pinned Asahi m1n1 revision (`upstream.lock`), applies the
ordered patch layers in `series` and compiles `m1n1.bin` with chainloading
enabled. Next to it go the same build as `m1n1-idevice.macho` (started by iBoot
as the kernelcache) and `monitor-stub.macho` (the monitor A7-A9 iBoot expects). Git and Podman are required.

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

## Optional resident PSCI on A7-A9

Orchard patch 0014 adds the linux-a9 opt-in EL3 PSCI shim. It requires
`psci=1` as a payload variable and entry at EL3; EL1 keeps the spin table.
There is no per-board default. See [J71t loader instructions](../../devices/j71t/README.md)
for `psci-loader`, launch/RAM framing, Pongo evidence and the cold-boot gate.
