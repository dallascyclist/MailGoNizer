"""Two log streams: a human narrative and a machine-readable decision record.

The first run classifies every message in a twenty-year inbox — plausibly
100k to 300k of them. Answering "why did this message go there" across that
much prose is painful; across JSONL it is one jq filter.
"""

from __future__ import annotations

import json
import resource
import sys
import time
from datetime import datetime
from pathlib import Path
from types import TracebackType

_LEVELS = {"debug": 10, "info": 20, "warn": 30, "error": 40}


def _rss_bytes() -> int:
    """Resident set size of this process.

    getrusage reports ru_maxrss in bytes on macOS and in kilobytes on Linux —
    the one portability wart worth handling, since a wrong unit here would
    misreport memory by 1024x on a run where memory is the thing an operator
    is watching.
    """
    usage = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss
    return usage if sys.platform == "darwin" else usage * 1024


def _human_seconds(seconds: float) -> str:
    seconds = int(max(seconds, 0))
    h, rem = divmod(seconds, 3600)
    m, s = divmod(rem, 60)
    if h:
        return f"{h}h{m:02d}m"
    if m:
        return f"{m}m{s:02d}s"
    return f"{s}s"


def _human_bytes(n: int) -> str:
    for unit in ("B", "KB", "MB", "GB"):
        if n < 1024 or unit == "GB":
            return f"{n:.0f}{unit}" if unit == "B" else f"{n:.1f}{unit}"
        n /= 1024.0
    return f"{n:.1f}GB"


class Progress:
    """Time-gated progress reporting for phases that can run for hours.

    A twenty-year mailbox makes the survey a multi-hour operation, and silence
    for that long is indistinguishable from a hang — an operator has no way to
    tell a working run from a wedged one. This emits a line every `interval`
    seconds rather than per item, so the cost is bounded no matter how many
    messages there are.

    The ETA is deliberately crude: total remaining divided by the average rate
    so far. It does not model the archive sweep being faster than the inbox
    sweep, or a server that slows under load. It is meant to answer "minutes or
    hours?", which is the question actually being asked.
    """

    def __init__(self, log: "RunLog", label: str, total: int | None = None,
                 interval: float = 5.0, clock=time.monotonic) -> None:
        self._log = log
        self._label = label
        self._total = total
        self._interval = interval
        self._clock = clock
        self._count = 0
        self._start = clock()
        self._last = self._start

    def tick(self, n: int = 1) -> None:
        self._count += n
        now = self._clock()
        if now - self._last >= self._interval:
            self._last = now
            self._emit(now)

    def done(self) -> None:
        self._emit(self._clock())

    def _emit(self, now: float) -> None:
        elapsed = max(now - self._start, 1e-9)
        rate = self._count / elapsed
        parts = [self._label]
        if self._total:
            pct = 100.0 * self._count / self._total
            parts.append(f"{self._count:,}/{self._total:,} ({pct:.0f}%)")
        else:
            parts.append(f"{self._count:,}")
        parts.append(f"{rate:,.0f}/s")
        parts.append(f"elapsed {_human_seconds(elapsed)}")
        if self._total and rate > 0 and self._count < self._total:
            parts.append(f"eta {_human_seconds((self._total - self._count) / rate)}")
        parts.append(f"rss {_human_bytes(_rss_bytes())}")
        self._log.info("  ".join(parts))


class RunLog:
    def __init__(self, narrative, jsonl, threshold: int) -> None:
        self._narrative = narrative
        self._jsonl = jsonl
        self._threshold = threshold

    @property
    def debug_enabled(self) -> bool:
        """Whether the configured threshold admits debug-level output.

        A fact about the sink's own configuration, not a policy decision —
        callers (e.g. do_plan's in_place suppression) use it to decide
        whether a given record is worth writing, but RunLog itself stays a
        dumb sink that knows only what level it was opened at.
        """
        return self._threshold <= _LEVELS["debug"]

    @staticmethod
    def stamp_now() -> str:
        """Zero-padded and numeric so lexical sort equals chronological sort."""
        return datetime.now().strftime("%Y%m%d-%H%M")

    @classmethod
    def open(cls, log_dir: Path, stamp: str, level: str = "info") -> RunLog:
        log_dir.mkdir(parents=True, exist_ok=True)
        narrative = (log_dir / f"{stamp}.log").open("a", encoding="utf-8")
        try:
            jsonl = (log_dir / f"{stamp}.jsonl").open(
                "a", encoding="utf-8", buffering=1
            )
        except OSError:
            narrative.close()
            raise
        return cls(narrative, jsonl, _LEVELS.get(level.lower(), 20))

    def __enter__(self) -> RunLog:
        return self

    def __exit__(self, exc_type: type[BaseException] | None,
                 exc: BaseException | None, tb: TracebackType | None) -> None:
        self.close()

    def _write(self, level: str, text: str) -> None:
        if _LEVELS[level] < self._threshold:
            return
        line = f"{datetime.now().isoformat(timespec='seconds')} {level.upper():5} {text}"
        self._narrative.write(line + "\n")
        self._narrative.flush()
        if _LEVELS[level] >= _LEVELS["info"]:
            print(line, flush=True)

    def debug(self, text: str) -> None:
        self._write("debug", text)

    def info(self, text: str) -> None:
        self._write("info", text)

    def warn(self, text: str) -> None:
        self._write("warn", text)

    def error(self, text: str) -> None:
        self._write("error", text)

    def phase(self, name: str) -> None:
        self._write("info", f"=== {name} ===")

    def decision(self, **fields) -> None:
        self._jsonl.write(json.dumps(fields, sort_keys=True, default=str) + "\n")

    def verdict(self, counts: dict) -> str:
        body = " ".join(f"{k}={v}" for k, v in sorted(counts.items()))
        text = f"VERDICT {body}"
        self._write("info", text)
        return text

    @staticmethod
    def prune(log_dir: Path, retention_runs: int) -> None:
        """Keep the newest N runs plus the very first, which is the baseline."""
        stamps = sorted(p.stem for p in log_dir.glob("*.log"))
        if len(stamps) <= retention_runs:
            return
        keep = set(stamps[-retention_runs:]) | {stamps[0]}
        for stamp in stamps:
            if stamp in keep:
                continue
            for suffix in (".log", ".jsonl"):
                target = log_dir / f"{stamp}{suffix}"
                if target.exists():
                    target.unlink()

    def close(self) -> None:
        self._narrative.close()
        self._jsonl.close()
