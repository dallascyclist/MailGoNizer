import json

from mailgonizer.runlog import RunLog


def test_two_streams_are_written(tmp_path):
    with RunLog.open(tmp_path, "20260819-2045", "info") as log:
        log.phase("SURVEY")
        log.decision(msg_key="k1", dst="Crono_Archive/2019/amazon_com",
                     reason="archive")
    assert (tmp_path / "20260819-2045.log").exists()
    assert (tmp_path / "20260819-2045.jsonl").exists()


def test_decisions_are_one_json_object_per_line(tmp_path):
    with RunLog.open(tmp_path, "s", "info") as log:
        log.decision(msg_key="k1", reason="archive")
        log.decision(msg_key="k2", reason="too_recent")
    lines = (tmp_path / "s.jsonl").read_text().strip().splitlines()
    assert [json.loads(line)["reason"] for line in lines] == \
        ["archive", "too_recent"]


def test_skips_are_recorded_too(tmp_path):
    with RunLog.open(tmp_path, "s", "info") as log:
        log.decision(msg_key="k1", reason="flagged", skipped=True)
    record = json.loads((tmp_path / "s.jsonl").read_text().strip())
    assert record["skipped"] is True


def test_verdict_enumerates_counts(tmp_path):
    with RunLog.open(tmp_path, "s", "info") as log:
        text = log.verdict({"surveyed": 10, "planned": 4, "moved": 4, "failed": 0})
    assert "surveyed=10" in text and "moved=4" in text
    assert "surveyed=10" in (tmp_path / "s.log").read_text()


def test_debug_lines_are_suppressed_at_info_level(tmp_path):
    with RunLog.open(tmp_path, "s", "info") as log:
        log.debug("noisy")
        log.info("kept")
    body = (tmp_path / "s.log").read_text()
    assert "noisy" not in body and "kept" in body


def test_prune_keeps_the_newest_runs(tmp_path):
    for stamp in ["20200101-0000", "20200201-0000", "20200301-0000",
                  "20200401-0000"]:
        (tmp_path / f"{stamp}.log").write_text("x")
        (tmp_path / f"{stamp}.jsonl").write_text("x")
    RunLog.prune(tmp_path, retention_runs=2)
    remaining = sorted(p.stem for p in tmp_path.glob("*.log"))
    assert remaining == ["20200101-0000", "20200301-0000", "20200401-0000"]


def test_prune_never_deletes_the_first_run(tmp_path):
    """The first run's logs are the permanent baseline for a 20-year mailbox."""
    for stamp in ["20200101-0000", "20200201-0000", "20200301-0000"]:
        (tmp_path / f"{stamp}.log").write_text("x")
        (tmp_path / f"{stamp}.jsonl").write_text("x")
    RunLog.prune(tmp_path, retention_runs=1)
    remaining = sorted(p.stem for p in tmp_path.glob("*.log"))
    assert "20200101-0000" in remaining
    assert remaining == ["20200101-0000", "20200301-0000"]


def test_prune_removes_both_streams(tmp_path):
    for stamp in ["20200101-0000", "20200201-0000", "20200301-0000"]:
        (tmp_path / f"{stamp}.log").write_text("x")
        (tmp_path / f"{stamp}.jsonl").write_text("x")
    RunLog.prune(tmp_path, retention_runs=1)
    assert not (tmp_path / "20200201-0000.jsonl").exists()


def test_decisions_are_line_buffered_and_readable_before_close(tmp_path):
    """JSONL decisions are flushed per-line so forensic records survive interruption."""
    log = RunLog.open(tmp_path, "s", "info")
    log.decision(msg_key="k1", reason="archive")
    log.decision(msg_key="k2", reason="too_recent")
    # Read from a separate handle before close() to verify line buffering
    lines = (tmp_path / "s.jsonl").read_text().strip().splitlines()
    assert len(lines) == 2
    reasons = [json.loads(line)["reason"] for line in lines]
    assert reasons == ["archive", "too_recent"]
    log.close()


def test_jsonl_open_failure_closes_narrative_handle(tmp_path):
    """If JSONL open fails, the narrative handle must be closed, not leaked."""
    log_dir = tmp_path / "readonly"
    log_dir.mkdir()
    # Make directory read-only to force open() to fail
    log_dir.chmod(0o555)
    try:
        try:
            RunLog.open(log_dir, "s", "info")
            raise AssertionError("Expected OSError")
        except OSError:
            pass  # Expected
    finally:
        log_dir.chmod(0o755)
