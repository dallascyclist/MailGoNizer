"""Plan execution. Reads a plan, performs moves, records what happened.

Makes no decisions: if the plan says move it, it moves it.
"""

from __future__ import annotations

import time
from collections import OrderedDict
from dataclasses import dataclass

from mailgonizer.config import Config
from mailgonizer.imap import TransientError
from mailgonizer.index import Index
from mailgonizer.records import PlanItem
from mailgonizer.runlog import RunLog


@dataclass(frozen=True)
class ExecutionResult:
    moved: int = 0
    failed: int = 0
    skipped: int = 0


def _group(items: list[PlanItem]) -> OrderedDict:
    """Group by (src_folder, dst_folder), preserving plan order within groups."""
    groups: OrderedDict[tuple[str, str], list[PlanItem]] = OrderedDict()
    for item in items:
        groups.setdefault((item.src_folder, item.dst_folder), []).append(item)
    return groups


def execute(mailbox, index: Index, run_id: int, cfg: Config, log: RunLog,
            reconnect=None) -> ExecutionResult:
    items = index.pending_items(run_id)
    if not items:
        log.info("nothing pending")
        return ExecutionResult()

    moved = failed = skipped = 0
    validated: dict[str, bool] = {}

    for (src, dst), group in _group(items).items():
        if src not in validated:
            live_uidvalidity, _ = mailbox.select(src, readonly=False)
            expected = group[0].src_uidvalidity
            validated[src] = live_uidvalidity == expected
            if not validated[src]:
                log.error(
                    f"{src}: UIDVALIDITY changed {expected} -> {live_uidvalidity}; "
                    "every stored UID for this folder is void"
                )
        else:
            mailbox.select(src, readonly=False)

        if not validated[src]:
            for item in group:
                index.mark_failed(
                    run_id, item.seq,
                    f"UIDVALIDITY changed for {src}; plan is void for this folder",
                )
                log.decision(msg_key=item.msg_key, seq=item.seq, state="failed",
                             reason="uidvalidity_changed", src=src, dst=dst)
                failed += 1
            continue

        mailbox.ensure_folder(dst, subscribe=cfg.execution.subscribe_created_folders)
        index.record_folder(dst, run_id)

        size = cfg.execution.batch_size
        for start in range(0, len(group), size):
            batch = group[start:start + size]

            # Idempotency first: the move log is authoritative about what has
            # already happened, even if the plan is stale.
            live: list[PlanItem] = []
            for item in batch:
                if index.already_moved(item.msg_key):
                    index.mark_skipped(run_id, item.seq, "already_moved")
                    log.decision(msg_key=item.msg_key, seq=item.seq,
                                 state="skipped", reason="already_moved")
                    skipped += 1
                else:
                    live.append(item)
            if not live:
                continue

            identities = mailbox.fetch_identity([i.src_uid for i in live])
            confirmed: list[PlanItem] = []
            for item in live:
                actual = identities.get(item.src_uid)
                if actual is None:
                    index.mark_skipped(run_id, item.seq, "vanished")
                    log.decision(msg_key=item.msg_key, seq=item.seq,
                                 state="skipped", reason="vanished")
                    skipped += 1
                elif actual != item.msg_key:
                    index.mark_skipped(run_id, item.seq, "identity_mismatch")
                    log.decision(msg_key=item.msg_key, seq=item.seq,
                                 state="skipped", reason="identity_mismatch",
                                 found=actual)
                    skipped += 1
                else:
                    confirmed.append(item)
            if not confirmed:
                continue

            uids = [i.src_uid for i in confirmed]
            error = _move_with_retry(mailbox, uids, dst, cfg, log, reconnect, src)

            if error is None:
                for item in confirmed:
                    index.mark_done(run_id, item.seq, None, None)
                    log.decision(msg_key=item.msg_key, seq=item.seq, state="done",
                                 src=src, dst=dst, reason=item.reason)
                moved += len(confirmed)
            else:
                for item in confirmed:
                    index.mark_failed(run_id, item.seq, error)
                    log.decision(msg_key=item.msg_key, seq=item.seq, state="failed",
                                 reason="move_failed", error=error)
                failed += len(confirmed)

            if cfg.execution.pause_between_batches_ms:
                time.sleep(cfg.execution.pause_between_batches_ms / 1000.0)

    return ExecutionResult(moved=moved, failed=failed, skipped=skipped)


def _move_with_retry(mailbox, uids, dst, cfg, log, reconnect, src) -> str | None:
    """Return None on success, or the verbatim final error text."""
    attempts = max(1, cfg.execution.connect_retries)
    last = ""
    for attempt in range(1, attempts + 1):
        try:
            mailbox.move(uids, dst)
            return None
        except TransientError as exc:
            last = str(exc)
            log.warn(f"attempt {attempt}/{attempts} moving {len(uids)} uids "
                     f"to {dst!r}: {last}")
            if attempt == attempts:
                break
            time.sleep(min(2 ** attempt, 30))
            if reconnect is not None:
                mailbox = reconnect()
                mailbox.select(src, readonly=False)
    return last
