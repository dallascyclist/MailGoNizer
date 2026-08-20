"""IMAP connection, capability probing, header sweeps, and moves.

This layer reads and writes. It makes no decisions about where mail belongs.
"""

from __future__ import annotations

import email
from collections.abc import Iterator
from dataclasses import dataclass
from email.policy import default as default_policy

from imapclient import IMAPClient
from imapclient.exceptions import IMAPClientError

from mailgonizer.config import Config
from mailgonizer.records import HeaderRecord, compute_msg_key

_HEADER_FIELDS = "DATE FROM SENDER RETURN-PATH LIST-ID MESSAGE-ID RECEIVED"

# Requested with PEEK so the survey never sets \Seen.
_FETCH_HEADERS = f"BODY.PEEK[HEADER.FIELDS ({_HEADER_FIELDS})]".encode()

# The server answers WITHOUT the PEEK. Looking up the response by the request
# string raises KeyError, and the tempting "fix" is to drop .PEEK — which would
# silently mark thousands of unread messages as read.
_RESPONSE_HEADERS = f"BODY[HEADER.FIELDS ({_HEADER_FIELDS})]".encode()  # response-key

_FETCH_META = [b"INTERNALDATE", b"FLAGS", b"RFC822.SIZE"]

_IDENTITY_FIELDS = "MESSAGE-ID"
_FETCH_IDENTITY = f"BODY.PEEK[HEADER.FIELDS ({_IDENTITY_FIELDS})]".encode()
_RESPONSE_IDENTITY = f"BODY[HEADER.FIELDS ({_IDENTITY_FIELDS})]".encode()  # response-key


class FatalError(Exception):
    """Abort the run. Nothing partial."""


class TransientError(Exception):
    """Retry with backoff, then demote to per-item failures."""


class UnsafeServerError(FatalError):
    """The server cannot perform this job safely."""


@dataclass(frozen=True)
class Capabilities:
    delimiter: str
    has_move: bool
    has_uidplus: bool
    special_use: dict[str, str]


def _decode(value) -> str:
    return value.decode() if isinstance(value, bytes) else str(value)


def _require_aware(source: str, uid: int, internaldate) -> None:
    """internaldate must be timezone-aware, for the same reason as in
    compute_msg_key and resolve_date: a naive datetime has no defined
    instant, so guessing whether it means UTC or local time would silently
    shift every message's key and date by the host's offset.

    With the client constructed with normalise_times=False (see
    Mailbox.connect), the server always returns an aware INTERNALDATE. A
    naive value here means the client was constructed some other way — a
    regression, not something to paper over. Raises ValueError rather than
    guessing.
    """
    if internaldate.tzinfo is None:
        raise ValueError(
            f"{source} received a naive INTERNALDATE for uid {uid} "
            f"({internaldate!r}). A naive datetime has no defined instant, "
            "so guessing whether it means UTC or local time would silently "
            "shift the message's key and date by the host's offset. This "
            "should be unreachable when the IMAPClient is constructed with "
            "normalise_times=False; check how the client was constructed."
        )


class Mailbox:
    def __init__(self, client, cfg: Config) -> None:
        self.client = client
        self.cfg = cfg
        self._caps: Capabilities | None = None
        self._selected: str | None = None

    @classmethod
    def connect(cls, cfg: Config) -> Mailbox:
        try:
            client = IMAPClient(cfg.server.host, port=cfg.server.port,
                                ssl=cfg.server.ssl, timeout=cfg.server.timeout_seconds)
            # IMAPClient defaults to normalise_times=True, which returns
            # INTERNALDATE as a timezone-naive datetime silently adjusted to
            # local time — the UTC offset is destroyed and unrecoverable.
            # This must be set before any FETCH is issued. (Not a __init__
            # kwarg in this IMAPClient version — it is a settable attribute.)
            client.normalise_times = False
            client.login(cfg.server.username, cfg.server.password)
        except IMAPClientError as exc:
            raise FatalError(f"login to {cfg.server.host} failed: {exc}") from exc
        except OSError as exc:
            raise TransientError(f"cannot reach {cfg.server.host}: {exc}") from exc
        return cls(client, cfg)

    # --- capabilities -----------------------------------------------------

    def capabilities(self) -> Capabilities:
        if self._caps is not None:
            return self._caps
        raw = {_decode(c).upper() for c in self.client.capabilities()}
        listing = self.client.list_folders()
        delimiter = _decode(listing[0][1]) if listing else "/"
        special = {}
        for flags, _delim, name in listing:
            for flag in flags:
                text = _decode(flag)
                if text.lower() in ("\\archive", "\\sent", "\\trash", "\\junk",
                                    "\\drafts"):
                    special[text.lower()] = _decode(name)
        self._caps = Capabilities(delimiter=delimiter, has_move="MOVE" in raw,
                                  has_uidplus="UIDPLUS" in raw, special_use=special)
        return self._caps

    def assert_safe(self) -> None:
        caps = self.capabilities()
        if not caps.has_move and not caps.has_uidplus:
            raise UnsafeServerError(
                f"{self.cfg.server.host} advertises neither MOVE nor UIDPLUS. "
                "The only remaining option is COPY + STORE \\Deleted + a bare "
                "EXPUNGE, which would permanently remove every \\Deleted message "
                "in the folder — including mail you soft-deleted in a client and "
                "never expunged. Refusing to run."
            )

    # --- folders ----------------------------------------------------------

    def list_folders(self, prefix: str | None = None) -> list[str]:
        pattern = f"{prefix}{self.capabilities().delimiter}*" if prefix else "*"
        names = [_decode(name) for _flags, _d, name in
                 self.client.list_folders(pattern=pattern)]
        if prefix and self.client.folder_exists(prefix):
            names.append(prefix)
        return sorted(set(names))

    def ensure_folder(self, path: str, subscribe: bool) -> None:
        """Create every missing component, parents first.

        Not all servers auto-create intermediates, and a server that does will
        simply report the folder as existing.
        """
        delimiter = self.capabilities().delimiter
        parts = path.split(delimiter)
        for depth in range(1, len(parts) + 1):
            partial = delimiter.join(parts[:depth])
            if not self.client.folder_exists(partial):
                try:
                    self.client.create_folder(partial)
                except (IMAPClientError, RuntimeError) as exc:
                    # A concurrent client may have won the race. Existence is
                    # the success condition; anything else re-raises.
                    if not self.client.folder_exists(partial):
                        raise FatalError(
                            f"cannot create folder {partial!r}: {exc}"
                        ) from exc
            if subscribe:
                self.client.subscribe_folder(partial)

    def select(self, folder: str, readonly: bool = True) -> tuple[int, int]:
        info = self.client.select_folder(folder, readonly=readonly)
        self._selected = folder
        return int(info.get(b"UIDVALIDITY", 0)), int(info.get(b"UIDNEXT", 0))

    # --- reads ------------------------------------------------------------

    def fetch_headers(self, folder: str) -> Iterator[HeaderRecord]:
        uidvalidity, _ = self.select(folder, readonly=True)
        uids = self.client.search(["ALL"])
        chunk_size = self.cfg.execution.batch_size
        for start in range(0, len(uids), chunk_size):
            chunk = uids[start:start + chunk_size]
            response = self.client.fetch(chunk, [*_FETCH_META, _FETCH_HEADERS])
            for uid, data in response.items():
                raw = data.get(_RESPONSE_HEADERS, b"")
                headers = email.message_from_bytes(raw, policy=default_policy)
                internaldate = data[b"INTERNALDATE"]
                _require_aware("fetch_headers", int(uid), internaldate)
                yield HeaderRecord(
                    folder=folder, uid=int(uid), uidvalidity=uidvalidity,
                    internaldate=internaldate, size=int(data[b"RFC822.SIZE"]),
                    flags=tuple(_decode(f) for f in data.get(b"FLAGS", ())),
                    headers=headers,
                )

    def fetch_identity(self, uids: list[int]) -> dict[int, str]:
        """Recompute msg_key for live UIDs, for pre-move re-verification."""
        response = self.client.fetch(
            uids, [b"INTERNALDATE", b"RFC822.SIZE", _FETCH_IDENTITY]
        )
        out = {}
        for uid, data in response.items():
            raw = data.get(_RESPONSE_IDENTITY, b"")
            headers = email.message_from_bytes(raw, policy=default_policy)
            message_id = headers.get("message-id")
            internaldate = data[b"INTERNALDATE"]
            _require_aware("fetch_identity", int(uid), internaldate)
            out[int(uid)] = compute_msg_key(
                str(message_id) if message_id else None,
                internaldate, int(data[b"RFC822.SIZE"]),
            )
        return out

    def list_uids(self, folder: str) -> list[int]:
        self.select(folder, readonly=True)
        return [int(u) for u in self.client.search(["ALL"])]

    # --- writes -----------------------------------------------------------

    def move(self, uids: list[int], dst: str) -> None:
        caps = self.capabilities()
        try:
            if caps.has_move:
                self.client.move(uids, dst)
                return
            self.client.copy(uids, dst)
            self.client.add_flags(uids, ["\\Deleted"])
            # UID EXPUNGE, never a bare EXPUNGE.
            self.client.expunge(messages=uids)
        except IMAPClientError as exc:
            raise TransientError(f"move of {len(uids)} uids to {dst!r}: {exc}") from exc

    def close(self) -> None:
        try:
            self.client.logout()
        except (IMAPClientError, OSError) as exc:
            # Losing the connection on the way out changes nothing that matters,
            # but it is still worth surfacing rather than discarding.
            raise TransientError(f"logout failed: {exc}") from exc
