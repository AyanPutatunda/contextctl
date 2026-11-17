"""Layer 3: the derived SQLite sidecar (paper, Sec. 2 "Metadata and time").

One SQLite file holds

* ``belief_history`` -- a slowly changing dimension Type 2 (SCD2) table over
  header fields plus a digest of the body. Valid time comes from headers,
  transaction time from Git (``snapshot``, ``tx_time``, ``tx_seq``);
* ``concepts_fts`` -- an FTS5 full-text index;
* ``links`` -- links between concepts;
* ``concepts`` -- the current head version of each concept.

The sidecar is a cache: :meth:`Sidecar.rebuild` deletes it and replays the
first-parent history of the main branch. The standing test is "delete, rebuild,
compare a canonical row dump" (:meth:`Sidecar.canonical_dump`).

SCD2 rule (per concept, per tracked field), applied for every first-parent
commit ``s`` on main that changes a concept:

* effective time ``v`` = the concept's ``valid_from`` header if this commit set
  or changed it, otherwise the commit time of ``s``;
* if the field's value changed: close the open row at ``v`` and open a new row
  ``[v, NULL)`` with ``snapshot = s``;
* if the concept was deleted: close all its open rows at the commit time.

A commit that edits only ``valid_from`` changes no tracked field and so writes
no row -- the gap the paper flags in Appendix B.
"""

from __future__ import annotations

import hashlib
import json
import sqlite3
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from .bundle import Bundle
from .okf import Concept, canonical, normalize_time

SCHEMA = """
CREATE TABLE IF NOT EXISTS meta (key TEXT PRIMARY KEY, value TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS concepts (
  path TEXT PRIMARY KEY, id TEXT, title TEXT, body TEXT, header_json TEXT,
  body_digest TEXT, snapshot TEXT
);
CREATE TABLE IF NOT EXISTS belief_history (
  concept TEXT NOT NULL, field TEXT NOT NULL,
  old_value TEXT, new_value TEXT,
  valid_from TEXT NOT NULL, valid_to TEXT,
  snapshot TEXT NOT NULL, tx_time TEXT NOT NULL, tx_seq INTEGER NOT NULL
);
CREATE INDEX IF NOT EXISTS bh_open ON belief_history(concept, field, valid_to);
CREATE INDEX IF NOT EXISTS bh_valid ON belief_history(concept, field, valid_from);
CREATE TABLE IF NOT EXISTS links (src TEXT NOT NULL, dst TEXT NOT NULL, PRIMARY KEY (src, dst));
CREATE VIRTUAL TABLE IF NOT EXISTS concepts_fts USING fts5(path UNINDEXED, title, content);
CREATE TABLE IF NOT EXISTS fts_map (path TEXT PRIMARY KEY, fts_rowid INTEGER NOT NULL);
"""


@dataclass(frozen=True)
class HistoryRow:
    concept: str
    field: str
    old_value: Any
    new_value: Any
    valid_from: str
    valid_to: str | None
    snapshot: str
    tx_time: str


def _decode(v: str | None) -> Any:
    return None if v is None else json.loads(v)


class Sidecar:
    def __init__(self, bundle: Bundle, path: Path | None = None) -> None:
        self.bundle = bundle
        self.path = Path(path) if path else bundle.sidecar_path
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.db = sqlite3.connect(self.path)
        self.db.executescript(SCHEMA)

    def close(self) -> None:
        self.db.close()

    @contextmanager
    def tx(self) -> Iterator[sqlite3.Connection]:
        with self.db:
            yield self.db

    # ------------------------------------------------------------- sync
    def last_synced(self) -> str | None:
        row = self.db.execute("SELECT value FROM meta WHERE key='last_snapshot'").fetchone()
        return row[0] if row else None

    def sync(self) -> int:
        """Apply every first-parent commit on main not yet applied. Idempotent.
        Returns the number of commits applied."""
        log = self.bundle.git.first_parent_log(self.bundle.main)
        last = self.last_synced()
        start = 0
        if last is not None:
            if last not in log:
                # history was rewritten (e.g. reset); rebuild from scratch
                self.reset()
                return self.sync()
            start = log.index(last) + 1
        for i in range(start, len(log)):
            prev = log[i - 1] if i > 0 else None
            self._apply(prev, log[i], i)
        return len(log) - start

    def reset(self) -> None:
        with self.tx() as db:
            for t in ("meta", "concepts", "belief_history", "links", "concepts_fts", "fts_map"):
                db.execute(f"DELETE FROM {t}")

    def rebuild(self) -> int:
        self.reset()
        return self.sync()

    def _apply(self, prev: str | None, sha: str, seq: int) -> None:
        git = self.bundle.git
        tx_time = git.commit_time(sha)
        snap = git.short(sha)
        changed = [p for p in git.changed_paths(prev, sha, "concepts") if p.endswith(".md")]
        olds = git.show_many(prev, changed) if prev else {}
        news = git.show_many(sha, changed)
        with self.tx() as db:
            for path in changed:
                ot, nt = olds.get(path), news.get(path)
                old = Concept.parse(path, ot) if ot is not None else None
                new = Concept.parse(path, nt) if nt is not None else None
                self._apply_concept(db, path, old, new, snap, tx_time, seq)
            db.execute(
                "INSERT INTO meta(key, value) VALUES('last_snapshot', ?) "
                "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
                (sha,),
            )

    def _apply_concept(
        self,
        db: sqlite3.Connection,
        path: str,
        old: Concept | None,
        new: Concept | None,
        snap: str,
        tx_time: str,
        seq: int,
    ) -> None:
        if new is None:
            db.execute(
                "UPDATE belief_history SET valid_to=? WHERE concept=? AND valid_to IS NULL",
                (tx_time, path),
            )
            db.execute("DELETE FROM concepts WHERE path=?", (path,))
            self._fts_delete(db, path)
            db.execute("DELETE FROM links WHERE src=?", (path,))
            return
        old_vf = old.header.get("valid_from") if old else None
        new_vf = new.header.get("valid_from")
        effective = normalize_time(new_vf) if new_vf and new_vf != old_vf else tx_time
        old_fields = old.tracked_fields() if old else {}
        new_fields = new.tracked_fields()
        for fname in sorted(set(old_fields) | set(new_fields)):
            ov, nv = old_fields.get(fname), new_fields.get(fname)
            if ov == nv:
                continue
            if ov is not None:
                db.execute(
                    "UPDATE belief_history SET valid_to=? WHERE concept=? AND field=? "
                    "AND valid_to IS NULL",
                    (effective, path, fname),
                )
            if nv is not None:
                db.execute(
                    "INSERT INTO belief_history VALUES (?,?,?,?,?,NULL,?,?,?)",
                    (path, fname, ov, nv, effective, snap, tx_time, seq),
                )
            else:  # field removed from the header: zero-length tombstone row
                db.execute(
                    "INSERT INTO belief_history VALUES (?,?,?,NULL,?,?,?,?,?)",
                    (path, fname, ov, effective, effective, snap, tx_time, seq),
                )
        db.execute(
            "INSERT OR REPLACE INTO concepts VALUES (?,?,?,?,?,?,?)",
            (
                path, new.concept_id, new.title, new.body, canonical(new.header),
                _decode(new_fields["body_digest"]), snap,
            ),
        )
        ff = self.bundle.config.fact_fields
        self._fts_delete(db, path)
        cur = db.execute(
            "INSERT INTO concepts_fts(path, title, content) VALUES (?,?,?)",
            (path, new.title, new.search_text(ff, self.bundle.config.similarity_text)),
        )
        db.execute("INSERT INTO fts_map VALUES (?,?)", (path, cur.lastrowid))
        db.execute("DELETE FROM links WHERE src=?", (path,))
        for dst in new.links():
            db.execute("INSERT OR IGNORE INTO links VALUES (?,?)", (path, dst))

    @staticmethod
    def _fts_delete(db: sqlite3.Connection, path: str) -> None:
        row = db.execute("SELECT fts_rowid FROM fts_map WHERE path=?", (path,)).fetchone()
        if row:
            db.execute("DELETE FROM concepts_fts WHERE rowid=?", (row[0],))
            db.execute("DELETE FROM fts_map WHERE path=?", (path,))

    # ----------------------------------------------------------- queries
    def history(self, concept: str, field: str | None = None) -> list[HistoryRow]:
        q = "SELECT * FROM belief_history WHERE concept=?"
        args: list[Any] = [concept]
        if field:
            q += " AND field=?"
            args.append(field)
        q += " ORDER BY field, tx_seq, valid_from"
        return [self._row(r) for r in self.db.execute(q, args)]

    def value_at(self, concept: str, field: str, t: str) -> Any:
        """Valid-time query (paper, Appendix B): the value of ``field`` that
        was valid at time ``t``."""
        t = normalize_time(t)
        row = self.db.execute(
            "SELECT new_value FROM belief_history WHERE concept=? AND field=? "
            "AND valid_from<=? AND (valid_to IS NULL OR valid_to>?) "
            "ORDER BY valid_from DESC, tx_seq DESC, rowid DESC LIMIT 1",
            (concept, field, t, t),
        ).fetchone()
        return _decode(row[0]) if row else None

    def fields_as_of_tx(self, concept: str, tx_seq: int) -> dict[str, Any]:
        """Transaction-time reconstruction from the sidecar: for each tracked
        field, the latest value written at or before commit number ``tx_seq``
        (absent if the concept did not exist then or the field had been removed)."""
        rows = self.db.execute(
            "SELECT field, new_value FROM belief_history b WHERE concept=? AND tx_seq = ("
            " SELECT MAX(tx_seq) FROM belief_history WHERE concept=b.concept "
            " AND field=b.field AND tx_seq<=?)",
            (concept, tx_seq),
        ).fetchall()
        return {f: _decode(v) for f, v in rows if v is not None}  # tombstones: absent

    def search(self, query: str, k: int = 10) -> list[tuple[str, str, float]]:
        """Full-text search; returns ``(path, title, bm25)`` best first."""
        terms = _fts_terms(query)
        if not terms:
            return []
        match = " OR ".join(f'"{t}"' for t in terms)
        rows = self.db.execute(
            "SELECT path, title, bm25(concepts_fts) AS r FROM concepts_fts "
            "WHERE concepts_fts MATCH ? ORDER BY r, path LIMIT ?",
            (match, k),
        ).fetchall()
        return [(p, t, float(r)) for p, t, r in rows]

    def snippet(self, query: str, k: int = 10) -> list[tuple[str, str, str]]:
        terms = _fts_terms(query)
        if not terms:
            return []
        match = " OR ".join(f'"{t}"' for t in terms)
        rows = self.db.execute(
            "SELECT path, title, snippet(concepts_fts, 2, '[', ']', '...', 12) FROM concepts_fts "
            "WHERE concepts_fts MATCH ? ORDER BY bm25(concepts_fts), path LIMIT ?",
            (match, k),
        ).fetchall()
        return [(str(p), str(t), str(s)) for p, t, s in rows]

    def all_search_texts(self) -> dict[str, str]:
        return {
            str(p): str(c)
            for p, c in self.db.execute("SELECT path, content FROM concepts_fts").fetchall()
        }

    def links(self, path: str | None = None) -> list[tuple[str, str]]:
        if path:
            rows = self.db.execute(
                "SELECT src, dst FROM links WHERE src=? OR dst=? ORDER BY src, dst", (path, path)
            )
        else:
            rows = self.db.execute("SELECT src, dst FROM links ORDER BY src, dst")
        return [(str(a), str(b)) for a, b in rows]

    def concept_rows(self) -> list[tuple[str, str]]:
        return [
            (str(p), str(t))
            for p, t in self.db.execute("SELECT path, title FROM concepts ORDER BY path")
        ]

    def canonical_dump(self) -> list[tuple[Any, ...]]:
        """Deterministic dump of every derived row (used by the rebuild test)."""
        out: list[tuple[Any, ...]] = []
        out += [("concepts", *r) for r in self.db.execute("SELECT * FROM concepts ORDER BY path")]
        out += [
            ("belief_history", *r)
            for r in self.db.execute(
                "SELECT * FROM belief_history ORDER BY concept, field, tx_seq, valid_from"
            )
        ]
        out += [("links", *r) for r in self.db.execute("SELECT * FROM links ORDER BY src, dst")]
        out += [
            ("fts", *r)
            for r in self.db.execute("SELECT path, title, content FROM concepts_fts ORDER BY path")
        ]
        return out

    def dump_digest(self) -> str:
        return hashlib.sha256(canonical(self.canonical_dump()).encode()).hexdigest()

    @staticmethod
    def _row(r: tuple[Any, ...]) -> HistoryRow:
        return HistoryRow(r[0], r[1], _decode(r[2]), _decode(r[3]), r[4], r[5], r[6], r[7])


def _fts_terms(text: str, limit: int = 64) -> list[str]:
    import re

    seen: dict[str, None] = {}
    for tok in re.findall(r"[A-Za-z0-9_]+", text.lower()):
        if len(tok) > 1:
            seen.setdefault(tok, None)
        if len(seen) >= limit:
            break
    return list(seen)
