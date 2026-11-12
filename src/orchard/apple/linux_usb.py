"""Discover Apple USB devices from Linux sysfs without a USB Python dependency."""
from pathlib import Path
import re
import uuid

# Orchard's one table of the device's USB identities, used for discovery on
# Linux and macOS (usb.py) and for the Linux access rules (host_setup.py).
# Apple's vendor ID with the product ID of each stage: 0x1227 DFU, 0x4141
# Pongo; Orchard's own USB gadget then uses 0x4142 for recovery, 0x4144 for
# a RAM payload's session, 0x4145 for an installed system and 0x4146 for the
# initrd with the install add-on (install_direct.py).
APPLE = 0x05ac
MODES = {0x1227: 'dfu', 0x4141: 'pongo', 0x4142: 'linux', 0x4144: 'ram', 0x4145: 'system', 0x4146: 'install'}
# The stages served by Orchard's gadget, which also has a serial console.
SERIAL_MODES = ('linux', 'ram', 'system', 'install')
# m1n1's USB proxy (vendor and product ID).
M1N1 = (0x1209, 0x316d)


def serial_tags(serial):
    """What an Apple USB serial string says beyond ECID, CPID and BDID.

    serial_number: the device's serial number (DFU's SRNM:[...]); pwned: the
    exploit that left its PWND:[...] tag (checkm8, ...), or checkra1n for its
    YOLO:checkra1n tag; None when absent.
    """
    number = re.search(r'\bSRNM:\[([A-Za-z0-9]{1,32})\]', serial)
    pwned = re.search(r'\bPWND:\[([^\]]{1,64})\]', serial)
    return {'serial_number': number.group(1) if number else None,
            'pwned': pwned.group(1) if pwned else ('checkra1n' if 'YOLO:checkra1n' in serial else None)}


def stable_id(serial):
    """Hashed per-device identity, absent on legacy gadgets."""
    match = re.search(r'\bID:([0-9a-fA-F]{12})\b', serial)
    return match.group(1).lower() if match else None


def devices(root=Path('/sys/bus/usb/devices')):
    found = []
    for node in root.iterdir():
        try:
            vid = int((node / 'idVendor').read_text().strip(), 16)
            pid = int((node / 'idProduct').read_text().strip(), 16)
            serial = (node / 'serial').read_text().strip() if (node / 'serial').exists() else ''
        except (OSError, ValueError):
            continue
        if vid != 0x05ac or not re.fullmatch(r'[0-9]+-[0-9]+(?:\.[0-9]+)*', node.name):
            continue
        boot = re.search(r'\bBOOT:([0-9a-fA-F-]{36})\b', serial)
        ecid = re.search(r'\bECID:([0-9a-fA-F]+)\b', serial)
        chip = re.search(r'\bCPID:([0-9a-fA-F]+)\b', serial)
        board = re.search(r'\bBDID:([0-9a-fA-F]+)\b', serial)
        usb_id = stable_id(serial)
        profile = re.search(r'\bBOARD:([a-z0-9]+)\b', serial)
        ports = {str(Path('/dev') / p.name) for p in node.glob('**/tty/ttyACM*')}
        if len(ports) > 1:
            raise ValueError('Ambiguous USB serial ports on a device')
        product = (node / 'product').read_text().strip() if (node / 'product').exists() else ''
        found.append(dict(location=node.name, mode=MODES.get(pid, 'other'),
                          ecid=int(ecid.group(1), 16) if ecid else None,
                          boot_id=str(uuid.UUID(boot.group(1))) if boot else None,
                          usb_id=usb_id,
                          chip=int(chip.group(1), 16) if chip else None,
                          board=int(board.group(1), 16) if board else None,
                          profile=profile.group(1) if profile else None, product=product,
                          serial_port=next(iter(ports), None), **serial_tags(serial)))
    return found


def m1n1_ports(root=Path('/sys/bus/usb/devices')):
    """Proxy (first CDC-ACM interface) tty of every m1n1 USB device."""
    found = []
    for node in root.iterdir():
        try:
            if (int((node / 'idVendor').read_text().strip(), 16), int((node / 'idProduct').read_text().strip(), 16)) \
                    != M1N1:
                continue
        except (OSError, ValueError):
            continue
        ports = sorted(str(Path('/dev') / p.name) for p in node.glob(node.name + ':*.0/tty/ttyACM*'))
        if ports:
            found.append(ports[0])
    return found
