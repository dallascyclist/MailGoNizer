"""undo, export-index, and rebuild-index.

These exist because the first run against a twenty-year mailbox is the
highest-stakes operation the tool will ever perform.
"""

from __future__ import annotations

import csv
import json
from collections import OrderedDict
from typing import TextIO

from mailgonizer.config import Config
from mailgonizer.executor import ExecutionResult
from mailgonizer.index import Index
from mailgonizer.psl import PublicSuffixList
from mailgonizer.runlog import RunLog
from mailgonizer.runner import classify, survey


def undo_run(mailbox, index: Index, cfg: Config, log: RunLog,
             run_id: int) -> ExecutionResult:
    """Reverse a run's moves, appending the reversals to the permanent log.

    Locates each message by recomputing msg_key over the destination folder,
    which works on any server and needs nothing the log does not hold.
    Promotions are NOT reversed: the folders remain and the ratchet holds.
    """
    mailbox.assert_safe()
    moves = index.moves_for_run(run_id)
    if not moves:
        log.info(f"run {run_id} moved nothing; undo is a no-op")
        return ExecutionResult()

    log.phase(f"UNDO run {run_id}")
    reversal_run = index.start_run("undo", "", "", cfg.server.host)

    by_dst: OrderedDict[str, list] = OrderedDict()
    for move in moves:
        by_dst.setdefault(move["dst_folder"], []).append(move)

    moved = skipped = 0
    for dst_folder, group in by_dst.items():
        present = mailbox.fetch_identity(mailbox.list_uids(dst_folder))
        by_key = {key: uid for uid, key in present.items()}

        back_to: OrderedDict[str, list] = OrderedDict()
        for move in group:
            uid = by_key.get(move["msg_key"])
            if uid is None:
                log.warn(f"{move['msg_key']} is not in {dst_folder}; skipping")
                log.decision(msg_key=move["msg_key"], state="skipped",
                             reason="not_at_destination", folder=dst_folder)
                skipped += 1
                continue
            back_to.setdefault(move["src_folder"], []).append((uid, move))

        for src_folder, pairs in back_to.items():
            mailbox.ensure_folder(
                src_folder, subscribe=cfg.execution.subscribe_created_folders
            )
            size = cfg.execution.batch_size
            for start in range(0, len(pairs), size):
                batch = pairs[start:start + size]
                mailbox.move([uid for uid, _ in batch], src_folder)
                for _uid, move in batch:
                    index.append_move(reversal_run, move["msg_key"],
                                      move["message_id"], dst_folder,
                                      src_folder, None)
                    log.decision(msg_key=move["msg_key"], state="done",
                                 reason="undo", src=dst_folder, dst=src_folder)
                moved += len(batch)

    counts = {"moved": moved, "skipped": skipped, "undid_run": run_id}
    index.finish_run(reversal_run, "ok", counts)
    log.verdict(counts)
    return ExecutionResult(moved=moved, failed=0, skipped=skipped)


_MESSAGE_COLUMNS = [
    "msg_key", "message_id", "resolved_date", "date_source", "year",
    "sender_domain", "sender_local", "list_id", "current_folder", "last_seen_run",
]


def export_index(index: Index, fmt: str, stream: TextIO) -> None:
    """Dump the index so the data is never trapped in a private database."""
    messages = [dict(r) for r in index.iter_messages()]
    if fmt == "csv":
        writer = csv.DictWriter(stream, fieldnames=_MESSAGE_COLUMNS,
                                extrasaction="ignore")
        writer.writeheader()
        writer.writerows(messages)
        return

    payload = {
        "messages": messages,
        "moves": [dict(r) for r in index.conn.execute(
            "SELECT * FROM moves ORDER BY id")],
        "promotions": [dict(r) for r in index.conn.execute(
            "SELECT * FROM promotions ORDER BY year, sender_domain, sender_local")],
        "runs": [dict(r) for r in index.conn.execute(
            "SELECT * FROM runs ORDER BY run_id")],
    }
    json.dump(payload, stream, indent=2, default=str)


def rebuild_index(mailbox, index: Index, cfg: Config, psl: PublicSuffixList,
                  log: RunLog) -> int:
    """Discard Layer 1 and rescan. moves and promotions are untouched."""
    log.phase("REBUILD INDEX")
    from datetime import datetime, timezone

    from mailgonizer.runner import config_hash

    run_id = index.start_run("rebuild-index", config_hash(cfg), psl.version,
                             cfg.server.host)
    index.clear_cache()
    records = survey(mailbox, cfg, log)
    messages = classify(records, cfg, psl, datetime.now(timezone.utc))
    index.upsert_messages(messages, run_id)
    counts = {"surveyed": len(records), "cached": len(messages)}
    index.finish_run(run_id, "ok", counts)
    log.verdict(counts)
    return run_id
