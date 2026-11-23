#!/bin/sh
# Run inside the Podman Linux VM with sudo:
#     prepare-vm-linux.sh RELEASE_DIRECTORY
# RELEASE_DIRECTORY holds a distribution release's root.blocks.zst and root.map.json (what
# `orchard install` writes to a partition). Writes ONLY a new file-backed VM disk under
# out/simulator/vm, then does what the installer's finish step does that a VM needs: the
# account (password, PIN, this Mac's SSH key) through orchard's own install_device code.
set -eu
ROOT=$(CDPATH= cd -- "$(dirname -- "$0")/.." && pwd)
RELEASE=$(CDPATH= cd -- "${1:?usage: prepare-vm-linux.sh RELEASE_DIRECTORY}" && pwd)
OUT="$ROOT/out/simulator/vm"
DISK="$OUT/root.raw"
MOUNT="$OUT/mount"
KVER=$(uname -r)
SIZE=${ORCHARD_VM_DISK:-12G}
[ "$(id -u)" = 0 ] || { echo 'Must run as root inside the Podman Linux VM' >&2; exit 1; }
[ "$(uname -m)" = aarch64 ] || { echo 'ARM64 Linux required' >&2; exit 1; }
[ -f "$RELEASE/root.blocks.zst" ] && [ -f "$RELEASE/root.map.json" ] || { echo "No root.blocks.zst/root.map.json in $RELEASE" >&2; exit 1; }
[ ! -e "$DISK" ] || { echo "Refusing to overwrite $DISK" >&2; exit 1; }
mkdir -p "$OUT" "$MOUNT"
cleanup() { if mountpoint -q "$MOUNT"; then umount "$MOUNT"; fi; }
trap cleanup EXIT INT TERM

# The block stream holds the listed 4 KiB blocks in order; the rest stays a hole.
zstd -dc "$RELEASE/root.blocks.zst" | python3 -c '
import json, os, sys
info = json.load(open(sys.argv[1]))
block, source = info["block_size"], sys.stdin.buffer
with open(sys.argv[2], "wb") as disk:
    disk.truncate(info["blocks"] * block)
    for start, count in info["ranges"]:
        disk.seek(start * block)
        remaining = count * block
        while remaining:
            data = source.read(min(4 << 20, remaining))
            if not data:
                raise SystemExit("block stream ended early")
            disk.write(data)
            remaining -= len(data)
    if source.read(1):
        raise SystemExit("block stream is longer than its map")
' "$RELEASE/root.map.json" "$DISK"
truncate -s "$SIZE" "$DISK"
e2fsck -fy "$DISK" >/dev/null || [ $? -le 1 ]
resize2fs "$DISK" >/dev/null
e2label "$DISK" omarchy-vm

mount -o loop "$DISK" "$MOUNT"
# VM kernel and matching modules are separate from the real iPad kernel.
mkdir -p "$MOUNT/usr/lib/modules"
cp -a "/usr/lib/modules/$KVER" "$MOUNT/usr/lib/modules/"
cp "/usr/lib/modules/$KVER/vmlinuz" "$OUT/vmlinuz"
printf '%s\n' "$KVER" > "$OUT/kernel-version"
printf 'LABEL=omarchy-vm / ext4 defaults 0 1\n' > "$MOUNT/etc/fstab"

# The account, as `orchard install` sets it. VM-only credentials, kept beside the disk.
umask 077
[ -f "$OUT/credentials" ] || python3 -c 'import secrets; print(f"password={secrets.token_urlsafe(9)}\npin=123456")' > "$OUT/credentials"
umask 022
KEYS=$(awk '{print $1" "$2}' "$OUT"/ssh-keys 2>/dev/null || true)
PYTHONPATH="$ROOT/src" python3 - "$MOUNT" "$OUT/credentials" "$KEYS" <<'PY'
import sys
from pathlib import Path
from orchard import install_device as device
root, values = Path(sys.argv[1]), dict(line.split('=', 1) for line in Path(sys.argv[2]).read_text().split())
account = {'user': 'tablet', 'password': True, 'pin': True, 'passwordless_sudo': True,
           'ssh_keys': [k for k in sys.argv[3].splitlines() if k.strip()]}
device.set_account(root, account, values['password'], values['pin'])
PY

# The tablets' GPU desktop (Labwc on GLES2, the shell and apps on OpenGL) has
# no GPU here: the VM runs that same path on Mesa's software OpenGL. The
# session's own probe finds no GPU and picks the software desktop, as on a
# board without one; this flag, which the simulator's menu switches, sends it
# to the GPU desktop instead. GPU is the default.
# Packaged releases (0.3.2 on) install it in /usr/libexec, earlier ones in /usr/local/libexec.
session=$MOUNT/usr/libexec/arch-ipad-login-session
[[ -e $session ]] || session=$MOUNT/usr/local/libexec/arch-ipad-login-session
# The hook goes before Labwc starts: its `exec` in releases up to 0.3.x, the
# start-locked export (followed by the restart loop) since the session
# starts locked.
hook='^\(exec /usr/bin/labwc -m\|export OMARCHY_START_LOCKED=1\)$'
grep -q "$hook" "$session" || { echo "$session: no Labwc start to hook" >&2; exit 1; }
sed -i "\\%$hook%i # Orchard Simulator: the GPU desktop on Mesa software OpenGL (/etc/omarchy-a/simulator-gpu).\\nif [[ -e /etc/omarchy-a/simulator-gpu ]]; then export WLR_RENDERER=gles2 WLR_RENDERER_ALLOW_SOFTWARE=1; unset QT_QUICK_BACKEND; fi" "$session"
mkdir -p "$MOUNT/etc/omarchy-a"
touch "$MOUNT/etc/omarchy-a/simulator-gpu"

# A VM has no wake source for s2idle: a suspended guest never comes back.
for target in sleep suspend hibernate hybrid-sleep suspend-then-hibernate; do
    ln -sfn /dev/null "$MOUNT/etc/systemd/system/$target.target"
done

# The simulator's guest side: shutdown requests and the backlight report (vm-support/).
install -D -m 0755 "$ROOT/simulator/vm-support/orchard-vm-bridge.py" "$MOUNT/usr/local/libexec/orchard-vm-bridge"
cat > "$MOUNT/etc/systemd/system/orchard-vm-bridge.service" <<'UNIT'
[Unit]
Description=Orchard simulator virtual hardware bridge

[Service]
ExecStart=/usr/local/libexec/orchard-vm-bridge
Restart=always
RestartSec=1

[Install]
WantedBy=multi-user.target
UNIT
ln -sfn ../orchard-vm-bridge.service "$MOUNT/etc/systemd/system/multi-user.target.wants/orchard-vm-bridge.service"

sync
umount "$MOUNT"
rmdir "$MOUNT"
trap - EXIT INT TERM
chown -R "$(stat -c %u:%g "$ROOT")" "$OUT"
printf 'VM disk prepared: %s\n' "$DISK"
