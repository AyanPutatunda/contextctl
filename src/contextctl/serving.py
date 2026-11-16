"""Layer 4: serving by progressive disclosure (paper, Sec. 2 "Serving").

The protocol is ``orient -> search -> read -> history -> provenance`` plus a
staging-only ``propose``. Serving never writes trusted state: ``propose`` drops
a draft into staging, which must re-enter through the gate.
"""

from __future__ import annotations

from collections import Counter
from dataclasses import dataclass, field
from typing import Any

from .bundle import Bundle
from .evidence import sha256_hex
from .gate import Gate
from .okf import Concept, normalize_time
from .sidecar import Sidecar


@dataclass
class ProvenanceHop:
    hop: str
    ok: bool
    detail: str


@dataclass
class Provenance:
    concept: str
    commit: str | None
    hops: list[ProvenanceHop] = field(default_factory=list)

    @property
    def ok(self) -> bool:
        return all(h.ok for h in self.hops)


class Server:
    def __init__(self, bundle: Bundle, sidecar: Sidecar | None = None) -> None:
        self.bundle = bundle
        self.sidecar = sidecar or Sidecar(bundle)
        self.sidecar.sync()

    # 1. orient --------------------------------------------------------
    def orient(self, limit: int = 50) -> dict[str, Any]:
        rows = self.sidecar.concept_rows()
        areas: Counter[str] = Counter()
        for p, _ in rows:
            parts = p.split("/")
            areas["/".join(parts[:2]) if len(parts) > 2 else "concepts"] += 1
        head = self.bundle.head()
        return {
            "head": self.bundle.git.short(head) if head else None,
            "concepts": len(rows),
            "areas": dict(sorted(areas.items())),
            "index": [{"path": p, "title": t} for p, t in rows[:limit]],
            "truncated": len(rows) > limit,
        }

    # 2. search --------------------------------------------------------
    def search(self, query: str, k: int = 5) -> list[dict[str, str]]:
        return [
            {"path": p, "title": t, "snippet": s} for p, t, s in self.sidecar.snippet(query, k)
        ]

    # 3. read ----------------------------------------------------------
    def read(self, path: str, as_of: str | None = None, body_only: bool = False) -> str | None:
        """Read a concept, optionally as of a transaction time (commit-ish or
        ISO time): the as-of read resolves to the last first-parent commit on
        main at or before ``as_of`` and returns the file as it was then."""
        rev = self.bundle.main
        if as_of is not None:
            at = as_of
            if len(at) >= 10 and at[4] == "-":
                at = normalize_time(at)
            resolved = self.bundle.git.resolve_as_of(at, self.bundle.main)
            if resolved is None:
                return None
            rev = resolved
        c = self.bundle.read(path, rev)
        if c is None:
            return None
        return c.body if body_only else c.dumps()

    # 4. history -------------------------------------------------------
    def history(
        self, path: str, field_name: str | None = None, valid_at: str | None = None
    ) -> list[dict[str, Any]] | Any:
        if valid_at is not None:
            if field_name is None:
                raise ValueError("valid_at requires a field")
            return self.sidecar.value_at(path, field_name, valid_at)
        return [r.__dict__ for r in self.sidecar.history(path, field_name)]

    # 5. provenance ----------------------------------------------------
    def provenance(self, path: str) -> Provenance:
        """Check each hop concept -> commit -> evidence -> source (P1)."""
        git = self.bundle.git
        c = self.bundle.read(path)
        commit = git.last_commit_touching(path, self.bundle.main)
        prov = Provenance(path, git.short(commit) if commit else None)
        if c is None or commit is None:
            prov.hops.append(
                ProvenanceHop("concept->commit", False, "concept not in trusted state")
            )
            return prov
        prov.hops.append(ProvenanceHop("concept->commit", True, f"last changed in {commit[:7]}"))
        digests = c.evidence_digests()
        if not digests:
            prov.hops.append(ProvenanceHop("commit->evidence", False, "no evidence cited"))
            return prov
        uris = {s.get("evidence"): s.get("uri") for s in c.sources()}
        for h in digests:
            blob = git.run_bytes("show", f"{commit}:evidence/{h}") if self._in(commit, h) else None
            ok = blob is not None and sha256_hex(blob) == h
            prov.hops.append(
                ProvenanceHop(
                    "commit->evidence", ok,
                    f"{h[:12]} {'verifies' if ok else 'missing or fails digest'} at {commit[:7]}",
                )
            )
            meta = self.bundle.evidence.meta(h)
            uri = uris.get(h) or meta.get("first_uri")
            prov.hops.append(
                ProvenanceHop(
                    "evidence->source", bool(uri),
                    f"{h[:12]} <- {uri}" if uri else f"{h[:12]} has no source URI",
                )
            )
        return prov

    def _in(self, commit: str, digest: str) -> bool:
        return f"evidence/{digest}" in self.bundle.git.ls_tree(commit, f"evidence/{digest}")

    # propose (staging only) ------------------------------------------
    def propose(
        self, concept: Concept, evidence: list[tuple[bytes, str, str]] | None = None,
        proposer: str = "agent",
    ) -> str:
        return Gate(self.bundle, self.sidecar).propose(concept, evidence, proposer)
