# Contributing

Thanks for helping. This repository is shared by every device Orchard supports,
so a change here can affect devices you don't have. The rules below exist so
that a reviewer can tell, for every line, why it is there and what it touches.

## Everything benefits every device

**Every change benefits every device it can.** A driver, device tree node,
firmware recipe or desktop change is on by default for every board that has
the hardware: describe it in the shared chip or model `.dtsi` with
`status = "okay"`, not in one board's `.dts`. A per-board file holds only what
really differs on that board, such as its wiring.

**Every opt-in or opt-out needs a very good reason, written next to it:** the
hardware is absent, or there is evidence that it breaks that board.
**"Untested" is never a reason.** Neither is "I only have one device". The
catalog marks the other boards `untested`, and their owners find out.

## What belongs here

This repository holds device descriptions, kernel configuration, firmware
recipes, the boot and recovery initramfs, and the
`orchard` tool on the host (booting, installing, the host link, firmware
conversion) in `src/orchard`. A device change and the converter steps or host
support it needs go into one PR here. Desktops and distribution profiles
belong in [omarchy-a](https://github.com/OrchardLinux/omarchy-a); work that
also needs a distro change is one PR per repository, and each says which one
it needs and in which order they merge. Kernel and m1n1 work are pull
requests in [OrchardLinux/linux](https://github.com/OrchardLinux/linux) and
[OrchardLinux/m1n1](https://github.com/OrchardLinux/m1n1), never patch files
here.

## One topic per pull request

Keep separate topics in separate PRs, each with a description of the problem
it solves:

- **A device:** its descriptor, firmware recipe, device tree, the driver
  changes it needs, the config options it needs and its documentation.
- **A boot or runtime path,** such as a new root filesystem source.
- **Shared build tooling:** `kernel/build`, `boot/m1n1/build`, `src/orchard/build.py`.
  These change every device's build.
- **CI.**

A device PR can depend on a tooling PR; say so and link it. Within a PR, split
commits by logical part. They don't need to be smaller than that.

If users, device owners or contributors would notice the change (a device, a
hardware feature, install or launch behaviour, a compatibility break), add a
line under Unreleased in [CHANGELOG.md](CHANGELOG.md). It becomes the release
notes. If you don't, we add it when the PR lands.

## How pull requests land

We usually rework a pull request to fit the kernel structure and orchard's
layout before merging it: drivers in the kernel rather than in userspace,
patches in upstream style, device data in the device layer. The rework goes
onto your PR branch, your commits keep your authorship, and the PR is merged
on GitHub, so it shows as your contribution. If you'd rather we didn't touch
your branch, say so in the PR and we'll review it with you instead.

**Read the merge comment carefully.** A merge is often partial: we merge
what fits now and record the rest. The comment on the PR says what was
changed on your branch, what was left out and why, and which follow-up work
is open. Nothing is thrown away. Your original commits stay in the history
through the merge, and code that isn't in the tree yet is tagged
`contrib/<login>-<topic>`. The device's `devices/<board>/README.md` (or its
bring-up doc in `docs/bring-up/`) lists that code under "Unmerged code" and
the follow-ups under "Open work", so you can pick them up from there.

## Where kernel changes go

The kernel's source is [OrchardLinux/linux](https://github.com/OrchardLinux/linux):
branch `main` for the arm64 boards, `armv7` for the 32-bit ones. Kernel work is
a pull request there, against the source; orchard holds no kernel patches. Its
checks compile every platform of the branch and run orchard's tests of the
kernel source.

- Keep driver changes and device trees in separate commits: a device tree
  commit that also changes a driver hides that it changes behaviour on other
  devices.
- Build your branch with `orchard build … --kernel-tree DIR`.
- What a board needs beside its kernel (its `devices/<board>/` data, firmware
  recipe, docs) is a pull request in orchard. When it needs the new kernel, it
  takes it in the same pull request: `tools/kernel-tree/take`, described in
  `kernel/README.md`. The kernel pull request merges first.

m1n1 works the same way: its source is [OrchardLinux/m1n1](https://github.com/OrchardLinux/m1n1),
and m1n1 work is a pull request there. `boot/m1n1/upstream.lock` names the
commit orchard builds; see `boot/m1n1/README.md`.

## Writing kernel commits

Write commits as you would for upstream Linux:

- Subject in the subsystem's style (`Input: apple_z2: …`,
  `arm64: dts: apple: …`), one logical change per patch, and a body that says
  what changes and why.
- Hardware differences belong in match data and the device tree, selected by
  compatible. No checks for a specific machine inside shared code, and no
  module parameters for hardware behaviour.
- Bindings for new compatibles or properties go in
  `Documentation/devicetree/bindings`, in their own patch.
- The author is whoever wrote the code. If a patch derives from someone else's
  work, they stay the author, and an `[Orchard: from <repo> <commit>; …]` note
  above the sign-offs says where it comes from and what changed.

## Kernel configuration

One config builds every device on a platform: `apple-4k` covers A7, A8 and
A8X, `apple-16k` covers A9 and later, `apple-armv7` the 32-bit boards. Its
source is the platform's defconfig in the kernel tree
(`arch/<arch>/configs/orchard_<platform>_defconfig`); change it there, with
`make savedefconfig`, in the kernel pull request. For each change, the commit
message says which options change, which device or feature needs them, and
why. That covers options you turn off too, and options you switch from module
(`=m`) to built-in (`=y`). Make a change in both arm64 defconfigs unless it's
only about the page size.

`kernel/configs/<platform>/config` in orchard is the expanded copy;
`tools/kernel-tree/take` writes it when orchard takes the kernel commit.

## Bring-up material

Bring-up documentation is welcome: what you measured, traced or found out, in
`docs/bring-up/` or the device's docs. It's often the only record of how the
hardware works.

Tools you used to get there stay on your fork: overlay modules, development
device trees, debug switches. Main carries the final form that boots on the
device.

## Private data

Public files contain recipes and code, not Apple firmware, calibration data,
device trees dumped from a device, or credentials. Firmware is extracted on the
user's host from the recipe in `devices/<device>/firmware.json`.

## Testing

- Run the unit tests: `PYTHONDONTWRITEBYTECODE=1 python -m unittest discover -s tests -v`.
- Test the exact revision you submit on the device, and say in the PR how: the
  command, the image, and what worked and what didn't.
- If you change a shared driver or config, say which other devices use it.
  We test those we have (currently the iPad 6, J71b) before merging.

Unit tests don't show that a device boots. A device feature is `working` in its
descriptor only after it was seen working on the device.
