#!/usr/bin/env python3
"""producers/platform_layer.py under its old path and import name.

The producer is a standalone script outside the package. This file runs its
text as this module, so imports, `python -m` and running this file by path
behave as they did before the move.
"""
from pathlib import Path

_producer = Path(__file__).resolve().parents[3] / 'producers/platform_layer.py'
exec(compile(_producer.read_bytes(), str(_producer), 'exec'))
