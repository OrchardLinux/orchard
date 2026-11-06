"""Typed host adapter boundary, not a device/plugin or boot-method framework.

Adapters remain ordinary modules. These records describe the current supported
USB/checkm8 workflow; common orchestration does not interpret chip/board values.
Runtime artifact/identity verification remains in the existing implementations.
"""
from contextlib import AbstractContextManager
from dataclasses import dataclass
from pathlib import Path
import re
from typing import Any, Callable, Dict, List, Literal, Mapping, Optional, Protocol, Tuple, TypedDict, Union, runtime_checkable

from .progress import Clock, Progress
from .state import BootState

USBLocation = Union[int, str]


class _SelectionDetails(TypedDict, total=False):
    chip: Optional[int]
    board: Optional[int]
    product: Optional[str]
    serial_port: Optional[str]
    pwned: bool
    boot_id: Optional[str]
    handoff_id: Optional[str]


class SelectedDevice(_SelectionDetails):
    """Validated discovery result; resumed is explicit, never inferred by the host."""
    location: USBLocation
    mode: Literal['dfu', 'recovery', 'pongo', 'linux']
    ecid: int
    resumed: bool


class BootAssets(TypedDict, total=False):
    """Verified local artifacts; Pongo is used by the checkm8-pongo chain.

    A running recovery needs no upload; otherwise recovery must be supplied.
    prepared_boot names private checked iBEC inputs for recovery-autogo.
    root is used by installation, not by the adapter's boot transport.
    """
    pongo: Path
    recovery: Path
    root: Path
    prepared_boot: Path


class BootReceipt(TypedDict):
    sha256: str


class _BootRequestDetails(TypedDict, total=False):
    boot_method: str


class BootRequest(_BootRequestDetails):
    """Minimum adapter boot input, shared by resolved and explicit-file requests."""
    assets: Mapping[str, BootReceipt]


class MixinReceipt(TypedDict):
    schema: Literal[1]
    kind: Literal['private-mixin']
    bytes: int
    sha256: str


@dataclass(frozen=True)
class HostHandoff:
    """A host-observed Apple identity carried by one ARM32 RAM upload."""
    handoff_id: str
    template_sha256: str
    chip: int
    board: int

    def __post_init__(self) -> None:
        if (not isinstance(self.handoff_id, str) or
                not re.fullmatch('[0-9a-f]{32}', self.handoff_id) or
                not int(self.handoff_id, 16) or
                not isinstance(self.template_sha256, str) or
                not re.fullmatch('[0-9a-f]{64}', self.template_sha256) or
                type(self.chip) is not int or not 0 < self.chip < 2**32 or
                type(self.board) is not int or not 0 <= self.board < 2**32):
            raise ValueError('Invalid host-bound recovery identity')

    def fields(self) -> Dict[str, Any]:
        return {'identity_source': 'host-usb', 'handoff_id': self.handoff_id,
                'template_sha256': self.template_sha256, 'chip': self.chip, 'board': self.board}

    def matches(self, observation: Any) -> bool:
        return (isinstance(observation, dict) and
                type(observation.get('chip')) is int and type(observation.get('board')) is int and
                all(observation.get(key) == value for key, value in self.fields().items()))


@dataclass(frozen=True)
class Connection:
    argv: List[str]
    scope: BootState
    check: Callable[[], None]
    handoff: Optional[HostHandoff] = None


@runtime_checkable
class Adapter(Protocol):
    """Required interface of a registered adapter module.

    Discovery is read-only and raises ValueError when no unique supported device
    is present. Boot must never retry uncertain mutations. connect(device) binds
    the selected ECID/boot.
    Exceptions propagate: the host owns confirmation/reporting, not recovery
    recipes. There are no optional members or legacy fallbacks.
    """
    PROFILE: str
    NAME: str
    REBOOT_MESSAGE: str
    # USB product of the device's recovery system; None until it has one.
    PRODUCT: Optional[str]
    # Support data from the hardware descriptor ([support], [features]).
    INSTALL: bool
    INSTALL_BLOCKERS: List[str]
    STATUS: str
    FEATURES: Dict[str, str]
    # Board DTB file name a boot entry must carry (from [kernel] dtb).
    DEVICETREE: Optional[str]

    def matches_dfu(self, device: Dict[str, Any]) -> bool: ...

    def list_devices(self) -> List[Dict[str, Any]]: ...

    def discover(self) -> SelectedDevice: ...

    def boot_inputs(self) -> List[Any]: ...

    def boot(self, device: SelectedDevice, assets: BootAssets,
             manifest: BootRequest, progress: Progress) -> None: ...

    def boot_payload(self, device: SelectedDevice, payload: Path, pongo_image: Path, progress: Progress) -> Dict[str, Any]: ...

    def start_loader(self, device: SelectedDevice, loader: Dict[str, Path], progress: Progress) -> str: ...

    def connect(self, device: SelectedDevice) -> AbstractContextManager[Connection]: ...

    def capture_adt(self, device: Optional[SelectedDevice] = None) -> bytes: ...

    def firmware_cached(self) -> bool: ...

    def firmware_inputs(self, clock: Clock, progress: Progress, *, allow_download: bool = True) -> Path: ...

    def prepare_mixin(self, firmware_dir: Path, outdir: Path) -> Tuple[Path, MixinReceipt]: ...
