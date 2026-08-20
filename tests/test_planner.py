from datetime import datetime, timedelta, timezone

from mailgonizer.config import (
    ArchiveConfig, Config, ExclusionsConfig, ExecutionConfig, ServerConfig,
)
from mailgonizer.planner import build_plan
from mailgonizer.records import ClassifiedMessage, SenderKey

NOW = datetime(2026, 8, 19, 12, 0, tzinfo=timezone.utc)
OLD = NOW - timedelta(days=400)
RECENT = NOW - timedelta(days=10)


def cfg(**over):
    return Config(
        server=ServerConfig(host="h", username="u", password="p"),
        archive=ArchiveConfig(**over.pop("archive", {})),
        exclusions=ExclusionsConfig(**over.pop("exclusions", {})),
        execution=ExecutionConfig(**over.pop("execution", {})),
    )


_counter = [0]


def msg(domain="amazon.com", local="orders", folder="INBOX", when=OLD,
        flags=(), kind="domain", list_id=None, uid=None):
    _counter[0] += 1
    n = _counter[0]
    return ClassifiedMessage(
        msg_key=f"key{n:04d}", message_id=f"<{n}@x>", folder=folder,
        uid=uid if uid is not None else n, uidvalidity=100,
        internaldate=when, size=1000, resolved_date=when, date_source="date",
        year=when.year,
        sender=SenderKey(kind, list_id, domain if kind == "domain" else None,
                         local if kind == "domain" else None, "from"),
        flags=tuple(flags),
    )


def dests(result):
    return [(i.reason, i.src_folder, i.dst_folder) for i in result.items]


# --- eligibility ----------------------------------------------------------

def test_old_inbox_mail_is_planned_for_archive():
    r = build_plan([msg()], cfg(), "/", set(), NOW)
    assert dests(r) == [("archive", "INBOX", f"Crono_Archive/{OLD.year}/amazon_com")]


def test_recent_mail_stays_put():
    r = build_plan([msg(when=RECENT)], cfg(), "/", set(), NOW)
    assert r.items == ()
    assert [s.reason for s in r.skips] == ["too_recent"]


def test_flagged_mail_stays_when_keep_flagged_is_on():
    r = build_plan([msg(flags=("\\Flagged",))], cfg(), "/", set(), NOW)
    assert r.items == ()
    assert [s.reason for s in r.skips] == ["flagged"]


def test_flagged_mail_moves_when_keep_flagged_is_off():
    r = build_plan([msg(flags=("\\Flagged",))],
                   cfg(exclusions={"keep_flagged": False}), "/", set(), NOW)
    assert len(r.items) == 1


def test_deleted_mail_is_always_skipped_even_with_keep_flagged_off():
    r = build_plan([msg(flags=("\\Deleted",))],
                   cfg(exclusions={"keep_flagged": False}), "/", set(), NOW)
    assert r.items == ()
    assert [s.reason for s in r.skips] == ["deleted"]


def test_never_archive_is_honoured():
    r = build_plan([msg()],
                   cfg(exclusions={"never_archive": ("amazon.com",)}),
                   "/", set(), NOW)
    assert r.items == ()
    assert [s.reason for s in r.skips] == ["never_archive"]


def test_a_message_already_at_its_destination_is_not_moved():
    m = msg(folder=f"Crono_Archive/{OLD.year}/amazon_com")
    r = build_plan([m], cfg(), "/", set(), NOW)
    assert r.items == ()
    assert [s.reason for s in r.skips] == ["in_place"]


# --- promotion ------------------------------------------------------------

def test_exactly_the_threshold_does_not_promote():
    msgs = [msg() for _ in range(13)]
    r = build_plan(msgs, cfg(), "/", set(), NOW)
    assert r.new_promotions == ()
    assert all(i.dst_folder.endswith("amazon_com") for i in r.items)


def test_one_over_the_threshold_promotes():
    msgs = [msg() for _ in range(14)]
    r = build_plan(msgs, cfg(), "/", set(), NOW)
    assert r.new_promotions == ((OLD.year, "amazon.com", "orders", 14),)
    assert all(i.dst_folder.endswith("amazon_com/orders") for i in r.items)


def test_promotion_counts_archived_and_inbox_mail_together():
    archived = [msg(folder=f"Crono_Archive/{OLD.year}/amazon_com")
                for _ in range(10)]
    inbox = [msg() for _ in range(4)]
    r = build_plan(archived + inbox, cfg(), "/", set(), NOW)
    assert r.new_promotions == ((OLD.year, "amazon.com", "orders", 14),)


def test_promotion_backfills_already_archived_mail():
    archived = [msg(folder=f"Crono_Archive/{OLD.year}/amazon_com")
                for _ in range(10)]
    inbox = [msg() for _ in range(4)]
    r = build_plan(archived + inbox, cfg(), "/", set(), NOW)
    reasons = [i.reason for i in r.items]
    assert reasons.count("backfill") == 10
    assert reasons.count("archive") == 4


def test_backfill_is_ordered_before_archive():
    archived = [msg(folder=f"Crono_Archive/{OLD.year}/amazon_com")
                for _ in range(10)]
    inbox = [msg() for _ in range(4)]
    r = build_plan(archived + inbox, cfg(), "/", set(), NOW)
    reasons = [i.reason for i in r.items]
    assert reasons == ["backfill"] * 10 + ["archive"] * 4
    assert [i.seq for i in r.items] == list(range(1, 15))


def test_the_ratchet_holds_when_the_count_later_drops():
    existing = {(OLD.year, "amazon.com", "orders")}
    r = build_plan([msg()], cfg(), "/", existing, NOW)
    assert r.items[0].dst_folder.endswith("amazon_com/orders")
    assert r.new_promotions == ()


def test_ineligible_inbox_mail_does_not_count_toward_promotion():
    """Flagged mail stays in the inbox, so it must not push a sender over."""
    msgs = [msg() for _ in range(13)] + [msg(flags=("\\Flagged",)) for _ in range(5)]
    r = build_plan(msgs, cfg(), "/", set(), NOW)
    assert r.new_promotions == ()


def test_promotion_is_per_year():
    y2018 = datetime(2018, 6, 1, tzinfo=timezone.utc)
    y2019 = datetime(2019, 6, 1, tzinfo=timezone.utc)
    msgs = [msg(when=y2018) for _ in range(14)] + [msg(when=y2019) for _ in range(3)]
    r = build_plan(msgs, cfg(), "/", set(), NOW)
    assert r.new_promotions == ((2018, "amazon.com", "orders", 14),)
    promoted = [i for i in r.items if i.dst_folder.endswith("/orders")]
    assert len(promoted) == 14


def test_promotion_is_per_local_part():
    msgs = [msg(local="orders") for _ in range(14)] + \
           [msg(local="deals") for _ in range(3)]
    r = build_plan(msgs, cfg(), "/", set(), NOW)
    assert r.new_promotions == ((OLD.year, "amazon.com", "orders", 14),)


def test_lists_never_promote():
    msgs = [msg(kind="list", list_id="dovecot.dovecot.org") for _ in range(50)]
    r = build_plan(msgs, cfg(), "/", set(), NOW)
    assert r.new_promotions == ()
    assert all(
        i.dst_folder == f"Crono_Archive/{OLD.year}/lists/dovecot_dovecot_org"
        for i in r.items
    )


def test_unknown_senders_never_promote():
    msgs = [msg(kind="unknown") for _ in range(50)]
    r = build_plan(msgs, cfg(), "/", set(), NOW)
    assert r.new_promotions == ()
    assert all(i.dst_folder.endswith("_unknown") for i in r.items)


# --- ordering, caps, determinism -----------------------------------------

def test_max_moves_per_run_truncates_and_is_reported():
    msgs = [msg() for _ in range(10)]
    r = build_plan(msgs, cfg(execution={"max_moves_per_run": 4}), "/", set(), NOW)
    assert len(r.items) == 4
    assert r.counts["truncated_by_cap"] == 6
    assert [i.seq for i in r.items] == [1, 2, 3, 4]


def test_planning_is_deterministic():
    msgs = [msg(local=f"user{i}") for i in range(20)]
    a = build_plan(msgs, cfg(), "/", set(), NOW)
    b = build_plan(list(reversed(msgs)), cfg(), "/", set(), NOW)
    assert dests(a) == dests(b)
    assert [i.msg_key for i in a.items] == [i.msg_key for i in b.items]


def test_counts_summarise_the_run():
    msgs = [msg(), msg(when=RECENT), msg(flags=("\\Flagged",))]
    r = build_plan(msgs, cfg(), "/", set(), NOW)
    assert r.counts["surveyed"] == 3
    assert r.counts["planned"] == 1
    assert r.counts["skipped_too_recent"] == 1
    assert r.counts["skipped_flagged"] == 1


def test_planning_fifty_thousand_messages_is_fast():
    import time
    msgs = [msg(domain=f"d{i % 900}.com", local=f"u{i % 60}")
            for i in range(50_000)]
    start = time.monotonic()
    r = build_plan(msgs, cfg(), "/", set(), NOW)
    elapsed = time.monotonic() - start
    assert len(r.items) > 0
    assert elapsed < 10.0, f"planner took {elapsed:.1f}s for 50k messages"
