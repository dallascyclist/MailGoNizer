"""Sender key derivation and folder naming. Pure functions, no I/O."""

from __future__ import annotations

import hashlib
import re
from email.message import EmailMessage
from email.utils import getaddresses

from mailgonizer.config import Config
from mailgonizer.psl import PublicSuffixList
from mailgonizer.records import SenderKey

_UNSAFE = re.compile(r"[^a-z0-9_-]")
_RUNS = re.compile(r"_{2,}")
_LIST_ID_BRACKETED = re.compile(r"<([^>]+)>")

_ADDRESS_HEADERS = (("from", "from"), ("sender", "sender"), ("return-path", "return-path"))

UNKNOWN = SenderKey(kind="unknown", list_id=None, domain=None, local=None,
                     source_header="none")


def escape_component(raw: str, separator: str = "_", max_len: int = 64) -> str:
    """Make one path component safe on any IMAP server.

    Lowercase because folder names are case-sensitive on nearly every server
    (only INBOX is specially case-insensitive per RFC 3501), so unnormalised
    data would produce both `Orders` and `orders`.
    """
    out = raw.strip().lower().replace(".", separator)
    out = _UNSAFE.sub("_", out)
    out = _RUNS.sub("_", out).strip("_")
    if not out:
        return "_"
    if len(out) > max_len:
        tag = hashlib.sha256(raw.encode("utf-8", "replace")).hexdigest()[:8]
        out = out[: max_len - 9].rstrip("_") + "_" + tag
    return out


def _first_address(msg: EmailMessage, header: str) -> tuple[str, str] | None:
    values = msg.get_all(header)
    if not values:
        return None
    for _display, addr in getaddresses([str(v) for v in values]):
        addr = addr.strip().strip("<>").lower()
        if "@" not in addr:
            continue
        local, _, domain = addr.rpartition("@")
        if local and domain:
            return local, domain
    return None


def derive_sender(msg: EmailMessage, psl: PublicSuffixList) -> SenderKey:
    """Resolve the sender key. First match wins: List-Id, From, Sender, Return-Path.

    Reply-To is deliberately excluded — it is frequently a different party
    than the sender.
    """
    list_id = msg.get("list-id")
    if list_id:
        raw = str(list_id).strip()
        match = _LIST_ID_BRACKETED.search(raw)
        value = (match.group(1) if match else raw).strip().lower()
        if value:
            return SenderKey(kind="list", list_id=value, domain=None, local=None,
                              source_header="list-id")

    for header, label in _ADDRESS_HEADERS:
        found = _first_address(msg, header)
        if found is None:
            continue
        local, domain = found
        # A dotless host such as `localhost` has no registrable domain;
        # keeping the literal is better than discarding the sender entirely.
        registrable = psl.registrable_domain(domain) or domain
        return SenderKey(kind="domain", list_id=None, domain=registrable,
                          local=local, source_header=label)

    return UNKNOWN


def archive_path(sender: SenderKey, year: int, cfg: Config, delimiter: str,
                  promoted: bool) -> str:
    """Build the destination folder path.

    Configured names (root, lists_folder, unknown_folder) are used verbatim;
    escaping applies only to sender-derived components.
    """
    sep = cfg.naming.domain_separator
    max_len = cfg.naming.max_component_length
    parts = [cfg.archive.root, str(year)]

    if sender.kind == "list":
        parts += [cfg.archive.lists_folder,
                  escape_component(sender.list_id or "", sep, max_len)]
    elif sender.kind == "unknown":
        parts.append(cfg.archive.unknown_folder)
    else:
        parts.append(escape_component(sender.domain or "", sep, max_len))
        if promoted:
            parts.append(escape_component(sender.local or "", sep, max_len))

    return delimiter.join(parts)


def matches_never_archive(sender: SenderKey, patterns: tuple[str, ...]) -> bool:
    """Match against the resolved key *before* escaping.

    A bare domain matches that eTLD+1 and everything that collapses into it;
    a full address matches only that local part at that domain.
    """
    if not patterns:
        return False
    candidates = set()
    if sender.kind == "list" and sender.list_id:
        candidates.add(sender.list_id)
    if sender.kind == "domain" and sender.domain:
        candidates.add(sender.domain)
        if sender.local:
            candidates.add(f"{sender.local}@{sender.domain}")
    return any(p.strip().lower() in candidates for p in patterns)
