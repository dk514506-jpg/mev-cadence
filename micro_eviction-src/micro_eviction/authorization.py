"""mev.authorization — standing go-live grant (RULING-2026-11-06, S-3).

The runtime authorization-to-fire check is DISCARDED. The grant is a standing
granted state resolved ONCE at import and never re-derived at runtime; no
per-fire charge parse exists. Deployment posture is a separate explicit
switch — env ``MEV_DEPLOYMENT_POSTURE = sandbox|live``, read ONCE at load,
DEFAULT ``sandbox`` (a live default would write production on every cadence).
The posture switch does NOT gate the sink (separate-concern clause).
"""
from __future__ import annotations

import os

DALLAS_VERBATIM = (
    "the idea for authorization to fire at runtime is fucking stupid and "
    "should be discarded, my authorization has already been given so it "
    "need not be granted at every runtime.")

STANDING_GRANT = {"grant": DALLAS_VERBATIM, "sandbox_only": False}

# Deployment posture, resolved ONCE at load. Only "live" enables writes past
# the sandbox; every other value (incl. the default) stays sandbox-contained.
POSTURE_SWITCH = os.environ.get("MEV_DEPLOYMENT_POSTURE", "sandbox")


def standing_stamp() -> dict:
    """The post-replacement firing-report stamp (S-3 schema).

    ``sandbox_only`` here is the POSTURE field (ND-4: True under the default
    posture, False only when ``posture_switch`` is "live"). The constant's own
    ``sandbox_only: False`` is the granted-text companion — the grant text is
    standing (no runtime re-derivation) — NOT a claim that production writes
    are enabled."""
    return {
        "grant": STANDING_GRANT["grant"],
        "sandbox_only": POSTURE_SWITCH != "live",
        "source": "standing-load",
        "posture_switch": POSTURE_SWITCH,
    }
