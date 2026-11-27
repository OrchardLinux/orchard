"""The install system's files (boot/initramfs/install), for tests: the device's install script as a module."""
from pathlib import Path
import sys

INSTALL = Path(__file__).resolve().parents[1] / 'boot/initramfs/install'
sys.path.insert(0, str(INSTALL))
import install_device  # noqa: E402,F401
