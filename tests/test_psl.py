import pytest

from mailgonizer.psl import PublicSuffixList

RULES = """
// ===BEGIN ICANN DOMAINS===
com
org
uk
co.uk
jp
*.ck
!www.ck
// ===END ICANN DOMAINS===
// ===BEGIN PRIVATE DOMAINS===
github.io
// ===END PRIVATE DOMAINS===
"""


@pytest.fixture
def psl():
    return PublicSuffixList(RULES.splitlines())


@pytest.mark.parametrize(
    "domain,expected",
    [
        ("amazon.com", "amazon.com"),
        ("mail.amazon.com", "amazon.com"),
        ("email.marketing.amazon.com", "amazon.com"),
        ("example.co.uk", "example.co.uk"),
        ("mail.example.co.uk", "example.co.uk"),
        ("user.github.io", "user.github.io"),
        ("blog.user.github.io", "user.github.io"),
    ],
)
def test_registrable_domain(psl, domain, expected):
    assert psl.registrable_domain(domain) == expected


def test_wildcard_rule(psl):
    assert psl.public_suffix("foo.ck") == "foo.ck"
    assert psl.registrable_domain("bar.foo.ck") == "bar.foo.ck"


def test_exception_rule_overrides_wildcard(psl):
    assert psl.public_suffix("www.ck") == "ck"
    assert psl.registrable_domain("www.ck") == "www.ck"


def test_unlisted_tld_falls_back_to_the_star_rule(psl):
    assert psl.public_suffix("thing.invalidtld") == "invalidtld"
    assert psl.registrable_domain("a.thing.invalidtld") == "thing.invalidtld"


def test_a_bare_public_suffix_has_no_registrable_domain(psl):
    assert psl.registrable_domain("co.uk") is None
    assert psl.registrable_domain("com") is None


def test_case_and_trailing_dot_are_normalised(psl):
    assert psl.registrable_domain("MAIL.Amazon.COM.") == "amazon.com"


def test_bundled_list_loads_and_reports_a_version():
    bundled = PublicSuffixList.bundled()
    assert bundled.registrable_domain("mail.amazon.com") == "amazon.com"
    assert bundled.version
