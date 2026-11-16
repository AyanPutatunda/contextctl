from __future__ import annotations

from pathlib import Path

import pytest

from contextctl.bundle import Bundle
from contextctl.gate import Gate
from contextctl.gitio import Identity
from contextctl.okf import Concept
from contextctl.sidecar import Sidecar

WHO = Identity("test", "test@contextctl.invalid")


@pytest.fixture
def bundle(tmp_path: Path) -> Bundle:
    return Bundle.init(tmp_path / "b", when="2024-01-01T00:00:00Z", who=WHO, install_hook=False)


@pytest.fixture
def gate(bundle: Bundle) -> Gate:
    return Gate(bundle, Sidecar(bundle))


def concept(path: str, body: str = "", **header: object) -> Concept:
    return Concept(path, dict(header), body)


def admit(
    gate: Gate, c: Concept, when: str, evidence: list[tuple[bytes, str, str]] | None = None,
    **approve: object,
) -> str | None:
    """Propose, curate, approve (if pending) and merge one draft."""
    did = gate.propose(c, evidence)
    rec = gate.curate([did])[0]
    if rec.status == "pending":
        gate.approve(did, "reviewer", **approve)  # type: ignore[arg-type]
    return gate.merge(f"admit {c.path}", when=when, who=WHO)
