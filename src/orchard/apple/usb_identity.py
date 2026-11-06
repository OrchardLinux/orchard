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
