"""dsh-cwl port package (G-A1, CHARGE-G1-BUILD-ENGINE-V2-2026-11-06).

Public surface is `lib_port` — the structural port of the GitHub reference
implementation dsh-cwl `lib.js`
(https://github.com/kalifun/dsh-cwl, frozen copy sha256_12 7318f403ded1 at
tests/g1/fixtures/reference_frozen/lib.js).
"""

from .lib_port import (
    BASH_WRITE_RE,
    EVICTION_PREFERENCE,
    EXPL_TOOLS,
    MAX_BATCHES,
    NEVER_EVICT_TYPES,
    ORDER_DEFAULT,
    PRESERVE_RECENT,
    SURFACE_THRESHOLD_CHARS,
    deriveEpisodes,
    episodeBlock,
    largeResultSeqs,
    mergeBlocksByPos,
    mergeRanges,
    newestEvictableBoundary,
    pairingBreaks,
    pickEvictionTarget,
    shadowedNodes,
    stubToolResultData,
    toolResultText,
)

__all__ = [
    "BASH_WRITE_RE",
    "EVICTION_PREFERENCE",
    "EXPL_TOOLS",
    "MAX_BATCHES",
    "NEVER_EVICT_TYPES",
    "ORDER_DEFAULT",
    "PRESERVE_RECENT",
    "SURFACE_THRESHOLD_CHARS",
    "deriveEpisodes",
    "episodeBlock",
    "largeResultSeqs",
    "mergeBlocksByPos",
    "mergeRanges",
    "newestEvictableBoundary",
    "pairingBreaks",
    "pickEvictionTarget",
    "shadowedNodes",
    "stubToolResultData",
    "toolResultText",
]
