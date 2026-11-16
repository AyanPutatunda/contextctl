"""The nine-check, no-model lifecycle (paper, Sec. 3 "Checks").

Runs in a throwaway bundle with synthetic notes and returns one result per
check. ``contextctl selfcheck`` prints them and exits non-zero on failure.
"""

from __future__ import annotations

import tempfile
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path

from .bundle import Bundle
from .connectors.notes import harvest_note
from .gate import DROPPED, PENDING, REJECTED, Gate
from .gitio import Identity
from .serving import Server
from .sidecar import Sidecar

WHO = Identity("selfcheck", "selfcheck@contextctl.invalid")
NOTE_V1 = b"""# Payments pipeline owner
Owner: alex
Tier: gold

The nightly payments pipeline is owned by alex. Escalate via the on-call rota.
"""
NOTE_V2 = NOTE_V1.replace(b"Owner: alex", b"Owner: blair").replace(b"by alex", b"by blair")


@dataclass
class Check:
    name: str
    ok: bool
    detail: str = ""


def run(root: Path | None = None) -> list[Check]:
    with tempfile.TemporaryDirectory() as td:
        return _run(Path(root) if root else Path(td) / "bundle")


def _run(root: Path) -> list[Check]:
    checks: list[Check] = []

    def check(name: str, fn: Callable[[], tuple[bool, str]]) -> None:
        try:
            ok, detail = fn()
        except Exception as exc:  # pragma: no cover - reported, not raised
            ok, detail = False, f"{type(exc).__name__}: {exc}"
        checks.append(Check(name, ok, detail))

    b = Bundle.init(root, when="2024-01-01T00:00:00Z", who=WHO, install_hook=False)
    sc = Sidecar(b)
    gate = Gate(b, sc)
    t1, t2 = "2024-02-01T00:00:00Z", "2024-03-01T00:00:00Z"
    h1 = harvest_note(NOTE_V1, "file:notes/payments.md", t1, "payments.md")

    def c1() -> tuple[bool, str]:
        r1 = b.staging_evidence.put(NOTE_V1, "file:notes/payments.md", t1)
        r2 = b.staging_evidence.put(NOTE_V1, "file:retry", t2)
        return (not r2.created and r1.digest == r2.digest
                and b.staging_evidence.meta(r1.digest)["first_uri"] == "file:notes/payments.md",
                f"same bytes -> one object {r1.digest[:12]}")

    def c2() -> tuple[bool, str]:
        head = b.head()
        Server(b, sc).propose(h1.concept, h1.evidence, proposer="agent")
        return b.head() == head and not b.concept_paths(), "propose wrote staging only"

    def c3() -> tuple[bool, str]:
        bad = harvest_note(NOTE_V1 + b"tampered", "file:x", t1, "x.md").concept
        bad.header["evidence"] = ["0" * 64]
        bad.header["sources"] = []
        did = gate.propose(bad, [], proposer="agent")
        r = gate.curate([did])[0]
        return r.status == REJECTED, "; ".join(r.problems)

    def c4() -> tuple[bool, str]:
        recs = [r for r in gate.curate() if r.status == PENDING]
        assert recs and recs[0].reconciliation
        gate.approve(recs[0].id, "reviewer")
        sha = gate.merge("admit payments owner", when=t1, who=WHO)
        return (sha is not None and len(b.concept_paths()) == 1,
                f"New -> merged {sha[:7] if sha else None}")

    def c5() -> tuple[bool, str]:
        again = harvest_note(NOTE_V1, "file:notes/payments.md", t2, "payments.md")
        did = gate.propose(again.concept, again.evidence)
        r = gate.curate([did])[0]
        return r.status == DROPPED, r.note or ""

    def c6() -> tuple[bool, str]:
        v2 = harvest_note(NOTE_V2, "file:notes/payments.md", t2, "payments.md")
        did = gate.propose(v2.concept, v2.evidence)
        r = gate.curate([did])[0]
        assert r.reconciliation
        ok = r.reconciliation["decision"] == "Conflict" and r.status == PENDING
        gate.approve(did, "reviewer", resolution={"owner": "blair"})
        gate.merge("resolve owner conflict", when=t2, who=WHO)
        c = b.read("concepts/notes/payments-pipeline-owner.md")
        return ok and c is not None and bool(c.header.get("supersedes")), "Conflict held, resolved"

    def c7() -> tuple[bool, str]:
        s = Server(b, sc)
        old = s.read("concepts/notes/payments-pipeline-owner.md", as_of="2024-02-15")
        new = s.read("concepts/notes/payments-pipeline-owner.md")
        return (old is not None and "owner: alex" in old and new is not None
                and "owner: blair" in new, "as-of read returns the prior belief")

    def c8() -> tuple[bool, str]:
        s = Server(b, sc)
        p = "concepts/notes/payments-pipeline-owner.md"
        v1 = s.history(p, "owner", valid_at="2024-02-15")
        v2 = s.history(p, "owner", valid_at="2024-03-15")
        prov = s.provenance(p)
        return v1 == "alex" and v2 == "blair" and prov.ok, "valid-time query + provenance chain"

    def c9() -> tuple[bool, str]:
        before = sc.canonical_dump()
        sc.close()
        b.sidecar_path.unlink()
        fresh = Sidecar(b)
        fresh.rebuild()
        ok = fresh.canonical_dump() == before
        fresh.close()
        return ok, f"{len(before)} rows identical after delete + rebuild"

    for name, fn in [
        ("evidence put is idempotent (content addressing)", c1),
        ("propose writes staging only", c2),
        ("gate rejects unverifiable evidence digest", c3),
        ("New draft admitted through review and merge", c4),
        ("Duplicate draft dropped and logged", c5),
        ("Conflict held, resolved, supersedes recorded", c6),
        ("as-of read (transaction time) returns prior version", c7),
        ("valid-time query and provenance chain resolve", c8),
        ("sidecar delete + rebuild matches row for row", c9),
    ]:
        check(name, fn)
    return checks
