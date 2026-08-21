# First run

The first run against a twenty-year inbox is the highest-stakes thing this
tool will ever do: it's the run that decides where two decades of mail land,
and it's the run where a naming mistake is most expensive to notice late.
Walk through this once, carefully. After that, `mailgonizer-cron` handles it
every month without supervision.

This assumes you've already run `./install.sh` and edited `etc/config.yaml`
(see the main `README.md` if not).

## 1. Check the server

```bash
./bin/mailgonizer check
```

`check` connects to the server, probes for `MOVE`/`UIDPLUS` and any
`\Archive` special-use folder, and warns if the server's hierarchy delimiter
is `.` (a literal `.` in a domain name would collide with it) or if
`archive.root` collides with the server's own `\Archive` folder. It touches
neither the mailbox nor the database — it does write its own run log
(`log/<stamp>.log`, plus an empty `<stamp>.jsonl`), so every `check` you run
still leaves a record. Confirm the reported delimiter, confirm `MOVE` or
`UIDPLUS` is present, and if it warns about an `\Archive` collision, change
`archive.root` before going further.

## 2. Cap the blast radius

In `etc/config.yaml`:

```yaml
execution:
  max_moves_per_run: 200
```

The default is `0` (uncapped). For a first run against a mailbox nobody has
sorted in twenty years, cap it low enough that reviewing — and if needed,
undoing — a batch is cheap.

## 3. Plan, then read it

```bash
./bin/mailgonizer plan
./bin/mailgonizer show-plan --format csv > /tmp/plan.csv
```

`plan` only surveys headers and writes a plan to the database; it performs
no moves. Read the plan before applying anything. Specifically check:

- **The `_unknown` bucket.** A large `Crono_Archive/<year>/_unknown` means
  many messages have no `List-Id`, `From`, `Sender`, or `Return-Path` header
  that resolves to a sender — worth understanding before you file it away.
  Count it:

  ```bash
  ./bin/mailgonizer show-plan --format json | jq '[.[] | select(.dst_folder | test("_unknown$"))] | length'
  ```

- **Date sources.** Each message's date resolves from `Date:`, falling back
  to the last `Received:` header, falling back to the IMAP `INTERNALDATE`.
  A large `received` count means many `Date:` headers are missing,
  unparseable, or outside `dates.min_valid`. A large `internaldate` count
  means part of the corpus has no usable header date at all. See the
  breakdown per message:

  ```bash
  ./bin/mailgonizer export-index --format json | jq -r '.messages[].date_source' | sort | uniq -c
  ```

- **The promotion list.** The narrative log (`log/<stamp>.log`) reports each
  new `promote <domain>/<local> in <year> (<count> messages)` decision.
  These become per-sender subfolders permanently — a promotion is never
  reversed by a later run, even if that sender goes quiet.

## 4. Apply the capped plan

```bash
./bin/mailgonizer apply
```

`apply` executes exactly the plan you just read, not a fresh one — and
`max_moves_per_run` already truncated that plan back in step 3, so this
moves only the small, capped batch, not the whole inbox.
(`execution.batch_size` is a separate setting: it controls how many UIDs go
into each IMAP `MOVE`/`COPY` command, not how many messages get moved in
total.) If the mailbox drifted since `plan` ran, per-message identity
verification skips the affected messages rather than moving the wrong mail.

## 5. Inspect the result in a mail client

Look at the tree on a real client, phone included. This is the point where
naming decisions are still cheap to change — `undo` exists, use it before
thousands of messages have moved and reversing it gets slow.

```bash
./bin/mailgonizer undo --run 1   # if you want to start over
```

## 6. Lift the cap and let it run

```yaml
execution:
  max_moves_per_run: 0
```

```bash
./bin/mailgonizer run
```

Expect this to take a while on a twenty-year inbox, and expect the first log
pair to be large — the JSONL stream carries one record per message. That
first log pair is exempt from retention pruning on purpose: it's the
permanent record of how the archive was originally built.

## 7. Schedule it

```cron
17 3 1 * * /path/to/mailgonizer/bin/mailgonizer-cron
```
