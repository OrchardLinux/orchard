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

## Resident PSCI on A7-A9

Orchard patch 0015 adds the resident EL3 PSCI monitor (CPU_ON, CPU_OFF,
AFFINITY_INFO); 0016-0019 make it the default whenever m1n1 is entered at EL3
and add SYSTEM_RESET (warm reboot back into m1n1, which boots the default BLS
entry with the last launch's DTB) and SYSTEM_OFF (halt). Any PSCI-aware
kernel reboots, powers off and kexecs through its ordinary paths; m1n1 takes
IPI-stopped CPUs back by trapping EL1 WFI/WFE to EL3. EL1/EL2 entries keep
the spin table. Payload variables (append to `m1n1.bin`, e.g. with
`psci-loader`): `psci=0` turns PSCI off, `psci_reset=0` keeps only the CPU
lifecycle (no WFI/WFE traps), `psci_reset=proxy` stops in the proxy after a
reset. 0020 marks retained hardware in the device tree after a re-entry.
