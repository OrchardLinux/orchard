# Orchard Simulator

An iPad 6 frame on your desktop, running the published Omarchy A system in a
virtual machine. Use it to work on the desktop: the greeter, the shell, the
on-screen keyboard, apps and touch gestures, without a tablet in DFU mode.

## Start

On an Apple Silicon Mac:

```sh
glu install qemu
podman machine start          # builds the VM disk
simulator/setup               # newest Omarchy A nightly → out/simulator/vm
simulator/run                 # opens the simulator
```

Sign in with PIN **123456**. `simulator/setup` downloads about 700 MB once and
builds a 12 GB sparse disk (about 4 GB used). The account is the installer's:
user `tablet`, PIN 123456, a random password in `out/simulator/vm/credentials`
(sudo needs none), and your `~/.ssh/id_*.pub` keys for SSH.

## What it runs, and what it does not

- **Real:** everything above the kernel, as an install puts it on a tablet: the
  release's root image, the installer's account and PIN setup, and the boot
  options of an installed system (`quiet fbcon=map:9 vt.global_cursor_default=0`).
- **Two desktops, as on the tablets:** **Orchard Simulator → GPU Desktop**
  (the default) is the iPad 6's: Labwc on GLES2 and the shell and apps on
  OpenGL, with their animations, here on Mesa's software OpenGL instead of the
  GPU. **Software Desktop** is that of a board without a GPU driver: Labwc on
  pixman, Qt's software renderer, no animations. Switching restarts the session
  (`simulator/vm desktop gpu|software` does the same).
- **Not real:** the kernel is the Podman machine's Fedora kernel, not orchard's;
  the Apple drivers cannot run in a VM. So there is no GPU (the GPU desktop
  draws in software, slower than on the tablet), no Apple touch controller
  (QEMU's virtual multitouch stands in), no sleep (suspend is masked: a VM has
  no wake source), and no panel backlight. Hardware behaviour still needs the
  tablet.

`simulator/run` prints which release the disk holds and says when a newer
nightly is out. Nothing updates on its own; to move to it:

```sh
simulator/vm stop && simulator/setup --replace     # or: simulator/setup --replace v0.3.1
```

Replacing throws the old disk away, with anything you changed in it.

## Using it

- Click and drag on the screen are touches (one finger). A drag that starts
  on the inner half of the bezel slides onto the screen from its edge (the
  keyboard from the bottom, panels from the sides and the top), also in mouse
  mode; the outer half moves the frame. **Orchard Simulator →
  Mouse Input** switches to an ordinary mouse with right-click and wheel.
- The Mac keyboard types into the guest after you click the screen.
- The frame's buttons are the tablet's: Power, Home, Volume up and down.
- Drag the frame to move it, its corners to resize.
- Quitting shuts the VM down; Option-Cmd-Q quits and leaves it running.

## For agents and scripts

- `ssh -p 2228 tablet@127.0.0.1`: a shell in the guest.
- `simulator/qmp shot FILE.png`: the guest screen as the guest draws it.
- `simulator/qmp key 1`, `simulator/qmp type TEXT`, `simulator/qmp tap X Y`:
  input, in guest pixels (768 × 1024).
- `simulator/vm status|start|stop|kill`: the VM without the window.

A change to a package can be tried by copying the built files in over SSH and
restarting the session; the disk is disposable, `setup --replace` restores it.

## How it works

QEMU runs the VM (HVF on macOS; KVM on Linux). The window gets the screen
from QEMU's D-Bus display, peer to peer over a private socket (no D-Bus daemon):
each frame is in memory shared with QEMU, drawn as soon as the guest draws it
(`display/qemu-display.c`, plain GLib, so a Linux viewer can use the same
code). Touch, keys and the hardware buttons go in through QMP. A small guest
service (`vm-support/orchard-vm-bridge.py`) takes shutdown requests and
reports the backlight. QMP sockets: `qmp.sock` (the window's input),
`viewer.sock` (the window's display), `agent.sock` (scripts).

Optional: `simulator/build-vm-backlight` and `vm-support/install-linux.sh`
(in the Podman machine, VM stopped) add a VM-only backlight driver, so the
Power button's screen blanking dims the window as it would the panel.

Orchard Simulator is not made or endorsed by Apple. iPad is a trademark of
Apple Inc.; the frame only shows which device the display size and buttons
model.
