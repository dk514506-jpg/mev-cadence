"""C13 eviction-run bookkeeping (G-E, CHARGE-G1-BUILD-ENGINE-V2 §2) — the
in_context id deltas (added/removed/retained) and the narrative-tau budget
used, bound to letta_bookkeeping's id-list accounting.

Binding surface: `id_deltas` computes the run's in_context id deltas through
`micro_eviction.letta_bookkeeping`'s id-list accounting (`messagesMissingFrom`,
`removeFromInContext`) — the port the compaction flow performs without the
summarizer.  `record_eviction_run` records, for one eviction run, those deltas
plus the narrative-tau budget used, and emits the package/report rows.

C12 (rename, binding): the budget token is spelled `narrative-tau` (ASCII,
hyphen) in every key and string this module emits.  The Python identifier
remains `narrative_tau` (identifiers cannot carry hyphens).

C10 (fidelity, binding): every emitted package/report row carries
`fidelity: LOSSY` plus a lossiness note; the raw message and CoT fields are
NAMED (`message_raw`, `cot_raw`) but not populated.  `validate_row` is the
bookkeeping-row validator and refuses any row that violates this.
"""

from ..letta_bookkeeping import messagesMissingFrom, removeFromInContext

# --- C10 fidelity vocabulary ---------------------------------------------

FIDELITY_LOSSY = "LOSSY"
MESSAGE_RAW_FIELD = "message_raw"
COT_RAW_FIELD = "cot_raw"

LOSSINESS_NOTE = (
    "bookkeeping row is LOSSY: only the id deltas and the narrative-tau "
    "budget are carried; message_raw and cot_raw are named but not populated"
)


class BookkeepingRowError(ValueError):
    """A bookkeeping row failed C10 validation."""


# --- id-list accounting bound to letta_bookkeeping ------------------------


def id_deltas(before_ids, after_ids):
    """in_context id deltas (added/removed/retained) across one eviction run.

    Bound to letta_bookkeeping's id-list accounting: `removed` are the ids
    the new window no longer holds (messagesMissingFrom over the before
    window), `retained` is the before list minus each removed id
    (removeFromInContext), and `added` is the symmetric messagesMissingFrom
    over the new window.
    """
    before = [{"id": i} for i in list(before_ids or [])]
    after = [{"id": i} for i in list(after_ids or [])]
    removed = [m.get("id") for m in messagesMissingFrom(before, after)]
    retained = [m.get("id") for m in before]
    for rid in removed:
        retained = removeFromInContext(retained, rid)
    added = [m.get("id") for m in messagesMissingFrom(after, before)]
    return {
        "added": added,
        "removed": removed,
        "retained": retained,
    }


# --- C10 row validator ---------------------------------------------------


def validate_row(row):
    """Bookkeeping-row validator (C10).

    Raises BookkeepingRowError unless the row carries `fidelity: LOSSY`, a
    non-empty lossiness note, and the NAMED raw fields (`message_raw`,
    `cot_raw`) left unpopulated.  Returns the row on success.
    """
    if not isinstance(row, dict):
        raise BookkeepingRowError("bookkeeping row must be a dict")
    if row.get("fidelity") != FIDELITY_LOSSY:
        raise BookkeepingRowError("bookkeeping row missing fidelity: LOSSY")
    note = row.get("lossiness_note")
    if not isinstance(note, str) or not note:
        raise BookkeepingRowError("bookkeeping row missing lossiness note")
    for fld in (MESSAGE_RAW_FIELD, COT_RAW_FIELD):
        if fld not in row:
            raise BookkeepingRowError(
                "bookkeeping row missing named raw field %s" % fld
            )
        if row.get(fld):
            raise BookkeepingRowError(
                "bookkeeping row populates raw field %s (must stay unpopulated)"
                % fld
            )
    return row


# --- C13 run bookkeeping -------------------------------------------------


def record_eviction_run(before_ids, after_ids, narrative_tau, package_ids=()):
    """C13 bookkeeping for one eviction run: the in_context id deltas
    (added/removed/retained) and the narrative-tau budget used.

    Returns the emitted package/report rows — one row per staged package id
    (one run-level row when the run staged none).  Every row carries
    `fidelity: LOSSY` plus a lossiness note (C10); `message_raw` and
    `cot_raw` are named but not populated.  Every row is validated before
    emission.
    """
    deltas = id_deltas(before_ids, after_ids)
    rows = []
    for pid in list(package_ids) or [None]:
        row = {
            "package_id": pid,
            "in_context_id_deltas": {
                "added": list(deltas["added"]),
                "removed": list(deltas["removed"]),
                "retained": list(deltas["retained"]),
            },
            "narrative-tau": narrative_tau,
            "fidelity": FIDELITY_LOSSY,
            "lossiness_note": LOSSINESS_NOTE,
            MESSAGE_RAW_FIELD: None,
            COT_RAW_FIELD: None,
        }
        validate_row(row)
        rows.append(row)
    return rows
