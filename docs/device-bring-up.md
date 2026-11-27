# Device bring-up

Bring an unknown device, reuse the closest port, attempt a RAM boot, and contribute what you learn. A boot log or partial port is enough for a PR. A desktop and installer can wait; no distribution is needed.

Field reports include the iPad mini 2 [Wi-Fi (J85)](bring-up/j85.md) and
[Cellular (J86)](bring-up/j86.md), and the [iPhone XR (N841)](bring-up/n841.md),
the first A12 board. These record experimental observations,
not supported installation profiles.

## 1. Set up

```sh
git clone https://github.com/OrchardLinux/orchard
cd orchard
./setup                                          # once: tools, the orchard command
git worktree add ../orchard-BOARD -b BOARD       # your bring-up branch
cd ../orchard-BOARD
```

`./setup` says what it will set up and asks first; `orchard doctor` shows what is still missing. `orchard` runs the checkout you are in, so this worktree's patches and device data are what you build; see the [README](../README.md#develop).

Charge the device and build before entering DFU: `orchard build --platform PLATFORM` in the example.

## 2. Find a starting point

Connect the device in DFU and inspect it:

```sh
orchard devices
```

Record the `chip` and `board` fields. Discovery lists unknown devices too. The USB `product` name may just say DFU, not the model.

Get the model from Settings → General → About or the case's model number. For the Apple product identifier, such as `iPad7,5`, run `ideviceinfo -k ProductType` with iOS running and the computer trusted. That optional tool comes from libimobiledevice. Missing a model name is not a reason to stop experimenting.

- Find an existing kernel/DTB for that board or the closest SoC. Keep its source revision, patches and licenses.
- Keep any working boot tools and payloads. Change one thing per attempt.
- Do not copy another board's GPIO, regulator or charging writes blindly.

## 3. Build and attempt a RAM boot

```sh
cd examples/minimal-boot
orchard run                        # a described board: built for it and booted
orchard run --platform apple-16k   # a board not described yet: the closest platform
orchard run --yes                  # without a prompt
```

A payload carries every DTB of its platform, so one build boots each of its described boards; a board not yet described gets a warning first. Once your board has its `devices/BOARD/device.toml`, `orchard run` recognizes it.

To keep your own experiment, copy an example anywhere (`cp -r examples/minimal-boot ~/board-tests`); run it with this worktree's launcher (`/path/to/orchard-BOARD/bin/orchard run`) to use the worktree.

The reference kernel/DTB is a starting experiment, not a guarantee it fits your board.

An experiment contains `init` and optional `rootfs/`. Put your startup and diagnostics there. No installer or device-side RPC is needed.

Reuse compiled components. Changing `init` should not rebuild Linux. The kernel tree and compiler cache in `~/.cache/orchard/` are shared by every checkout; check `orchard cache` before starting another full compile.

## 4. Add your board's recipe

Your board probably has a `devices/BOARD/` already, with its identity and `boot.json` from public sources (`tools/catalog/generate`). Bring-up fills in the rest of `device.toml`: `[kernel]`, `[boot]`, `[firmware]`, `[support]` and `[features]`. Installing becomes available by itself once the board has a kernel, a boot method, firmware and `storage_write` isn't `missing`. Notes that aren't a feature state go in the board's README. When `name` carries the chip (`iPhone 6s+ (S8000)`), `[device] short_name` gives it without (`iPhone 6s Plus`) for tables that show the chip beside it. `[support] tested_by` lists the GitHub handles of the people who have run Orchard on the board. Remove `[catalog]` when you add `[kernel]`. See [device compatibility](compatibility.md) for what is known about it.

In your worktree:

| Path | Add |
| --- | --- |
| `devices/BOARD/device.toml` | Model name, product identifiers, chip/board IDs and compatible strings. |
| `kernel/configs/BOARD/config` | Kernel configuration. |
| `kernel/devicetree/BOARD/build` | Commands producing the final DTB. Accepts `--kernel-build`, `--headers`, `--output`. |
| `kernel/modules/BOARD/` | External modules, if needed. |

Use J71b's files as format examples, not hardware settings to copy wholesale. See the [component API](ram-development.md) to build a named recipe or assemble explicit kernel/DTB/loader inputs.

## 5. Find where it stops

| Observation | Check next |
| --- | --- |
| Still in DFU | Boot-chain tool output and USB transport. |
| Pongo runs, no kernel output | Payload, loader, DTB and console routing. |
| Kernel hangs or panics | Last messages, config, DTB and failing driver. |
| Kernel runs, no init | Initramfs, executable `/init` and boot arguments. |
| Init runs, no USB shell | Controller driver, gadget setup and host serial endpoint. |

Save commands, revisions and logs for each attempt. Successful upload does not prove Linux booted. Missing USB serial does not prove it failed. Inspect an uncertain result before retrying.

## 6. Send the PR

Include:

- Exact device tested and how to reproduce the build/boot.
- Source revisions, patch attribution and relevant logs.
- Furthest boot stage; working, broken and untested hardware.
- Any shared changes that could affect existing ports.

Keep firmware bytes, calibration, raw ADT captures, ECIDs and credentials out of public commits/logs. Contribute firmware recipes instead.

Hardware data, recipes and host tooling are one PR here; see [CONTRIBUTING.md](../CONTRIBUTING.md) for how to split topics. Distribution changes go to the distribution.

Run the tests (`orchard python -m unittest discover -s tests`). Report physical boot results separately from compilation/test results.
