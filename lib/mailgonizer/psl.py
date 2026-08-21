"""Public Suffix List matching, per https://publicsuffix.org/list/

Implemented directly rather than via a dependency: the file is vendored
regardless, the algorithm is short and published, and a local implementation
has no third-party API surface to drift under us.
"""

from __future__ import annotations

import hashlib
from collections.abc import Iterable
from pathlib import Path

_BUNDLED = Path(__file__).parent / "data" / "public_suffix_list.dat"


class PublicSuffixList:
    def __init__(self, lines: Iterable[str]) -> None:
        self.rules: set[str] = set()
        self.exceptions: set[str] = set()
        digest = hashlib.sha256()
        count = 0
        for line in lines:
            digest.update(line.encode("utf-8", "replace"))
            stripped = line.strip()
            if not stripped or stripped.startswith("//"):
                continue
            # The format allows "rule<whitespace>// trailing comment", and the
            # rule is only the first token. Keeping the whole line would store
            # a rule that can never match, so the true suffix goes unrecognised
            # and every sender under it collapses into one folder -- silently,
            # and permanently, because promotion is a one-way ratchet.
            stripped = stripped.split()[0]
            count += 1
            if stripped.startswith("!"):
                self.exceptions.add(stripped[1:].lower())
            else:
                self.rules.add(stripped.lower())
        self.version = f"{count}-rules-sha256:{digest.hexdigest()[:16]}"

    @classmethod
    def from_file(cls, path: Path) -> PublicSuffixList:
        return cls(path.read_text(encoding="utf-8").splitlines())

    @classmethod
    def bundled(cls) -> PublicSuffixList:
        return cls.from_file(_BUNDLED)

    @staticmethod
    def _labels(domain: str) -> list[str]:
        return [lbl for lbl in domain.strip().strip(".").lower().split(".") if lbl]

    def public_suffix(self, domain: str) -> str:
        labels = self._labels(domain)
        if not labels:
            return ""

        # An exception rule wins outright; its suffix is the rule minus its
        # leftmost label. Longest candidates are checked first.
        for i in range(len(labels)):
            if ".".join(labels[i:]) in self.exceptions:
                return ".".join(labels[i + 1:])

        for i in range(len(labels)):
            candidate = ".".join(labels[i:])
            if candidate in self.rules:
                return candidate
            if ".".join(["*", *labels[i + 1:]]) in self.rules:
                return candidate

        # "If no rules match, the prevailing rule is '*'."
        return labels[-1]

    def registrable_domain(self, domain: str) -> str | None:
        labels = self._labels(domain)
        suffix = self.public_suffix(domain)
        suffix_len = len(self._labels(suffix)) if suffix else 0
        if len(labels) <= suffix_len:
            return None
        return ".".join(labels[len(labels) - suffix_len - 1:])
