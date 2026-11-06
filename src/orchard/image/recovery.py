#!/usr/bin/env python3
"""Assemble the RAM recovery image from explicit kernel, loader and tool inputs."""
import argparse
from pathlib import Path
import subprocess
import sys
import tempfile

from ..hardware import ROOT
HERE = ROOT / 'boot/initramfs/recovery'


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    for name in ('loader', 'kernel', 'busybox', 'dropbear', 'dropbearkey',
                 'wpa-supplicant', 'wpa-cli', 'iw',
                 'base', 'base-receipt', 'modules-dir', 'root-receipt', 'output'):
        parser.add_argument('--' + name, type=Path, required=True)
    parser.add_argument('--dtb', type=Path, action='append', required=True,
                        help='the DTB of each device this recovery boots; repeat')
    parser.add_argument('--logo', type=Path, help="the distribution's m1n1 logo payload (see assemble.py)")
    parser.add_argument('--platform', default='apple-16k', help='kernel platform and payload format')
    parser.add_argument('--device', help='the board whose Cascadia handoff profile to use')
    diagnostics = parser.add_mutually_exclusive_group()
    diagnostics.add_argument('--verbose', action='store_true',
                             help='enable visible Linux framebuffer console and kernel diagnostics')
    args = parser.parse_args()
    from ..kernel import descriptor
    from .platform import boards
    described = descriptor(ROOT, args.platform)
    profile = None
    if described['loader'] == 'cascadia':
        if len(args.dtb) != 1 or args.logo is not None:
            parser.error('Cascadia recovery takes one board DTB and no m1n1 logo')
        available = boards(described['platform'])
        selected = args.device
        if selected is None:
            candidates = [board for board, dtb in available.items() if args.dtb[0].stem == dtb]
            if len(candidates) != 1:
                parser.error('Select the Cascadia board with --device')
            selected = candidates[0]
        if selected not in available or args.dtb[0].stem != available[selected]:
            parser.error('The Cascadia board and supplied DTB differ')
        from ..hardware import payload_profile, device as described_device
        from .cascadia import check_compatible
        try:
            profile = payload_profile(selected, 'cascadia', ROOT)
            check_compatible(args.dtb[0].read_bytes(), described_device(selected, ROOT)['device']['dt_compatible'])
        except ValueError as error:
            parser.error(str(error))
    manifest = 'boot-verbose.json' if args.verbose else 'boot.json'
    if args.output.exists():
        parser.error('choose a new output path')
    args.output.parent.mkdir(parents=True, exist_ok=True)
    work = args.output.parent / '.work'
    work.mkdir(exist_ok=True)
    with tempfile.TemporaryDirectory(prefix='recovery-stage-', dir=work) as temporary:
        stage = Path(temporary)
        initramfs = stage / 'initramfs.gz'
        subprocess.run([sys.executable, '-m', 'orchard.image.prepare_runtime',
                        '--busybox', str(args.busybox), '--dropbear', str(args.dropbear),
                        '--dropbearkey', str(args.dropbearkey),
                        '--wpa-supplicant', str(args.wpa_supplicant),
                        '--wpa-cli', str(args.wpa_cli), '--iw', str(args.iw),
                        '--base', str(args.base), '--base-receipt', str(args.base_receipt),
                        '--modules-dir', str(args.modules_dir), '--root-receipt', str(args.root_receipt),
                        '--architecture', described['arch'],
                        '--output', str(stage / 'root')], check=True)
        subprocess.run([sys.executable, '-m', 'orchard.image.initramfs',
                        '--root', str(stage / 'root'),
                        '--output', str(initramfs)], check=True)
        assembly = 'orchard.image.cascadia' if profile is not None else 'orchard.image.assemble'
        subprocess.run([sys.executable, '-m', assembly,
                        *(['--profile', str(profile)] if profile is not None else []),
                        '--manifest', str(HERE / manifest),
                        '--loader', str(args.loader), *(f'--dtb={dtb}' for dtb in args.dtb),
                        *(['--logo', str(args.logo)] if args.logo else []),
                        '--kernel', str(args.kernel), '--initramfs', str(initramfs),
                        '--output', str(args.output)], check=True)


if __name__ == '__main__':
    main()
