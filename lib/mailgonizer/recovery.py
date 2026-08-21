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
from mailgonizer.executor import ExecutionResult, move_individually
from mailgonizer.imap import TransientError
from mailgonizer.index import Index
from mailgonizer.psl import PublicSuffixList
from mailgonizer.runlog import RunLog
from mailgonizer.runner import classify, config_hash, survey


def undo_run(mailbox, index: Index, cfg: Config, psl: PublicSuffixList,
             log: RunLog, run_id: int) -> ExecutionResult:
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
    # Stamp the real config hash and PSL version, not blanks. `last_run()` is
    # ordered run_id DESC, so a blank psl_version here would make every later
    # run read an empty `last_psl` and silently disable the drift warning --
    # the sole protection against a re-vendored list re-bucketing senders.
    # first-run.md presents undo as a normal part of the first-run loop.
    reversal_run = index.start_run("undo", config_hash(cfg), psl.version,
                                   cfg.server.host)

    by_dst: OrderedDict[str, list] = OrderedDict()
    for move in moves:
        by_dst.setdefault(move["dst_folder"], []).append(move)

    size = cfg.execution.batch_size
    moved = skipped = failed = 0
    for dst_folder, group in by_dst.items():
        # readonly=False: this folder is about to have mail moved out of it,
        # and RFC 3501 permits no permanent-state changes under EXAMINE. The
        # selection is held read-write for the whole reversal of this folder.
        uids = mailbox.list_uids(dst_folder, readonly=False)
        # Chunked, because IMAPClient joins UIDs with "," and collapses no
        # ranges: undoing a first run of 20,000 messages would otherwise build
        # a ~140 KB command line against Dovecot's 64 KB imap_max_line_length.
        present: dict[int, str] = {}
        for start in range(0, len(uids), size):
            present.update(mailbox.fetch_identity(uids[start:start + size]))
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
            for start in range(0, len(pairs), size):
                batch = pairs[start:start + size]
                try:
                    mailbox.move([uid for uid, _ in batch], src_folder)
                    results = dict.fromkeys((uid for uid, _ in batch), None)
                except TransientError as exc:
                    # A per-item NO must not become an uncaught traceback that
                    # strands this reversal run at status='running' forever.
                    # Retry one at a time so one bad message costs one message,
                    # then record and count each outcome and keep going.
                    log.warn(f"batch of {len(batch)} back to {src_folder!r} "
                             f"failed ({exc}); retrying one at a time")
                    results = move_individually(
                        mailbox, [uid for uid, _ in batch], src_folder)

                for uid, move in batch:
                    error = results[uid]
                    if error is not None:
                        log.warn(f"{move['msg_key']} could not be moved back "
                                 f"to {src_folder}: {error}")
                        log.decision(msg_key=move["msg_key"], state="failed",
                                     reason="undo_failed", src=dst_folder,
                                     dst=src_folder, error=error)
                        failed += 1
                        continue
                    index.append_move(reversal_run, move["msg_key"],
                                      move["message_id"], dst_folder,
                                      src_folder, None)
                    log.decision(msg_key=move["msg_key"], state="done",
                                 reason="undo", src=dst_folder, dst=src_folder)
                    moved += 1

    counts = {"moved": moved, "failed": failed, "skipped": skipped,
              "undid_run": run_id}
    index.finish_run(reversal_run, "ok" if failed == 0 else "failed", counts)
    log.verdict(counts)
    return ExecutionResult(moved=moved, failed=failed, skipped=skipped)


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
