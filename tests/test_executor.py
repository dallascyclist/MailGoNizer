import time

import pytest

from mailgonizer.config import Config, ExecutionConfig, ServerConfig
from mailgonizer.executor import execute
from mailgonizer.imap import Capabilities, TransientError
from mailgonizer.index import Index
from mailgonizer.records import PlanItem
from mailgonizer.runlog import RunLog


class FakeMailbox:
    def __init__(self, uidvalidity=100, identities=None, fail_moves=0, uidvalidities=None):
        self.uidvalidity = uidvalidity
        self.uidvalidities = uidvalidities or {}
        self.identities = identities or {}
        self.fail_moves = fail_moves
        self.moves = []
        self.ensured = []
        self.selected = []

    def capabilities(self):
        return Capabilities("/", True, True, {})

    def select(self, folder, readonly=True):
        self.selected.append(folder)
        return self.uidvalidities.get(folder, self.uidvalidity), 999

    def fetch_identity(self, uids):
        return {u: self.identities[u] for u in uids if u in self.identities}

    def ensure_folder(self, path, subscribe):
        self.ensured.append(path)

    def move(self, uids, dst):
        if self.fail_moves > 0:
            self.fail_moves -= 1
            raise TransientError("connection reset")
        self.moves.append((tuple(uids), dst))


def cfg(**over):
    # Real-time sleeps between batches would make the suite slow for no benefit;
    # tests that care about pausing override this explicitly via execution=.
    execution_over = {"pause_between_batches_ms": 0, **over.pop("execution", {})}
    return Config(
        server=ServerConfig(host="h", username="u", password="p"),
        execution=ExecutionConfig(**execution_over),
    )


@pytest.fixture
def setup(tmp_path):
    index = Index.open(tmp_path / "t.sqlite")
    log = RunLog.open(tmp_path, "s", "info")
    run_id = index.start_run("apply", "c", "p", "h")
    yield index, log, run_id
    log.close()


def plan(index, run_id, items):
    index.save_plan(run_id, items, inbox_uidnext=1)


def test_happy_path_moves_and_records(setup):
    index, log, run_id = setup
    plan(index, run_id, [
        PlanItem(1, "k1", "INBOX", 10, 100, "Crono_Archive/2019/a", "archive"),
        PlanItem(2, "k2", "INBOX", 11, 100, "Crono_Archive/2019/a", "archive"),
    ])
    mb = FakeMailbox(identities={10: "k1", 11: "k2"})

    result = execute(mb, index, run_id, cfg(), log)

    assert result.moved == 2 and result.failed == 0
    assert mb.moves == [((10, 11), "Crono_Archive/2019/a")]
    assert mb.ensured == ["Crono_Archive/2019/a"]
    assert index.pending_items(run_id) == []
    assert index.already_moved("k1") and index.already_moved("k2")


def test_uidvalidity_change_refuses_that_folders_items(setup):
    index, log, run_id = setup
    plan(index, run_id, [
        PlanItem(1, "k1", "INBOX", 10, 100, "dst", "archive"),
    ])
    mb = FakeMailbox(uidvalidity=999, identities={10: "k1"})

    result = execute(mb, index, run_id, cfg(), log)

    assert result.moved == 0 and result.failed == 1
    assert mb.moves == []
    row = index.all_items(run_id)[0]
    assert row["state"] == "failed"
    assert "UIDVALIDITY" in row["error"]


def test_uidvalidity_change_in_one_folder_does_not_block_other_folders(setup):
    index, log, run_id = setup
    plan(index, run_id, [
        PlanItem(1, "k1", "Crono_Archive/2019/old", 10, 100, "dstA", "backfill"),
        PlanItem(2, "k2", "INBOX", 20, 100, "dstB", "archive"),
    ])
    mb = FakeMailbox(
        identities={10: "k1", 20: "k2"},
        uidvalidities={"Crono_Archive/2019/old": 999, "INBOX": 100},
    )

    result = execute(mb, index, run_id, cfg(), log)

    assert result.failed == 1 and result.moved == 1
    rows = {r["seq"]: r for r in index.all_items(run_id)}
    assert rows[1]["state"] == "failed"
    assert "UIDVALIDITY" in rows[1]["error"]
    assert rows[2]["state"] == "done"
    assert mb.moves == [((20,), "dstB")]


def test_identity_mismatch_is_skipped_never_moved(setup):
    index, log, run_id = setup
    plan(index, run_id, [
        PlanItem(1, "k1", "INBOX", 10, 100, "dst", "archive"),
        PlanItem(2, "k2", "INBOX", 11, 100, "dst", "archive"),
    ])
    mb = FakeMailbox(identities={10: "SOMETHING-ELSE", 11: "k2"})

    result = execute(mb, index, run_id, cfg(), log)

    assert result.moved == 1 and result.skipped == 1
    assert mb.moves == [((11,), "dst")]
    states = {r["seq"]: r["state"] for r in index.all_items(run_id)}
    assert states == {1: "skipped", 2: "done"}


def test_a_vanished_message_is_skipped(setup):
    index, log, run_id = setup
    plan(index, run_id, [PlanItem(1, "k1", "INBOX", 10, 100, "dst", "archive")])
    mb = FakeMailbox(identities={})

    result = execute(mb, index, run_id, cfg(), log)

    assert result.skipped == 1 and mb.moves == []
    assert index.all_items(run_id)[0]["error"] == "vanished"


def test_an_already_moved_message_is_not_moved_twice(setup):
    index, log, run_id = setup
    plan(index, run_id, [PlanItem(1, "k1", "INBOX", 10, 100, "dst", "archive")])
    index.mark_done(run_id, 1, "<a@b>", 5)

    plan(index, run_id, [PlanItem(1, "k1", "INBOX", 10, 100, "dst", "archive")])
    mb = FakeMailbox(identities={10: "k1"})
    result = execute(mb, index, run_id, cfg(), log)

    assert result.skipped == 1 and mb.moves == []


def test_a_message_already_moved_once_can_still_move_to_a_new_destination(setup):
    """Regression: a promotion backfill moves mail that a previous run already
    archived once (INBOX -> flat domain folder) onward to a newly-promoted
    per-sender folder. That second, different-destination move must happen —
    `already_moved` must not treat "moved somewhere, ever" as "done forever"."""
    index, log, first_run = setup
    plan(index, first_run, [PlanItem(1, "k1", "INBOX", 10, 100, "flat", "archive")])
    index.mark_done(first_run, 1, "<a@b>", 5)

    second_run = index.start_run("apply", "c", "p", "h")
    plan(index, second_run, [PlanItem(1, "k1", "flat", 10, 100, "promoted", "backfill")])
    mb = FakeMailbox(identities={10: "k1"})
    result = execute(mb, index, second_run, cfg(), log)

    assert result.moved == 1 and result.skipped == 0
    assert mb.moves == [((10,), "promoted")]


def test_a_transient_failure_is_retried_then_succeeds(setup, monkeypatch):
    index, log, run_id = setup
    plan(index, run_id, [PlanItem(1, "k1", "INBOX", 10, 100, "dst", "archive")])
    mb = FakeMailbox(identities={10: "k1"}, fail_moves=2)
    sleeps = []
    monkeypatch.setattr(time, "sleep", lambda seconds: sleeps.append(seconds))

    result = execute(mb, index, run_id, cfg(execution={"connect_retries": 3}), log)

    assert result.moved == 1
    # Two failed attempts (of three) each sleep before retrying, backing off
    # exponentially; the third attempt succeeds without sleeping.
    assert sleeps == [2, 4]


def test_exhausted_retries_become_per_item_failures_with_verbatim_text(setup, monkeypatch):
    index, log, run_id = setup
    plan(index, run_id, [PlanItem(1, "k1", "INBOX", 10, 100, "dst", "archive")])
    mb = FakeMailbox(identities={10: "k1"}, fail_moves=99)
    sleeps = []
    monkeypatch.setattr(time, "sleep", lambda seconds: sleeps.append(seconds))

    result = execute(mb, index, run_id, cfg(execution={"connect_retries": 3}), log)

    assert result.moved == 0 and result.failed == 1
    assert "connection reset" in index.all_items(run_id)[0]["error"]
    # All three attempts fail; the first two sleep before retrying (backing off
    # exponentially), the last does not (nothing left to retry into).
    assert sleeps == [2, 4]


def test_reconnect_is_propagated_to_later_batches(setup, monkeypatch):
    index, log, run_id = setup
    plan(index, run_id, [
        PlanItem(1, "k1", "INBOX", 10, 100, "dst", "archive"),
        PlanItem(2, "k2", "INBOX", 11, 100, "dst", "archive"),
    ])
    monkeypatch.setattr(time, "sleep", lambda seconds: None)

    original = FakeMailbox(identities={10: "k1", 11: "k2"}, fail_moves=1)
    replacement = FakeMailbox(identities={10: "k1", 11: "k2"})

    result = execute(
        original, index, run_id,
        cfg(execution={"batch_size": 1, "connect_retries": 2}),
        log, reconnect=lambda: replacement,
    )

    assert result.moved == 2
    # The first batch's only successful attempt happens on the replacement
    # (the original's single call raised and was never retried on itself).
    # The second batch must also land on the replacement: if execute() kept
    # using its stale `mailbox` reference after the reconnect, this batch's
    # fetch_identity/move calls would silently go to the original instead.
    assert original.moves == []
    assert replacement.moves == [((10,), "dst"), ((11,), "dst")]


def test_resumption_only_touches_pending_items(setup):
    index, log, run_id = setup
    plan(index, run_id, [
        PlanItem(1, "k1", "INBOX", 10, 100, "dst", "archive"),
        PlanItem(2, "k2", "INBOX", 11, 100, "dst", "archive"),
    ])
    index.mark_done(run_id, 1, "<a@b>", 5)
    mb = FakeMailbox(identities={10: "k1", 11: "k2"})

    result = execute(mb, index, run_id, cfg(), log)

    assert result.moved == 1
    assert mb.moves == [((11,), "dst")]


def test_batches_respect_batch_size(setup):
    index, log, run_id = setup
    items = [PlanItem(n, f"k{n}", "INBOX", n, 100, "dst", "archive")
             for n in range(1, 6)]
    plan(index, run_id, items)
    mb = FakeMailbox(identities={n: f"k{n}" for n in range(1, 6)})

    execute(mb, index, run_id, cfg(execution={"batch_size": 2}), log)

    assert [len(uids) for uids, _ in mb.moves] == [2, 2, 1]


def test_items_are_grouped_by_destination(setup):
    index, log, run_id = setup
    plan(index, run_id, [
        PlanItem(1, "k1", "INBOX", 10, 100, "dstA", "archive"),
        PlanItem(2, "k2", "INBOX", 11, 100, "dstB", "archive"),
        PlanItem(3, "k3", "INBOX", 12, 100, "dstA", "archive"),
    ])
    mb = FakeMailbox(identities={10: "k1", 11: "k2", 12: "k3"})

    execute(mb, index, run_id, cfg(), log)

    by_dst = {dst: uids for uids, dst in mb.moves}
    assert by_dst == {"dstA": (10, 12), "dstB": (11,)}


def test_creating_a_destination_folder_records_it_in_the_index(setup):
    index, log, run_id = setup
    plan(index, run_id, [
        PlanItem(1, "k1", "INBOX", 10, 100, "Crono_Archive/2019/a", "archive"),
    ])
    mb = FakeMailbox(identities={10: "k1"})

    execute(mb, index, run_id, cfg(), log)

    row = index.conn.execute(
        "SELECT * FROM folders WHERE name=?", ("Crono_Archive/2019/a",)
    ).fetchone()
    assert row is not None
    assert row["created_run"] == run_id
