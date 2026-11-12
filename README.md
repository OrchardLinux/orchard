# Orchard

Linux on Apple devices. This repository holds everything a device needs and the
`orchard` tool that builds, boots and installs it: device descriptions, kernel
patches and configuration, m1n1 and Pongo patches, firmware recipes, the recovery
and device runtime, and the host side (USB transports, firmware conversion,
installation, launch). Distributions such as
[omarchy-a](https://github.com/OrchardLinux/omarchy-a) and
[minimal-linux](https://github.com/OrchardLinux/minimal-linux) build on it.

## Start

```sh
git clone https://github.com/OrchardLinux/orchard && cd orchard
./setup
orchard run examples/minimal-boot
```

`./setup` shows what this computer needs to build and boot (uv, Podman, libusb,
palera1n, the `orchard` command), asks, and sets it up; rerun it until it says
Ready. `orchard doctor` shows the same without changing anything. Put the device
in DFU mode before `orchard run`: it builds what is missing for the attached
board, then boots it. The first build compiles Linux, which takes a while; later
ones reuse it.

## Supported hardware

Currently: iPad 6 Wi-Fi (`iPad7,5`, J71b), experimental. Boot is tethered and GPU acceleration is not available.

The iPhone 6 (`iPhone7,2`, N61AP) has experimental tethered support. See the [bring-up record](docs/n61-bringup.md) for its tested features and remaining work.

Every board, chip and feature state: [device compatibility](docs/compatibility.md).

## Install a system

Follow the install instructions of a distribution, e.g. omarchy-a. Every release
of a distribution carries its installer:

```sh
curl -fsSL https://github.com/OrchardLinux/omarchy-a/releases/latest/download/install.sh | sh
```

It needs no Python, uv or checkout: `install.sh` downloads a pinned standalone
Python and the release's `orchard-install.pyz`, checks both, and installs. It
leaves the user's `orchard` on PATH (`install`, `launch`, `devices`, `update`),
which a later installer replaces only with a newer one. Releases built before
orchard 0.2 install with the released Orchard 0.1.10
([OrchardLinux/cli releases](https://github.com/OrchardLinux/cli/releases)).

To start an installed system again, connect the device in DFU mode and run
`orchard launch`. The host sends only Pongo and m1n1; m1n1 reads the boot entries
from the device's boot partition and starts the one you choose (`--entry ID` picks
one without asking). Any computer with Orchard can launch any installed system.

## Connect to a running system

`orchard shell`, `exec`, `push` and `pull` discover a running device over USB by
default. To use Wi-Fi instead, pass an SSH address, for example
`orchard shell --host 192.168.0.19 --user tablet` or
`orchard exec --host 192.168.0.19 --user tablet -- uname -a`. The installed
system must allow this computer's Orchard SSH key. For a RAM payload, use
`--host 192.168.0.19 --ram` instead; it connects as root with the RAM key.
The payload must have SSH and Wi-Fi configured.

Network connections accept and remember a new SSH host key on first use; unlike
USB RAM sessions, this does not verify the device's identity over serial. Verify
the key out of band. Changed keys are rejected. RAM payloads generate a new
key each boot; to trust a new boot, remove the old address with
`ssh-keygen -R 192.168.0.19 -f ~/.cache/orchard/private/ram-ssh/network_known_hosts`
(adjust the cache path if configured differently).

## Develop

The `orchard` command `./setup` puts on your PATH (`tools/launcher`) runs the
newest nightly of orchard, its tooling and its components, looked up once per
command; `ORCHARD_VERSION` pins one, and `orchard update` installs the newest
nightly's launcher. To work on orchard itself, run a checkout's own
`bin/orchard` by its path: it runs that checkout, from any directory, with
its hardware data and recipes beside `src/`.

A payload is built for every board of one platform (`out/apple-16k/`, `out/apple-4k/`).
`orchard run` takes the platform from the attached board; `orchard build` from
`--device BOARD`, `--platform`, a `device` in the checkout's `orchard.local.toml`
(never committed) or the attached board. Nothing in a repository names a board.

Bring up a device or try a driver in a worktree of its own:

```sh
git worktree add ../orchard-gpu -b gpu-a10
cd ../orchard-gpu/examples/minimal-boot
orchard run
```

The [examples](examples/) are starting points: `minimal-boot` (a serial shell),
`charger-001` (a read-only observation), `hardware-check` (a report on what a device
offers). `orchard python ARGS` runs Python in the checkout's environment, e.g. a
distribution's own scripts or `orchard python -m unittest discover -s tests`. See
[BUILD.md](BUILD.md) for building components directly.

A distribution checkout has an `orchard.toml` with a `[release]` table naming its
build entry (`name = "..."`, `[release] build = "tools/build.py"`). There
`orchard build` builds a whole release into `out/releases/dev-DATE-TIME`,
`orchard run` boots its recovery on the attached device without installing, and
`orchard install` installs the last build ([tooling](docs/tooling.md)).

- [Kernel producers](kernel/README.md)
- [Device bring-up](docs/device-bring-up.md)
- [RAM builds and development](docs/ram-development.md)
- [Firmware recipes](docs/firmware-recipes.md)
- [Device operation protocol](docs/device-protocol.md)
- [Host adapter contract](docs/adapter-contract.md)
- [SSH lifecycle](docs/ssh-lifecycle.md)
- [Recovery tools](boot/initramfs/recovery/README.md)
- [Quiet boot patches](boot/quiet/README.md)
- [J71b compatibility audit and validation limits](docs/j71b-compatibility-audit.md)
- [GitHub builds](docs/ci.md)
- [Tooling design](docs/tooling.md)
- [Platforms, kernel trees, builds and boot](docs/platforms-and-builds.md)
- [Contributing](CONTRIBUTING.md)

The repository contains recipes and code, not private Apple firmware, calibration,
device trees captured from hardware, or session credentials. Device-specific inputs
are prepared on the user's host.

## License

See [LICENSE](LICENSE), [NOTICE](NOTICE), `LICENSES/`, and per-file SPDX identifiers for component licenses and upstream credits.
