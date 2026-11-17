"""Concept files in the Open Knowledge Format (OKF) with the OKF-MEM header profile.

A concept is a markdown file with a YAML front-matter header. OKF-MEM adds the
memory fields ``actor``, ``evidence``, ``sources``, ``confidence``,
``supersedes`` and the validity dates ``valid_from`` / ``valid_to`` (paper, Sec. 2).

Header fields are split into three groups:

* identity fields (``id``, ``title``),
* provenance fields (never part of a Conflict, paper Sec. 2 "Reconciliation"),
* fact fields (everything else, or an explicit list from the bundle config).
"""

from __future__ import annotations

import datetime as dt
import hashlib
import json
import re
from collections.abc import Iterable, Mapping
from dataclasses import dataclass, field
from typing import Any

import yaml

IDENTITY_FIELDS: frozenset[str] = frozenset({"id", "title"})
PROVENANCE_FIELDS: frozenset[str] = frozenset(
    {"actor", "evidence", "sources", "source", "confidence", "supersedes", "valid_from", "valid_to"}
)
VALIDITY_FIELDS: frozenset[str] = frozenset({"valid_from", "valid_to"})

try:  # libyaml bindings are much faster; fall back to pure Python
    _Loader: Any = yaml.CSafeLoader
    _Dumper: Any = yaml.CSafeDumper
except AttributeError:  # pragma: no cover - depends on the PyYAML build
    _Loader, _Dumper = yaml.SafeLoader, yaml.SafeDumper

_FRONT_MATTER = re.compile(r"\A---\n(.*?)\n---\n?(.*)\Z", re.DOTALL)
_HEX64 = re.compile(r"\A[0-9a-f]{64}\Z")


class OKFError(ValueError):
    """Raised when a file is not a valid OKF-MEM concept."""


def canonical(value: Any) -> str:
    """Canonical JSON encoding used for stored field values and comparisons."""
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False, default=str)


def body_digest(body: str) -> str:
    return hashlib.sha256(body.encode("utf-8")).hexdigest()


def is_digest(value: object) -> bool:
    return isinstance(value, str) and bool(_HEX64.match(value))


def normalize_time(value: object) -> str:
    """Normalize a date / datetime / ISO string to ``YYYY-MM-DDTHH:MM:SSZ`` (UTC)."""
    if isinstance(value, dt.datetime):
        d = value if value.tzinfo else value.replace(tzinfo=dt.timezone.utc)
        return d.astimezone(dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    if isinstance(value, dt.date):
        return f"{value.isoformat()}T00:00:00Z"
    if isinstance(value, str):
        s = value.strip()
        if re.fullmatch(r"\d{4}-\d{2}-\d{2}", s):
            return f"{s}T00:00:00Z"
        try:
            parsed = dt.datetime.fromisoformat(s.replace("Z", "+00:00"))
        except ValueError as exc:
            raise OKFError(f"not an ISO-8601 date/time: {value!r}") from exc
        return normalize_time(parsed)
    raise OKFError(f"not a date/time: {value!r}")


def _plain(value: Any) -> Any:
    """Convert YAML-native dates to strings so headers round-trip deterministically."""
    if isinstance(value, dt.datetime):
        return normalize_time(value)
    if isinstance(value, dt.date):
        return value.isoformat()
    if isinstance(value, Mapping):
        return {str(k): _plain(v) for k, v in value.items()}
    if isinstance(value, list | tuple):
        return [_plain(v) for v in value]
    return value


@dataclass
class Concept:
    """One OKF concept: a bundle-relative path, a header and a markdown body."""

    path: str
    header: dict[str, Any] = field(default_factory=dict)
    body: str = ""

    # ------------------------------------------------------------------ I/O
    @classmethod
    def parse(cls, path: str, text: str) -> Concept:
        m = _FRONT_MATTER.match(text)
        if not m:
            raise OKFError(f"{path}: missing YAML front matter")
        try:
            raw = yaml.load(m.group(1), Loader=_Loader)  # noqa: S506 - safe loader
        except yaml.YAMLError as exc:
            raise OKFError(f"{path}: invalid YAML header: {exc}") from exc
        if raw is None:
            raw = {}
        if not isinstance(raw, dict):
            raise OKFError(f"{path}: header must be a mapping")
        return cls(path=path, header=_plain(raw), body=m.group(2))

    def dumps(self) -> str:
        head = yaml.dump(
            _plain(self.header), Dumper=_Dumper, sort_keys=True, allow_unicode=True,
            default_flow_style=False,
        )
        body = self.body if self.body.endswith("\n") or not self.body else self.body + "\n"
        return f"---\n{head}---\n{body}"

    # -------------------------------------------------------------- helpers
    @property
    def concept_id(self) -> str:
        return str(self.header.get("id") or self.path)

    @property
    def title(self) -> str:
        return str(self.header.get("title") or self.concept_id)

    def evidence_digests(self) -> list[str]:
        ev = self.header.get("evidence") or []
        if isinstance(ev, str):
            ev = [ev]
        out = [str(e) for e in ev]
        for s in self.sources():
            h = s.get("evidence")
            if isinstance(h, str) and h not in out:
                out.append(h)
        return out

    def sources(self) -> list[dict[str, Any]]:
        src = self.header.get("sources") or self.header.get("source") or []
        if isinstance(src, Mapping):
            src = [src]
        if isinstance(src, str):
            src = [{"uri": src}]
        return [dict(s) if isinstance(s, Mapping) else {"uri": str(s)} for s in src]

    def facts(self, fact_fields: Iterable[str] | None = None) -> dict[str, Any]:
        """Fact header fields. Provenance and identity fields are never facts."""
        if fact_fields is not None:
            allowed = set(fact_fields) - PROVENANCE_FIELDS - IDENTITY_FIELDS
            return {k: v for k, v in self.header.items() if k in allowed}
        return {
            k: v
            for k, v in self.header.items()
            if k not in PROVENANCE_FIELDS and k not in IDENTITY_FIELDS
        }

    def tracked_fields(self) -> dict[str, str]:
        """Fields tracked in ``belief_history``: header fields (minus validity
        dates, which are the SCD2 axis) plus a digest of the body."""
        out = {k: canonical(v) for k, v in self.header.items() if k not in VALIDITY_FIELDS}
        out["body_digest"] = canonical(body_digest(self.body))
        return out

    def search_text(self, fact_fields: Iterable[str] | None = None, mode: str = "full") -> str:
        """Text indexed for full-text search. ``full`` (default): title, fact
        values and body. ``body``: title and body only. Provenance is excluded
        in both, so a provenance-only update is textually identical to the
        concept it updates."""
        if mode == "body":
            return f"{self.title}\n{self.body}"
        parts = [self.title]
        for k, v in sorted(self.facts(fact_fields).items()):
            parts.append(f"{k} {v if isinstance(v, str) else canonical(v)}")
        parts.append(self.body)
        return "\n".join(parts)

    def similarity_text(self, fact_fields: Iterable[str] | None = None, mode: str = "full") -> str:
        """Text compared by TF-IDF cosine. ``full`` (default) is
        :meth:`search_text`; ``body`` compares bodies only (paper Sec. 2: facts
        stated only in the body affect only ``sim``)."""
        return self.body if mode == "body" else self.search_text(fact_fields)

    def links(self) -> list[str]:
        """Outgoing links: ``[[path]]`` wiki links, relative ``.md`` links and
        a ``links`` header list."""
        found: list[str] = []
        for m in re.finditer(r"\[\[([^\]|#]+)(?:[|#][^\]]*)?\]\]", self.body):
            found.append(m.group(1).strip())
        for m in re.finditer(r"\]\(([^)\s]+\.md)\)", self.body):
            found.append(m.group(1).strip())
        hdr = self.header.get("links") or []
        if isinstance(hdr, list):
            found.extend(str(x) for x in hdr)
        seen: dict[str, None] = {}
        for f in found:
            seen.setdefault(f, None)
        return list(seen)


def validate_header(c: Concept) -> list[str]:
    """Structural checks on an OKF-MEM header. Returns a list of problems."""
    problems: list[str] = []
    h = c.header
    if not c.path.startswith("concepts/") or not c.path.endswith(".md"):
        problems.append(f"path must be concepts/**.md, got {c.path!r}")
    if ".." in c.path.split("/"):
        problems.append("path may not contain '..'")
    if "confidence" in h:
        conf = h["confidence"]
        if not isinstance(conf, int | float) or isinstance(conf, bool) or not 0 <= conf <= 1:
            problems.append("confidence must be a number in [0, 1]")
    for key in ("valid_from", "valid_to"):
        if key in h and h[key] is not None:
            try:
                normalize_time(h[key])
            except OKFError as exc:
                problems.append(f"{key}: {exc}")
    if h.get("valid_from") and h.get("valid_to"):
        try:
            if normalize_time(h["valid_to"]) < normalize_time(h["valid_from"]):
                problems.append("valid_to precedes valid_from")
        except OKFError:
            pass
    for d in c.evidence_digests():
        if not is_digest(d):
            problems.append(f"evidence digest is not SHA-256 hex: {d!r}")
    return problems
