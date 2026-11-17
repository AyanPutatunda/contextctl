"""Layer 2: curation, the trust boundary (paper, Sec. 2).

Generation is untrusted; curation is the only path into trusted state. A draft
passes four steps: **validate** (header schema; every cited evidence digest
must resolve and verify -- the gate rejects unverifiable digests, which is what
property P2 relies on), **reconcile** (Table I), **review** (a human, or a
scripted policy in the replay) and **merge** (a ``--no-ff`` Git merge into the
main branch, which is the write gate and the transaction-time record).

Serving may only *propose*: proposals land in ``.contextctl/staging`` and must
come back through this gate.
"""

from __future__ import annotations

import hashlib
import json
import shutil
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from .bundle import Bundle
from .evidence import EvidenceStore
from .gitio import GitError, Identity
from .okf import Concept, OKFError, validate_header
from .reconcile import Decision, Reconciler
from .sidecar import Sidecar

# Draft lifecycle states
STAGED = "staged"
PENDING = "pending"  # reconciled; awaiting review
DROPPED = "dropped"  # Duplicate: dropped, match logged
REJECTED = "rejected"
APPROVED = "approved"
MERGED = "merged"

NO_HOOKS = ("-c", "core.hooksPath=/dev/null")


class GateError(RuntimeError):
    pass


class RevertConflict(GateError):
    def __init__(self, merge: str, detail: str) -> None:
        super().__init__(f"revert of {merge[:7]} conflicts with later history: {detail}")
        self.merge = merge


@dataclass
class DraftRecord:
    id: str
    path: str
    proposer: str
    status: str = STAGED
    problems: list[str] = field(default_factory=list)
    reconciliation: dict[str, Any] | None = None
    reviewer: str | None = None
    resolution: dict[str, Any] | None = None
    final_path: str | None = None
    merge: str | None = None
    note: str | None = None

    def to_json(self) -> str:
        return json.dumps(self.__dict__, sort_keys=True, indent=2, default=str) + "\n"

    @classmethod
    def from_json(cls, text: str) -> DraftRecord:
        return cls(**json.loads(text))


class Gate:
    def __init__(self, bundle: Bundle, sidecar: Sidecar | None = None) -> None:
        self.bundle = bundle
        self.sidecar = sidecar or Sidecar(bundle)
        self.dir = bundle.staging_dir
        self.dir.mkdir(parents=True, exist_ok=True)
        self.enforce_actor = bool(bundle.config.extra.get("enforce_actor", False))

    # ----------------------------------------------------------- staging
    def _draft_file(self, draft_id: str) -> Path:
        return self.dir / f"{draft_id}.md"

    def _rec_file(self, draft_id: str) -> Path:
        return self.dir / f"{draft_id}.json"

    def propose(
        self,
        concept: Concept,
        evidence: list[tuple[bytes, str, str]] | None = None,
        proposer: str = "agent",
    ) -> str:
        """Stage a draft (untrusted). ``evidence`` is a list of
        ``(bytes, uri, fetched_at)`` fetched by a connector; blobs are stored
        content-addressed in the staging evidence store."""
        for data, uri, fetched_at in evidence or []:
            self.bundle.staging_evidence.put(data, uri, fetched_at)
        text = concept.dumps()
        draft_id = hashlib.sha256(f"{concept.path}\0{text}".encode()).hexdigest()[:16]
        if not self._rec_file(draft_id).exists():
            self._draft_file(draft_id).write_text(text)
            rec = DraftRecord(draft_id, concept.path, proposer)
            self._save(rec)
        return draft_id

    def _save(self, rec: DraftRecord) -> None:
        self._rec_file(rec.id).write_text(rec.to_json())

    def record(self, draft_id: str) -> DraftRecord:
        f = self._rec_file(draft_id)
        if not f.exists():
            raise GateError(f"no such draft: {draft_id}")
        return DraftRecord.from_json(f.read_text())

    def draft(self, draft_id: str) -> Concept:
        rec = self.record(draft_id)
        return Concept.parse(rec.path, self._draft_file(draft_id).read_text())

    def records(self, status: str | None = None) -> list[DraftRecord]:
        recs = [DraftRecord.from_json(p.read_text()) for p in sorted(self.dir.glob("*.json"))]
        return [r for r in recs if status is None or r.status == status]

    # ---------------------------------------------------------- validate
    def validate(self, c: Concept) -> list[str]:
        problems = validate_header(c)
        for h in c.evidence_digests():
            store = self._store_for(h)
            if store is None:
                problems.append(f"evidence {h[:12]} does not resolve")
            elif not store.verify(h):
                problems.append(f"evidence {h[:12]} fails digest verification")
        for s in c.sources():
            if s.get("evidence") and s["evidence"] not in c.evidence_digests():
                problems.append("source cites evidence not listed in header")  # pragma: no cover
        return problems

    def _store_for(self, digest: str) -> EvidenceStore | None:
        for store in (self.bundle.staging_evidence, self.bundle.evidence):
            if store.exists(digest):
                return store
        return None

    # --------------------------------------------------------- reconcile
    def reconciler(self) -> Reconciler:
        self.sidecar.sync()
        trusted = {c.path: c for c in self.bundle.concepts()}
        cfg = self.bundle.config

        def search(q: str, k: int) -> list[str]:
            return [p for p, _t, _r in self.sidecar.search(q, k)]

        return Reconciler(
            trusted, search=search, theta_dup=cfg.theta_dup, theta_rev=cfg.theta_rev,
            top_k=cfg.top_k, fact_fields=cfg.fact_fields, similarity_text=cfg.similarity_text,
        )

    def curate(self, draft_ids: list[str] | None = None) -> list[DraftRecord]:
        """Validate and reconcile staged drafts against current trusted state."""
        rec = self.reconciler()
        out = []
        ids = draft_ids or [r.id for r in self.records(STAGED)]
        for did in ids:
            r = self.record(did)
            if r.status != STAGED:
                out.append(r)
                continue
            try:
                d = self.draft(did)
            except OKFError as exc:
                r.status, r.problems = REJECTED, [str(exc)]
                self._save(r)
                out.append(r)
                continue
            problems = self.validate(d)
            if problems:
                r.status, r.problems = REJECTED, problems
            else:
                res = rec.reconcile(d)
                r.reconciliation = res.as_dict()
                if res.decision is Decision.DUPLICATE and not res.held:
                    r.status = DROPPED
                    r.note = f"duplicate of {res.best_match} (sim={res.best_sim:.3f})"
                else:
                    r.status = PENDING
            self._save(r)
            out.append(r)
        return out

    # ------------------------------------------------------------ review
    def approve(
        self,
        draft_id: str,
        reviewer: str,
        resolution: dict[str, Any] | None = None,
        as_new: bool = False,
    ) -> DraftRecord:
        """Approve a pending draft. For a Conflict, ``resolution`` maps each
        conflicting field to the chosen value (default: the proposed value);
        the admitted header records ``supersedes``. ``as_new`` admits a held
        draft at its own path instead of as an edit of its best match."""
        r = self.record(draft_id)
        if r.status != PENDING or r.reconciliation is None:
            raise GateError(f"draft {draft_id} is {r.status}, not pending")
        res = r.reconciliation
        if res["held"] and res["decision"] == Decision.DUPLICATE.value and not as_new:
            raise GateError("held Duplicate: approve with as_new=True or reject")
        r.final_path = r.path if (as_new or res["decision"] == "New") else res["target_path"]
        if res["decision"] == Decision.CONFLICT.value:
            chosen = {c["field"]: c["proposed"] for c in res["conflicts"]}
            chosen.update(resolution or {})
            r.resolution = chosen
        r.status, r.reviewer = APPROVED, reviewer
        self._save(r)
        return r

    def reject(self, draft_id: str, reviewer: str, reason: str = "") -> DraftRecord:
        r = self.record(draft_id)
        if r.status not in (STAGED, PENDING, APPROVED):
            raise GateError(f"draft {draft_id} is {r.status}")
        r.status, r.reviewer, r.note = REJECTED, reviewer, reason or "rejected by reviewer"
        self._save(r)
        return r

    def _final_concept(self, r: DraftRecord) -> Concept:
        d = self.draft(r.id)
        assert r.final_path is not None
        current = self.bundle.read(r.final_path)
        header = dict(d.header)
        if current is not None and "id" in current.header and r.final_path != d.path:
            header["id"] = current.header["id"]
        if r.resolution:
            header.update(r.resolution)
        if r.reconciliation and r.reconciliation["decision"] == Decision.CONFLICT.value:
            assert current is not None
            last = self.bundle.git.last_commit_touching(r.final_path, self.bundle.main)
            entry = {
                "snapshot": self.bundle.git.short(last or ""),
                "fields": sorted(c["field"] for c in r.reconciliation["conflicts"]),
            }
            prior = header.get("supersedes") or []
            header["supersedes"] = [*([prior] if isinstance(prior, dict) else prior), entry]
        if self.enforce_actor:
            header["actor"] = r.proposer
        return Concept(r.final_path, header, d.body)

    # ------------------------------------------------------------- merge
    def merge(
        self, message: str = "contextctl: admit reviewed drafts", when: str | None = None,
        who: Identity | None = None,
    ) -> str | None:
        """Merge every approved draft into main as one ``--no-ff`` merge.
        Returns the merge commit SHA, or None if nothing was approved."""
        approved = self.records(APPROVED)
        if not approved:
            return None
        git, main = self.bundle.git, self.bundle.main
        batch = hashlib.sha256("".join(r.id for r in approved).encode()).hexdigest()[:10]
        branch = f"contextctl/admit-{batch}"
        git.run("checkout", "-q", "-B", branch, main)
        try:
            for r in approved:
                c = self._final_concept(r)
                dest = self.bundle.root / c.path
                dest.parent.mkdir(parents=True, exist_ok=True)
                dest.write_text(c.dumps())
                for h in c.evidence_digests():
                    src = self.bundle.staging_evidence
                    if src.exists(h) and not self.bundle.evidence.exists(h):
                        self.bundle.evidence.root.mkdir(parents=True, exist_ok=True)
                        shutil.copyfile(src.path(h), self.bundle.evidence.path(h))
                        if src.meta_path(h).exists():
                            shutil.copyfile(src.meta_path(h), self.bundle.evidence.meta_path(h))
            git.run("add", "-A", "concepts", "evidence")
            body = "\n".join(
                f"- {r.final_path}: {(r.reconciliation or {}).get('decision')} "
                f"(draft {r.id}, reviewer {r.reviewer})"
                for r in approved
            )
            git.run(
                "commit", "-q", "--allow-empty", "-m", f"{message}\n\n{body}", when=when, who=who
            )
            git.run("checkout", "-q", main)
            git.run(*NO_HOOKS, "merge", "-q", "--no-ff", "-m", message, branch, when=when, who=who)
        except GitError:
            git.run("checkout", "-q", "-f", main, check=False)
            raise
        finally:
            git.run("branch", "-q", "-D", branch, check=False)
        sha = self.bundle.head()
        assert sha is not None
        for r in approved:
            r.status, r.merge = MERGED, sha
            self._save(r)
        self.sidecar.sync()
        return sha

    # ------------------------------------------------------------ revert
    def revert(
        self, merges: list[str], when: str | None = None, who: Identity | None = None
    ) -> list[str]:
        """Revert merges in the given order (newest first is usually needed).
        On a conflict the revert is aborted and :class:`RevertConflict` raised;
        commits already made in this call are kept."""
        git = self.bundle.git
        made: list[str] = []
        for m in merges:
            sha = git.head(m)
            if sha is None:
                raise GateError(f"unknown commit {m}")
            args = ["revert", "--no-edit"]
            if len(git.parents(sha)) > 1:
                args += ["-m", "1"]
            try:
                git.run(*NO_HOOKS, *args, sha, when=when, who=who)
            except GitError as exc:
                git.run("revert", "--abort", check=False)
                last = exc.stderr.strip().splitlines()[-1] if exc.stderr.strip() else ""
                raise RevertConflict(sha, last) from exc
            head = git.head()
            assert head is not None
            made.append(head)
        self.sidecar.sync()
        return made

    def clear(self) -> None:
        """Remove finished (merged / dropped / rejected) staging records."""
        for r in self.records():
            if r.status in (MERGED, DROPPED, REJECTED):
                self._rec_file(r.id).unlink(missing_ok=True)
                self._draft_file(r.id).unlink(missing_ok=True)


