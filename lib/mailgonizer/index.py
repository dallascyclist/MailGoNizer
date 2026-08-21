"""SQLite state: rebuildable cache, permanent record, and working plan.

The index persists; it does not decide. Bucket counts are computed by the
planner from data it already holds, because a cached count that drifts would
produce a wrong promotion that the ratchet then makes permanent.
"""

from __future__ import annotations

import json
import sqlite3
from collections.abc import Iterable, Iterator
from datetime import datetime, timezone
from pathlib import Path
from types import TracebackType

from mailgonizer.records import ClassifiedMessage, PlanItem

SCHEMA_VERSION = 1

_SCHEMA = """
CREATE TABLE IF NOT EXISTS meta (
    key TEXT PRIMARY KEY, value TEXT
);

-- Layer 1: rebuildable cache
CREATE TABLE IF NOT EXISTS messages (
    msg_key        TEXT PRIMARY KEY,
    message_id     TEXT,
    resolved_date  TEXT,
    date_source    TEXT,
    year           INTEGER,
    sender_domain  TEXT,
    sender_local   TEXT,
    list_id        TEXT,
    current_folder TEXT,
    last_seen_run  INTEGER
);
CREATE INDEX IF NOT EXISTS ix_msg_bucket
    ON messages(year, sender_domain, sender_local);

CREATE TABLE IF NOT EXISTS folders (
    name TEXT PRIMARY KEY, created_run INTEGER, first_seen TEXT
);

-- Layer 2: permanent record, append-only
CREATE TABLE IF NOT EXISTS moves (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    run_id INTEGER, msg_key TEXT, message_id TEXT,
    src_folder TEXT, dst_folder TEXT, moved_at TEXT, dst_uid INTEGER
);
CREATE INDEX IF NOT EXISTS ix_moves_key ON moves(msg_key);
CREATE INDEX IF NOT EXISTS ix_moves_run ON moves(run_id);

CREATE TABLE IF NOT EXISTS promotions (
    year INTEGER, sender_domain TEXT, sender_local TEXT,
    promoted_run INTEGER, promoted_at TEXT, trigger_count INTEGER,
    PRIMARY KEY (year, sender_domain, sender_local)
);

CREATE TABLE IF NOT EXISTS runs (
    run_id INTEGER PRIMARY KEY AUTOINCREMENT,
    mode TEXT, started_at TEXT, finished_at TEXT, status TEXT,
    config_hash TEXT, psl_version TEXT, server_host TEXT,
    inbox_uidnext INTEGER, counts_json TEXT
);

-- Layer 3: working plan
CREATE TABLE IF NOT EXISTS plan_items (
    run_id INTEGER, seq INTEGER, msg_key TEXT,
    src_folder TEXT, src_uid INTEGER, src_uidvalidity INTEGER,
    dst_folder TEXT, reason TEXT, state TEXT, error TEXT, attempted_at TEXT,
    PRIMARY KEY (run_id, seq)
);
CREATE INDEX IF NOT EXISTS ix_plan_pending ON plan_items(run_id, state);
"""


def _utcnow() -> str:
    return datetime.now(timezone.utc).isoformat()


class Index:
    def __init__(self, conn: sqlite3.Connection) -> None:
        self.conn = conn

    @classmethod
    def open(cls, path: Path) -> Index:
        path.parent.mkdir(parents=True, exist_ok=True)
        conn = sqlite3.connect(path, isolation_level=None)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("PRAGMA foreign_keys=ON")
        conn.executescript(_SCHEMA)
        conn.execute(
            "INSERT OR IGNORE INTO meta(key, value) VALUES('schema_version', ?)",
            (str(SCHEMA_VERSION),),
        )
        return cls(conn)

    def __enter__(self) -> Index:
        return self

    def __exit__(self, exc_type: type[BaseException] | None,
                 exc: BaseException | None, tb: TracebackType | None) -> None:
        self.conn.close()

    # --- runs -------------------------------------------------------------

    def start_run(self, mode: str, config_hash: str, psl_version: str,
                  server_host: str) -> int:
        cur = self.conn.execute(
            "INSERT INTO runs(mode, started_at, status, config_hash, psl_version,"
            " server_host) VALUES(?,?,?,?,?,?)",
            (mode, _utcnow(), "running", config_hash, psl_version, server_host),
        )
        return int(cur.lastrowid)

    def finish_run(self, run_id: int, status: str, counts: dict) -> None:
        self.conn.execute(
            "UPDATE runs SET finished_at=?, status=?, counts_json=? WHERE run_id=?",
            (_utcnow(), status, json.dumps(counts, sort_keys=True), run_id),
        )

    def last_run(self) -> sqlite3.Row | None:
        return self.conn.execute(
            "SELECT * FROM runs ORDER BY run_id DESC LIMIT 1"
        ).fetchone()

    # --- cache ------------------------------------------------------------

    def upsert_messages(self, messages: Iterable[ClassifiedMessage],
                        run_id: int) -> None:
        self.conn.executemany(
            "INSERT INTO messages(msg_key, message_id, resolved_date, date_source,"
            " year, sender_domain, sender_local, list_id, current_folder,"
            " last_seen_run) VALUES(?,?,?,?,?,?,?,?,?,?)"
            " ON CONFLICT(msg_key) DO UPDATE SET"
            " current_folder=excluded.current_folder,"
            " last_seen_run=excluded.last_seen_run",
            [
                (m.msg_key, m.message_id, m.resolved_date.isoformat(),
                 m.date_source, m.year, m.sender.domain, m.sender.local,
                 m.sender.list_id, m.folder, run_id)
                for m in messages
            ],
        )

    def iter_messages(self) -> Iterator[sqlite3.Row]:
        yield from self.conn.execute("SELECT * FROM messages ORDER BY msg_key")

    def record_folder(self, name: str, run_id: int) -> None:
        self.conn.execute(
            "INSERT OR IGNORE INTO folders(name, created_run, first_seen)"
            " VALUES(?,?,?)",
            (name, run_id, _utcnow()),
        )

    def clear_cache(self) -> None:
        """Discard Layer 1 only. moves, promotions, and runs are permanent."""
        self.conn.execute("DELETE FROM messages")
        self.conn.execute("DELETE FROM folders")

    # --- promotions -------------------------------------------------------

    def known_promotions(self) -> set[tuple[int, str, str]]:
        return {
            (r["year"], r["sender_domain"], r["sender_local"])
            for r in self.conn.execute(
                "SELECT year, sender_domain, sender_local FROM promotions"
            )
        }

    def record_promotion(self, year: int, domain: str, local: str, run_id: int,
                         trigger_count: int) -> None:
        # OR IGNORE, not OR REPLACE: the ratchet records when it fired.
        self.conn.execute(
            "INSERT OR IGNORE INTO promotions(year, sender_domain, sender_local,"
            " promoted_run, promoted_at, trigger_count) VALUES(?,?,?,?,?,?)",
            (year, domain, local, run_id, _utcnow(), trigger_count),
        )

    # --- plan -------------------------------------------------------------

    def save_plan(self, run_id: int, items: Iterable[PlanItem],
                  inbox_uidnext: int) -> None:
        self.conn.execute("DELETE FROM plan_items WHERE run_id=?", (run_id,))
        self.conn.executemany(
            "INSERT INTO plan_items(run_id, seq, msg_key, src_folder, src_uid,"
            " src_uidvalidity, dst_folder, reason, state)"
            " VALUES(?,?,?,?,?,?,?,?,'pending')",
            [
                (run_id, i.seq, i.msg_key, i.src_folder, i.src_uid,
                 i.src_uidvalidity, i.dst_folder, i.reason)
                for i in items
            ],
        )
        self.conn.execute(
            "UPDATE runs SET inbox_uidnext=? WHERE run_id=?", (inbox_uidnext, run_id)
        )

    def pending_items(self, run_id: int) -> list[PlanItem]:
        rows = self.conn.execute(
            "SELECT * FROM plan_items WHERE run_id=? AND state='pending'"
            " ORDER BY seq",
            (run_id,),
        )
        return [
            PlanItem(r["seq"], r["msg_key"], r["src_folder"], r["src_uid"],
                     r["src_uidvalidity"], r["dst_folder"], r["reason"])
            for r in rows
        ]

    def all_items(self, run_id: int) -> list[sqlite3.Row]:
        return list(self.conn.execute(
            "SELECT * FROM plan_items WHERE run_id=? ORDER BY seq", (run_id,)
        ))

    def mark_done(self, run_id: int, seq: int, message_id: str | None,
                  dst_uid: int | None) -> None:
        """State change and move-log append happen atomically.

        The log must never claim a move that was not recorded, nor vice versa.
        """
        row = self.conn.execute(
            "SELECT * FROM plan_items WHERE run_id=? AND seq=?", (run_id, seq)
        ).fetchone()
        if row is None:
            raise KeyError(f"no plan item run_id={run_id} seq={seq}")

        self.conn.execute("BEGIN")
        try:
            self.conn.execute(
                "UPDATE plan_items SET state='done', attempted_at=?"
                " WHERE run_id=? AND seq=?",
                (_utcnow(), run_id, seq),
            )
            self.conn.execute(
                "INSERT INTO moves(run_id, msg_key, message_id, src_folder,"
                " dst_folder, moved_at, dst_uid) VALUES(?,?,?,?,?,?,?)",
                (run_id, row["msg_key"], message_id, row["src_folder"],
                 row["dst_folder"], _utcnow(), dst_uid),
            )
            self.conn.execute("COMMIT")
        except sqlite3.Error:
            self.conn.execute("ROLLBACK")
            raise

    def mark_failed(self, run_id: int, seq: int, error: str) -> None:
        self.conn.execute(
            "UPDATE plan_items SET state='failed', error=?, attempted_at=?"
            " WHERE run_id=? AND seq=?",
            (error, _utcnow(), run_id, seq),
        )

    def mark_skipped(self, run_id: int, seq: int, reason: str) -> None:
        self.conn.execute(
            "UPDATE plan_items SET state='skipped', error=?, attempted_at=?"
            " WHERE run_id=? AND seq=?",
            (reason, _utcnow(), run_id, seq),
        )

    # --- move log ---------------------------------------------------------

    def already_moved(self, msg_key: str, dst_folder: str | None = None) -> bool:
        """Whether *msg_key*'s most recent recorded move landed at *dst_folder*.

        Only the most recent move counts, not "ever": a message already at
        one destination must still be free to move again to a *different*
        one later (a promotion backfill moving mail out of the flat
        per-domain folder into a newly-promoted per-sender subfolder is
        exactly this case). Using the most recent row rather than "does a
        matching row exist anywhere in history" also means a message `undo`
        has reversed back to the inbox (which appends a reversal row rather
        than rewriting history — see `append_move`) is free to be
        re-archived to that same destination later; an "ever" check would
        block it forever. Without *dst_folder*, matches any prior move at
        all — for callers that only care whether the message has moved.
        """
        if dst_folder is None:
            return self.conn.execute(
                "SELECT 1 FROM moves WHERE msg_key=? LIMIT 1", (msg_key,)
            ).fetchone() is not None
        row = self.conn.execute(
            "SELECT dst_folder FROM moves WHERE msg_key=? ORDER BY id DESC LIMIT 1",
            (msg_key,),
        ).fetchone()
        return row is not None and row["dst_folder"] == dst_folder

    def moves_for_run(self, run_id: int) -> list[sqlite3.Row]:
        return list(self.conn.execute(
            "SELECT * FROM moves WHERE run_id=? ORDER BY id", (run_id,)
        ))

    def append_move(self, run_id: int, msg_key: str, message_id: str | None,
                    src_folder: str, dst_folder: str, dst_uid: int | None) -> None:
        """Used by `undo`, which appends reversals rather than rewriting history."""
        self.conn.execute(
            "INSERT INTO moves(run_id, msg_key, message_id, src_folder, dst_folder,"
            " moved_at, dst_uid) VALUES(?,?,?,?,?,?,?)",
            (run_id, msg_key, message_id, src_folder, dst_folder, _utcnow(), dst_uid),
        )
