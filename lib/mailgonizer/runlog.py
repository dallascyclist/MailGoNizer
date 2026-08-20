"""Two log streams: a human narrative and a machine-readable decision record.

The first run classifies every message in a twenty-year inbox — plausibly
100k to 300k of them. Answering "why did this message go there" across that
much prose is painful; across JSONL it is one jq filter.
"""

from __future__ import annotations

import json
from datetime import datetime
from pathlib import Path
from types import TracebackType

_LEVELS = {"debug": 10, "info": 20, "warn": 30, "error": 40}


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
