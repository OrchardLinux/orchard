"""Identity of the source-built ARMv7 handoff utility.

Its standalone producer is ``boot/ios-launcher/kloader/build.py``. These
constants bind launcher preparation to that producer's pinned source.
"""

SOURCE_COMMIT = "a1bbca8a8f2847d9c53070e76e0b5d093bda5993"
SOURCE_SHA256 = "c64f3252118d65b0ab52f10c8eaadc1dd1d7a989352a6e497075f15fb174296a"
HARDENING_PATCH_SHA256 = "efd2b63a85a448701a33c80f2e42323f8e4560d685c5342553f36225595d4a87"
DERIVED_SOURCE_SHA256 = "ddcea7cd58afb437cbe7ac65d09e14f6f7ba98b04814d5e088bf4404c23360fa"
SLEEP_WAIT_SECONDS = 90
