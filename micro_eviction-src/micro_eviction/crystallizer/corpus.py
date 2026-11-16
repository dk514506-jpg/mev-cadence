"""T1 — corpus slice of record + span resolver.

Reads only. Manifest paths are campaign-relative; resolution is confined to
the campaign root (D-25). Byte offsets are authoritative; line numbers are
informational only (D-02). Trailing-newline policy: slices are end-exclusive
and never include a trailing newline.
"""
from __future__ import annotations

import hashlib
import json
from pathlib import Path

CAMPAIGN_ROOT = Path(__file__).resolve().parent.parent.parent
if CAMPAIGN_ROOT.name != "micro-eviction":  # fail-closed: wrong tree
    raise RuntimeError(f"crystallizer outside campaign root: {CAMPAIGN_ROOT}")

CORPUS_DIR = Path(__file__).resolve().parent / "corpus"
SLICE_MANIFEST = CORPUS_DIR / "wave1-external-record.slice.json"


class CorpusError(ValueError):
    """Any confinement, bounds, missing-file, or hash-mismatch failure."""


def confine(rel_path: str) -> Path:
    """Resolve a manifest path inside the campaign root or raise CorpusError."""
    if not isinstance(rel_path, str) or not rel_path:
        raise CorpusError(f"bad manifest path: {rel_path!r}")
    p = Path(rel_path)
    if p.is_absolute() or ".." in p.parts:
        raise CorpusError(f"path escapes campaign root: {rel_path!r}")
    full = CAMPAIGN_ROOT / p
    try:
        resolved = full.resolve()
    except OSError as e:
        raise CorpusError(f"cannot resolve {rel_path!r}: {e}")
    try:
        resolved.relative_to(CAMPAIGN_ROOT.resolve())
    except ValueError:
        raise CorpusError(f"path escapes campaign root: {rel_path!r}")
    if resolved.is_symlink() or any(
        part.is_symlink() for part in _parents_within(resolved)
    ):
        raise CorpusError(f"symlink escape refused: {rel_path!r}")
    return resolved


def _parents_within(resolved: Path):
    root = CAMPAIGN_ROOT.resolve()
    cur = resolved.parent
    while cur != root and root in cur.parents:
        yield cur
        cur = cur.parent
    if cur == root:
        yield cur


def load_manifest(path: Path | str = SLICE_MANIFEST) -> dict:
    """Load the committed slice manifest (frozen in git; the pin)."""
    with open(path, "r", encoding="utf-8") as f:
        return json.load(f)


def resolve_span(span: dict) -> str:
    """Re-slice bytes from disk, verify sha256, return exact text.

    Fail-closed: hash mismatch, missing file, out-of-bounds range, or path
    traversal all raise CorpusError and return no text.
    """
    for key in ("file", "byte_start", "byte_end", "text_sha256"):
        if key not in span:
            raise CorpusError(f"span missing {key}: {span!r}")
    full = confine(span["file"])
    try:
        raw = full.read_bytes()
    except OSError as e:
        raise CorpusError(f"missing file {span['file']!r}: {e}")
    bs, be = span["byte_start"], span["byte_end"]
    if (
        not isinstance(bs, int)
        or not isinstance(be, int)
        or bs < 0
        or be > len(raw)
        or bs >= be
    ):
        raise CorpusError(
            f"byte range out of bounds for {span['file']!r}: [{bs}, {be})"
        )
    sl = raw[bs:be]
    digest = hashlib.sha256(sl).hexdigest()
    if digest != span["text_sha256"]:
        raise CorpusError(
            f"hash mismatch for span in {span['file']!r} [{bs}, {be}): "
            f"disk={digest} manifest={span['text_sha256']}"
        )
    try:
        return sl.decode("utf-8")
    except UnicodeDecodeError as e:
        raise CorpusError(f"span is not UTF-8 text: {e}")
