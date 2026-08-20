from datetime import datetime, timezone
from email import message_from_string
from email.policy import default as default_policy

import pytest

from mailgonizer.config import Config, DatesConfig, ServerConfig
from mailgonizer.dates import resolve_date

INTERNAL = datetime(2011, 6, 1, 12, 0, tzinfo=timezone.utc)


@pytest.fixture
def cfg():
    return Config(
        server=ServerConfig(host="h", username="u", password="p"),
        dates=DatesConfig(timezone="America/Chicago"),
    )


def parse(text):
    return message_from_string(text, policy=default_policy)


def test_date_header_is_preferred(cfg):
    dt, src = resolve_date(parse("Date: Tue, 3 Mar 2009 10:00:00 -0600\n\n"),
                           INTERNAL, cfg)
    assert src == "date"
    assert (dt.year, dt.month, dt.day) == (2009, 3, 3)


def test_result_is_expressed_in_the_configured_timezone(cfg):
    dt, _ = resolve_date(parse("Date: Tue, 3 Mar 2009 10:00:00 +0000\n\n"),
                         INTERNAL, cfg)
    assert str(dt.tzinfo) == "America/Chicago"
    assert dt.hour == 4


def test_year_boundary_uses_local_time_not_utc(cfg):
    """2020-01-01 03:00 UTC is 2019-12-31 in US Central."""
    dt, _ = resolve_date(parse("Date: Wed, 1 Jan 2020 03:00:00 +0000\n\n"),
                         INTERNAL, cfg)
    assert dt.year == 2019


def test_future_date_is_rejected_and_falls_through(cfg):
    dt, src = resolve_date(parse("Date: Fri, 1 Jan 2038 00:00:00 +0000\n\n"),
                           INTERNAL, cfg)
    assert src == "internaldate"
    assert dt.year == 2011


def test_prehistoric_date_is_rejected(cfg):
    dt, src = resolve_date(parse("Date: Thu, 1 Jan 1970 00:00:00 +0000\n\n"),
                           INTERNAL, cfg)
    assert src == "internaldate"


def test_unparseable_date_falls_through_to_received(cfg):
    msg = parse(
        "Date: garbage\n"
        "Received: from a.example.com by b.example.com; "
        "Mon, 2 Feb 2009 08:00:00 -0600\n\n"
    )
    dt, src = resolve_date(msg, INTERNAL, cfg)
    assert src == "received"
    assert (dt.year, dt.month, dt.day) == (2009, 2, 2)


def test_the_last_received_header_is_used_because_it_is_the_origin(cfg):
    """Received: headers are prepended in transit, so the last one is earliest."""
    msg = parse(
        "Received: from relay2 by final; Wed, 4 Mar 2009 12:00:00 +0000\n"
        "Received: from relay1 by relay2; Tue, 3 Mar 2009 12:00:00 +0000\n"
        "Received: from origin by relay1; Mon, 2 Mar 2009 12:00:00 +0000\n\n"
    )
    dt, src = resolve_date(msg, INTERNAL, cfg)
    assert src == "received"
    assert dt.day == 2


def test_received_without_a_semicolon_is_skipped(cfg):
    msg = parse(
        "Received: malformed no timestamp here\n"
        "Received: from origin by relay1; Mon, 2 Mar 2009 12:00:00 +0000\n\n"
    )
    dt, src = resolve_date(msg, INTERNAL, cfg)
    assert src == "received" and dt.day == 2


def test_internaldate_is_the_final_fallback(cfg):
    dt, src = resolve_date(parse("Subject: nothing useful\n\n"), INTERNAL, cfg)
    assert src == "internaldate"
    assert dt.year == 2011


def test_date_is_not_rejected_merely_for_diverging_from_internaldate(cfg):
    """Migration-flattened INTERNALDATEs must not veto good Date: headers."""
    dt, src = resolve_date(parse("Date: Tue, 3 Mar 1998 10:00:00 +0000\n\n"),
                           INTERNAL, cfg)
    assert src == "date"
    assert dt.year == 1998


def test_naive_date_is_interpreted_in_the_configured_timezone(cfg):
    dt, src = resolve_date(parse("Date: Tue, 3 Mar 2009 10:00:00 -0000\n\n"),
                           INTERNAL, cfg)
    assert src == "date"
    assert str(dt.tzinfo) == "America/Chicago"
    assert dt.hour == 10


def test_naive_internaldate_is_rejected(cfg):
    """A naive internaldate must never be silently guessed at (as UTC or as
    local time) — IMAPClient.normalise_times=True (the default) produces
    naive-but-local datetimes, so treating one as naive UTC would shift the
    resolved date by the host's offset. resolve_date must fail loudly
    instead, matching compute_msg_key's contract.
    """
    naive = datetime(2011, 6, 1, 12, 0)
    with pytest.raises(ValueError, match="timezone-aware"):
        resolve_date(parse("Subject: x\n\n"), naive, cfg)
