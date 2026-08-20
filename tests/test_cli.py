import json

import pytest

from mailgonizer.cli import main
from mailgonizer.imap import Capabilities, UnsafeServerError
from mailgonizer.index import Index

CONFIG = """
server:
  host: mail.example.com
  username: doug@example.com
  password: secret
execution:
  pause_between_batches_ms: 0
"""


@pytest.fixture
def root(tmp_path):
    (tmp_path / "etc").mkdir()
    (tmp_path / "etc" / "config.yaml").write_text(CONFIG)
    (tmp_path / "etc" / "config.yaml.example").write_text(CONFIG)
    return tmp_path


class StubMailbox:
    def __init__(self, safe=True, headers=None):
        self.safe = safe
        self.headers = headers or {}
        self.moves = []

    def capabilities(self):
        return Capabilities("/", True, True, {})

    def assert_safe(self):
        if not self.safe:
            raise UnsafeServerError("neither MOVE nor UIDPLUS")

    def list_folders(self, prefix=None):
        return [f for f in self.headers if f != "INBOX"]

    def select(self, folder, readonly=True):
        return 100, 500

    def fetch_headers(self, folder):
        yield from self.headers.get(folder, [])

    def fetch_identity(self, uids):
        return {}

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


def test_check_performs_no_writes(root):
    run_cli(root, ["check"], StubMailbox())
    assert not (root / "db" / "mailgonizer.sqlite").exists()


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
    # "mode=plan", not the bare substring "plan" — the counts dict also
    # contains the key "planned", which would let a broken status command
    # (one that silently drops mode=) pass a looser check for free.
    assert "mode=plan" in capsys.readouterr().out


def test_status_with_no_runs_is_not_an_error(root, capsys):
    assert run_cli(root, ["status"], StubMailbox()) == 0
    assert "no runs" in capsys.readouterr().out.lower()


def test_apply_without_a_plan_is_a_fatal_error(root):
    assert run_cli(root, ["apply", "--run", "42"], StubMailbox()) == 1


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
