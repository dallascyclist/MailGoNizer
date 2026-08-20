from datetime import datetime, timezone

import pytest

from mailgonizer.index import Index
from mailgonizer.records import ClassifiedMessage, PlanItem, SenderKey

NOW = datetime(2026, 8, 19, tzinfo=timezone.utc)


def classified(msg_key, folder="INBOX", uid=1, year=2019, domain="amazon.com",
               local="orders"):
    return ClassifiedMessage(
        msg_key=msg_key, message_id=f"<{msg_key}@x>", folder=folder, uid=uid,
        uidvalidity=100, internaldate=NOW, size=1000, resolved_date=NOW,
        date_source="date", year=year,
        sender=SenderKey("domain", None, domain, local, "from"), flags=(),
    )


@pytest.fixture
def idx(tmp_path):
    with Index.open(tmp_path / "t.sqlite") as index:
        yield index


def test_schema_is_created_and_wal_enabled(idx):
    mode = idx.conn.execute("PRAGMA journal_mode").fetchone()[0]
    assert mode.lower() == "wal"
    tables = {
        r[0] for r in idx.conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table'"
        )
    }
    assert {"messages", "folders", "moves", "promotions", "runs",
            "plan_items"} <= tables


def test_run_lifecycle(idx):
    run_id = idx.start_run("plan", "cfghash", "psl-v1", "mail.example.com")
    assert run_id == 1
    idx.finish_run(run_id, "ok", {"moved": 5})
    row = idx.last_run()
    assert row["status"] == "ok"
    assert row["psl_version"] == "psl-v1"
    assert row["finished_at"] is not None


def test_upsert_is_idempotent(idx):
    idx.start_run("plan", "c", "p", "h")
    idx.upsert_messages([classified("k1")], run_id=1)
    idx.upsert_messages([classified("k1", folder="Crono_Archive/2019/amazon_com")],
                        run_id=1)
    rows = list(idx.iter_messages())
    assert len(rows) == 1
    assert rows[0]["current_folder"] == "Crono_Archive/2019/amazon_com"


def test_promotions_are_recorded_and_read_back(idx):
    idx.start_run("plan", "c", "p", "h")
    assert idx.known_promotions() == set()
    idx.record_promotion(2019, "amazon.com", "orders", run_id=1, trigger_count=14)
    assert idx.known_promotions() == {(2019, "amazon.com", "orders")}


def test_recording_the_same_promotion_twice_does_not_duplicate(idx):
    idx.start_run("plan", "c", "p", "h")
    idx.record_promotion(2019, "amazon.com", "orders", 1, 14)
    idx.record_promotion(2019, "amazon.com", "orders", 1, 20)
    count = idx.conn.execute("SELECT COUNT(*) FROM promotions").fetchone()[0]
    assert count == 1
    # First write wins: the ratchet records when it fired, not the latest count.
    assert idx.conn.execute(
        "SELECT trigger_count FROM promotions"
    ).fetchone()[0] == 14


def test_plan_round_trips_and_pending_items_are_ordered(idx):
    run_id = idx.start_run("plan", "c", "p", "h")
    items = [
        PlanItem(2, "k2", "INBOX", 11, 100, "Crono_Archive/2019/b", "archive"),
        PlanItem(1, "k1", "INBOX", 10, 100, "Crono_Archive/2019/a", "backfill"),
    ]
    idx.save_plan(run_id, items, inbox_uidnext=500)
    pending = idx.pending_items(run_id)
    assert [p.seq for p in pending] == [1, 2]
    assert pending[0].reason == "backfill"
    assert pending[0].src_uidvalidity == 100


def test_mark_done_writes_state_and_move_log_in_one_transaction(idx):
    run_id = idx.start_run("apply", "c", "p", "h")
    idx.save_plan(run_id, [PlanItem(1, "k1", "INBOX", 10, 100, "dst", "archive")],
                  inbox_uidnext=1)
    idx.mark_done(run_id, seq=1, message_id="<a@b>", dst_uid=77)

    assert idx.pending_items(run_id) == []
    move = idx.conn.execute("SELECT * FROM moves").fetchone()
    assert move["src_folder"] == "INBOX"
    assert move["dst_folder"] == "dst"
    assert move["dst_uid"] == 77
    assert idx.already_moved("k1")


def test_mark_done_on_an_unknown_item_raises_and_writes_nothing(idx):
    run_id = idx.start_run("apply", "c", "p", "h")
    idx.save_plan(run_id, [PlanItem(1, "k1", "INBOX", 10, 100, "dst", "archive")],
                  inbox_uidnext=1)
    with pytest.raises(KeyError):
        idx.mark_done(run_id, seq=99, message_id=None, dst_uid=None)
    assert idx.conn.execute("SELECT COUNT(*) FROM moves").fetchone()[0] == 0


def test_failures_record_the_verbatim_server_response(idx):
    run_id = idx.start_run("apply", "c", "p", "h")
    idx.save_plan(run_id, [PlanItem(1, "k1", "INBOX", 10, 100, "dst", "archive")],
                  inbox_uidnext=1)
    idx.mark_failed(run_id, 1, "NO [TRYCREATE] Mailbox does not exist")
    row = idx.conn.execute("SELECT state, error FROM plan_items").fetchone()
    assert row["state"] == "failed"
    assert row["error"] == "NO [TRYCREATE] Mailbox does not exist"
    assert idx.pending_items(run_id) == []


def test_clear_cache_keeps_the_permanent_record(idx):
    run_id = idx.start_run("run", "c", "p", "h")
    idx.upsert_messages([classified("k1")], run_id=run_id)
    idx.record_promotion(2019, "amazon.com", "orders", run_id, 14)
    idx.save_plan(run_id, [PlanItem(1, "k1", "INBOX", 10, 100, "dst", "archive")],
                  inbox_uidnext=1)
    idx.mark_done(run_id, 1, "<a@b>", 5)

    idx.clear_cache()

    assert list(idx.iter_messages()) == []
    assert idx.conn.execute("SELECT COUNT(*) FROM moves").fetchone()[0] == 1
    assert idx.known_promotions() == {(2019, "amazon.com", "orders")}
    assert idx.last_run() is not None


def test_moves_for_run_returns_reversal_material(idx):
    run_id = idx.start_run("run", "c", "p", "h")
    idx.save_plan(run_id, [PlanItem(1, "k1", "INBOX", 10, 100, "dst", "archive")],
                  inbox_uidnext=1)
    idx.mark_done(run_id, 1, "<a@b>", 5)
    moves = idx.moves_for_run(run_id)
    assert len(moves) == 1
    assert (moves[0]["src_folder"], moves[0]["dst_folder"]) == ("INBOX", "dst")
