#!/usr/bin/env python3
# SPDX-License-Identifier: GPL-2.0-only
"""Command entry point for the shared standard-library CB32 payload codec."""
from pathlib import Path
import sys

if not __package__:
    sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from orchard.cascadia_payload import main

if __name__ == '__main__':
    main()
