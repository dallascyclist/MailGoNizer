"""Tests for the IMAP layer's decision logic, against a stub client.

Live protocol behavior (real IMAPClient wire traffic) is covered by Task 12.
"""

import pytest

from mailgonizer.config import Config, ExecutionConfig, ServerConfig
from mailgonizer.imap import Mailbox, UnsafeServerError


class FakeClient:
    def __init__(self, capabilities=(b"IMAP4REV1", b"MOVE", b"UIDPLUS"),
                 folders=(), delimiter=b"/"):
        self._caps = capabilities
        self._folders = list(folders)
        self._delimiter = delimiter
        self.created = []
        self.subscribed = []
        self.moved = []
        self.copied = []
        self.expunged = []
        self.flagged = []

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

    def expunge(self, messages=None):
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
