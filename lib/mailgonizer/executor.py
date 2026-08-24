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
from mailgonizer.runlog import Progress, RunLog


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
    # Applying a first run moves tens of thousands of messages one batch at a
    # time. Report against the plan's own item count so the operator can see
    # it advancing rather than guessing whether it has stalled.
    progress = Progress(log, "EXECUTE", total=len(items))
    validated: dict[str, bool] = {}

    for (src, dst), group in _group(items).items():
        expected = group[0].src_uidvalidity
        if src not in validated:
            live_uidvalidity, _ = mailbox.select(src, readonly=False)
            validated[src] = live_uidvalidity == expected
            if not validated[src]:
                log.error(
                    f"{src}: UIDVALIDITY changed {expected} -> {live_uidvalidity}; "
                    "every stored UID for this folder is void"
                )
        else:
            mailbox.select(src, readonly=False)

        if not validated[src]:
            failed += _fail_items(
                index, log, run_id, group,
                f"UIDVALIDITY changed for {src}; plan is void for this folder",
                "uidvalidity_changed", src, dst,
            )
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
                if index.already_moved(item.msg_key, item.dst_folder):
                    index.mark_skipped(run_id, item.seq, "already_moved")
                    log.decision(msg_key=item.msg_key, seq=item.seq,
                                 state="skipped", reason="already_moved")
                    skipped += 1
                    progress.tick()
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
                    progress.tick()
                elif actual != item.msg_key:
                    index.mark_skipped(run_id, item.seq, "identity_mismatch")
                    log.decision(msg_key=item.msg_key, seq=item.seq,
                                 state="skipped", reason="identity_mismatch",
                                 found=actual)
                    skipped += 1
                    progress.tick()
                else:
                    confirmed.append(item)
            if not confirmed:
                continue

            uids = [i.src_uid for i in confirmed]
            error, mailbox, uidvalidity_lost = _move_with_retry(
                mailbox, uids, dst, cfg, log, reconnect, src, expected)

            if error is None:
                for item in confirmed:
                    index.mark_done(run_id, item.seq, None, None)
                    log.decision(msg_key=item.msg_key, seq=item.seq, state="done",
                                 src=src, dst=dst, reason=item.reason)
                moved += len(confirmed)
                progress.tick(len(confirmed))
                progress.note(f"moved {moved:,} skipped {skipped:,} failed {failed:,}")
            elif uidvalidity_lost:
                # Every stored UID for this folder now names a different
                # message, so there is nothing left to salvage here: not this
                # batch, and not the batches behind it. Retrying per-message
                # would be the one way this tool could move the wrong mail.
                validated[src] = False
                failed += _fail_items(index, log, run_id, confirmed, error,
                                      "uidvalidity_changed", src, dst)
                failed += _fail_items(index, log, run_id, group[start + size:],
                                      error, "uidvalidity_changed", src, dst)
                break
            else:
                # A batch-level error names no UID, so failing all of them
                # would strand every innocent message in the batch: they stop
                # being `pending`, a resumed `apply` skips them, and next
                # month's plan re-poisons the same neighbours all over again.
                # One pass one-at-a-time keeps the blast radius at one message.
                log.warn(f"batch of {len(confirmed)} to {dst!r} failed "
                         f"({error}); retrying one message at a time")
                results = move_individually(mailbox, uids, dst)
                for item in confirmed:
                    item_error = results[item.src_uid]
                    if item_error is None:
                        index.mark_done(run_id, item.seq, None, None)
                        log.decision(msg_key=item.msg_key, seq=item.seq,
                                     state="done", src=src, dst=dst,
                                     reason=item.reason)
                        moved += 1
                    else:
                        index.mark_failed(run_id, item.seq, item_error)
                        log.decision(msg_key=item.msg_key, seq=item.seq,
                                     state="failed", reason="move_failed",
                                     error=item_error)
                        failed += 1

            if cfg.execution.pause_between_batches_ms:
                time.sleep(cfg.execution.pause_between_batches_ms / 1000.0)

    progress.done()
    return ExecutionResult(moved=moved, failed=failed, skipped=skipped)


def _fail_items(index, log, run_id, items, error, reason, src, dst) -> int:
    """Record *items* as failed under one shared error. Returns how many."""
    for item in items:
        index.mark_failed(run_id, item.seq, error)
        log.decision(msg_key=item.msg_key, seq=item.seq, state="failed",
                     reason=reason, src=src, dst=dst)
    return len(items)


def move_individually(mailbox, uids: list[int], dst: str) -> dict[int, str | None]:
    """Move each UID in its own command; map uid -> error text (None = moved).

    A batch-level failure identifies no particular UID, so the only way to
    find out which message is actually poison is to ask about them one at a
    time. Used after a batch has already exhausted its retries, so each UID
    gets a single attempt rather than another full backoff cycle.
    """
    results: dict[int, str | None] = {}
    for uid in uids:
        try:
            mailbox.move([uid], dst)
            results[uid] = None
        except TransientError as exc:
            results[uid] = str(exc)
    return results


def _move_with_retry(mailbox, uids, dst, cfg, log, reconnect, src,
                     expected_uidvalidity) -> tuple[str | None, object, bool]:
    """Attempt the move with retry/backoff.

    Returns (error, mailbox, uidvalidity_lost). error is None on success, else
    the verbatim final error text. mailbox is returned because a mid-retry
    reconnect replaces it with a new object — the caller must rebind its own
    reference, since rebinding this function's local parameter has no effect
    on the caller. uidvalidity_lost reports that the reconnect landed on a
    folder that is no longer the one the plan was verified against; the caller
    must not retry these UIDs individually, and must not move them at all.
    """
    attempts = max(1, cfg.execution.connect_retries)
    last = ""
    for attempt in range(1, attempts + 1):
        try:
            mailbox.move(uids, dst)
            return None, mailbox, False
        except TransientError as exc:
            last = str(exc)
            log.warn(f"attempt {attempt}/{attempts} moving {len(uids)} uids "
                     f"to {dst!r}: {last}")
            if attempt == attempts:
                break
            time.sleep(min(2 ** attempt, 30))
            if reconnect is not None:
                mailbox = reconnect()
                # Re-verify UIDVALIDITY, do not merely re-SELECT. The FETCH
                # that proved these UIDs are the planned messages happened on
                # the connection that just died. If the failure was a server
                # restart that bumped UIDVALIDITY, these same integers now
                # address entirely different messages, and moving them would
                # relocate mail nobody asked us to touch.
                live_uidvalidity, _ = mailbox.select(src, readonly=False)
                if live_uidvalidity != expected_uidvalidity:
                    return (
                        f"UIDVALIDITY for {src} changed "
                        f"({expected_uidvalidity} -> {live_uidvalidity}) "
                        "during reconnect; abandoning this move rather than "
                        "relocating unverified messages",
                        mailbox,
                        True,
                    )
    return last, mailbox, False
