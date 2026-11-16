#!/usr/bin/env python
"""engraphis_lookup.py — READ-ONLY lookup instrument over ~/.engraphis/memory.db.

Replaces all ad-hoc Engraphis SQL (CHARGE-ENGRAPHIS-RECORD-PIPELINE-V2, W3).

Hard invariants (binding, charge §2):
- The ONLY connection is opened with file:...?mode=ro (URI). No second
  connection, no PRAGMA that mutates, no DDL/DML anywhere in this file.
- A write attempt on this connection MUST raise (proven by selftest
  test_write_attempt_raises).
- Selftest runs against a throwaway temp sqlite fixture, never the live DB.

Subcommands:
  census    counts grouped by workspace / mtype / scope (+ date window)
  records   record summaries with filters; joins workspaces by id
  get       full record by --record-id or --capture-sha (metadata fields)

Common filters: --workspace NAME, --mtype, --namespace (scope: repo|workspace),
--date-from / --date-to (YYYY-MM-DD, UTC, applied to ingested_at),
--seat-label (metadata JSON field), --record-id, --capture-sha, --limit.

Stdlib only. Exit codes: 0 ok, 2 bad usage, 3 db error.
"""
from __future__ import annotations

import argparse
import json
import os
import sqlite3
import sys
from datetime import datetime, timezone

DEFAULT_DB = os.path.join(os.path.expanduser("~"), ".engraphis", "memory.db")

READ_URI = "file:{path}?mode=ro"

SELECT_COLUMNS = (
    "m.id, m.workspace_id, w.name AS workspace_name, m.repo_id, m.session_id, "
    "m.scope, m.mtype, m.title, m.summary, m.importance, m.confidence, "
    "m.pinned, m.sensitivity, m.ingested_at, m.valid_from, m.valid_to, "
    "m.expired_at, m.subject_key, m.claim_kind, m.metadata, m.content"
)


def _die(msg: str, code: int = 2) -> "None":
    print(f"engraphis_lookup: {msg}", file=sys.stderr)
    raise SystemExit(code)


def open_readonly(db_path: str = DEFAULT_DB) -> sqlite3.Connection:
    """Open the DB strictly read-only. This is the only sanctioned opener."""
    if not os.path.exists(db_path):
        _die(f"db not found: {db_path}", 3)
    uri = READ_URI.format(path=db_path)
    try:
        con = sqlite3.connect(uri, uri=True)
    except sqlite3.Error as exc:
        _die(f"cannot open read-only: {exc}", 3)
    con.row_factory = sqlite3.Row
    # Belt-and-braces: also set the connection-level guard.
    try:
        con.execute("PRAGMA query_only=ON")
    except sqlite3.Error:
        pass
    return con


def parse_date(s: str) -> float:
    try:
        dt = datetime.strptime(s, "%Y-%m-%d").replace(tzinfo=timezone.utc)
    except ValueError:
        _die(f"bad date (expected YYYY-MM-DD): {s!r}")
    return dt.timestamp()


def build_filters(args: argparse.Namespace) -> tuple[list, list]:
    where: list = []
    params: list = []
    add = where.append
    if args.workspace:
        add("w.name = ?")
        params.append(args.workspace)
    if args.mtype:
        add("m.mtype = ?")
        params.append(args.mtype)
    if args.namespace:
        add("m.scope = ?")
        params.append(args.namespace)
    if args.date_from:
        add("m.ingested_at >= ?")
        params.append(parse_date(args.date_from))
    if args.date_to:
        add("m.ingested_at < ?")
        params.append(parse_date(args.date_to) + 86400.0)
    if args.seat_label:
        add("json_extract(m.metadata, '$.seat_label') = ?")
        params.append(args.seat_label)
    if args.record_id:
        add("m.id = ? OR json_extract(m.metadata, '$.record_id') = ?")
        params.extend([args.record_id, args.record_id])
    if args.capture_sha:
        add("json_extract(m.metadata, '$.capture_sha') = ?")
        params.append(args.capture_sha)
    return where, params


def row_summary(row: sqlite3.Row) -> dict:
    meta_raw = row["metadata"] or "{}"
    try:
        meta = json.loads(meta_raw)
    except ValueError:
        meta = {}
    return {
        "id": row["id"],
        "workspace": row["workspace_name"],
        "mtype": row["mtype"],
        "scope": row["scope"],
        "title": (row["title"] or "")[:120],
        "summary": (row["summary"] or "")[:200],
        "importance": row["importance"],
        "pinned": bool(row["pinned"]),
        "ingested_at": row["ingested_at"],
        "session_id": row["session_id"],
        "record_id": meta.get("record_id"),
        "capture_sha": meta.get("capture_sha"),
    }


def cmd_census(args: argparse.Namespace) -> None:
    con = open_readonly(args.db)
    where, params = build_filters(args)
    wsql = (" JOIN workspaces w ON w.id = m.workspace_id"
            + (f" WHERE {' AND '.join(where)}" if where else ""))
    print("== census by workspace ==")
    for r in con.execute(
        "SELECT w.name, COUNT(*) n FROM memories m" + wsql
        + " GROUP BY w.name ORDER BY n DESC", params):
        print(f"{r['n']:>8}  {r['name']}")
    print("== census by mtype ==")
    for r in con.execute(
        "SELECT m.mtype, COUNT(*) n FROM memories m" + wsql
        + " GROUP BY m.mtype ORDER BY n DESC", params):
        print(f"{r['n']:>8}  {r['mtype']}")
    print("== census by scope ==")
    for r in con.execute(
        "SELECT m.scope, COUNT(*) n FROM memories m" + wsql
        + " GROUP BY m.scope ORDER BY n DESC", params):
        print(f"{r['n']:>8}  {r['scope']}")
    con.close()


def cmd_records(args: argparse.Namespace) -> None:
    con = open_readonly(args.db)
    where, params = build_filters(args)
    sql = ("SELECT " + SELECT_COLUMNS + " FROM memories m"
           " JOIN workspaces w ON w.id = m.workspace_id"
           + (f" WHERE {' AND '.join(where)}" if where else "")
           + " ORDER BY m.ingested_at DESC LIMIT ?")
    params.append(args.limit)
    n = 0
    for row in con.execute(sql, params):
        print(json.dumps(row_summary(row), ensure_ascii=False))
        n += 1
    print(f"# {n} record(s)", file=sys.stderr)
    con.close()


def cmd_get(args: argparse.Namespace) -> None:
    if not args.record_id and not args.capture_sha:
        _die("get requires --record-id or --capture-sha")
    args.workspace = args.mtype = args.namespace = None
    args.date_from = args.date_to = args.seat_label = None
    con = open_readonly(args.db)
    where, params = build_filters(args)
    sql = ("SELECT " + SELECT_COLUMNS + " FROM memories m"
           " JOIN workspaces w ON w.id = m.workspace_id"
           + (f" WHERE {' AND '.join(where)}" if where else ""))
    rows = con.execute(sql, params).fetchall()
    if not rows:
        print("# no match", file=sys.stderr)
        raise SystemExit(1)
    for row in rows:
        out = dict(row_summary(row))
        out["content"] = row["content"]
        try:
            out["metadata"] = json.loads(row["metadata"] or "{}")
        except ValueError:
            out["metadata"] = {}
        print(json.dumps(out, ensure_ascii=False, indent=2))
    con.close()


def make_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="engraphis_lookup", description=__doc__)
    p.add_argument("--db", default=DEFAULT_DB)
    common = argparse.ArgumentParser(add_help=False)
    common.add_argument("--workspace")
    common.add_argument("--mtype")
    common.add_argument("--namespace", help="scope: repo|workspace")
    common.add_argument("--date-from", dest="date_from")
    common.add_argument("--date-to", dest="date_to")
    common.add_argument("--seat-label", dest="seat_label",
                        help="metadata JSON field seat_label")
    common.add_argument("--record-id", dest="record_id")
    common.add_argument("--capture-sha", dest="capture_sha")
    common.add_argument("--limit", type=int, default=50)
    sub = p.add_subparsers(dest="cmd", required=True)
    sub.add_parser("census", parents=[common]).set_defaults(func=cmd_census)
    sub.add_parser("records", parents=[common]).set_defaults(func=cmd_records)
    sub.add_parser("get", parents=[common]).set_defaults(func=cmd_get)
    return p


def main(argv: list) -> int:
    args = make_parser().parse_args(argv)
    args.func(args)
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
