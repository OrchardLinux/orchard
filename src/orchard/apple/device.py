"""Generic Apple device adapter: identity from hardware's device.toml, shared transports.

Supporting a new device needs only its hardware descriptor and firmware
recipe, not CLI code.
"""
from contextlib import AbstractContextManager
from functools import partial
from pathlib import Path
from typing import Any, Dict, List, Optional, cast

from orchard.contracts import BootAssets, BootRequest, Connection, SelectedDevice
from orchard.state import BootState, DeviceState
from orchard.progress import Progress
from orchard.apple import entries, usb, boot as apple_boot, adt_capture
from orchard.apple.adt_tree import parse
from orchard.transports import ssh
from orchard import firmware, hardware
from .adt import validate as validate_adt


# The recovery's USB product; its serial string names the board (BOARD:j71b).
RECOVERY_PRODUCT = 'Orchard Recovery'


class Device:
    def __init__(self, descriptor: Dict[str, Any]):
        self._descriptor = descriptor
        identity = descriptor['device']
        self.PROFILE = identity['profile']
        self.NAME = identity['name']
        self.APPLE_PRODUCT = identity['apple_products'][0]
        # USB product of Orchard's recovery, the same on every device; absent until it has one.
        self.PRODUCT = RECOVERY_PRODUCT if hardware.has_recovery(descriptor) else None
        self.CHIP = descriptor['match']['dfu']['chip']
        self.BOARD = descriptor['match']['dfu']['board']
        dtb = descriptor.get('kernel', {}).get('dtb')
        self.DEVICETREE = dtb + '.dtb' if dtb else None
        support = descriptor.get('support', {})
        self.INSTALL_BLOCKERS = hardware.install_blockers(self.PROFILE, descriptor)
        self.INSTALL = not self.INSTALL_BLOCKERS
        self.STATUS = support.get('status', 'experimental')
        self.FEATURES = dict(descriptor.get('features', {}))
        if 'recovery-autogo' in descriptor.get('boot', {}).get('methods', []):
            self.REBOOT_MESSAGE = ('A fresh ARM32 recovery boot is required.\n'
                                   f'Enter a pwned iBSS or iBEC Recovery on the {self.NAME}, then run: orchard install')
        else:
            self.REBOOT_MESSAGE = ('A fresh recovery boot is required.\n'
                                   f'Put the {self.NAME} in DFU mode, then run: orchard install')
        self.firmware_inputs = partial(firmware.ensure, self.PROFILE)
        self.firmware_cached = partial(firmware.cached, self.PROFILE)
        self.prepare_mixin = partial(firmware.mixin, self.PROFILE)

    def matches_dfu(self, device: Dict[str, Any]) -> bool:
        return (device.get('chip'), device.get('board')) == (self.CHIP, self.BOARD)

    def recovery_endpoint(self) -> Dict[str, Any]:
        if self.PRODUCT is None:
            raise ValueError(f'The {self.NAME} has no Orchard recovery system yet')
        return usb.recovery(self.PRODUCT, self.PROFILE)

    def connect(self, device: SelectedDevice) -> AbstractContextManager[Connection]:
        from .cascadia import recovery_handoff
        handoff = recovery_handoff(self.PROFILE, {**device, 'profile': self.PROFILE})
        return ssh.session(DeviceState(self.PROFILE, device['ecid']), self.recovery_endpoint,
                           expected_boot=device.get('boot_id'), handoff=handoff)

    def capture_adt(self, device: Optional[SelectedDevice] = None) -> bytes:
        # Bring-up and diagnostics only; installs do not need the ADT.
        # DFU/Pongo and Linux location IDs differ: bind the fresh ADT to the ECID.
        data = adt_capture.capture(self.recovery_endpoint)
        if parse(data).properties.get('model', b'').rstrip(b'\0') != self.APPLE_PRODUCT.encode():
            raise ValueError('Captured ADT is not for ' + self.APPLE_PRODUCT)
        if device is not None and validate_adt(data) != device['ecid']:
            raise ValueError('Fresh ADT identity differs from the confirmed device')
        return data

    def list_devices(self) -> List[Dict[str, Any]]:
        return usb.devices()

    def discover(self) -> SelectedDevice:
        device = usb.sole_target()
        if device['mode'] == 'linux':
            endpoint = self.recovery_endpoint()
            BootState(DeviceState(self.PROFILE, endpoint['ecid']), endpoint['boot_id'])
            from .cascadia import recovery_handoff
            recovery_handoff(self.PROFILE, endpoint)
            return cast(SelectedDevice, {**endpoint, 'resumed': True})
        if not self.matches_dfu(device) or not device['ecid']:
            raise ValueError(f'The connected device is not a {self.NAME}')
        DeviceState(self.PROFILE, device['ecid'])
        return cast(SelectedDevice, {**device, 'resumed': False})

    def boot_inputs(self) -> List[Any]:
        return entries.inputs(entries.select(self._descriptor))

    def boot(self, device: SelectedDevice, assets: BootAssets,
             manifest: BootRequest, progress: Progress) -> None:
        declared = self._descriptor.get('boot', {}).get('methods', [])
        method = manifest.get('boot_method')
        if method is None:
            method = next((name for name in declared if name in hardware.IMPLEMENTED_METHODS), None)
        if method not in declared or method not in hardware.IMPLEMENTED_METHODS:
            raise ValueError('The requested boot method is unavailable for this board')
        if method == 'recovery-autogo':
            from . import cascadia
            return cascadia.boot(device, assets, manifest, progress, profile=self.PROFILE,
                                 chip=self.CHIP, board=self.BOARD, product=self.PRODUCT)
        return apple_boot.boot(device, assets, manifest, progress,
                               profile=self.PROFILE, chip=self.CHIP, board=self.BOARD, product=self.PRODUCT,
                               entry=entries.select(self._descriptor))

    def boot_payload(self, device: SelectedDevice, payload: Path, pongo_image: Path, progress: Progress) -> Dict[str, Any]:
        return apple_boot.boot_payload(device, payload, pongo_image, progress, profile=self.PROFILE, chip=self.CHIP,
                                       board=self.BOARD, product=self.PRODUCT, entry=entries.select(self._descriptor))

    def start_loader(self, device: SelectedDevice, loader: Dict[str, Path], progress: Progress,
                     quiet: bool = False) -> str:
        if not any(name == 'checkm8-pongo' for name in self._descriptor.get('boot', {}).get('methods', [])):
            raise ValueError('This platform starts an ARM32 staging bundle; a standalone m1n1 storage reader is unavailable')
        return apple_boot.start_loader(device, loader, progress, profile=self.PROFILE,
                                       chip=self.CHIP, board=self.BOARD, product=self.PRODUCT, quiet=quiet,
                                       entry=entries.select(self._descriptor))
