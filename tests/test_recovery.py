import csv
import io
import json

import pytest

from mailgonizer.config import Config, ExecutionConfig, ServerConfig
from mailgonizer.imap import Capabilities
from mailgonizer.index import Index
from mailgonizer.records import PlanItem
from mailgonizer.recovery import export_index, undo_run
from mailgonizer.runlog import RunLog


class FakeMailbox:
    def __init__(self, contents=None):
        # folder -> {uid: msg_key}
        self.contents = contents or {}
        self.moves = []

    def capabilities(self):
        return Capabilities("/", True, True, {})

    def assert_safe(self):
        pass

    def select(self, folder, readonly=True):
        return 100, 500

    def list_uids(self, folder):
        return sorted(self.contents.get(folder, {}))

    def fetch_identity(self, uids):
        merged = {}
        for mapping in self.contents.values():
            merged.update(mapping)
        return {u: merged[u] for u in uids if u in merged}

    def ensure_folder(self, path, subscribe):
        pass

    def move(self, uids, dst):
        self.moves.append((tuple(uids), dst))


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

    result = undo_run(mb, index, cfg(), log, run_id)

    assert result.moved == 2
    assert mb.moves == [((50, 51), "INBOX")]


def test_undo_appends_reversals_and_never_rewrites_history(prepared):
    index, log, run_id = prepared
    mb = FakeMailbox({"Crono_Archive/2019/a": {50: "k1", 51: "k2"}})

    undo_run(mb, index, cfg(), log, run_id)

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

    result = undo_run(mb, index, cfg(), log, run_id)

    assert result.moved == 1 and result.skipped == 1
    assert mb.moves == [((50,), "INBOX")]


def test_undo_does_not_reverse_promotions(prepared):
    index, log, run_id = prepared
    index.record_promotion(2019, "amazon.com", "orders", run_id, 14)
    mb = FakeMailbox({"Crono_Archive/2019/a": {50: "k1", 51: "k2"}})

    undo_run(mb, index, cfg(), log, run_id)

    assert index.known_promotions() == {(2019, "amazon.com", "orders")}


def test_undo_of_a_run_with_no_moves_is_a_no_op(tmp_path):
    with Index.open(tmp_path / "t.sqlite") as index, \
            RunLog.open(tmp_path, "s", "info") as log:
        run_id = index.start_run("run", "c", "p", "h")
        result = undo_run(FakeMailbox(), index, cfg(), log, run_id)
        assert result == type(result)()


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
