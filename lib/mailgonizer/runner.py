"""Phase wiring. Owns no rules of its own."""

from __future__ import annotations

import hashlib
import json
from dataclasses import asdict
from datetime import datetime, timezone

from mailgonizer.config import Config
from mailgonizer.dates import resolve_date
from mailgonizer.executor import ExecutionResult, execute
from mailgonizer.imap import FatalError
from mailgonizer.index import Index
from mailgonizer.planner import PlanResult, build_plan
from mailgonizer.psl import PublicSuffixList
from mailgonizer.records import ClassifiedMessage, HeaderRecord, compute_msg_key
from mailgonizer.runlog import Progress, RunLog
from mailgonizer.sender import derive_sender


def config_hash(cfg: Config) -> str:
    """Fingerprint the settings that change behaviour. Never the password."""
    payload = {
        section: asdict(getattr(cfg, section))
        for section in ("source", "archive", "naming", "dates", "exclusions")
    }
    payload["server"] = {"host": cfg.server.host, "username": cfg.server.username}
    payload["dates"]["min_valid"] = str(payload["dates"]["min_valid"])
    return hashlib.sha256(
        json.dumps(payload, sort_keys=True, default=str).encode()
    ).hexdigest()[:16]


def classify(records: list[HeaderRecord], cfg: Config, psl: PublicSuffixList,
             now: datetime, log: RunLog | None = None) -> list[ClassifiedMessage]:
    out = []
    progress = Progress(log, "CLASSIFY", total=len(records)) if log else None
    for rec in records:
        if progress:
            progress.tick()
        resolved, source = resolve_date(rec.headers, rec.internaldate, cfg, now)
        message_id = rec.headers.get("message-id")
        message_id = str(message_id).strip() if message_id else None
        out.append(ClassifiedMessage(
            msg_key=compute_msg_key(message_id, rec.internaldate, rec.size),
            message_id=message_id, folder=rec.folder, uid=rec.uid,
            uidvalidity=rec.uidvalidity, internaldate=rec.internaldate,
            size=rec.size, resolved_date=resolved, date_source=source,
            year=resolved.year, sender=derive_sender(rec.headers, psl),
            flags=rec.flags,
        ))
    if progress:
        progress.done()
    return out


def survey(mailbox, cfg: Config, log: RunLog) -> list[HeaderRecord]:
    """Sweep INBOX and the existing archive tree. Headers only, always PEEK."""
    log.phase("SURVEY")

    # A twenty-year mailbox makes this phase run for hours. Report against a
    # real denominator so an operator can tell a working run from a hung one.
    folders = [cfg.source.folder] + list(mailbox.list_folders(prefix=cfg.archive.root))
    totals = {f: mailbox.message_count(f) for f in folders}
    grand_total = sum(totals.values())
    log.info(f"{len(folders)} folders, {grand_total:,} messages to survey "
             f"({cfg.source.folder}: {totals[cfg.source.folder]:,})")

    records: list[HeaderRecord] = []
    overall = Progress(log, "SURVEY total", total=grand_total)
    for folder in folders:
        per_folder = Progress(log, f"SURVEY {folder}", total=totals[folder])
        found = 0
        for rec in mailbox.fetch_headers(folder):
            records.append(rec)
            found += 1
            per_folder.tick()
            overall.tick()
        per_folder.done()
        log.info(f"{folder}: {found:,} messages")

    overall.done()
    log.info(f"surveyed {len(records):,} messages total")
    return records


def do_check(mailbox, cfg: Config, log: RunLog,
             last_psl_version: str | None = None) -> int:
    """Validate the environment. Touches neither the mailbox nor the
    database (it does write its own run log).

    `last_psl_version` is passed in rather than read here, because opening the
    database would create the file, and `check` must not touch the database.
    """
    log.phase("CHECK")

    bundled = PublicSuffixList.bundled()
    log.info(f"public suffix list: {bundled.version}")
    if last_psl_version and last_psl_version != bundled.version:
        log.warn(
            f"public suffix list changed since the last run "
            f"({last_psl_version} -> {bundled.version}). A sender may now "
            "resolve to a different registrable domain than before, which "
            "would split an existing folder. Review before running."
        )
    try:
        caps = mailbox.capabilities()
        log.info(f"delimiter={caps.delimiter!r} MOVE={caps.has_move} "
                 f"UIDPLUS={caps.has_uidplus}")
        mailbox.assert_safe()
    except FatalError as exc:
        log.error(str(exc))
        return 1

    if caps.delimiter == ".":
        log.warn(
            "server delimiter is '.'; sender domains must not contain literal "
            "dots. naming.domain_separator handles this — confirm it is not '.'"
        )
    archive_special = caps.special_use.get("\\archive")
    if archive_special:
        log.info(
            f"server designates {archive_special!r} as the \\Archive special-use "
            f"folder; archive.root is {cfg.archive.root!r}"
        )
        if archive_special == cfg.archive.root:
            log.warn(
                "archive.root collides with the \\Archive special-use folder. "
                "Client Archive buttons will drop loose mail into the root of "
                "the tree. Choose a different archive.root."
            )
    log.info("check passed")
    return 0


def do_plan(mailbox, index: Index, cfg: Config, psl: PublicSuffixList,
            log: RunLog, now: datetime | None = None) -> tuple[int, PlanResult]:
    now = now or datetime.now(timezone.utc)
    mailbox.assert_safe()
    delimiter = mailbox.capabilities().delimiter

    run_id = index.start_run("plan", config_hash(cfg), psl.version, cfg.server.host)
    records = survey(mailbox, cfg, log)

    log.phase("CLASSIFY")
    messages = classify(records, cfg, psl, now, log)
    index.upsert_messages(messages, run_id)

    log.phase("PLAN")
    result = build_plan(messages, cfg, delimiter, index.known_promotions(), now)

    for year, domain, local, count in result.new_promotions:
        index.record_promotion(year, domain, local, run_id, count)
        log.info(f"promote {domain}/{local} in {year} ({count} messages)")

    _, inbox_uidnext = mailbox.select(cfg.source.folder, readonly=True)
    index.save_plan(run_id, result.items, inbox_uidnext=inbox_uidnext)

    for skip in result.skips:
        # "in_place" fires for every already-correctly-placed archived
        # message, on every run, because promotion counting requires
        # sweeping the whole archive tree. On a 300k-message archive that is
        # 300k JSONL records a month for a decision nobody needs to review —
        # the message is already exactly where it belongs. Suppress the
        # per-message record unless debugging; result.counts still reports
        # the total regardless, so "why did this message go there" stays
        # answerable on demand via --verbose, while routine monthly logs
        # stay proportional to the work actually done rather than the size
        # of the archive.
        if skip.reason == "in_place" and not log.debug_enabled:
            continue
        log.decision(msg_key=skip.msg_key, folder=skip.folder, state="skipped",
                     reason=skip.reason, skipped=True)
    for item in result.items:
        log.decision(msg_key=item.msg_key, seq=item.seq, state="planned",
                     src=item.src_folder, dst=item.dst_folder, reason=item.reason)

    log.info(f"planned {len(result.items)} moves as run {run_id}")
    if result.counts.get("truncated_by_cap"):
        log.warn(f"{result.counts['truncated_by_cap']} moves withheld by "
                 "execution.max_moves_per_run")
    return run_id, result


def do_apply(mailbox, index: Index, cfg: Config, log: RunLog, run_id: int,
             reconnect=None) -> ExecutionResult:
    log.phase("EXECUTE")
    mailbox.assert_safe()
    return execute(mailbox, index, run_id, cfg, log, reconnect=reconnect)
