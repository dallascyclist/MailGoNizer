"""Command line entry point. Argument parsing and exit codes only."""

from __future__ import annotations

import argparse
import csv
import json
import os
import shutil
import sys
from pathlib import Path

from mailgonizer import recovery, runner
from mailgonizer.config import Config, ConfigError, load_config
from mailgonizer.imap import FatalError, Mailbox
from mailgonizer.index import Index
from mailgonizer.psl import PublicSuffixList
from mailgonizer.runlog import RunLog

EXIT_OK = 0
EXIT_FATAL = 1
EXIT_WITH_FAILURES = 2


def resolve_root(explicit: str | None = None) -> Path:
    if explicit:
        return Path(explicit).resolve()
    env = os.environ.get("MAILGONIZER_ROOT")
    if env:
        return Path(env).resolve()
    # lib/mailgonizer/cli.py -> lib/mailgonizer -> lib -> root
    return Path(__file__).resolve().parents[2]


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="mailgonizer",
        description="Archive an unorganized IMAP inbox into a year/sender tree.",
    )
    parser.add_argument("--root", help="application directory (default: auto)")
    parser.add_argument("--config", help="path to config.yaml (default: <root>/etc)")
    parser.add_argument("-v", "--verbose", action="store_true",
                        help="log at debug level")
    sub = parser.add_subparsers(dest="command", required=True)

    sub.add_parser("init", help="seed etc/config.yaml and create log/ and db/")
    sub.add_parser("check", help="validate config and server; writes nothing")
    sub.add_parser("plan", help="survey, classify, and persist a plan")
    sub.add_parser("status", help="report the last run")

    show = sub.add_parser("show-plan", help="print a stored plan for review")
    show.add_argument("--run", type=int, help="run id (default: latest)")
    show.add_argument("--format", choices=["text", "csv", "json"], default="text")

    apply_p = sub.add_parser("apply", help="execute a stored plan")
    apply_p.add_argument("--run", type=int, help="run id (default: latest)")

    run_p = sub.add_parser("run", help="plan and apply in one process")
    run_p.add_argument("--dry-run", action="store_true",
                       help="plan only; perform no moves")

    undo_p = sub.add_parser("undo", help="reverse a run's moves")
    undo_p.add_argument("--run", type=int, required=True, help="run id to reverse")

    export_p = sub.add_parser("export-index", help="dump the index")
    export_p.add_argument("--format", choices=["csv", "json"], default="json")

    sub.add_parser("rebuild-index", help="discard the cache and rescan the server")
    return parser


def _paths(args) -> tuple[Path, Path, Path, Path]:
    root = resolve_root(args.root)
    config_path = Path(args.config) if args.config else root / "etc" / "config.yaml"
    return root, config_path, root / "log", root / "db" / "mailgonizer.sqlite"


def _cmd_init(root: Path) -> int:
    example = root / "etc" / "config.yaml.example"
    target = root / "etc" / "config.yaml"
    (root / "log").mkdir(parents=True, exist_ok=True)
    (root / "db").mkdir(parents=True, exist_ok=True)
    if target.exists():
        print(f"init: {target} already exists, leaving it alone")
        return EXIT_OK
    if not example.exists():
        print(f"init: no template at {example}", file=sys.stderr)
        return EXIT_FATAL
    shutil.copy(example, target)
    target.chmod(0o600)
    print(f"init: wrote {target} (mode 0600) — edit it, then run `check`")
    return EXIT_OK


def _show_plan(index: Index, run_id: int, fmt: str) -> int:
    rows = index.all_items(run_id)
    if fmt == "json":
        print(json.dumps([dict(r) for r in rows], indent=2, default=str))
    elif fmt == "csv":
        writer = csv.writer(sys.stdout)
        writer.writerow(["seq", "state", "reason", "src_folder", "src_uid",
                         "dst_folder", "error"])
        for r in rows:
            writer.writerow([r["seq"], r["state"], r["reason"], r["src_folder"],
                             r["src_uid"], r["dst_folder"], r["error"] or ""])
    else:
        print(f"run {run_id}: {len(rows)} items")
        for r in rows:
            print(f"  {r['seq']:>7}  {r['state']:<8} {r['reason']:<8} "
                  f"{r['src_folder']} -> {r['dst_folder']}")
    return EXIT_OK


def _latest_run(index: Index, explicit: int | None) -> int:
    if explicit is not None:
        return explicit
    row = index.last_run()
    if row is None:
        raise FatalError("no runs recorded yet")
    return int(row["run_id"])


def main(argv: list[str] | None = None, mailbox_factory=None) -> int:
    args = build_parser().parse_args(argv)
    root, config_path, log_dir, db_path = _paths(args)

    if args.command == "init":
        return _cmd_init(root)

    try:
        cfg: Config = load_config(config_path)
    except ConfigError as exc:
        print(f"mailgonizer: config error: {exc}", file=sys.stderr)
        return EXIT_FATAL

    level = "debug" if args.verbose else cfg.logging.level
    connect = mailbox_factory or Mailbox.connect

    # `check` deliberately touches neither the database nor the log directory
    # beyond its own run log, because it is what an operator runs first.
    if args.command == "check":
        last_psl = None
        if db_path.exists():
            with Index.open(db_path) as index:
                row = index.last_run()
                last_psl = row["psl_version"] if row else None
        with RunLog.open(log_dir, RunLog.stamp_now(), level) as log:
            try:
                return runner.do_check(connect(cfg), cfg, log, last_psl)
            except FatalError as exc:
                log.error(str(exc))
                return EXIT_FATAL

    with Index.open(db_path) as index, \
            RunLog.open(log_dir, RunLog.stamp_now(), level) as log:
        try:
            if args.command == "status":
                row = index.last_run()
                if row is None:
                    print("no runs recorded yet")
                    return EXIT_OK
                print(f"run {row['run_id']} mode={row['mode']} "
                      f"status={row['status']} started={row['started_at']}")
                print(f"counts: {row['counts_json'] or '{}'}")
                return EXIT_OK

            if args.command == "show-plan":
                return _show_plan(index, _latest_run(index, args.run), args.format)

            if args.command == "export-index":
                recovery.export_index(index, args.format, sys.stdout)
                return EXIT_OK

            psl = PublicSuffixList.bundled()
            mailbox = connect(cfg)

            if args.command == "plan":
                run_id, result = runner.do_plan(mailbox, index, cfg, psl, log)
                index.finish_run(run_id, "ok", result.counts)
                log.verdict(result.counts)
                RunLog.prune(log_dir, cfg.logging.retention_runs)
                return EXIT_OK

            if args.command == "apply":
                run_id = _latest_run(index, args.run)
                if not index.all_items(run_id):
                    raise FatalError(f"run {run_id} has no plan items")
                outcome = runner.do_apply(mailbox, index, cfg, log, run_id,
                                          reconnect=lambda: connect(cfg))
                counts = {"moved": outcome.moved, "failed": outcome.failed,
                          "skipped": outcome.skipped}
                status = "ok" if outcome.failed == 0 else "completed_with_failures"
                index.finish_run(run_id, status, counts)
                log.verdict(counts)
                RunLog.prune(log_dir, cfg.logging.retention_runs)
                return EXIT_OK if outcome.failed == 0 else EXIT_WITH_FAILURES

            if args.command == "run":
                run_id, result = runner.do_plan(mailbox, index, cfg, psl, log)
                if args.dry_run:
                    index.finish_run(run_id, "ok", result.counts)
                    log.info("dry run: no moves performed")
                    log.verdict(result.counts)
                    RunLog.prune(log_dir, cfg.logging.retention_runs)
                    return EXIT_OK
                outcome = runner.do_apply(mailbox, index, cfg, log, run_id,
                                          reconnect=lambda: connect(cfg))
                counts = dict(result.counts)
                counts.update({"moved": outcome.moved, "failed": outcome.failed,
                               "skipped_at_apply": outcome.skipped})
                status = "ok" if outcome.failed == 0 else "completed_with_failures"
                index.finish_run(run_id, status, counts)
                log.verdict(counts)
                RunLog.prune(log_dir, cfg.logging.retention_runs)
                return EXIT_OK if outcome.failed == 0 else EXIT_WITH_FAILURES

            if args.command == "undo":
                recovery.undo_run(mailbox, index, cfg, log, args.run)
                RunLog.prune(log_dir, cfg.logging.retention_runs)
                return EXIT_OK

            if args.command == "rebuild-index":
                recovery.rebuild_index(mailbox, index, cfg, psl, log)
                RunLog.prune(log_dir, cfg.logging.retention_runs)
                return EXIT_OK

        except FatalError as exc:
            log.error(str(exc))
            print(f"mailgonizer: {exc}", file=sys.stderr)
            return EXIT_FATAL

    return EXIT_FATAL


if __name__ == "__main__":
    sys.exit(main())
