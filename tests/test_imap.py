"""Tests for the IMAP layer's decision logic, against a stub client.

Live protocol behavior (real IMAPClient wire traffic) is covered by Task 12.
"""

import imaplib

import pytest

from imapclient.exceptions import IMAPClientError

from mailgonizer.config import Config, ExecutionConfig, ServerConfig
from mailgonizer.imap import (
    _RESPONSE_HEADERS,
    FatalError,
    Mailbox,
    TransientError,
    UnsafeServerError,
)


class FakeClient:
    def __init__(self, capabilities=(b"IMAP4REV1", b"MOVE", b"UIDPLUS"),
                 folders=(), delimiter=b"/", expunge_fails=False):
        self._caps = capabilities
        self._folders = list(folders)
        self._delimiter = delimiter
        self.expunge_fails = expunge_fails
        self.created = []
        self.subscribed = []
        self.moved = []
        self.copied = []
        self.expunged = []
        self.flagged = []
        self.unflagged = []

    def capabilities(self):
        return self._caps

    def list_folders(self, directory="", pattern="*"):
        return [((), self._delimiter, name) for name in self._folders]

    def folder_exists(self, name):
        return name in self._folders

    def create_folder(self, name):
        if name in self._folders:
            raise RuntimeError("NO Mailbox already exists")
        self._folders.append(name)
        self.created.append(name)

    def subscribe_folder(self, name):
        self.subscribed.append(name)

    def move(self, uids, dst):
        self.moved.append((tuple(uids), dst))

    def copy(self, uids, dst):
        self.copied.append((tuple(uids), dst))

    def add_flags(self, uids, flags):
        self.flagged.append((tuple(uids), tuple(flags)))

    def remove_flags(self, uids, flags):
        self.unflagged.append((tuple(uids), tuple(flags)))

    def expunge(self, messages=None):
        if self.expunge_fails:
            raise IMAPClientError("NO [SERVERBUG] expunge failed")
        self.expunged.append(tuple(messages) if messages else None)

    def logout(self):
        pass


def cfg(**over):
    return Config(
        server=ServerConfig(host="h", username="u", password="p"),
        execution=ExecutionConfig(**over.pop("execution", {})),
    )


def test_delimiter_is_probed_from_the_server():
    mb = Mailbox(FakeClient(folders=["INBOX"], delimiter=b"."), cfg())
    assert mb.capabilities().delimiter == "."


def test_move_and_uidplus_are_detected():
    caps = mb_caps(FakeClient())
    assert caps.has_move and caps.has_uidplus


def mb_caps(client):
    return Mailbox(client, cfg()).capabilities()


def test_a_server_with_neither_move_nor_uidplus_is_refused():
    client = FakeClient(capabilities=(b"IMAP4REV1",), folders=["INBOX"])
    mb = Mailbox(client, cfg())
    with pytest.raises(UnsafeServerError, match="EXPUNGE"):
        mb.assert_safe()


def test_uidplus_alone_is_acceptable():
    client = FakeClient(capabilities=(b"IMAP4REV1", b"UIDPLUS"), folders=["INBOX"])
    Mailbox(client, cfg()).assert_safe()


def test_move_alone_is_acceptable():
    client = FakeClient(capabilities=(b"IMAP4REV1", b"MOVE"), folders=["INBOX"])
    Mailbox(client, cfg()).assert_safe()


def test_move_refuses_an_unsafe_server_on_its_own_account():
    """Every call site calls assert_safe() first today, and they stay -- they
    fail fast, before surveying 300,000 headers. But move() is the command
    that actually relocates mail, so it refuses without being asked. This is
    the backstop for the fifth call site someone adds in 2027."""
    client = FakeClient(capabilities=(b"IMAP4REV1",), folders=["INBOX"])
    mb = Mailbox(client, cfg())

    # The COPY-fallback branch below refuses this same server, so matching on
    # assert_safe()'s own wording ("Refusing to run", against the fallback's
    # "Refusing to move mail") is what proves the entry guard is the one that
    # fired -- and what makes deleting it a visible regression.
    with pytest.raises(UnsafeServerError, match="Refusing to run"):
        mb.move([1, 2], "dst")

    assert client.moved == [] and client.copied == []


def test_the_copy_fallback_states_its_own_uidplus_precondition(monkeypatch):
    """assert_safe() only rules out "neither MOVE nor UIDPLUS", which leaves
    this branch's real precondition -- UID EXPUNGE exists -- true only two
    functions away. Stated where it applies, it still holds if assert_safe()
    is ever loosened."""
    client = FakeClient(capabilities=(b"IMAP4REV1",), folders=["INBOX"])
    mb = Mailbox(client, cfg())
    # Stand in for a future assert_safe() that no longer covers this case.
    monkeypatch.setattr(mb, "assert_safe", lambda: None)

    with pytest.raises(UnsafeServerError, match="UIDPLUS"):
        mb.move([1, 2], "dst")

    # Refused before COPY, so no half-done move to clean up.
    assert client.copied == [] and client.flagged == [] and client.expunged == []


def test_a_failed_expunge_rolls_back_the_deleted_flag_it_set():
    """Left set, \\Deleted loses no mail but is untidy in a way neither the
    operator nor this tool would detect: the planner skips these messages
    forever as "deleted", the user's client shows them awaiting compaction,
    and the retry COPYs them to the destination a second time."""
    client = FakeClient(capabilities=(b"IMAP4REV1", b"UIDPLUS"),
                        folders=["INBOX"], expunge_fails=True)
    mb = Mailbox(client, cfg())

    with pytest.raises(TransientError):
        mb.move([1, 2], "dst")

    assert client.copied == [((1, 2), "dst")]
    assert client.flagged == [((1, 2), ("\\Deleted",))]
    assert client.unflagged == [((1, 2), ("\\Deleted",))]


def test_ensure_folder_creates_parents_first():
    client = FakeClient(folders=["INBOX"], delimiter=b"/")
    mb = Mailbox(client, cfg())
    mb.ensure_folder("Crono_Archive/2019/amazon_com/orders", subscribe=False)
    assert client.created == [
        "Crono_Archive",
        "Crono_Archive/2019",
        "Crono_Archive/2019/amazon_com",
        "Crono_Archive/2019/amazon_com/orders",
    ]


def test_ensure_folder_is_idempotent():
    client = FakeClient(folders=["INBOX", "Crono_Archive", "Crono_Archive/2019"])
    mb = Mailbox(client, cfg())
    mb.ensure_folder("Crono_Archive/2019", subscribe=False)
    assert client.created == []


def test_subscription_is_off_by_default_and_honoured_when_on():
    client = FakeClient(folders=["INBOX"])
    Mailbox(client, cfg()).ensure_folder("A/B", subscribe=False)
    assert client.subscribed == []

    client2 = FakeClient(folders=["INBOX"])
    Mailbox(client2, cfg()).ensure_folder("A/B", subscribe=True)
    assert client2.subscribed == ["A", "A/B"]


def test_move_uses_the_move_command_when_available():
    client = FakeClient()
    Mailbox(client, cfg()).move([1, 2, 3], "dst")
    assert client.moved == [((1, 2, 3), "dst")]
    assert client.copied == []


def test_move_falls_back_to_copy_and_uid_expunge():
    client = FakeClient(capabilities=(b"IMAP4REV1", b"UIDPLUS"))
    Mailbox(client, cfg()).move([1, 2], "dst")
    assert client.copied == [((1, 2), "dst")]
    assert client.flagged == [((1, 2), ("\\Deleted",))]
    # The UID list must be passed: a bare EXPUNGE would remove unrelated
    # \Deleted mail the owner never asked us to touch.
    assert client.expunged == [(1, 2)]


def test_fallback_never_issues_a_bare_expunge():
    client = FakeClient(capabilities=(b"IMAP4REV1", b"UIDPLUS"))
    Mailbox(client, cfg()).move([7], "dst")
    assert None not in client.expunged


# --- large-mailbox SEARCH windowing -------------------------------------
#
# A twenty-year INBOX answers `SEARCH ALL` with every UID on ONE untagged
# line. imaplib refuses any line over _MAXLINE (1,000,000 bytes), which at
# ~7-8 bytes per UID is a hard ceiling around 125,000 messages. The survey
# then dies before fetching a single header. These fakes reproduce that.

class _TooManyBytes(imaplib.IMAP4.error):
    pass


class LargeMailboxClient(FakeClient):
    """A mailbox big enough that an unbounded SEARCH blows imaplib's line limit."""

    def __init__(self, message_count=200_000, **kw):
        super().__init__(**kw)
        self._uids = list(range(1, message_count + 1))
        self.uidnext = message_count + 1
        self.search_criteria = []

    def select_folder(self, folder, readonly=True):
        return {b"UIDVALIDITY": 100, b"UIDNEXT": self.uidnext,
                b"EXISTS": len(self._uids)}

    def search(self, criteria):
        self.search_criteria.append(criteria)
        if criteria == ["ALL"] or criteria == "ALL":
            # What a real server + imaplib do: one line, over the limit.
            raise _TooManyBytes("got more than 1000000 bytes")
        if len(criteria) == 2 and str(criteria[0]).upper() == "UID":
            lo, _, hi = str(criteria[1]).partition(":")
            lo, hi = int(lo), int(hi)
            window = [u for u in self._uids if lo <= u <= hi]
            # Guard the property under test: no window may exceed the limit.
            approx = sum(len(str(u)) + 1 for u in window)
            if approx > imaplib._MAXLINE:
                raise _TooManyBytes("got more than 1000000 bytes")
            return window
        raise AssertionError(f"unexpected search criteria: {criteria!r}")

    def fetch(self, uids, specs):
        from datetime import datetime, timezone
        blob = b"From: a@example.com\r\nMessage-ID: <x@y>\r\n\r\n"
        return {u: {b"INTERNALDATE": datetime(2019, 1, 1, tzinfo=timezone.utc),
                    b"RFC822.SIZE": 100, b"FLAGS": (),
                    _RESPONSE_HEADERS: blob} for u in uids}


def test_fetch_headers_survives_a_mailbox_too_large_for_one_search():
    client = LargeMailboxClient(message_count=200_000, folders=["INBOX"])
    mb = Mailbox(client, cfg())
    first = next(mb.fetch_headers("INBOX"))
    assert first.uid == 1
    assert all(c != ["ALL"] for c in client.search_criteria), \
        "survey issued an unbounded SEARCH ALL"


def test_list_uids_survives_a_mailbox_too_large_for_one_search():
    client = LargeMailboxClient(message_count=200_000, folders=["INBOX"])
    mb = Mailbox(client, cfg())
    uids = mb.list_uids("INBOX")
    assert len(uids) == 200_000
    assert all(c != ["ALL"] for c in client.search_criteria), \
        "list_uids issued an unbounded SEARCH ALL"


# --- server-specific response-key spelling ------------------------------
#
# We request BODY.PEEK[HEADER.FIELDS (...)]; the server answers with a key
# that is supposed to be the same minus PEEK. Dovecot echoes it verbatim.
# CommuniGate Pro quotes every field name. An exact-string lookup misses the
# second form, and defaulting to b"" turned that miss into a 349,000-message
# survey that completed with every header field empty and every message filed
# under _unknown.

_DOVECOT_KEY = b'BODY[HEADER.FIELDS (DATE FROM SENDER RETURN-PATH LIST-ID MESSAGE-ID RECEIVED)]'
_COMMUNIGATE_KEY = (b'BODY[HEADER.FIELDS ("DATE" "FROM" "SENDER" "RETURN-PATH" '
                    b'"LIST-ID" "MESSAGE-ID" "RECEIVED")]')

_REAL_HEADERS = (b"Date: Fri, 8 Dec 2017 22:39:00 +0000\r\n"
                 b"From: Orders <orders@amazon.com>\r\n"
                 b"Message-ID: <abc123@amazon.com>\r\n\r\n")


class KeySpellingClient(FakeClient):
    """A server that spells the fetch response key its own way."""

    def __init__(self, response_key, include_body=True, **kw):
        super().__init__(**kw)
        self.response_key = response_key
        self.include_body = include_body

    def select_folder(self, folder, readonly=True):
        return {b"UIDVALIDITY": 100, b"UIDNEXT": 2, b"EXISTS": 1}

    def search(self, criteria):
        return [1]

    def fetch(self, uids, specs):
        from datetime import datetime, timezone
        data = {b"INTERNALDATE": datetime(2017, 12, 8, 22, 39, tzinfo=timezone.utc),
                b"RFC822.SIZE": 69678, b"FLAGS": (b"\\Seen",), b"SEQ": 1}
        if self.include_body:
            data[self.response_key] = _REAL_HEADERS
        return {u: dict(data) for u in uids}


@pytest.mark.parametrize("key,label", [
    (_DOVECOT_KEY, "dovecot"),
    (_COMMUNIGATE_KEY, "communigate-pro"),
])
def test_headers_are_found_whatever_the_server_calls_the_key(key, label):
    mb = Mailbox(KeySpellingClient(key, folders=["INBOX"]), cfg())
    [rec] = list(mb.fetch_headers("INBOX"))
    assert rec.headers.get("from") == "Orders <orders@amazon.com>", label
    assert rec.headers.get("message-id") == "<abc123@amazon.com>", label
    assert rec.headers.get("date") is not None, label


def test_missing_header_payload_fails_loudly_rather_than_yielding_nothing():
    """A survey that cannot find its headers must abort, not file 349k
    messages under _unknown while reporting success."""
    client = KeySpellingClient(_DOVECOT_KEY, include_body=False, folders=["INBOX"])
    mb = Mailbox(client, cfg())
    with pytest.raises(FatalError, match="header"):
        list(mb.fetch_headers("INBOX"))


def test_fetch_identity_also_tolerates_a_quoted_key():
    quoted = b'BODY[HEADER.FIELDS ("MESSAGE-ID")]'
    client = KeySpellingClient(quoted, folders=["INBOX"])
    mb = Mailbox(client, cfg())
    ident = mb.fetch_identity([1])

    # A 64-char digest proves nothing: compute_msg_key returns one even when
    # message_id is None. Assert the key matches one computed WITH the real
    # Message-ID, which is only possible if the payload was actually found.
    from datetime import datetime, timezone

    from mailgonizer.records import compute_msg_key
    expected = compute_msg_key("<abc123@amazon.com>",
                               datetime(2017, 12, 8, 22, 39, tzinfo=timezone.utc),
                               69678)
    assert ident == {1: expected}


# --- vanished messages vs. a server we cannot talk to -------------------
#
# A message expunged between plan and apply comes back as a degenerate FETCH:
# the server answers with FLAGS and SEQ and nothing else. That is a per-item
# condition the executor already handles as "vanished". A response that
# describes a real message but whose header key we cannot find is a different
# thing entirely and must still be fatal.

class VanishedMessageClient(FakeClient):
    """uid 1 is alive; uid 2 was expunged and comes back as FLAGS/SEQ only."""

    def select_folder(self, folder, readonly=True):
        return {b"UIDVALIDITY": 100, b"UIDNEXT": 3, b"EXISTS": 1}

    def search(self, criteria):
        return [1, 2]

    def fetch(self, uids, specs):
        from datetime import datetime, timezone
        out = {}
        for u in uids:
            if u == 2:
                out[u] = {b"FLAGS": (), b"SEQ": 2}          # gone
            else:
                out[u] = {b"INTERNALDATE": datetime(2017, 12, 8, 22, 39,
                                                    tzinfo=timezone.utc),
                          b"RFC822.SIZE": 69678, b"FLAGS": (), b"SEQ": 1,
                          _DOVECOT_KEY: _REAL_HEADERS}
        return out


def test_fetch_identity_omits_a_vanished_message_instead_of_aborting():
    mb = Mailbox(VanishedMessageClient(folders=["INBOX"]), cfg())
    ident = mb.fetch_identity([1, 2])
    assert 1 in ident, "the live message must still resolve"
    assert 2 not in ident, "a vanished message must be omitted, not fatal"


def test_survey_skips_a_vanished_message_rather_than_aborting():
    mb = Mailbox(VanishedMessageClient(folders=["INBOX"]), cfg())
    recs = list(mb.fetch_headers("INBOX"))
    assert [r.uid for r in recs] == [1]


def test_a_real_message_with_an_unfindable_header_key_is_still_fatal():
    """Degenerate responses are benign; a populated one we cannot read is not."""
    class WrongKeyClient(VanishedMessageClient):
        def fetch(self, uids, specs):
            from datetime import datetime, timezone
            return {u: {b"INTERNALDATE": datetime(2017, 12, 8, tzinfo=timezone.utc),
                        b"RFC822.SIZE": 100, b"FLAGS": (), b"SEQ": u,
                        b"SOMETHING.ELSE": b"payload"} for u in uids}
    mb = Mailbox(WrongKeyClient(folders=["INBOX"]), cfg())
    with pytest.raises(FatalError, match="header"):
        mb.fetch_identity([1])


def test_select_does_not_reissue_for_the_folder_already_selected():
    """48,744 destination groups once meant 48,744 full SELECTs of a
    190,000-message INBOX -- about seven hours of pure overhead."""
    client = VanishedMessageClient(folders=["INBOX"])
    calls = []
    original = client.select_folder
    client.select_folder = lambda f, readonly=True: (calls.append((f, readonly))
                                                     or original(f, readonly))
    mb = Mailbox(client, cfg())
    for _ in range(50):
        mb.select("INBOX", readonly=False)
    assert len(calls) == 1, f"re-selected {len(calls)} times"

    mb.select("INBOX", readonly=True)      # mode change must re-select
    mb.select("Other", readonly=False)     # folder change must re-select
    assert len(calls) == 3
