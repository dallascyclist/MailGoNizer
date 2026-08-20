import pytest

from mailgonizer.config import Config, ExecutionConfig, ServerConfig
from mailgonizer.executor import execute
from mailgonizer.imap import Capabilities, TransientError
from mailgonizer.index import Index
from mailgonizer.records import PlanItem
from mailgonizer.runlog import RunLog


class FakeMailbox:
    def __init__(self, uidvalidity=100, identities=None, fail_moves=0):
        self.uidvalidity = uidvalidity
        self.identities = identities or {}
        self.fail_moves = fail_moves
        self.moves = []
        self.ensured = []
        self.selected = []

    def capabilities(self):
        return Capabilities("/", True, True, {})

    def select(self, folder, readonly=True):
        self.selected.append(folder)
        return self.uidvalidity, 999

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
    return Config(
        server=ServerConfig(host="h", username="u", password="p"),
        execution=ExecutionConfig(**over.pop("execution", {})),
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


def test_a_transient_failure_is_retried_then_succeeds(setup):
    index, log, run_id = setup
    plan(index, run_id, [PlanItem(1, "k1", "INBOX", 10, 100, "dst", "archive")])
    mb = FakeMailbox(identities={10: "k1"}, fail_moves=2)

    result = execute(mb, index, run_id, cfg(execution={"connect_retries": 3}), log)

    assert result.moved == 1


def test_exhausted_retries_become_per_item_failures_with_verbatim_text(setup):
    index, log, run_id = setup
    plan(index, run_id, [PlanItem(1, "k1", "INBOX", 10, 100, "dst", "archive")])
    mb = FakeMailbox(identities={10: "k1"}, fail_moves=99)

    result = execute(mb, index, run_id, cfg(execution={"connect_retries": 2}), log)

    assert result.moved == 0 and result.failed == 1
    assert "connection reset" in index.all_items(run_id)[0]["error"]


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
