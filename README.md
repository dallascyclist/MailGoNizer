# MailGoNizer

Archive a long-neglected IMAP inbox into a browsable `Crono_Archive/YYYY/<sender>`
tree, so mobile mail clients stop re-enumerating twenty years of mail on every
launch. Runs monthly via cron; safe to run by hand too.

## What it does

- Sweeps `INBOX` (and the existing archive tree, to keep promotion counts
  accurate) for messages older than `source.age_days` (default 90).
- Resolves each message's date from its `Date:` header, falling back to the
  last (earliest-hop) `Received:` header, falling back to the IMAP
  `INTERNALDATE` — whichever is the first to parse and land in a sane range.
- Resolves the sender from, in order, `List-Id`, `From`, `Sender`, then
  `Return-Path` (`Reply-To` is deliberately skipped — it's often a different
  party than the sender). A message that matches none of these lands in a
  flat `_unknown` bucket for the year.
- Groups ordinary mail by year and the sender's registrable domain (e.g.
  everything `@amazon.com` under one folder), so `orders@amazon.com` and
  `bounces@amazon.com` start out sharing `Crono_Archive/2025/amazon_com`.
  Once one local part crosses `archive.promote_threshold` (default 13)
  messages in a year, it's promoted to its own
  `Crono_Archive/2025/amazon_com/orders` folder — permanently; once promoted,
  a bucket never demotes.
- Groups mailing-list mail by `List-Id` instead, under
  `Crono_Archive/YYYY/<lists_folder>/<list-id>`, regardless of who posted.
- Keeps `\Flagged` mail in the inbox (configurable via
  `exclusions.keep_flagged`) and never moves `\Deleted` mail.
- Creates archive folders **unsubscribed by default**
  (`execution.subscribe_created_folders`), so mobile clients don't enumerate
  them at startup.
- Never deletes mail — only moves it. Moves are server-side (`MOVE`,
  RFC 6851, or `COPY` + targeted `UID EXPUNGE` under `UIDPLUS`, RFC 4315) —
  the message body is never downloaded.

## Requirements

- **Python 3.11, 3.12, or 3.13.** `pyproject.toml` pins
  `requires-python = ">=3.11,<3.14"`: Python 3.14 turned `imaplib.IMAP4.file`
  into a read-only property, and `IMAPClient` 3.1.0 (the only release on
  PyPI) still assigns `self.file` directly during `IMAP4WithTimeout.open()`,
  so every connection fails under 3.14 with an `AttributeError` before login
  is ever attempted.
- A mail server that advertises **`MOVE` (RFC 6851) or `UIDPLUS`
  (RFC 4315)**. Without either, the only fallback is `COPY` + `STORE
  \Deleted` + a bare `EXPUNGE`, which would permanently remove every
  `\Deleted` message in the folder — including mail you soft-deleted in a
  client and never expunged. The tool refuses to run rather than risk that.
- `flock` for the cron wrapper. Present by default on Linux; on macOS,
  `brew install flock` (the `discoteq/flock` reimplementation).

## Install

```bash
git clone git@github.com:dallascyclist/MailGoNizer.git mailgonizer
cd mailgonizer
./install.sh
$EDITOR etc/config.yaml
export MAILGONIZER_PASSWORD='...'
./bin/mailgonizer check
```

## Usage

```bash
./bin/mailgonizer init                    # seed etc/config.yaml and create log/, db/
./bin/mailgonizer check                   # validate config and server; touches
                                           # neither the mailbox nor the database
                                           # (it does write its own run log)
./bin/mailgonizer plan                    # survey, classify, and persist a plan
./bin/mailgonizer show-plan               # review the stored plan (--format text|csv|json)
./bin/mailgonizer apply                   # execute the stored plan
./bin/mailgonizer run                     # plan and apply in one process (the cron path)
./bin/mailgonizer run --dry-run           # plan only, no moves
./bin/mailgonizer status                  # last run's verdict
./bin/mailgonizer undo --run 3            # reverse run 3's moves
./bin/mailgonizer export-index            # dump the index (--format csv|json)
./bin/mailgonizer rebuild-index           # discard the cache and rescan the server
```

**Read `docs/first-run.md` before running against a real mailbox.**

## Cron

```cron
17 3 1 * * /path/to/mailgonizer/bin/mailgonizer-cron
```

`mailgonizer-cron` holds a `flock` lock on `db/.run.lock` so overlapping
invocations refuse rather than race, then runs `mailgonizer run`. Exit code
passes through: `0` on a clean run, `1` on a fatal error (nothing was
touched, or the run aborted mid-way), `2` when the run completed but some
individual messages failed. Skipped messages (`already_moved`, `vanished`,
`identity_mismatch`, and similar) are normal — expected on any idempotent
re-run — and do not affect the exit code. Any non-zero exit is what
surfaces the run via cron's mail-on-failure behavior.

## Directory layout

```
etc/config.yaml.example   # template — copy to config.yaml and edit
etc/config.yaml           # your config (mode 0600, gitignored)
lib/mailgonizer/          # library code
bin/mailgonizer           # CLI entry point
bin/mailgonizer-cron      # flock-guarded cron wrapper around `run`
install.sh                # creates ./venv, seeds config, makes log/ and db/
venv/                     # virtualenv built by install.sh (gitignored)
log/YYYYMMDD-HHMM.log     # narrative log — one pair of files per run
log/YYYYMMDD-HHMM.jsonl   # one JSON line per message decision
db/mailgonizer.sqlite     # index: rebuildable cache, permanent move record, working plan
```

`log/` is pruned/retained per `logging.retention_runs` (default 24) — but
the very first log pair ever written is exempt from pruning on purpose:
it's the permanent record of how the archive was originally built.
`db/mailgonizer.sqlite` is never pruned and grows without bound by design.
`moves` and `promotions` are strictly append-only — never `UPDATE`d, never
`DELETE`d — which is what makes `undo` possible and the promotion ratchet
durable. `runs` is permanent too, but not append-only: a run row is opened
by `start_run` and completed in place when the run ends, and a finished row
is never rewritten afterward.

## Debugging a decision

Every message's fate is one JSON line in that run's `.jsonl` file, keyed by
`msg_key` (a 64-character sha256 hex digest of message-id, the raw IMAP
`INTERNALDATE` normalized to UTC, and size — not the resolved `Date:` →
`Received:` → `INTERNALDATE` date used for archive placement. `msg_key`
deliberately does not depend on `Date:`-header parsing, which keeps it
durable across `MOVE`/`COPY` because none of those inputs change when a
message relocates):

```bash
# find one message's key first, e.g. from `show-plan --format json`, then:
jq 'select(.msg_key == "8c7d962188587a1dc15647ac00811a4f3b515ee095cdb3bc45a43180455a13a7")' log/20260819-2045.jsonl
jq -r 'select(.reason == "identity_mismatch")' log/*.jsonl
jq -r '.reason' log/20260819-2045.jsonl | sort | uniq -c | sort -rn
```

`reason` values you'll see include `archive`/`backfill` (planned moves),
`deleted`/`flagged`/`never_archive`/`too_recent`/`in_place` (planning skips),
and `already_moved`/`vanished`/`identity_mismatch`/`uidvalidity_changed`/
`move_failed` (execution-time skips and failures).

## Design notes

**Folder names are escaped, not passed through.** Each sender-derived path
component is lowercased (IMAP folder names are case-sensitive on nearly every
server, so `Orders` and `orders` would otherwise become two folders), has `.`
replaced by `naming.domain_separator` (default `_`), has anything outside
`[a-z0-9_-]` replaced by `_`, has runs of `_` collapsed, and is truncated to
`naming.max_component_length` with a short hash appended. The dot substitution
is the load-bearing part: `.` is the hierarchy delimiter under Dovecot's
Maildir++ layout, so a folder literally named `amazon.com` would shatter into
`amazon/com`. Names produced this way copy byte-for-byte between servers, which
matters if you ever migrate. Folder names *you* configure — `archive.root`,
`lists_folder`, `unknown_folder` — are used verbatim.

**Registrable domains come from a vendored Public Suffix List**
(`lib/mailgonizer/data/public_suffix_list.dat`), not fetched at runtime. A run
that resolved senders against a different list than a previous run could
re-bucket a domain and silently split a folder, so the version in use is
recorded per run and `check` warns if it changed. Updating the list is a
deliberate, visible act.

**The index has three layers with different lifetimes.** `messages` and
`folders` are a rebuildable cache — `rebuild-index` discards and rescans them.
`moves`, `promotions`, and `runs` are the permanent record. `plan_items` is the
per-run working plan, which is what makes `apply` resumable: an interrupted run
picks up where it stopped, and because the move log is consulted first, a
message already moved is never moved twice even if the plan is stale.

**UIDs are never identity.** A UID is per-folder and changes when a message
moves, and `UIDVALIDITY` can void an entire folder's UIDs at any time. UIDs
appear only in `plan_items`, always paired with the `UIDVALIDITY` they were
observed under, and every batch re-verifies each message's `msg_key` against
the server immediately before moving it. A mismatch is skipped, never moved —
which is what makes it safe to review a plan on Monday and apply it on Friday.

**Errors are classified, never swallowed.** Fatal errors (bad config, auth
failure, `UIDVALIDITY` changed, an unsafe server) abort with nothing partial.
Transient errors retry with exponential backoff and re-verify `UIDVALIDITY`
after reconnecting. Per-item failures record the server's verbatim response and
let the run continue. No bare `except:` and no `except Exception: pass` appear
anywhere; CI enforces this.

**Two safety properties are structural rather than conventional.** Header
fetches always use `BODY.PEEK`, so surveying never sets `\Seen` — a `make
check-peek` grep gate and an integration test against a live server both guard
it. And `assert_safe()` runs inside `Mailbox.move()` itself, not only at the
call sites, so no code path can perform a destructive write against a server
that advertises neither `MOVE` nor `UIDPLUS`.

## Testing

```bash
make test           # unit tests
make lint           # ruff, plus the non-PEEK BODY[ fetch guard
make integration-up # two Dovecot containers, '/' and '.' separators
make integration    # integration suite against them
make integration-down
```

The integration suite runs against real Dovecot rather than mocks, because
three properties cannot be verified any other way: that surveying never sets
`\Seen`, that the `COPY` fallback never expunges unrelated `\Deleted` mail,
and that a server using `.` as its hierarchy delimiter still produces a flat
`amazon_com` folder rather than a shattered `amazon/com` tree.

## License

MIT. See `LICENSE`.
