# Notes for coding agents

Several agents and people work on one machine at the same time, in different
checkouts, sharing one Orchard cache (`~/.cache/orchard`), one Podman VM and
the devices on the desk. The rules below keep that from breaking each other's
work. When a rule and your task conflict, ask the person you work for.

## Which orchard runs

- `orchard` on PATH (`tools/launcher`) runs the newest nightly of orchard,
  looked up once per command; `ORCHARD_VERSION=nightly-…` pins one. It does
  not look at the current directory.
- To run the code of a checkout or worktree, call its own launcher by path:
  `/path/to/checkout/bin/orchard build …`. Nothing else selects a checkout.
- Build with `orchard build` and boot with `orchard run` (RAM) or
  `orchard launch` (installed). A project says what it builds in its
  `orchard.toml`; a board or platform is an argument (`--device j71b`,
  `--platform apple-16k`) or a line in the uncommitted `orchard.local.toml`.
- `orchard build --explain` prints what a build ran as plain commands.

## Kernel and m1n1 changes

- Work on a branch of OrchardLinux/linux (or m1n1) in a worktree under
  `~/projects/OrchardLinux/trees/` (a case-sensitive volume), as ordinary
  source. Build it with `orchard build … --kernel-tree DIR`; its build output
  stays inside the tree, in `.orchard-out/` (git ignores it there).
- Commits in upstream style: one topic each, `Assisted-by: LLM`, no
  Signed-off-by, the author from the global git config.
- When it works on hardware, `tools/kernel-tree/export.py` turns the branch
  into orchard's patch files (defconfig changes become orchard's config).
  Don't pick or reserve patch numbers: a branch needs none, and export
  numbers its patches after those already in each directory.
  Until the switch, orchard's patch files are the source of truth: the
  `main` branches of OrchardLinux/linux and m1n1 are generated from them by
  `tools/kernel-tree/regenerate` (which refuses a branch with commits it did
  not make); never commit to those branches by hand.

## Distributions

- A distribution's build imports none of orchard's Python: it runs orchard's
  producers as commands from a snapshot (`docs/distribution-contract.md`).
  Changing one of those commands is a contract change: update both
  distributions in the same push set.

## Disk: every GB counts

The machine's disk is always nearly full, and a full disk breaks everyone's
builds and can hang the VM. Treat space as scarce and time as scarce too:

- **Clean up what you made, when you are done with it:** your worktrees,
  scratch and work directories, test releases and `out/` builds you no longer
  need, downloaded archives. Before you report a task as finished, list what
  you created and remove what nobody needs any more. Keep what is still in
  use (the last good build someone boots, a tree you keep working in).
- **Don't delete what others made**, nor package caches, git mirrors,
  builder images, firmware or private inputs, unless the person asks you to.
  If something looks abandoned, list it with its size and ask.
- **Check before large work:** `df -h ~` before a kernel, root or image build
  or a large download; below about 10 GB free, clean up first or ask.
  `orchard build` and `orchard run` refuse to start below 10 GB
  (`ORCHARD_MIN_FREE_GB` changes the limit).
- **Prefer reuse to copies:** a published nightly component over a local
  build, one worktree over several, a pointer over a duplicate.
- Inside `~/.cache/orchard`, delete through `orchard cache` (or ask), never
  by editing its trees or bare repositories.

## Devices

- One holder per device. Say when you take it ("taking the J71b for one RAM
  boot") and when you give it back; coordinate with the other sessions.
- Never boot or flash without the person's go for that attempt (e.g. "dfu
  ready"), even if the device shows up in DFU. One attempt, no retry loops.
- RAM boots by default; write to a device's storage only when asked.
- USB serial and SSH must always come up; no change may take them away.
- Bound every command you run on a device (`timeout -k 5 N …`) and read the
  serial log and dmesg before guessing.
- **The DFU bell:** when a person has to do something physical (put a
  device into DFU, replug it, unlock it), ring it once with
  `tools/dfu-bell/dfu-bell`, say in one line what you need, then wait.

## Device specs and capabilities

- Device data must always match the code. When a change affects what a board
  or chip is or can do (a driver, kernel config, firmware, boot method, a test
  result), update `devices/<board>/device.toml` and `chips/*.toml` in the same
  change, including a change that breaks or removes something.
- Regenerate the hardware support matrix with `tools/catalog/generate matrix`;
  never edit `docs/compatibility.md` by hand.

## Changelog

- CHANGELOG.md is curated: one entry per meaningful unit of work (a device, a
  hardware feature, an install or launch change, a compatibility break), not
  per commit, refactor or CI change.
- When you finish such a unit, propose the entry under Unreleased: what
  changed, described by its effect. Add it only after the person explicitly
  approves it, and never edit the changelog without that approval.
