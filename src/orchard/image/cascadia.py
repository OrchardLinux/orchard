#!/usr/bin/env python3
# SPDX-License-Identifier: GPL-2.0-only
"""producers/cascadia.py under its old path and import name.

The producer is a standalone script outside the package. This file runs its
text as this module, so imports, `python -m` and running this file by path
behave as they did before the move.
"""
from pathlib import Path

_producer = Path(__file__).resolve().parents[3] / 'producers/cascadia.py'
exec(compile(_producer.read_bytes(), str(_producer), 'exec'))
