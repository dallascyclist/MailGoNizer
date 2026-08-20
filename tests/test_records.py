"""Tests for compute_msg_key — the durable message identity."""

from __future__ import annotations

import os
import time
from datetime import datetime, timezone
from zoneinfo import ZoneInfo

from mailgonizer.records import compute_msg_key

UTC = timezone.utc


def test_utc_normalization_same_instant_different_timezones_same_key():
    # 17:00 UTC on 2020-06-01 is 12:00 in Chicago (UTC-5, CDT in June).
    utc_dt = datetime(2020, 6, 1, 17, 0, 0, tzinfo=UTC)
    chicago_dt = datetime(2020, 6, 1, 12, 0, 0, tzinfo=ZoneInfo("America/Chicago"))
    assert utc_dt == chicago_dt  # sanity: genuinely the same instant
    assert compute_msg_key("m1", utc_dt, 100) == compute_msg_key("m1", chicago_dt, 100)


def test_naive_internaldate_is_treated_as_local_system_time():
    """Locked-in, documented behaviour: compute_msg_key does not itself
    enforce tz-awareness (that contract lives on HeaderRecord.internaldate,
    which is documented '# tz-aware'). A naive datetime falls through to
    datetime.astimezone()'s own default: it is assumed to already be in the
    local system timezone. This test pins that fallback down explicitly, by
    fixing the system TZ for its duration, so a future refactor can't
    silently change it (e.g. by assuming naive datetimes are UTC) without a
    test failing.
    """
    original_tz = os.environ.get("TZ")
    os.environ["TZ"] = "America/Chicago"
    time.tzset()
    try:
        naive = datetime(2020, 6, 1, 12, 0, 0)
        aware_local = datetime(2020, 6, 1, 12, 0, 0, tzinfo=ZoneInfo("America/Chicago"))
        aware_utc_same_wallclock = datetime(2020, 6, 1, 12, 0, 0, tzinfo=UTC)

        key_naive = compute_msg_key("m1", naive, 100)
        key_local_aware = compute_msg_key("m1", aware_local, 100)
        key_utc_same_wallclock = compute_msg_key("m1", aware_utc_same_wallclock, 100)

        # Naive input is treated as local system time...
        assert key_naive == key_local_aware
        # ...and therefore differs from treating the same wall-clock digits
        # as already being UTC.
        assert key_naive != key_utc_same_wallclock
    finally:
        if original_tz is None:
            os.environ.pop("TZ", None)
        else:
            os.environ["TZ"] = original_tz
        time.tzset()


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
