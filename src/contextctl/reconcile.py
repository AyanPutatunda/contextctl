"""Reconciliation rules (paper, Sec. 2 and Table I).

A draft ``d`` is compared with the top-k full-text matches (k=10) and the
same-path concept ``c0`` using TF-IDF cosine similarity ``sim`` and
``fields(d, c)``: the *fact* header fields that both assert with different
values. Provenance fields are excluded, so a provenance-only update never
raises a Conflict; with an unchanged body it is a Duplicate and is dropped
(losing the newer citation -- a gap the paper flags). Facts stated only in the
body affect only ``sim``. The first matching rule applies:

==========  ===============================================  =====================
Rule        Condition                                        Action
==========  ===============================================  =====================
Conflict    fields(d, c0) != {}                              human resolves;
                                                             record ``supersedes``
Duplicate   max sim >= theta_dup and no new facts            drop draft; log match
Revision    theta_rev <= max sim < theta_dup                 edit of best match
New         max sim < theta_rev                              add as new concept
==========  ===============================================  =====================

A Duplicate or Revision whose best match is a *different* concept (a rename or
a sibling) is held for review rather than applied.

Interpretation choices (see docs/design-notes.md): "no new facts" means the
draft asserts no fact field that the best match lacks or holds with a different
value; a draft with sim >= theta_dup that *does* carry new facts is a Revision.
"""

from __future__ import annotations

import enum
from collections.abc import Iterable, Mapping
from dataclasses import dataclass, field
from typing import Any

from .okf import Concept, canonical
from .similarity import TfIdf


class Decision(str, enum.Enum):
    CONFLICT = "Conflict"
    DUPLICATE = "Duplicate"
    REVISION = "Revision"
    NEW = "New"


@dataclass(frozen=True)
class FieldConflict:
    field: str
    current: Any
    proposed: Any


@dataclass
class Reconciliation:
    decision: Decision
    target_path: str  # where the draft would land if admitted
    best_match: str | None
    best_sim: float
    held: bool = False  # Duplicate/Revision whose best match is a different concept
    conflicts: list[FieldConflict] = field(default_factory=list)
    candidates: list[tuple[str, float]] = field(default_factory=list)
    new_facts: list[str] = field(default_factory=list)

    def as_dict(self) -> dict[str, Any]:
        return {
            "decision": self.decision.value,
            "target_path": self.target_path,
            "best_match": self.best_match,
            "best_sim": round(self.best_sim, 6),
            "held": self.held,
            "conflicts": [
                {"field": c.field, "current": c.current, "proposed": c.proposed}
                for c in self.conflicts
            ],
            "candidates": [[p, round(s, 6)] for p, s in self.candidates],
            "new_facts": self.new_facts,
        }


def conflicting_fields(
    d: Concept, c: Concept, fact_fields: Iterable[str] | None = None
) -> list[FieldConflict]:
    """``fields(d, c)``: fact fields both assert with different values."""
    ff = list(fact_fields) if fact_fields is not None else None
    df, cf = d.facts(ff), c.facts(ff)
    return [
        FieldConflict(k, cf[k], df[k])
        for k in sorted(set(df) & set(cf))
        if canonical(df[k]) != canonical(cf[k])
    ]


def new_facts(d: Concept, c: Concept, fact_fields: Iterable[str] | None = None) -> list[str]:
    """Fact fields the draft asserts that ``c`` lacks or holds differently."""
    ff = list(fact_fields) if fact_fields is not None else None
    df, cf = d.facts(ff), c.facts(ff)
    return sorted(k for k in df if k not in cf or canonical(df[k]) != canonical(cf[k]))


class Reconciler:
    def __init__(
        self,
        trusted: Mapping[str, Concept],
        search: Any = None,
        theta_dup: float = 0.9,
        theta_rev: float = 0.5,
        top_k: int = 10,
        fact_fields: Iterable[str] | None = None,
        corpus_texts: Mapping[str, str] | None = None,
    ) -> None:
        """``trusted`` maps path -> concept (current trusted state). ``search``
        is a callable ``(query, k) -> list[path]`` (the FTS index); when absent,
        every trusted concept is a candidate."""
        self.trusted = dict(trusted)
        self.search = search
        self.theta_dup = theta_dup
        self.theta_rev = theta_rev
        self.top_k = top_k
        self.fact_fields = list(fact_fields) if fact_fields is not None else None
        texts = corpus_texts or {p: c.search_text(self.fact_fields) for p, c in trusted.items()}
        self._texts = dict(texts)
        self._tfidf: TfIdf | None = None

    def _model(self, extra: str) -> TfIdf:
        return TfIdf([*self._texts.values(), extra])

    def candidates(self, d: Concept, text: str) -> list[str]:
        if self.search is not None:
            paths = [p for p in self.search(text, self.top_k) if p in self.trusted]
        else:
            paths = sorted(self.trusted)
        if d.path in self.trusted and d.path not in paths:
            paths.append(d.path)
        return paths

    def reconcile(self, d: Concept) -> Reconciliation:
        text = d.search_text(self.fact_fields)
        c0 = self.trusted.get(d.path)

        # Rule 1: Conflict -- scoped to the same-path concept (replay finding (a)).
        if c0 is not None:
            conf = conflicting_fields(d, c0, self.fact_fields)
            if conf:
                sim0 = self._model(text).cosine(text, self._texts.get(d.path, ""))
                return Reconciliation(
                    Decision.CONFLICT, d.path, d.path, sim0, conflicts=conf,
                    candidates=[(d.path, sim0)],
                )

        cands = self.candidates(d, text)
        model = self._model(text)
        vd = model.vector(text)
        from .similarity import cosine

        scored = sorted(
            ((p, cosine(vd, model.vector(self._texts.get(p, "")))) for p in cands),
            key=lambda ps: (-ps[1], ps[0] != d.path, ps[0]),
        )
        if not scored:
            return Reconciliation(Decision.NEW, d.path, None, 0.0)
        best, best_sim = scored[0]
        nf = new_facts(d, self.trusted[best], self.fact_fields)
        held = best != d.path
        if best_sim >= self.theta_dup and not nf:
            return Reconciliation(
                Decision.DUPLICATE, best, best, best_sim, held=held, candidates=scored, new_facts=nf
            )
        if best_sim >= self.theta_rev:
            return Reconciliation(
                Decision.REVISION, best, best, best_sim, held=held, candidates=scored, new_facts=nf
            )
        return Reconciliation(
            Decision.NEW, d.path, best, best_sim, candidates=scored, new_facts=nf
        )
