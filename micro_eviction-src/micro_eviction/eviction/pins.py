"""Bounded pin layer (G-E, CHARGE-G1-BUILD-ENGINE-V2 §2) — C8 never-evictable
blocks with a BOUNDED pin set and surfaced overflow, plus the C9 operator-side
recall query over staged packages.

C8 boundary (pin layer): the pin set INCLUDES user turns and goal statements
(§R1 item 6) — never-evictable blocks, built on the lib_port
`NEVER_EVICT_TYPES` class (`frozenset({"user", "goal", "prologue"})`).  The
pin set is BOUNDED: `PIN_CEILING_DEFAULT` is the frozen ceiling; when pins
exceed the ceiling, the overflow condition is SURFACED as a structured record
(returned by the pin-set builder, appendable via staging_registry.alarm
semantics) — never a silent drop.  The kept set never exceeds the ceiling at
any time.

C9 boundary (recall): an OPERATOR-SIDE recall query over staged packages —
a human-invoked CLI/library query.  NO host tool slot, NO agent self-service:
the query function is exposed, but it is never registered as an agent-facing
tool and this module adds no tool-registration surface.  Evicted content is
recoverable-by-human only.

Alarm semantics (§7 / AMB-1 adjudication): the overflow record carries the
alarm envelope `{ts, kind, episode, reason}` with kind "pin_overflow" and
reason "pin set exceeds ceiling", plus the AMB-1 payload
`{pin_ceiling, pin_count}` and the overflowed pin records themselves.

Source of record: the GitHub reference implementation dsh-cwl
(https://github.com/kalifun/dsh-cwl).  Cite the GitHub reference, never the
arXiv id (C14).
"""

import time

from ..dsh_cwl.lib_port import NEVER_EVICT_TYPES
from . import staging_registry

# --- frozen policy constants ---------------------------------------------

# C8 boundedness: frozen ceiling of the pin set.  Matches the frozen
# Stage-0 fixture params (ctx_pin_overflow params.pin_ceiling).
PIN_CEILING_DEFAULT = 8

# Overflow signal vocabulary (§7 signal definitions / AMB-1 alarm shape).
PIN_OVERFLOW_KIND = "pin_overflow"
PIN_OVERFLOW_CONDITION = "pin set exceeds ceiling"

# Derived pin reasons (vocabulary of the frozen Stage-0 fixtures).
REASON_GOAL = "goal-statement"
REASON_USER = "user-turn"

# Goal statements carry the "GOAL:" text prefix (frozen fixture convention).
GOAL_TEXT_PREFIX = "GOAL:"


# --- pin derivation (§R1 item 6, built on NEVER_EVICT_TYPES) --------------


def _block_class(event):
    """NEVER_EVICT_TYPES class of an event block.

    Returns "goal" for goal statements, "user" for user turns, None for any
    block that is not in the never-evict class.
    """
    if not isinstance(event, dict) or event.get("type") != "user/message":
        return None
    data = event.get("data") or {}
    message = data.get("message") or {}
    content = message.get("content") or []
    text = ""
    for block in content:
        if isinstance(block, dict) and block.get("type") == "text":
            text += block.get("text") or ""
    text = text.strip()
    if text.startswith(GOAL_TEXT_PREFIX):
        return "goal"
    return "user"


def derive_pins(events):
    """Derive the mandatory pin records of an event stream.

    §R1 item 6: user turns and goal statements are never-evictable blocks;
    every block whose class is a member of NEVER_EVICT_TYPES is pinned.
    Records are the frozen fixture shape {"seq", "reason"} in seq order.
    """
    out = []
    for ev in sorted(events or [], key=lambda e: e.get("seq", 0)):
        cls = _block_class(ev)
        if cls in NEVER_EVICT_TYPES:
            reason = REASON_GOAL if cls == "goal" else REASON_USER
            out.append({"seq": ev.get("seq"), "reason": reason})
    return out


# --- bounded pin set (C8) ------------------------------------------------


def add_pin(pins, pin, ceiling=PIN_CEILING_DEFAULT):
    """Add one pin record to the bounded pin set.

    Returns (pins, dropped).  `pins` is a NEW list (the input is never
    mutated) holding at most `ceiling` records, so boundedness holds at all
    times.  When the pin cannot be kept (ceiling reached) the record is
    returned as `dropped` — surfaced to the caller, never a silent drop.
    A seq that is already pinned stays pinned and is never dropped.
    """
    if not isinstance(pin, dict) or "seq" not in pin:
        raise TypeError("pin must be a dict with a 'seq' field")
    kept = list(pins)
    if any(p.get("seq") == pin.get("seq") for p in kept):
        return kept, None
    if len(kept) >= ceiling:
        return kept, dict(pin)
    kept.append(dict(pin))
    return kept, None


def _overflow_record(dropped, kept_count, ceiling):
    """The surfaced C8 overflow record.

    Structured and staging_registry.alarm-appendable: the alarm envelope
    {ts, kind, episode, reason} plus the AMB-1 payload {pin_ceiling,
    pin_count} and every overflowed pin record (never a silent drop).
    """
    return {
        "ts": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "kind": PIN_OVERFLOW_KIND,
        "episode": "",
        "reason": PIN_OVERFLOW_CONDITION,
        "payload": {"pin_ceiling": ceiling, "pin_count": kept_count + len(dropped)},
        "overflow_pins": list(dropped),
    }


def build_pin_set(events=None, seed=(), ceiling=PIN_CEILING_DEFAULT):
    """Build the bounded pin set: derive_pins(events) first (the mandatory
    never-evictable blocks), then the seeded pin records.

    Returns (pins, overflow_record).  `pins` holds at most `ceiling` records.
    `overflow_record` is the surfaced structured overflow record (see
    `_overflow_record`) when the ceiling was exceeded, else None.
    """
    pins = []
    dropped = []
    for pin in derive_pins(events):
        pins, d = add_pin(pins, pin, ceiling=ceiling)
        if d is not None:
            dropped.append(d)
    for pin in seed or ():
        pins, d = add_pin(pins, pin, ceiling=ceiling)
        if d is not None:
            dropped.append(d)
    record = _overflow_record(dropped, len(pins), ceiling) if dropped else None
    return pins, record


def protected_seqs(pins):
    """Engine-visible protected set (C8 / §R1 item 6): the seqs of the
    never-evictable pin set."""
    return frozenset(p.get("seq") for p in (pins or []))


def append_overflow(record, alarms_dir=None):
    """Append the overflow record's alarm sliver ({ts, kind, episode, reason}
    per §7) via staging_registry.alarm.  Returns the appended record."""
    return staging_registry.alarm(record, alarms_dir=alarms_dir)


# --- C9 operator-side recall ---------------------------------------------


def recall(package_ids=None):
    """C9 OPERATOR-SIDE recall query over staged packages.

    Human-invoked CLI/library query ONLY: evicted content is
    recoverable-by-human.  NOT agent self-service — this function is exposed
    as a plain library call, is never registered as an agent-facing tool, and
    this module adds no tool-registration surface.

    recall()                -> tuple of every staged package id
    recall("pkg-1")         -> ("pkg-1",) if staged else ()
    recall(["pkg-1", ...])  -> the staged subset, in query order
    """
    if package_ids is None:
        # Read-only enumeration of staging_registry's in-process registry
        # (it exposes no list(); the module is not modified here).
        return tuple(sorted(getattr(staging_registry, "_registry", {})))
    if isinstance(package_ids, str):
        package_ids = [package_ids]
    return tuple(pid for pid in package_ids if staging_registry.exists(pid))
