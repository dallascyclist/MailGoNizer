"""Tests for compute_msg_key — the durable message identity."""

from __future__ import annotations

from datetime import datetime, timezone
from zoneinfo import ZoneInfo

import pytest

from mailgonizer.records import compute_msg_key

UTC = timezone.utc


def test_utc_normalization_same_instant_different_timezones_same_key():
    """This is the whole point of normalizing to UTC before hashing: the
    archive directory is designed to be relocatable (tarred up and moved to
    a mail host in a different timezone later), so the same message must
    key identically no matter which zone produced its internaldate.
    """
    # 17:00 UTC on 2020-06-01 is 12:00 in Chicago (UTC-5, CDT in June) — two
    # independently constructed, differently-zoned representations of one
    # instant, not one derived from the other.
    utc_dt = datetime(2020, 6, 1, 17, 0, 0, tzinfo=UTC)
    chicago_dt = datetime(2020, 6, 1, 12, 0, 0, tzinfo=ZoneInfo("America/Chicago"))
    assert utc_dt == chicago_dt  # sanity: genuinely the same instant
    assert compute_msg_key("m1", utc_dt, 100) == compute_msg_key("m1", chicago_dt, 100)


def test_naive_internaldate_is_rejected():
    """A naive internaldate must never be silently guessed at (as UTC or as
    local time) — either guess can make the key differ by host, which is
    exactly the failure this key exists to prevent. compute_msg_key must
    fail loudly instead.
    """
    naive = datetime(2020, 6, 1, 12, 0, 0)
    with pytest.raises(ValueError, match="timezone-aware"):
        compute_msg_key("m1", naive, 100)


def test_message_id_none_and_empty_string_produce_the_same_key():
    dt = datetime(2020, 1, 1, tzinfo=UTC)
    assert compute_msg_key(None, dt, 100) == compute_msg_key("", dt, 100)


def test_different_message_id_yields_a_different_key():
    dt = datetime(2020, 1, 1, tzinfo=UTC)
    assert compute_msg_key("a@x", dt, 100) != compute_msg_key("b@x", dt, 100)


def test_different_size_yields_a_different_key():
    dt = datetime(2020, 1, 1, tzinfo=UTC)
    assert compute_msg_key("a@x", dt, 100) != compute_msg_key("a@x", dt, 101)


def test_different_internaldate_yields_a_different_key():
    a = compute_msg_key("a@x", datetime(2020, 1, 1, tzinfo=UTC), 100)
    b = compute_msg_key("a@x", datetime(2020, 1, 2, tzinfo=UTC), 100)
    assert a != b


def test_identical_message_id_with_different_internaldate_or_size_yields_different_keys():
    """The entire reason the key isn't just Message-ID: a mailbox can
    legitimately hold both a mailing-list copy and a direct copy of the same
    mail, sharing one Message-ID but differing in INTERNALDATE and/or size.
    """
    mid = "shared@example.com"
    base = compute_msg_key(mid, datetime(2020, 1, 1, 9, 0, tzinfo=UTC), 4000)

    diff_date = compute_msg_key(mid, datetime(2020, 1, 1, 9, 5, tzinfo=UTC), 4000)
    assert diff_date != base

    diff_size = compute_msg_key(mid, datetime(2020, 1, 1, 9, 0, tzinfo=UTC), 4200)
    assert diff_size != base

    diff_both = compute_msg_key(mid, datetime(2020, 1, 1, 9, 5, tzinfo=UTC), 4200)
    assert diff_both != base
    assert diff_both != diff_date
    assert diff_both != diff_size


def test_same_inputs_always_produce_the_same_stable_hex_key():
    dt = datetime(2020, 1, 1, 9, 0, tzinfo=UTC)
    a = compute_msg_key("m@x", dt, 100)
    b = compute_msg_key("m@x", dt, 100)
    assert a == b
    assert len(a) == 64
    assert all(c in "0123456789abcdef" for c in a)
