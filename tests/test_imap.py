"""Tests for the IMAP layer's decision logic, against a stub client.

Live protocol behavior (real IMAPClient wire traffic) is covered by Task 12.
"""

import pytest

from imapclient.exceptions import IMAPClientError

from mailgonizer.config import Config, ExecutionConfig, ServerConfig
from mailgonizer.imap import Mailbox, TransientError, UnsafeServerError


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
