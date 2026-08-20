"""Canonical date resolution: Date: -> last Received: -> INTERNALDATE."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from email.message import EmailMessage
from email.utils import parsedate_to_datetime
from zoneinfo import ZoneInfo

from mailgonizer.config import Config

_FUTURE_TOLERANCE = timedelta(hours=24)


def _localise(dt: datetime, tz: ZoneInfo) -> datetime:
    """A datetime with no offset is interpreted in the mailbox owner's zone.

    RFC 5322 uses -0000 to mean "offset unknown", which Python surfaces as a
    naive datetime. The owner's local zone is the best available guess.
    """
    if dt.tzinfo is None:
        return dt.replace(tzinfo=tz)
    return dt.astimezone(tz)


def _parse_rfc5322(raw: str) -> datetime | None:
    try:
        return parsedate_to_datetime(raw)
    except (TypeError, ValueError):
        return None


def _is_sane(dt: datetime, cfg: Config, now: datetime) -> bool:
    """Absolute window only. Divergence from INTERNALDATE is measured
    elsewhere and never used as a rejection test."""
    floor = datetime.combine(
        cfg.dates.min_valid, datetime.min.time(), tzinfo=timezone.utc
    )
    return floor <= dt.astimezone(timezone.utc) <= now + _FUTURE_TOLERANCE


def resolve_date(msg: EmailMessage, internaldate: datetime,
                 cfg: Config, now: datetime | None = None) -> tuple[datetime, str]:
    """Return the canonical (datetime, source) for one message.

    The same value drives both the year bucket and the age cutoff.

    internaldate must be timezone-aware, for the same reason as in
    compute_msg_key: IMAPClient's default normalise_times=True yields a
    naive-but-local datetime, not naive UTC, so guessing an offset here
    would silently shift every message's resolved date by the host's
    timezone. Raises ValueError rather than guessing.
    """
    if internaldate.tzinfo is None:
        raise ValueError(
            "resolve_date requires a timezone-aware internaldate; got a "
            f"naive datetime ({internaldate!r}). A naive datetime has no "
            "defined instant, so guessing whether it means UTC or local "
            "time would silently shift the resolved date by the host's "
            "offset. Pass a timezone-aware datetime (e.g. construct "
            "IMAPClient with normalise_times=False)."
        )

    tz = cfg.dates.tzinfo
    now = now or datetime.now(timezone.utc)

    raw_date = msg.get("date")
    if raw_date:
        parsed = _parse_rfc5322(str(raw_date))
        if parsed is not None:
            localised = _localise(parsed, tz)
            if _is_sane(localised, cfg, now):
                return localised, "date"

    # Received: headers are prepended in transit, so the LAST one is the
    # earliest hop — the origin, and the one least likely to have been
    # rewritten by a migration.
    for header in reversed(msg.get_all("received") or []):
        _, sep, tail = str(header).rpartition(";")
        if not sep:
            continue
        parsed = _parse_rfc5322(tail.strip())
        if parsed is None:
            continue
        localised = _localise(parsed, tz)
        if _is_sane(localised, cfg, now):
            return localised, "received"

    return internaldate.astimezone(tz), "internaldate"
