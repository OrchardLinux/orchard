#!/usr/bin/env python3
"""producers/platform.py under its old path and import name.

The producer is a standalone script outside the package and takes the kernel
bundle and BusyBox as given. This file runs its text as this module and adds
what the tool can do besides: build them or take the published ones when
none is given.
"""
from pathlib import Path

_producer = Path(__file__).resolve().parents[3] / 'producers/platform.py'
_text, _run, _ = _producer.read_text().rpartition("\n\nif __name__ == '__main__':\n    main()\n")
exec(compile(_text, str(_producer), 'exec'))


def kernel(platform, *, hardware=None, cache=None):
    """The staged kernel bundle of PLATFORM: the nightly's when it was built from
    this checkout's kernel state (orchard.published), else built or reused from
    the cache. ORCHARD_KERNEL=local always compiles."""
    from ..build import cache_root, prepare_kernel
    from ..hardware import ROOT
    from .. import published
    found = published.kernel(Path(hardware or ROOT), Path(cache) if cache else cache_root(), platform)
    if found is not None:
        return found
    return prepare_kernel(hardware, cache, device=sorted(boards(platform, hardware))[0])


def tools(hardware=None, cache=None, platform='apple-16k'):
    """orchard's static BusyBox, built or reused from the cache."""
    from ..build import prepare_tools
    return prepare_tools(hardware, cache, device=sorted(boards(platform, hardware))[0]) / 'busybox'


if __name__ == '__main__':
    main()
