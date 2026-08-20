"""Configuration loading and validation."""

from __future__ import annotations

import os
import re
from dataclasses import dataclass, field
from datetime import date
from pathlib import Path
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

import yaml


class ConfigError(Exception):
    """Raised when configuration is missing, malformed, or contradictory."""


@dataclass(frozen=True)
class ServerConfig:
    host: str
    username: str
    password: str
    port: int = 993
    ssl: bool = True
    timeout_seconds: int = 60


@dataclass(frozen=True)
class SourceConfig:
    folder: str = "INBOX"
    age_days: int = 90


@dataclass(frozen=True)
class ArchiveConfig:
    root: str = "Crono_Archive"
    promote_threshold: int = 13
    lists_folder: str = "lists"
    unknown_folder: str = "_unknown"


@dataclass(frozen=True)
class NamingConfig:
    domain_separator: str = "_"
    max_component_length: int = 64


@dataclass(frozen=True)
class DatesConfig:
    timezone: str = "UTC"
    min_valid: date = date(1990, 1, 1)

    @property
    def tzinfo(self) -> ZoneInfo:
        return ZoneInfo(self.timezone)


@dataclass(frozen=True)
class ExclusionsConfig:
    keep_flagged: bool = True
    never_archive: tuple[str, ...] = ()


@dataclass(frozen=True)
class ExecutionConfig:
    batch_size: int = 200
    pause_between_batches_ms: int = 250
    max_moves_per_run: int = 0
    subscribe_created_folders: bool = False
    connect_retries: int = 3


@dataclass(frozen=True)
class LoggingConfig:
    level: str = "info"
    retention_runs: int = 24


@dataclass(frozen=True)
class Config:
    server: ServerConfig
    source: SourceConfig = field(default_factory=SourceConfig)
    archive: ArchiveConfig = field(default_factory=ArchiveConfig)
    naming: NamingConfig = field(default_factory=NamingConfig)
    dates: DatesConfig = field(default_factory=DatesConfig)
    exclusions: ExclusionsConfig = field(default_factory=ExclusionsConfig)
    execution: ExecutionConfig = field(default_factory=ExecutionConfig)
    logging: LoggingConfig = field(default_factory=LoggingConfig)


_SECTIONS = {
    "source": SourceConfig,
    "archive": ArchiveConfig,
    "naming": NamingConfig,
    "dates": DatesConfig,
    "exclusions": ExclusionsConfig,
    "execution": ExecutionConfig,
    "logging": LoggingConfig,
}

_SAFE_SEPARATOR = re.compile(r"^[a-z0-9_-]+$")


def _reject_unknown_keys(section: str, given: dict, cls: type) -> None:
    known = {f.name for f in cls.__dataclass_fields__.values()}
    for key in given:
        if key not in known:
            raise ConfigError(
                f"{section}.{key} is not a recognised setting. "
                f"Known settings: {', '.join(sorted(known))}"
            )


def _build_section(name: str, raw: dict, cls: type):
    given = raw.get(name) or {}
    if not isinstance(given, dict):
        raise ConfigError(f"{name} must be a mapping, got {type(given).__name__}")
    given = dict(given)
    _reject_unknown_keys(name, given, cls)
    # YAML lists must become tuples for any field typed as tuple[...] — the
    # dataclasses are frozen and downstream tasks assume their sequence
    # fields are immutable.
    for f in cls.__dataclass_fields__.values():
        if f.name in given and isinstance(f.type, str) and f.type.startswith("tuple"):
            value = given[f.name]
            if not isinstance(value, (list, tuple)):
                raise ConfigError(
                    f"{name}.{f.name} must be a list, got {type(value).__name__}"
                )
            given[f.name] = tuple(value)
    return cls(**given)


def load_config(path: Path) -> Config:
    """Read, validate, and freeze the configuration at *path*."""
    try:
        raw = yaml.safe_load(path.read_text()) or {}
    except FileNotFoundError as exc:
        raise ConfigError(f"no config file at {path}") from exc
    except yaml.YAMLError as exc:
        raise ConfigError(f"{path} is not valid YAML: {exc}") from exc

    server_raw = raw.get("server") or {}
    if not isinstance(server_raw, dict):
        raise ConfigError(f"server must be a mapping, got {type(server_raw).__name__}")
    server_raw = dict(server_raw)

    for required in ("host", "username"):
        if not server_raw.get(required):
            raise ConfigError(f"server.{required} is required")

    # password/password_env are pseudo-fields resolved here, not real
    # ServerConfig fields, so they must be popped off before the
    # unknown-key check below runs against ServerConfig's actual fields.
    password = server_raw.pop("password", None)
    password_env = server_raw.pop("password_env", None)
    if password is None and password_env:
        password = os.environ.get(password_env)
        if password is None:
            raise ConfigError(
                f"server.password_env names {password_env}, "
                "but that environment variable is not set"
            )
    if password is None:
        raise ConfigError("one of server.password or server.password_env is required")

    _reject_unknown_keys("server", server_raw, ServerConfig)

    sections = {
        name: _build_section(name, raw, cls) for name, cls in _SECTIONS.items()
    }

    naming = sections["naming"]
    if not _SAFE_SEPARATOR.match(naming.domain_separator):
        raise ConfigError(
            f"naming.domain_separator must match [a-z0-9_-]+, got "
            f"{naming.domain_separator!r}. A '.' or '/' would collide with the "
            "IMAP hierarchy delimiter and shatter the archive tree."
        )

    try:
        ZoneInfo(sections["dates"].timezone)
    except (ZoneInfoNotFoundError, ValueError) as exc:
        raise ConfigError(
            f"dates.timezone {sections['dates'].timezone!r} is not a known zone"
        ) from exc

    if sections["archive"].promote_threshold < 1:
        raise ConfigError("archive.promote_threshold must be at least 1")
    if sections["execution"].batch_size < 1:
        raise ConfigError("execution.batch_size must be at least 1")

    unknown_top = set(raw) - {"server", *_SECTIONS}
    if unknown_top:
        raise ConfigError(f"unrecognised top-level sections: {sorted(unknown_top)}")

    return Config(server=ServerConfig(password=password, **server_raw), **sections)
