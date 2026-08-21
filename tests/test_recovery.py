import csv
import io
import json

import pytest

from mailgonizer.config import Config, ExecutionConfig, ServerConfig
from mailgonizer.imap import Capabilities, TransientError, UnsafeServerError
from mailgonizer.index import Index
from mailgonizer.psl import PublicSuffixList
from mailgonizer.records import PlanItem
from mailgonizer.recovery import export_index, undo_run
from mailgonizer.runlog import RunLog


def psl():
    return PublicSuffixList(["com", "co.uk"])


class FakeMailbox:
    def __init__(self, contents=None, poison=()):
        # folder -> {uid: msg_key}
        self.contents = contents or {}
        self.moves = []
        # UIDs the server permanently refuses to move.
        self.poison = set(poison)
        # How each folder was selected, and how big each FETCH was.
        self.list_readonly = {}
        self.fetch_sizes = []

    def capabilities(self):
        return Capabilities("/", True, True, {})

    def assert_safe(self):
        pass

    def select(self, folder, readonly=True):
        return 100, 500

    def list_uids(self, folder, readonly=True):
        self.list_readonly[folder] = readonly
        return sorted(self.contents.get(folder, {}))

    def fetch_identity(self, uids):
        self.fetch_sizes.append(len(uids))
        merged = {}
        for mapping in self.contents.values():
            merged.update(mapping)
        return {u: merged[u] for u in uids if u in merged}

    def ensure_folder(self, path, subscribe):
        pass

    def move(self, uids, dst):
        bad = sorted(self.poison.intersection(uids))
        if bad:
            raise TransientError(f"NO [CANNOT] uid {bad[0]} is unreadable")
        self.moves.append((tuple(uids), dst))


class UnsafeMailbox(FakeMailbox):
    """Reports neither MOVE nor UIDPLUS -- exactly what assert_safe() refuses."""

    def capabilities(self):
        return Capabilities("/", False, False, {})

    def assert_safe(self):
        raise UnsafeServerError("neither MOVE nor UIDPLUS")


def cfg():
    return Config(
        server=ServerConfig(host="h", username="u", password="p"),
        execution=ExecutionConfig(pause_between_batches_ms=0),
    )


@pytest.fixture
def prepared(tmp_path):
    index = Index.open(tmp_path / "t.sqlite")
    run_id = index.start_run("run", "c", "p", "h")
    index.save_plan(run_id, [
        PlanItem(1, "k1", "INBOX", 10, 100, "Crono_Archive/2019/a", "archive"),
        PlanItem(2, "k2", "INBOX", 11, 100, "Crono_Archive/2019/a", "archive"),
    ], inbox_uidnext=1)
    index.mark_done(run_id, 1, "<a@x>", None)
    index.mark_done(run_id, 2, "<b@x>", None)
    log = RunLog.open(tmp_path, "s", "info")
    yield index, log, run_id
    log.close()


def test_undo_moves_messages_back_to_their_source(prepared):
    index, log, run_id = prepared
    mb = FakeMailbox({"Crono_Archive/2019/a": {50: "k1", 51: "k2"}})

    result = undo_run(mb, index, cfg(), psl(), log, run_id)

    assert result.moved == 2
    assert mb.moves == [((50, 51), "INBOX")]


def test_undo_appends_reversals_and_never_rewrites_history(prepared):
    index, log, run_id = prepared
    mb = FakeMailbox({"Crono_Archive/2019/a": {50: "k1", 51: "k2"}})

    undo_run(mb, index, cfg(), psl(), log, run_id)

    original = index.moves_for_run(run_id)
    assert len(original) == 2
    assert all(m["dst_folder"] == "Crono_Archive/2019/a" for m in original)

    total = index.conn.execute("SELECT COUNT(*) FROM moves").fetchone()[0]
    assert total == 4
    reversal_run = index.last_run()["run_id"]
    reversals = index.moves_for_run(reversal_run)
    assert [m["dst_folder"] for m in reversals] == ["INBOX", "INBOX"]


def test_undo_skips_messages_that_are_no_longer_where_the_log_says(prepared):
    index, log, run_id = prepared
    mb = FakeMailbox({"Crono_Archive/2019/a": {50: "k1"}})

    result = undo_run(mb, index, cfg(), psl(), log, run_id)

    assert result.moved == 1 and result.skipped == 1
    assert mb.moves == [((50,), "INBOX")]


def test_undo_does_not_reverse_promotions(prepared):
    index, log, run_id = prepared
    index.record_promotion(2019, "amazon.com", "orders", run_id, 14)
    mb = FakeMailbox({"Crono_Archive/2019/a": {50: "k1", 51: "k2"}})

    undo_run(mb, index, cfg(), psl(), log, run_id)

    assert index.known_promotions() == {(2019, "amazon.com", "orders")}


def test_undo_refuses_an_unsafe_server_and_does_nothing(prepared):
    index, log, run_id = prepared
    before = index.conn.execute("SELECT COUNT(*) FROM moves").fetchone()[0]
    mb = UnsafeMailbox({"Crono_Archive/2019/a": {50: "k1", 51: "k2"}})

    with pytest.raises(UnsafeServerError):
        undo_run(mb, index, cfg(), psl(), log, run_id)

    assert mb.moves == []
    after = index.conn.execute("SELECT COUNT(*) FROM moves").fetchone()[0]
    assert after == before


def test_undo_of_a_run_with_no_moves_is_a_no_op(tmp_path):
    with Index.open(tmp_path / "t.sqlite") as index, \
            RunLog.open(tmp_path, "s", "info") as log:
        run_id = index.start_run("run", "c", "p", "h")
        result = undo_run(FakeMailbox(), index, cfg(), psl(), log, run_id)
        assert result == type(result)()


def test_undo_selects_the_source_folder_read_write(prepared):
    """RFC 3501 permits no permanent-state changes under EXAMINE, and undo
    moves mail out of the folder it just listed. Dovecot tolerates the
    violation, so only an explicit assertion catches it before CommuniGate
    Pro does."""
    index, log, run_id = prepared
    mb = FakeMailbox({"Crono_Archive/2019/a": {50: "k1", 51: "k2"}})

    undo_run(mb, index, cfg(), psl(), log, run_id)

    assert mb.list_readonly == {"Crono_Archive/2019/a": False}


def test_undo_chunks_the_identity_fetch_by_batch_size(prepared):
    """IMAPClient joins UIDs with "," and collapses no ranges, so an unchunked
    FETCH of a first run's 20,000 messages overruns Dovecot's 64 KB
    imap_max_line_length."""
    index, log, run_id = prepared
    contents = {50: "k1", 51: "k2"}
    # Fill the folder with unrelated mail so there is something to chunk.
    contents.update({uid: f"other{uid}" for uid in range(100, 350)})
    mb = FakeMailbox({"Crono_Archive/2019/a": contents})

    cfg_small = Config(
        server=ServerConfig(host="h", username="u", password="p"),
        execution=ExecutionConfig(pause_between_batches_ms=0, batch_size=100),
    )
    result = undo_run(mb, index, cfg_small, psl(), log, run_id)

    assert result.moved == 2
    assert max(mb.fetch_sizes) <= 100
    assert sum(mb.fetch_sizes) == len(contents)


def test_undo_records_a_failed_message_and_keeps_going(prepared):
    """A per-item NO must not become an uncaught traceback -- `main` catches
    only FatalError, and TransientError is not a subclass -- nor strand the
    reversal run at status='running' forever."""
    index, log, run_id = prepared
    mb = FakeMailbox({"Crono_Archive/2019/a": {50: "k1", 51: "k2"}}, poison={50})

    result = undo_run(mb, index, cfg(), psl(), log, run_id)

    assert result.moved == 1 and result.failed == 1
    assert mb.moves == [((51,), "INBOX")]
    # The good message is logged as reversed; the bad one is not.
    reversal = index.last_run()
    assert [m["msg_key"] for m in index.moves_for_run(reversal["run_id"])] == ["k2"]
    assert reversal["status"] == "failed"
    assert reversal["finished_at"] is not None


def test_undo_stamps_the_psl_version_so_drift_detection_survives(prepared):
    """cli.py reads last_psl from the newest run row. A blank psl_version here
    would silently disable runner.py's mismatch warning from the first undo
    onward -- and first-run.md presents undo as part of the normal loop."""
    index, log, run_id = prepared
    mb = FakeMailbox({"Crono_Archive/2019/a": {50: "k1", 51: "k2"}})

    undo_run(mb, index, cfg(), psl(), log, run_id)

    newest = index.last_run()
    assert newest["mode"] == "undo"
    assert newest["psl_version"] == psl().version
    assert newest["config_hash"]


def test_export_index_as_json(tmp_path):
    from datetime import datetime, timezone

    from mailgonizer.records import ClassifiedMessage, SenderKey

    with Index.open(tmp_path / "t.sqlite") as index:
        run_id = index.start_run("run", "c", "p", "h")
        index.upsert_messages([ClassifiedMessage(
            msg_key="k1", message_id="<a@x>", folder="INBOX", uid=1,
            uidvalidity=1, internaldate=datetime(2019, 1, 1, tzinfo=timezone.utc),
            size=10, resolved_date=datetime(2019, 1, 1, tzinfo=timezone.utc),
            date_source="date", year=2019,
            sender=SenderKey("domain", None, "amazon.com", "orders", "from"),
            flags=(),
        )], run_id)

        out = io.StringIO()
        export_index(index, "json", out)

    payload = json.loads(out.getvalue())
    assert payload["messages"][0]["sender_domain"] == "amazon.com"
    assert "moves" in payload and "promotions" in payload


def test_export_index_as_csv_is_flat_and_parseable(tmp_path):
    from datetime import datetime, timezone

    from mailgonizer.records import ClassifiedMessage, SenderKey

    with Index.open(tmp_path / "t.sqlite") as index:
        run_id = index.start_run("run", "c", "p", "h")
        index.upsert_messages([ClassifiedMessage(
            msg_key="k1", message_id="<a@x>", folder="INBOX", uid=1,
            uidvalidity=1, internaldate=datetime(2019, 1, 1, tzinfo=timezone.utc),
            size=10, resolved_date=datetime(2019, 1, 1, tzinfo=timezone.utc),
            date_source="date", year=2019,
            sender=SenderKey("domain", None, "amazon.com", "orders", "from"),
            flags=(),
        )], run_id)

        out = io.StringIO()
        export_index(index, "csv", out)

    rows = list(csv.DictReader(io.StringIO(out.getvalue())))
    assert rows[0]["msg_key"] == "k1"
    assert rows[0]["sender_domain"] == "amazon.com"
