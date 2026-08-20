from datetime import datetime, timedelta, timezone
from email import message_from_string
from email.policy import default as default_policy

import pytest

from mailgonizer.config import ArchiveConfig, Config, ServerConfig
from mailgonizer.imap import Capabilities
from mailgonizer.index import Index
from mailgonizer.psl import PublicSuffixList
from mailgonizer.records import HeaderRecord
from mailgonizer.runlog import RunLog
from mailgonizer.runner import classify, config_hash, do_check, do_plan

NOW = datetime(2026, 8, 19, tzinfo=timezone.utc)
OLD = NOW - timedelta(days=400)


@pytest.fixture
def psl():
    return PublicSuffixList("com\norg\nco.uk\n".splitlines())


def cfg():
    return Config(
        server=ServerConfig(host="h", username="u", password="p"),
        archive=ArchiveConfig(),
    )


def record(text, folder="INBOX", uid=1, when=OLD, flags=(), size=1000):
    return HeaderRecord(
        folder=folder, uid=uid, uidvalidity=100, internaldate=when, size=size,
        flags=tuple(flags),
        headers=message_from_string(text, policy=default_policy),
    )


def test_classify_produces_a_stable_key_and_resolved_sender(psl):
    rec = record("From: orders@mail.amazon.com\nMessage-ID: <a@b>\n"
                 "Date: Mon, 2 Mar 2009 12:00:00 +0000\n\n")
    [m] = classify([rec], cfg(), psl, NOW)
    assert m.sender.domain == "amazon.com"
    assert m.sender.local == "orders"
    assert m.year == 2009
    assert m.date_source == "date"
    assert len(m.msg_key) == 64


def test_classify_is_deterministic_for_identical_input(psl):
    rec = record("From: a@b.com\nMessage-ID: <a@b>\n\n")
    a = classify([rec], cfg(), psl, NOW)[0]
    b = classify([rec], cfg(), psl, NOW)[0]
    assert a.msg_key == b.msg_key


def test_messages_without_message_id_still_get_distinct_keys(psl):
    a = record("From: a@b.com\n\n", uid=1, size=100)
    b = record("From: a@b.com\n\n", uid=2, size=200)
    keys = {m.msg_key for m in classify([a, b], cfg(), psl, NOW)}
    assert len(keys) == 2


def test_check_reports_an_archive_special_use_collision(tmp_path, capsys):
    class MB:
        def capabilities(self):
            return Capabilities("/", True, True, {"\\archive": "Crono_Archive"})

        def assert_safe(self):
            return None

    with RunLog.open(tmp_path, "s", "info") as log:
        code = do_check(MB(), cfg(), log)
    assert code == 0
    assert "special-use" in (tmp_path / "s.log").read_text().lower()


def test_check_warns_when_the_public_suffix_list_has_changed(tmp_path):
    class MB:
        def capabilities(self):
            return Capabilities("/", True, True, {})

        def assert_safe(self):
            return None

    with RunLog.open(tmp_path, "s", "info") as log:
        code = do_check(MB(), cfg(), log, last_psl_version="stale-version")
    assert code == 0
    assert "public suffix list changed" in (tmp_path / "s.log").read_text()


def test_check_is_quiet_when_the_public_suffix_list_matches(tmp_path):
    from mailgonizer.psl import PublicSuffixList as PSL

    class MB:
        def capabilities(self):
            return Capabilities("/", True, True, {})

        def assert_safe(self):
            return None

    with RunLog.open(tmp_path, "s", "info") as log:
        do_check(MB(), cfg(), log, last_psl_version=PSL.bundled().version)
    assert "public suffix list changed" not in (tmp_path / "s.log").read_text()


def test_check_returns_fatal_when_the_server_is_unsafe(tmp_path):
    from mailgonizer.imap import UnsafeServerError

    class MB:
        def capabilities(self):
            return Capabilities("/", False, False, {})

        def assert_safe(self):
            raise UnsafeServerError("neither MOVE nor UIDPLUS")

    with RunLog.open(tmp_path, "s", "info") as log:
        assert do_check(MB(), cfg(), log) == 1


def test_do_plan_surveys_inbox_and_archive_then_persists(tmp_path, psl):
    class MB:
        def __init__(self):
            self.folders_listed = []

        def capabilities(self):
            return Capabilities("/", True, True, {})

        def assert_safe(self):
            return None

        def list_folders(self, prefix=None):
            self.folders_listed.append(prefix)
            return ["Crono_Archive/2009/amazon_com"]

        def select(self, folder, readonly=True):
            return 100, 500

        def fetch_headers(self, folder):
            if folder == "INBOX":
                yield record("From: orders@amazon.com\nMessage-ID: <n1@x>\n"
                             "Date: Mon, 2 Mar 2009 12:00:00 +0000\n\n", uid=1)
            else:
                yield record("From: orders@amazon.com\nMessage-ID: <a1@x>\n"
                             "Date: Mon, 2 Mar 2009 12:00:00 +0000\n\n",
                             folder=folder, uid=2)

    mb = MB()
    with Index.open(tmp_path / "t.sqlite") as index, \
            RunLog.open(tmp_path, "s", "info") as log:
        run_id, result = do_plan(mb, index, cfg(), psl, log, NOW)

        assert mb.folders_listed == ["Crono_Archive"]
        assert result.counts["surveyed"] == 2
        assert result.counts["planned"] == 1
        assert len(index.pending_items(run_id)) == 1
        assert index.last_run()["psl_version"]


def test_new_promotions_are_persisted_by_do_plan(tmp_path, psl):
    class MB:
        def capabilities(self):
            return Capabilities("/", True, True, {})

        def assert_safe(self):
            return None

        def list_folders(self, prefix=None):
            return []

        def select(self, folder, readonly=True):
            return 100, 500

        def fetch_headers(self, folder):
            if folder != "INBOX":
                return
            for n in range(14):
                yield record(f"From: orders@amazon.com\nMessage-ID: <{n}@x>\n"
                             "Date: Mon, 2 Mar 2009 12:00:00 +0000\n\n", uid=n + 1)

    with Index.open(tmp_path / "t.sqlite") as index, \
            RunLog.open(tmp_path, "s", "info") as log:
        run_id, result = do_plan(MB(), index, cfg(), psl, log, NOW)
        assert result.new_promotions == ((2009, "amazon.com", "orders", 14),)
        assert index.known_promotions() == {(2009, "amazon.com", "orders")}


def test_config_hash_changes_with_meaningful_settings():
    from mailgonizer.config import ArchiveConfig

    a = cfg()
    b = Config(server=ServerConfig(host="h", username="u", password="p"),
               archive=ArchiveConfig(promote_threshold=20))
    assert config_hash(a) != config_hash(b)


def test_config_hash_excludes_the_password():
    a = Config(server=ServerConfig(host="h", username="u", password="one"))
    b = Config(server=ServerConfig(host="h", username="u", password="two"))
    assert config_hash(a) == config_hash(b)


# --- in_place skip suppression -------------------------------------------
#
# The planner emits an "in_place" SkipRecord for every already-correctly-
# placed archived message, on every run, because promotion counting requires
# sweeping the whole archive tree. On a 300k-message archive that is 300k
# JSONL records a month for a decision nobody needs to review — the message
# is already exactly where it belongs. do_plan suppresses writing the JSONL
# decision record for "in_place" skips unless the log is at debug level;
# result.counts still reports the number regardless of level.


def _in_place_mailbox():
    class MB:
        def capabilities(self):
            return Capabilities("/", True, True, {})

        def assert_safe(self):
            return None

        def list_folders(self, prefix=None):
            return ["Crono_Archive/2009/amazon_com"]

        def select(self, folder, readonly=True):
            return 100, 500

        def fetch_headers(self, folder):
            if folder == "INBOX":
                return
            yield record(
                "From: orders@amazon.com\nMessage-ID: <a1@x>\n"
                "Date: Mon, 2 Mar 2009 12:00:00 +0000\n\n",
                folder="Crono_Archive/2009/amazon_com", uid=2,
            )

    return MB()


def test_in_place_skips_are_not_written_to_jsonl_at_info_level(tmp_path, psl):
    with Index.open(tmp_path / "t.sqlite") as index, \
            RunLog.open(tmp_path, "s", "info") as log:
        run_id, result = do_plan(_in_place_mailbox(), index, cfg(), psl, log, NOW)

    assert result.counts["skipped_in_place"] == 1
    jsonl_text = (tmp_path / "s.jsonl").read_text()
    assert "in_place" not in jsonl_text


def test_in_place_skips_are_written_to_jsonl_at_debug_level(tmp_path, psl):
    with Index.open(tmp_path / "t.sqlite") as index, \
            RunLog.open(tmp_path, "s", "debug") as log:
        run_id, result = do_plan(_in_place_mailbox(), index, cfg(), psl, log, NOW)

    assert result.counts["skipped_in_place"] == 1
    jsonl_text = (tmp_path / "s.jsonl").read_text()
    assert "in_place" in jsonl_text
