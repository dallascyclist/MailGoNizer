import email.utils
import os
import socket
from datetime import datetime, timedelta, timezone

import pytest

from mailgonizer.config import (
    ArchiveConfig, Config, ExecutionConfig, ServerConfig, SourceConfig,
)
from mailgonizer.imap import Mailbox

NOW = datetime.now(timezone.utc)
OLD = NOW - timedelta(days=400)


def _reachable(port: int) -> bool:
    with socket.socket() as sock:
        sock.settimeout(1)
        return sock.connect_ex(("127.0.0.1", port)) == 0


def _require(port: int) -> None:
    if not _reachable(port):
        pytest.skip(f"no Dovecot on port {port}; run `make integration-up`")


def make_cfg(port: int, user: str) -> Config:
    return Config(
        server=ServerConfig(host="127.0.0.1", port=port, ssl=False,
                            username=user, password="testpass"),
        source=SourceConfig(folder="INBOX", age_days=90),
        archive=ArchiveConfig(),
        execution=ExecutionConfig(batch_size=10, pause_between_batches_ms=0),
    )


@pytest.fixture
def slash_cfg(request):
    _require(10143)
    return make_cfg(10143, f"u{abs(hash(request.node.name)) % 100000}")


@pytest.fixture
def dot_cfg(request):
    _require(10144)
    return make_cfg(10144, f"u{abs(hash(request.node.name)) % 100000}")


@pytest.fixture
def mailbox(slash_cfg):
    mb = Mailbox.connect(slash_cfg)
    yield mb
    mb.client.logout()


def message(sender="orders@amazon.com", subject="hi", when=OLD, msgid=None):
    stamp = email.utils.format_datetime(when)
    mid = msgid or f"<{os.urandom(8).hex()}@test>"
    return (
        f"From: {sender}\r\n"
        f"To: doug@example.com\r\n"
        f"Subject: {subject}\r\n"
        f"Date: {stamp}\r\n"
        f"Message-ID: {mid}\r\n"
        f"\r\n"
        f"body of {subject}\r\n"
    ).encode()


def seed(mailbox, count=5, sender="orders@amazon.com", flags=(), when=OLD):
    for n in range(count):
        mailbox.client.append("INBOX", message(sender=sender, subject=f"m{n}",
                                               when=when),
                              flags=list(flags), msg_time=when)
    mailbox.select("INBOX", readonly=True)
    return mailbox.client.search(["ALL"])
