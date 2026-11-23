#!/usr/bin/env bash
# verify-payload.sh [OUT_DIR]: orchard's own pre-boot check (orchard.ram.verify_ram), without a device.
# It is what `orchard run --no-build` checks before booting: every file's digest and every recorded
# source path. A payload built in a scratch copy records the scratch paths; after the scratch dir is
# deleted the boot is refused ("<scratch>/rootfs changed; rebuild before booting"). This also refuses
# any source that is not under examples/ipod-gpu-drm.
set -euo pipefail
here=$(cd "$(dirname "$0")/.." && pwd)
orchard=$(cd "$here/../.." && pwd)
out=${1:-$here/out/apple-4k}
cd "$orchard"
PYTHONPATH=src "${PYTHON:-python3}" - "$out" "$here" <<'EOF'
import sys
from orchard.ram import verify_ram
out, here = sys.argv[1], sys.argv[2]
r = verify_ram(out)
bad = [p for p in r.get('sources', {}) if '/examples/' in p and not p.startswith(here + '/')]
if bad:
    sys.exit('verify-payload: sources outside %s: %s' % (here, bad[:3]))
print('verify-payload: OK (%d files, %d sources, all under %s)' % (len(r['files']), len(r.get('sources', {})), here))
EOF
