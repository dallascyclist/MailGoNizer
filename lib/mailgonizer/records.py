"""Dataclasses that cross phase boundaries, plus the durable message key."""

from __future__ import annotations

import hashlib
from dataclasses import dataclass
from datetime import datetime, timezone
from email.message import EmailMessage


def compute_msg_key(message_id: str | None, internaldate: datetime, size: int) -> str:
    """The durable identity of a message.

    Message-ID alone is neither guaranteed present nor unique — a mailbox
    legitimately holds the list copy and the direct copy of the same mail.
    INTERNALDATE, flags, and size are preserved across both MOVE (RFC 6851)
    and COPY (RFC 3501), so this key survives relocation while still telling
    genuine duplicates apart.

    internaldate must be timezone-aware. A naive datetime carries no defined
    instant, so this function would otherwise have to guess whether it means
    UTC or local time — and a wrong guess makes the key silently different
    on a different host, defeating the whole point of a relocatable archive.
    Raises ValueError rather than guessing.
    """
    if internaldate.tzinfo is None:
        raise ValueError(
            "compute_msg_key requires a timezone-aware internaldate; got a "
            f"naive datetime ({internaldate!r}). A naive datetime has no "
            "defined instant, so guessing whether it means UTC or local "
            "time would silently make the key host-dependent. Pass a "
            "timezone-aware datetime (e.g. construct IMAPClient with "
            "normalise_times=False)."
        )
    mid = (message_id or "").strip()
    stamp = internaldate.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S")
    return hashlib.sha256(f"{mid}\x00{stamp}\x00{size}".encode()).hexdigest()


@dataclass(frozen=True)
class HeaderRecord:
    """Raw SURVEY output. Deliberately uninterpreted."""

    folder: str
    uid: int
    uidvalidity: int
    internaldate: datetime          # tz-aware
    size: int
    flags: tuple[str, ...]
    headers: EmailMessage           # header-only parse


@dataclass(frozen=True)
class SenderKey:
    kind: str                       # 'list' | 'domain' | 'unknown'
    list_id: str | None
    domain: str | None              # eTLD+1, unescaped
    local: str | None               # local part, unescaped
    source_header: str              # 'list-id'|'from'|'sender'|'return-path'|'none'


@dataclass(frozen=True)
class ClassifiedMessage:
    msg_key: str
    message_id: str | None
    folder: str
    uid: int
    uidvalidity: int
    internaldate: datetime
    size: int
    resolved_date: datetime
    date_source: str                # 'date' | 'received' | 'internaldate'
    year: int
    sender: SenderKey
    flags: tuple[str, ...]


@dataclass(frozen=True)
class PlanItem:
    seq: int
    msg_key: str
    src_folder: str
    src_uid: int
    src_uidvalidity: int
    dst_folder: str
    reason: str                     # 'archive' | 'backfill'


@dataclass(frozen=True)
class SkipRecord:
    msg_key: str
    folder: str
    reason: str                     # 'deleted'|'flagged'|'never_archive'|'too_recent'|'in_place'
