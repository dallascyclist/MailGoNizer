from email import message_from_string
from email.policy import default as default_policy

import pytest

from mailgonizer.config import ArchiveConfig, Config, NamingConfig, ServerConfig
from mailgonizer.psl import PublicSuffixList
from mailgonizer.sender import (
    archive_path,
    derive_sender,
    escape_component,
    matches_never_archive,
)

RULES = "com\norg\nuk\nco.uk\ngithub.io\n"


@pytest.fixture
def psl():
    return PublicSuffixList(RULES.splitlines())


@pytest.fixture
def cfg():
    return Config(
        server=ServerConfig(host="h", username="u", password="p"),
        archive=ArchiveConfig(),
        naming=NamingConfig(),
    )


def parse(text):
    return message_from_string(text, policy=default_policy)


# --- escaping -------------------------------------------------------------

@pytest.mark.parametrize(
    "raw,expected",
    [
        ("amazon.com", "amazon_com"),
        ("AMAZON.COM", "amazon_com"),
        ("dovecot.dovecot.org", "dovecot_dovecot_org"),
        ("orders", "orders"),
        ("weird name!", "weird_name"),
        ("a...b", "a_b"),
        ("__leading_and_trailing__", "leading_and_trailing"),
        ("münchen.de", "m_nchen_de"),
    ],
)
def test_escape_component(raw, expected):
    assert escape_component(raw) == expected


def test_escape_is_idempotent():
    for raw in ["amazon.com", "weird name!", "a" * 200, "münchen.de", "!!!"]:
        once = escape_component(raw)
        assert escape_component(once) == once


def test_escape_never_emits_a_hierarchy_delimiter():
    for raw in ["amazon.com", "a/b/c", "x.y.z", "a\\b"]:
        out = escape_component(raw)
        assert "." not in out and "/" not in out and "\\" not in out


def test_escape_truncates_with_a_disambiguating_hash():
    a = escape_component("x" * 100 + "aaa")
    b = escape_component("x" * 100 + "bbb")
    assert len(a) <= 64 and len(b) <= 64
    assert a != b


def test_escape_of_empty_input_is_still_a_valid_component():
    assert escape_component("") == "_"
    assert escape_component("...") == "_"


def test_escape_honours_a_configured_separator():
    assert escape_component("amazon.com", separator="-") == "amazon-com"


# --- sender derivation ----------------------------------------------------

def test_list_id_wins_over_from(psl):
    msg = parse(
        "From: someone@gmail.com\n"
        "List-Id: Dovecot Mailing List <dovecot.dovecot.org>\n\n"
    )
    s = derive_sender(msg, psl)
    assert s.kind == "list"
    assert s.list_id == "dovecot.dovecot.org"
    assert s.source_header == "list-id"


@pytest.mark.parametrize(
    "value,expected",
    [
        ("Dovecot Mailing List <dovecot.dovecot.org>", "dovecot.dovecot.org"),
        ("<dovecot.dovecot.org>", "dovecot.dovecot.org"),
        ("dovecot.dovecot.org", "dovecot.dovecot.org"),
        ("  <Spaced.List.ORG>  ", "spaced.list.org"),
    ],
)
def test_list_id_dialects(psl, value, expected):
    s = derive_sender(parse(f"List-Id: {value}\n\n"), psl)
    assert s.list_id == expected


def test_from_yields_etld_plus_one_and_local_part(psl):
    s = derive_sender(parse("From: Orders <orders@mail.amazon.com>\n\n"), psl)
    assert s.kind == "domain"
    assert s.domain == "amazon.com"
    assert s.local == "orders"
    assert s.source_header == "from"


def test_subdomains_collapse_to_the_same_domain(psl):
    a = derive_sender(parse("From: a@mail.amazon.com\n\n"), psl)
    b = derive_sender(parse("From: a@email.marketing.amazon.com\n\n"), psl)
    assert a.domain == b.domain == "amazon.com"


def test_github_pages_hosts_do_not_collapse(psl):
    a = derive_sender(parse("From: a@user.github.io\n\n"), psl)
    b = derive_sender(parse("From: a@other.github.io\n\n"), psl)
    assert a.domain != b.domain


def test_encoded_display_name_does_not_break_address_extraction(psl):
    msg = parse("From: =?utf-8?q?M=C3=BCnchen?= <info@example.com>\n\n")
    s = derive_sender(msg, psl)
    assert s.domain == "example.com"
    assert s.local == "info"


def test_first_address_wins_when_from_lists_several(psl):
    s = derive_sender(parse("From: a@first.com, b@second.com\n\n"), psl)
    assert s.domain == "first.com"


def test_falls_back_to_sender_then_return_path(psl):
    s = derive_sender(parse("Sender: s@sender.com\n\n"), psl)
    assert s.domain == "sender.com" and s.source_header == "sender"

    s = derive_sender(parse("Return-Path: <r@bounce.com>\n\n"), psl)
    assert s.domain == "bounce.com" and s.source_header == "return-path"


def test_reply_to_is_deliberately_not_consulted(psl):
    s = derive_sender(parse("Reply-To: r@replyto.com\n\n"), psl)
    assert s.kind == "unknown"


def test_unparseable_sender_becomes_unknown(psl):
    for header in ["From: not an address\n\n", "From: \n\n", "\n"]:
        assert derive_sender(parse(header), psl).kind == "unknown"


def test_dotless_domain_survives(psl):
    """root@localhost has no registrable domain; the literal must still work."""
    s = derive_sender(parse("From: root@localhost\n\n"), psl)
    assert s.kind == "domain"
    assert s.domain == "localhost"


# --- destination paths ----------------------------------------------------

def test_archive_path_for_unpromoted_domain(cfg, psl):
    s = derive_sender(parse("From: orders@mail.amazon.com\n\n"), psl)
    assert archive_path(s, 2019, cfg, "/", promoted=False) == \
        "Crono_Archive/2019/amazon_com"


def test_archive_path_for_promoted_domain_uses_local_part_only(cfg, psl):
    s = derive_sender(parse("From: orders@mail.amazon.com\n\n"), psl)
    assert archive_path(s, 2019, cfg, "/", promoted=True) == \
        "Crono_Archive/2019/amazon_com/orders"


def test_archive_path_honours_the_server_delimiter(cfg, psl):
    s = derive_sender(parse("From: orders@amazon.com\n\n"), psl)
    assert archive_path(s, 2019, cfg, ".", promoted=False) == \
        "Crono_Archive.2019.amazon_com"


def test_archive_path_for_lists(cfg, psl):
    s = derive_sender(parse("List-Id: <dovecot.dovecot.org>\n\n"), psl)
    assert archive_path(s, 2019, cfg, "/", promoted=False) == \
        "Crono_Archive/2019/lists/dovecot_dovecot_org"


def test_archive_path_for_unknown(cfg, psl):
    s = derive_sender(parse("From: garbage\n\n"), psl)
    assert archive_path(s, 2019, cfg, "/", promoted=False) == \
        "Crono_Archive/2019/_unknown"


def test_configured_folder_names_are_used_verbatim(psl):
    cfg = Config(
        server=ServerConfig(host="h", username="u", password="p"),
        archive=ArchiveConfig(root="My.Archive", unknown_folder="_NoSender"),
    )
    s = derive_sender(parse("From: garbage\n\n"), psl)
    assert archive_path(s, 2019, cfg, "/", promoted=False) == \
        "My.Archive/2019/_NoSender"


# --- never-archive matching ----------------------------------------------

def test_never_archive_matches_full_address_and_bare_domain(psl):
    s = derive_sender(parse("From: orders@mail.amazon.com\n\n"), psl)
    assert matches_never_archive(s, ("orders@amazon.com",))
    assert matches_never_archive(s, ("amazon.com",))
    assert matches_never_archive(s, ("AMAZON.COM",))
    assert not matches_never_archive(s, ("other.com", "sales@amazon.com"))


def test_never_archive_matches_a_list_id(psl):
    s = derive_sender(parse("List-Id: <dovecot.dovecot.org>\n\n"), psl)
    assert matches_never_archive(s, ("dovecot.dovecot.org",))
