import dataclasses
from datetime import datetime, timezone

import pytest

from mailgonizer.index import Index
from mailgonizer.psl import PublicSuffixList
from mailgonizer.runlog import RunLog
from mailgonizer.runner import do_apply, do_plan

from .conftest import OLD, message, seed

pytestmark = pytest.mark.integration


@pytest.fixture
def psl():
    return PublicSuffixList.bundled()


def run_once(mailbox, cfg, psl, tmp_path, name="s"):
    with Index.open(tmp_path / "t.sqlite") as index, \
            RunLog.open(tmp_path, name, "debug") as log:
        run_id, result = do_plan(mailbox, index, cfg, psl, log,
                                 datetime.now(timezone.utc))
        outcome = do_apply(mailbox, index, cfg, log, run_id)
        return run_id, result, outcome


def test_capabilities_are_discovered(mailbox):
    caps = mailbox.capabilities()
    assert caps.delimiter == "/"
    assert caps.has_move or caps.has_uidplus
    mailbox.assert_safe()


def test_survey_never_sets_the_seen_flag(mailbox, slash_cfg, psl, tmp_path):
    """A BODY.PEEK that loses its PEEK is a one-character diff with
    irreversible consequences. This is the assertion that catches it."""
    seed(mailbox, count=5)
    mailbox.select("INBOX", readonly=True)
    before = mailbox.client.fetch(mailbox.client.search(["ALL"]), [b"FLAGS"])
    assert all(b"\\Seen" not in data[b"FLAGS"] for data in before.values())

    list(mailbox.fetch_headers("INBOX"))

    mailbox.select("INBOX", readonly=True)
    after = mailbox.client.fetch(mailbox.client.search(["ALL"]), [b"FLAGS"])
    assert {uid: data[b"FLAGS"] for uid, data in before.items()} == \
           {uid: data[b"FLAGS"] for uid, data in after.items()}


def test_end_to_end_archive(mailbox, slash_cfg, psl, tmp_path):
    seed(mailbox, count=5)
    _run_id, result, outcome = run_once(mailbox, slash_cfg, psl, tmp_path)

    assert outcome.moved == 5 and outcome.failed == 0
    mailbox.select("INBOX", readonly=True)
    assert mailbox.client.search(["ALL"]) == []
    dest = f"Crono_Archive/{OLD.year}/amazon_com"
    mailbox.select(dest, readonly=True)
    assert len(mailbox.client.search(["ALL"])) == 5


def test_created_folders_are_unsubscribed_by_default(mailbox, slash_cfg, psl,
                                                     tmp_path):
    seed(mailbox, count=2)
    run_once(mailbox, slash_cfg, psl, tmp_path)
    subscribed = {name for _f, _d, name in mailbox.client.list_sub_folders()}
    assert not any("Crono_Archive" in str(n) for n in subscribed)


def test_running_twice_is_a_no_op(mailbox, slash_cfg, psl, tmp_path):
    seed(mailbox, count=5)
    run_once(mailbox, slash_cfg, psl, tmp_path, name="first")
    _run_id, _result, second = run_once(mailbox, slash_cfg, psl, tmp_path,
                                        name="second")
    assert second.moved == 0


def test_flagged_mail_stays_in_the_inbox(mailbox, slash_cfg, psl, tmp_path):
    seed(mailbox, count=3)
    seed(mailbox, count=2, flags=("\\Flagged",))
    run_once(mailbox, slash_cfg, psl, tmp_path)
    mailbox.select("INBOX", readonly=True)
    assert len(mailbox.client.search(["ALL"])) == 2


def test_promotion_creates_a_per_sender_folder_and_backfills(
        mailbox, slash_cfg, psl, tmp_path):
    seed(mailbox, count=10)
    run_once(mailbox, slash_cfg, psl, tmp_path, name="first")
    seed(mailbox, count=4)
    run_once(mailbox, slash_cfg, psl, tmp_path, name="second")

    promoted = f"Crono_Archive/{OLD.year}/amazon_com/orders"
    mailbox.select(promoted, readonly=True)
    assert len(mailbox.client.search(["ALL"])) == 14


def test_copy_fallback_does_not_expunge_unrelated_deleted_mail(
        mailbox, slash_cfg, psl, tmp_path, monkeypatch):
    """The hostile test. A bare EXPUNGE here would destroy the victim."""
    from mailgonizer.imap import Capabilities

    victim_id = "<victim@test>"
    mailbox.client.append("INBOX", message(msgid=victim_id, subject="victim"),
                          msg_time=OLD)
    mailbox.select("INBOX", readonly=False)
    victim_uid = mailbox.client.search(["HEADER", "MESSAGE-ID", victim_id])
    mailbox.client.add_flags(victim_uid, ["\\Deleted"])

    seed(mailbox, count=3, sender="deals@example.org")

    caps = mailbox.capabilities()
    monkeypatch.setattr(
        mailbox, "_caps",
        Capabilities(caps.delimiter, has_move=False, has_uidplus=True,
                     special_use=caps.special_use),
    )
    run_once(mailbox, slash_cfg, psl, tmp_path)

    mailbox.select("INBOX", readonly=True)
    assert mailbox.client.search(["HEADER", "MESSAGE-ID", victim_id]), \
        "the soft-deleted victim was expunged by the COPY fallback"


def test_resumption_after_interruption(mailbox, slash_cfg, psl, tmp_path):
    seed(mailbox, count=10)
    with Index.open(tmp_path / "t.sqlite") as index, \
            RunLog.open(tmp_path, "s", "debug") as log:
        run_id, _result = do_plan(mailbox, index, slash_cfg, psl, log,
                                  datetime.now(timezone.utc))

        # Simulate a kill partway through by executing in small batches.
        capped = dataclasses.replace(
            slash_cfg,
            execution=dataclasses.replace(slash_cfg.execution, batch_size=4),
        )
        partial = do_apply(mailbox, index, capped, log, run_id)
        assert partial.moved == 10 or index.pending_items(run_id)

        rest = do_apply(mailbox, index, slash_cfg, log, run_id)

    mailbox.select("INBOX", readonly=True)
    assert mailbox.client.search(["ALL"]) == []
    assert partial.moved + rest.moved == 10


def test_undo_returns_everything_to_the_inbox(mailbox, slash_cfg, psl, tmp_path):
    from mailgonizer.recovery import undo_run

    seed(mailbox, count=5)
    with Index.open(tmp_path / "t.sqlite") as index, \
            RunLog.open(tmp_path, "s", "debug") as log:
        run_id, _result = do_plan(mailbox, index, slash_cfg, psl, log,
                                  datetime.now(timezone.utc))
        do_apply(mailbox, index, slash_cfg, log, run_id)
        undo_run(mailbox, index, slash_cfg, log, run_id)

    mailbox.select("INBOX", readonly=True)
    assert len(mailbox.client.search(["ALL"])) == 5


def test_dot_delimiter_server_produces_a_flat_domain_folder(dot_cfg, psl,
                                                            tmp_path):
    """The mailcow rehearsal. With separator='.', a folder literally named
    amazon.com would become amazon/com. The escaping rule must prevent it."""
    from mailgonizer.imap import Mailbox

    mb = Mailbox.connect(dot_cfg)
    try:
        assert mb.capabilities().delimiter == "."
        seed(mb, count=3)
        run_once(mb, dot_cfg, psl, tmp_path)

        folders = {str(name) for _f, _d, name in mb.client.list_folders()}
        expected = f"Crono_Archive.{OLD.year}.amazon_com"
        assert expected in folders
        assert not any(f.endswith(".amazon.com") for f in folders)
        mb.select(expected, readonly=True)
        assert len(mb.client.search(["ALL"])) == 3
    finally:
        mb.client.logout()
