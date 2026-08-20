import pytest

from mailgonizer.config import ConfigError, load_config

MINIMAL = """
server:
  host: mail.example.com
  username: doug@example.com
  password_env: MAILGONIZER_PASSWORD
"""


def write(tmp_path, text):
    p = tmp_path / "config.yaml"
    p.write_text(text)
    return p


def test_defaults_match_the_spec(tmp_path, monkeypatch):
    monkeypatch.setenv("MAILGONIZER_PASSWORD", "hunter2")
    cfg = load_config(write(tmp_path, MINIMAL))
    assert cfg.server.port == 993
    assert cfg.server.ssl is True
    assert cfg.source.folder == "INBOX"
    assert cfg.source.age_days == 90
    assert cfg.archive.root == "Crono_Archive"
    assert cfg.archive.promote_threshold == 13
    assert cfg.archive.lists_folder == "lists"
    assert cfg.archive.unknown_folder == "_unknown"
    assert cfg.naming.domain_separator == "_"
    assert cfg.naming.max_component_length == 64
    assert cfg.exclusions.keep_flagged is True
    assert cfg.exclusions.never_archive == ()
    assert cfg.execution.batch_size == 200
    assert cfg.execution.max_moves_per_run == 0
    assert cfg.execution.subscribe_created_folders is False
    assert cfg.logging.retention_runs == 24


def test_missing_required_field_is_rejected(tmp_path):
    with pytest.raises(ConfigError, match="server.host"):
        load_config(write(tmp_path, "server:\n  username: a@b.com\n"))


def test_separator_may_not_be_a_hierarchy_delimiter(tmp_path, monkeypatch):
    monkeypatch.setenv("MAILGONIZER_PASSWORD", "hunter2")
    text = MINIMAL + 'naming:\n  domain_separator: "."\n'
    with pytest.raises(ConfigError, match="domain_separator"):
        load_config(write(tmp_path, text))


def test_unknown_key_is_rejected_rather_than_ignored(tmp_path, monkeypatch):
    monkeypatch.setenv("MAILGONIZER_PASSWORD", "hunter2")
    text = MINIMAL + "archive:\n  promote_threshhold: 20\n"
    with pytest.raises(ConfigError, match="promote_threshhold"):
        load_config(write(tmp_path, text))


def test_timezone_must_be_resolvable(tmp_path, monkeypatch):
    monkeypatch.setenv("MAILGONIZER_PASSWORD", "hunter2")
    text = MINIMAL + 'dates:\n  timezone: "Mars/Olympus_Mons"\n'
    with pytest.raises(ConfigError, match="timezone"):
        load_config(write(tmp_path, text))


def test_password_resolves_from_environment(tmp_path, monkeypatch):
    monkeypatch.setenv("MAILGONIZER_PASSWORD", "hunter2")
    cfg = load_config(write(tmp_path, MINIMAL))
    assert cfg.server.password == "hunter2"


def test_missing_password_env_is_reported_clearly(tmp_path, monkeypatch):
    monkeypatch.delenv("MAILGONIZER_PASSWORD", raising=False)
    with pytest.raises(ConfigError, match="MAILGONIZER_PASSWORD"):
        load_config(write(tmp_path, MINIMAL))


def test_never_archive_is_coerced_to_a_tuple(tmp_path, monkeypatch):
    monkeypatch.setenv("MAILGONIZER_PASSWORD", "hunter2")
    text = MINIMAL + 'exclusions:\n  never_archive: ["a@b.com"]\n'
    cfg = load_config(write(tmp_path, text))
    assert cfg.exclusions.never_archive == ("a@b.com",)
    assert isinstance(cfg.exclusions.never_archive, tuple)


def test_never_archive_rejects_a_bare_string(tmp_path, monkeypatch):
    monkeypatch.setenv("MAILGONIZER_PASSWORD", "hunter2")
    text = MINIMAL + "exclusions:\n  never_archive: alice@example.com\n"
    with pytest.raises(ConfigError, match="never_archive"):
        load_config(write(tmp_path, text))


def test_never_archive_rejects_a_null_value(tmp_path, monkeypatch):
    monkeypatch.setenv("MAILGONIZER_PASSWORD", "hunter2")
    text = MINIMAL + "exclusions:\n  never_archive:\n"
    with pytest.raises(ConfigError, match="never_archive"):
        load_config(write(tmp_path, text))


def test_non_mapping_server_value_is_rejected(tmp_path):
    with pytest.raises(ConfigError, match="server"):
        load_config(write(tmp_path, "server: not-a-mapping\n"))
