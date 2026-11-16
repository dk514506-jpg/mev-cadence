#!/usr/bin/env python
"""Selftest for engraphis_lookup.py — runs ONLY against a throwaway temp
sqlite fixture. Never touches the live ~/.engraphis/memory.db.

Includes the A3 falsifier: a write attempt on the mode=ro connection MUST
raise (ro-by-convention proven, not assumed).
"""
import importlib.util
import json
import os
import sqlite3
import sys
import tempfile
import unittest

TOOLS = os.path.dirname(os.path.abspath(__file__))
spec = importlib.util.spec_from_file_location(
    "engraphis_lookup", os.path.join(TOOLS, "engraphis_lookup.py"))
el = importlib.util.module_from_spec(spec)
spec.loader.exec_module(el)


def make_fixture(path: str) -> None:
    con = sqlite3.connect(path)
    con.executescript(
        """
        CREATE TABLE workspaces (id TEXT PRIMARY KEY, name TEXT NOT NULL,
                                 created_at REAL, settings TEXT DEFAULT '{}');
        CREATE TABLE memories (id TEXT PRIMARY KEY, workspace_id TEXT NOT NULL,
                               repo_id TEXT, session_id TEXT, scope TEXT DEFAULT 'repo',
                               mtype TEXT DEFAULT 'semantic', title TEXT DEFAULT '',
                               content TEXT, summary TEXT DEFAULT '', keywords TEXT DEFAULT '[]',
                               metadata TEXT DEFAULT '{}', importance REAL DEFAULT 0.0,
                               surprise REAL DEFAULT 1.0, stability REAL DEFAULT 1.0,
                               confidence REAL DEFAULT 1.0, access_count INTEGER DEFAULT 0,
                               last_access REAL, valid_from REAL, valid_to REAL,
                               valid_to_recorded_at REAL, ingested_at REAL, expired_at REAL,
                               subject_key TEXT DEFAULT '', claim_kind TEXT DEFAULT '',
                               pinned INTEGER DEFAULT 0, sensitivity TEXT DEFAULT 'normal',
                               provenance TEXT DEFAULT '{}', pinned_at REAL, unpinned_at REAL,
                               sort_order REAL);
        INSERT INTO workspaces VALUES ('ws_staging', 'mev-staging', 0, '{}');
        INSERT INTO workspaces VALUES ('ws_other', 'other', 0, '{}');
        INSERT INTO memories (id, workspace_id, scope, mtype, title, content,
                              ingested_at, metadata)
        VALUES ('mem_1', 'ws_staging', 'repo', 'episodic', 'staged build test',
                'body one', 1761900000.0,
                json_object('record_id','rec_abc','capture_sha','cafe01',
                            'seat_label','cmd-code-main'));
        INSERT INTO memories (id, workspace_id, scope, mtype, title, content,
                              ingested_at, metadata)
        VALUES ('mem_2', 'ws_other', 'workspace', 'semantic', 'unrelated',
                'body two', 1700000000.0, '{}');
        """)
    con.commit()
    con.close()


class EngraphisLookupSelftest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.TemporaryDirectory(prefix="engraphis-selftest-")
        cls.db = os.path.join(cls.tmp.name, "fixture.db")
        make_fixture(cls.db)

    @classmethod
    def tearDownClass(cls):
        cls.tmp.cleanup()

    def test_write_attempt_raises(self):
        """A3 falsifier: mode=ro connection must reject writes."""
        con = el.open_readonly(self.db)
        with self.assertRaises(sqlite3.OperationalError):
            con.execute("INSERT INTO workspaces VALUES ('ws_x','x',0,'{}')")
        with self.assertRaises(sqlite3.OperationalError):
            con.execute("UPDATE memories SET title='tampered' WHERE id='mem_1'")
        with self.assertRaises(sqlite3.OperationalError):
            con.execute("CREATE TABLE droptest (a TEXT)")
        con.close()
        # Fixture unchanged.
        con = sqlite3.connect(self.db)
        self.assertEqual(con.execute(
            "SELECT COUNT(*) FROM workspaces").fetchone()[0], 2)
        con.close()

    def test_open_readonly_query_only(self):
        con = el.open_readonly(self.db)
        self.assertEqual(con.execute("PRAGMA query_only").fetchone()[0], 1)
        con.close()

    def test_census(self):
        import io, contextlib
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            el.main(["--db", self.db, "census"])
        out = buf.getvalue()
        self.assertIn("mev-staging", out)
        self.assertIn("other", out)
        self.assertIn("episodic", out)

    def test_records_filters(self):
        import io, contextlib
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            el.main(["--db", self.db, "records", "--workspace", "mev-staging"])
        rows = [json.loads(l) for l in buf.getvalue().strip().splitlines()
                if l.startswith("{")]
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["id"], "mem_1")
        self.assertEqual(rows[0]["record_id"], "rec_abc")

        for filt, val, want in (("--mtype", "episodic", "mem_1"),
                                ("--namespace", "workspace", "mem_2"),
                                ("--seat-label", "cmd-code-main", "mem_1"),
                                ("--record-id", "rec_abc", "mem_1"),
                                ("--capture-sha", "cafe01", "mem_1"),
                                ("--date-from", "2025-10-31", "mem_1")):
            buf2 = io.StringIO()
            with contextlib.redirect_stdout(buf2), contextlib.redirect_stderr(io.StringIO()):
                el.main(["--db", self.db, "records", filt, val])
            rows2 = [json.loads(l) for l in buf2.getvalue().strip().splitlines()
                     if l.startswith("{")]
            self.assertEqual([r["id"] for r in rows2], [want],
                             f"filter {filt}={val} gave {[r['id'] for r in rows2]}")

    def test_get_by_capture_sha(self):
        import io, contextlib
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            el.main(["--db", self.db, "get", "--capture-sha", "cafe01"])
        doc = json.loads(buf.getvalue())
        self.assertEqual(doc["id"], "mem_1")
        self.assertEqual(doc["content"], "body one")
        self.assertEqual(doc["metadata"]["record_id"], "rec_abc")

    def test_no_writes_anywhere(self):
        """The module source must contain no non-SELECT statements."""
        src = open(el.__file__ if hasattr(el, "__file__") else
                   os.path.join(TOOLS, "engraphis_lookup.py")).read()
        for bad in ("INSERT ", "UPDATE ", "DELETE ", "DROP ", "CREATE ",
                    "REPLACE ", "PRAGMA journal_mode"):
            self.assertNotIn(bad, src.split("def make_fixture")[0]
                             if "def make_fixture" in src else src,
                             f"forbidden statement in tool source: {bad!r}")
        # the real source file, not the test:
        tool_src = open(os.path.join(TOOLS, "engraphis_lookup.py")).read()
        for bad in ("INSERT ", "UPDATE ", "DELETE ", "DROP ", "CREATE ",
                    "VACUUM", "journal_mode"):
            self.assertNotIn(bad, tool_src, f"forbidden: {bad!r}")


if __name__ == "__main__":
    unittest.main(verbosity=2)
