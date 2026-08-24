import json
from datetime import datetime, timezone
from email import message_from_string
from email.policy import default as default_policy
from pathlib import Path

import pytest

from mailgonizer.cli import main
from mailgonizer.imap import Capabilities, TransientError, UnsafeServerError
from mailgonizer.index import Index
from mailgonizer.records import HeaderRecord, compute_msg_key

CONFIG = """
server:
  host: mail.example.com
  username: doug@example.com
  password: secret
execution:
  pause_between_batches_ms: 0
"""

# Used only by the real-move / real-failure tests below. A TransientError
# drives execute()'s retry loop, which sleeps min(2 ** attempt, 30) real
# seconds between attempts; connect_retries: 1 keeps the single,
# guaranteed-to-fail attempt from ever reaching that sleep.
CONFIG_ONE_RETRY = """
server:
  host: mail.example.com
  username: doug@example.com
  password: secret
execution:
  pause_between_batches_ms: 0
  connect_retries: 1
"""


@pytest.fixture
def root(tmp_path):
    (tmp_path / "etc").mkdir()
    (tmp_path / "etc" / "config.yaml").write_text(CONFIG)
    (tmp_path / "etc" / "config.yaml.example").write_text(CONFIG)
    return tmp_path


def make_record(uid, folder="INBOX", from_addr="orders@amazon.com",
                message_id="<n1@x>", size=1000):
    """A realistic, classifiable, plannable inbox message.

    Dated 2009 so it is always well past source.age_days (default 90),
    regardless of when the suite actually runs.
    """
    text = (f"From: {from_addr}\nMessage-ID: {message_id}\n"
           f"Date: Mon, 2 Mar 2009 12:00:00 +0000\n\n")
    return HeaderRecord(
        folder=folder, uid=uid, uidvalidity=100,
        internaldate=datetime(2009, 3, 2, 12, tzinfo=timezone.utc),
        size=size, flags=(),
        headers=message_from_string(text, policy=default_policy),
    )


class StubMailbox:
    def __init__(self, safe=True, headers=None):
        self.safe = safe
        self.headers = headers or {}
        self.moves = []
        self._selected = None

    def capabilities(self):
        return Capabilities("/", True, True, {})

    def assert_safe(self):
        if not self.safe:
            raise UnsafeServerError("neither MOVE nor UIDPLUS")

    def list_folders(self, prefix=None):
        return [f for f in self.headers if f != "INBOX"]

    def select(self, folder, readonly=True):
        self._selected = folder
        return 100, 500

    def message_count(self, folder):
        return sum(1 for _ in self.fetch_headers(folder))

    def fetch_headers(self, folder):
        yield from self.headers.get(folder, [])

    def fetch_identity(self, uids):
        # A real Mailbox answers this from the live server. This stub
        # answers it the same way classify() computed each item's msg_key
        # in the first place, from whichever folder was most recently
        # select()ed -- so a genuine apply can actually confirm identity
        # and call move(). Tests that never drive apply's real execution
        # path (headers={}, or do_apply monkeypatched out) never reach
        # this method, so returning nothing for an unknown uid is moot for
        # them and correctly means "vanished" for anyone who does.
        by_uid = {r.uid: r for r in self.headers.get(self._selected, [])}
        identities = {}
        for uid in uids:
            rec = by_uid.get(uid)
            if rec is None:
                continue
            message_id = rec.headers.get("message-id")
            message_id = str(message_id).strip() if message_id else None
            identities[uid] = compute_msg_key(message_id, rec.internaldate, rec.size)
        return identities

    def ensure_folder(self, path, subscribe):
        pass

    def move(self, uids, dst):
        self.moves.append((tuple(uids), dst))

    def close(self):
        pass


def run_cli(root, args, mailbox):
    return main(["--root", str(root), *args], mailbox_factory=lambda cfg: mailbox)


def test_check_returns_zero_on_a_safe_server(root):
    assert run_cli(root, ["check"], StubMailbox()) == 0


def test_check_returns_one_on_an_unsafe_server(root):
    assert run_cli(root, ["check"], StubMailbox(safe=False)) == 1


def test_check_touches_neither_mailbox_nor_database(root):
    # Renamed from test_check_performs_no_writes, which asserted only that
    # the database file was absent -- so it could not have caught check
    # writing anything else under root. check DOES write its own run log
    # (`=== CHECK ===`, the PSL version, `check passed`); that's correct,
    # documented behavior, not a regression. What must never happen is a
    # database file or a real mailbox move.
    mb = StubMailbox()
    assert run_cli(root, ["check"], mb) == 0
    assert mb.moves == []
    assert not (root / "db").exists()

    log_dir = root / "log"
    assert log_dir.is_dir()
    log_files = sorted(log_dir.iterdir())
    assert len(log_files) == 2
    stamps = {p.stem for p in log_files}
    assert len(stamps) == 1
    stamp = stamps.pop()
    assert {p.name for p in log_files} == {f"{stamp}.log", f"{stamp}.jsonl"}

    # Enumerate everything under root and compare against an explicit
    # expected set, so an unanticipated new artifact (a stray db/
    # directory, a cache file, ...) fails the test instead of passing
    # silently because nobody happened to assert its absence by name.
    created = {p.relative_to(root) for p in root.rglob("*")}
    expected = {
        Path("etc"), Path("etc/config.yaml"), Path("etc/config.yaml.example"),
        Path("log"), Path("log") / f"{stamp}.log", Path("log") / f"{stamp}.jsonl",
    }
    assert created == expected


def test_missing_config_is_a_fatal_error_not_a_traceback(tmp_path, capsys):
    code = main(["--root", str(tmp_path), "check"], mailbox_factory=lambda c: None)
    assert code == 1
    assert "config" in capsys.readouterr().err.lower()


def test_init_seeds_config_with_restrictive_permissions(tmp_path):
    (tmp_path / "etc").mkdir()
    (tmp_path / "etc" / "config.yaml.example").write_text(CONFIG)
    assert main(["--root", str(tmp_path), "init"]) == 0
    target = tmp_path / "etc" / "config.yaml"
    assert target.exists()
    assert oct(target.stat().st_mode)[-3:] == "600"
    assert (tmp_path / "log").is_dir()
    assert (tmp_path / "db").is_dir()


def test_init_does_not_clobber_an_existing_config(root):
    (root / "etc" / "config.yaml").write_text("server:\n  host: mine\n")
    main(["--root", str(root), "init"])
    assert "mine" in (root / "etc" / "config.yaml").read_text()


def test_plan_persists_a_run_and_writes_logs(root):
    assert run_cli(root, ["plan"], StubMailbox(headers={"INBOX": []})) == 0
    with Index.open(root / "db" / "mailgonizer.sqlite") as index:
        assert index.last_run()["mode"] == "plan"
    assert list((root / "log").glob("*.log"))
    assert list((root / "log").glob("*.jsonl"))


def test_run_dry_run_plans_but_never_moves(root):
    mb = StubMailbox(headers={"INBOX": []})
    assert run_cli(root, ["run", "--dry-run"], mb) == 0
    assert mb.moves == []
    with Index.open(root / "db" / "mailgonizer.sqlite") as index:
        assert index.last_run()["mode"] == "plan"


def test_show_plan_emits_json(root, capsys):
    run_cli(root, ["plan"], StubMailbox(headers={"INBOX": []}))
    capsys.readouterr()  # drain the plan's own RunLog narrative echo
    assert run_cli(root, ["show-plan", "--format", "json"],
                   StubMailbox()) == 0
    assert isinstance(json.loads(capsys.readouterr().out), list)


def test_status_reports_the_last_run(root, capsys):
    run_cli(root, ["plan"], StubMailbox(headers={"INBOX": []}))
    capsys.readouterr()  # drain the plan's own RunLog narrative echo
    assert run_cli(root, ["status"], StubMailbox()) == 0
    # "mode=plan", not the bare substring "plan" -- the counts dict also
    # contains the key "planned", which would let a broken status command
    # (one that silently drops mode=) pass a looser check for free.
    assert "mode=plan" in capsys.readouterr().out


def test_status_with_no_runs_is_not_an_error(root, capsys):
    assert run_cli(root, ["status"], StubMailbox()) == 0
    assert "no runs" in capsys.readouterr().out.lower()


def test_apply_without_a_plan_is_a_fatal_error(root):
    assert run_cli(root, ["apply", "--run", "42"], StubMailbox()) == 1


def test_apply_with_no_prior_run_is_a_fatal_error(root, capsys):
    # No --run given, and no run has ever been recorded: _latest_run() must
    # raise FatalError, and cli.main() must convert that to a clean exit 1
    # with a readable stderr message -- not a traceback.
    assert run_cli(root, ["apply"], StubMailbox()) == 1
    assert "no runs" in capsys.readouterr().err.lower()


def test_a_transient_failure_exits_cleanly_rather_than_raising(root, capsys):
    """Mailbox.connect raises TransientError on OSError, but main() caught
    only FatalError -- TransientError is not a subclass -- so a brief network
    blip at cron time produced a raw traceback instead of the classified,
    retryable failure spec 9.3 describes."""
    def unreachable(cfg):
        raise TransientError("cannot reach mail.example.com: timed out")

    code = main(["--root", str(root), "plan"], mailbox_factory=unreachable)

    assert code == 1
    err = capsys.readouterr().err.lower()
    assert "traceback" not in err
    assert "timed out" in err and "retry" in err


def test_check_exits_cleanly_on_a_transient_failure(root):
    def unreachable(cfg):
        raise TransientError("cannot reach mail.example.com: timed out")

    assert main(["--root", str(root), "check"], mailbox_factory=unreachable) == 1


def test_show_plan_with_no_prior_run_is_a_fatal_error(root, capsys):
    assert run_cli(root, ["show-plan"], StubMailbox()) == 1
    assert "no runs" in capsys.readouterr().err.lower()


def test_apply_moves_mail_through_a_real_plan(root):
    # The only prior test reaching exit 0 through apply's own code
    # (test_run_dry_run_plans_but_never_moves) never calls apply at all,
    # and the only test exercising apply's exit-code mapping
    # (test_exit_code_two_when_items_fail) does so through run's separate,
    # duplicate copy with do_apply monkeypatched out. Nothing previously
    # proved a real mailbox.move() happens through the CLI at all.
    mb = StubMailbox(headers={"INBOX": [make_record(uid=1)]})
    assert run_cli(root, ["plan"], mb) == 0
    assert run_cli(root, ["apply"], mb) == 0
    assert mb.moves != []
    with Index.open(root / "db" / "mailgonizer.sqlite") as index:
        assert index.last_run()["status"] == "ok"


def test_apply_reports_exit_2_on_a_real_per_item_failure(root):
    class AlwaysFailMailbox(StubMailbox):
        def move(self, uids, dst):
            raise TransientError("simulated move failure")

    mb = AlwaysFailMailbox(headers={"INBOX": [make_record(uid=1)]})
    assert run_cli(root, ["plan"], mb) == 0
    (root / "etc" / "config.yaml").write_text(CONFIG_ONE_RETRY)
    assert run_cli(root, ["apply"], mb) == 2
    with Index.open(root / "db" / "mailgonizer.sqlite") as index:
        assert index.last_run()["status"] == "completed_with_failures"


def test_exit_code_two_when_items_fail(root, monkeypatch):
    from mailgonizer import cli
    from mailgonizer.executor import ExecutionResult

    monkeypatch.setattr(
        cli.runner, "do_apply",
        lambda *a, **k: ExecutionResult(moved=3, failed=2, skipped=0),
    )
    code = run_cli(root, ["run"], StubMailbox(headers={"INBOX": []}))
    assert code == 2


def test_unknown_subcommand_exits_nonzero(root):
    with pytest.raises(SystemExit):
        main(["--root", str(root), "frobnicate"])


def test_export_index_runs_without_a_server(root, capsys):
    run_cli(root, ["plan"], StubMailbox(headers={"INBOX": []}))
    capsys.readouterr()  # drain the plan's own RunLog narrative echo
    assert run_cli(root, ["export-index", "--format", "json"],
                   StubMailbox()) == 0
    assert "messages" in json.loads(capsys.readouterr().out)


def test_undo_requires_an_explicit_run_id(root):
    with pytest.raises(SystemExit):
        main(["--root", str(root), "undo"])


def test_a_fatal_error_mid_apply_closes_the_run_and_says_how_to_resume(root, capsys):
    """A crash left two runs stuck at status='running' forever, and nothing
    told the operator that 48,000 moves were durable and resumable."""
    from mailgonizer import cli as cli_mod
    from mailgonizer.imap import FatalError

    mb = StubMailbox(headers={"INBOX": [make_record(uid=1), make_record(uid=2)]})
    run_cli(root, ["plan"], mb)

    # Simulate a run that moved some mail and then hit the fatal error, which
    # is what actually happened: 48,092 moves durable, then an abort.
    db = root / "db" / "mailgonizer.sqlite"
    with Index.open(db) as index:
        run_id = index.last_run()["run_id"]
        index.mark_done(run_id, index.pending_items(run_id)[0].seq, "<a@b>", None)

    def boom(*a, **k):
        raise FatalError("no header payload in the FETCH response for uid 153663")

    original = cli_mod.runner.do_apply
    cli_mod.runner.do_apply = boom
    try:
        code = run_cli(root, ["apply"], mb)
    finally:
        cli_mod.runner.do_apply = original

    assert code == 1
    with Index.open(root / "db" / "mailgonizer.sqlite") as index:
        row = index.last_run()
        assert row["status"] != "running", "run left open after a fatal error"
        assert row["finished_at"] is not None
    err = capsys.readouterr().err
    assert "apply --run" in err, "no resume hint given"
