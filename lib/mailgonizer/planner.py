"""Eligibility, promotion, and plan construction. Pure — no I/O, no clock."""

from __future__ import annotations

from collections import Counter
from dataclasses import dataclass, field
from datetime import datetime, timedelta

from mailgonizer.config import Config
from mailgonizer.records import ClassifiedMessage, PlanItem, SkipRecord
from mailgonizer.sender import archive_path, matches_never_archive


@dataclass(frozen=True)
class PlanResult:
    items: tuple[PlanItem, ...] = ()
    skips: tuple[SkipRecord, ...] = ()
    new_promotions: tuple[tuple[int, str, str, int], ...] = ()
    counts: dict[str, int] = field(default_factory=dict)


def _ineligible_reason(m: ClassifiedMessage, cfg: Config,
                        cutoff: datetime) -> str | None:
    if "\\Deleted" in m.flags:
        # Never configurable: moving soft-deleted mail resurrects what the
        # owner believed they had thrown away.
        return "deleted"
    if cfg.exclusions.keep_flagged and "\\Flagged" in m.flags:
        return "flagged"
    if matches_never_archive(m.sender, cfg.exclusions.never_archive):
        return "never_archive"
    if m.resolved_date > cutoff:
        return "too_recent"
    return None


def build_plan(messages: list[ClassifiedMessage], cfg: Config, delimiter: str,
               existing_promotions: set[tuple[int, str, str]],
               now: datetime) -> PlanResult:
    cutoff = now - timedelta(days=cfg.source.age_days)
    skips: list[SkipRecord] = []
    eligible_inbox: list[ClassifiedMessage] = []
    archived: list[ClassifiedMessage] = []

    for m in messages:
        if m.folder == cfg.source.folder:
            reason = _ineligible_reason(m, cfg, cutoff)
            if reason is not None:
                skips.append(SkipRecord(m.msg_key, m.folder, reason))
                continue
            eligible_inbox.append(m)
        else:
            # Already archived — assume the archive tree only ever holds
            # mail we put there. Ineligible mail still sitting in the
            # inbox stays there and must not influence the promotion
            # decision.
            archived.append(m)

    counts: Counter[tuple[int, str, str]] = Counter()
    for m in (*eligible_inbox, *archived):
        if m.sender.kind == "domain" and m.sender.domain and m.sender.local:
            counts[(m.year, m.sender.domain, m.sender.local)] += 1

    promoted = set(existing_promotions)
    new_promotions = []
    for bucket, count in sorted(counts.items()):
        if count > cfg.archive.promote_threshold and bucket not in promoted:
            promoted.add(bucket)
            new_promotions.append((*bucket, count))

    def destination(m: ClassifiedMessage) -> str:
        key = (m.year, m.sender.domain, m.sender.local)
        return archive_path(m.sender, m.year, cfg, delimiter,
                             promoted=key in promoted)

    backfills, archives = [], []
    for m in archived:
        dst = destination(m)
        if dst == m.folder:
            skips.append(SkipRecord(m.msg_key, m.folder, "in_place"))
        else:
            backfills.append((m, dst))
    for m in eligible_inbox:
        dst = destination(m)
        if dst == m.folder:
            skips.append(SkipRecord(m.msg_key, m.folder, "in_place"))
        else:
            archives.append((m, dst))

    # Backfill first: promotion's existing mail must land in the new folder
    # before the run's new mail joins it. Sorting is for determinism, so the
    # plan a human reviews is the byte-identical one that gets executed.
    #
    # The reason is carried alongside (m, dst) through the merge and sort so
    # that PlanItem construction below never needs a membership test against
    # `backfills` — that would be an O(n) scan per item, quadratic overall
    # and much too slow at real mailbox scale.
    ordered = (
        [("backfill", m, dst) for m, dst in
         sorted(backfills, key=lambda p: (p[1], p[0].folder, p[0].uid, p[0].msg_key))]
        + [("archive", m, dst) for m, dst in
           sorted(archives, key=lambda p: (p[1], p[0].folder, p[0].uid, p[0].msg_key))]
    )

    truncated = 0
    cap = cfg.execution.max_moves_per_run
    if cap and len(ordered) > cap:
        truncated = len(ordered) - cap
        ordered = ordered[:cap]

    items = tuple(
        PlanItem(seq=n, msg_key=m.msg_key, src_folder=m.folder, src_uid=m.uid,
                 src_uidvalidity=m.uidvalidity, dst_folder=dst, reason=reason)
        for n, (reason, m, dst) in enumerate(ordered, start=1)
    )

    skip_counts = Counter(s.reason for s in skips)
    summary = {
        "surveyed": len(messages),
        "eligible": len(eligible_inbox),
        "planned": len(items),
        "backfill": sum(1 for i in items if i.reason == "backfill"),
        "archive": sum(1 for i in items if i.reason == "archive"),
        "promotions": len(new_promotions),
        "truncated_by_cap": truncated,
    }
    summary.update({f"skipped_{k}": v for k, v in skip_counts.items()})

    return PlanResult(items=items, skips=tuple(skips),
                       new_promotions=tuple(new_promotions), counts=summary)
