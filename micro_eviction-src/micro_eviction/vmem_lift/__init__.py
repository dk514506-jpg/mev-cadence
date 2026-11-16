"""mev.vmem_lift — component lift of vMem's eviction-policy core.

Pure functions over mev data shapes. No I/O, no vMem imports at runtime.
See ``policy.py`` for the attribution notice (MIT, upstream commit 1d57f12).
"""
from __future__ import annotations

from . import policy

__all__ = ["policy"]
