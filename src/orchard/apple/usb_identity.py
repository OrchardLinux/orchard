"""Validate the optional attempt identity carried by Orchard USB descriptors."""
import re


def handoff_id(serial: str) -> str | None:
    """Read one complete HND field; legacy descriptors omit the field."""
    fields = re.findall(r'\bHND\b', serial)
    if not fields:
        return None
    value = re.search(r'(?<!\S)HND:([0-9a-f]{32})(?!\S)', serial)
    if len(fields) != 1 or value is None:
        raise ValueError('Malformed or duplicate USB handoff identity')
    return value.group(1)


def identity_source(serial: str) -> str | None:
    """Keep older host handoffs readable; iOS handoffs always name their origin."""
    nonce = handoff_id(serial)
    fields = re.findall(r'\bORG\b', serial)
    if not fields:
        return 'host-usb' if nonce is not None else None
    value = re.search(r'(?<!\S)ORG:([hi])(?!\S)', serial)
    if len(fields) != 1 or value is None or nonce is None:
        raise ValueError('Malformed or duplicate USB identity origin')
    return {'h': 'host-usb', 'i': 'ios-device'}[value.group(1)]
