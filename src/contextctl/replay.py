"""Replay harness (paper, Sec. 3 and Appendix C).

Replays the first-parent history of a source Git repository (a dbt project)
through the full no-model pipeline: harvest the project at each step (layer 1;
every YAML entry by default, or one concept per metric with ``harvest="metrics"``),
validate + reconcile + scripted admission + ``--no-ff`` merge (layer 2), post-merge
sidecar sync (layer 3), and serving reads (layer 4).

Scripted admission policy (no model, no human):

* Duplicate (not held) -> dropped by the gate;
* held Duplicate -> rejected (it matches a different concept verbatim), or
  admitted at its own path with ``held_duplicates="admit"`` (the paper's policy);
* Revision / New -> approved; a held Revision is admitted at its own path;
* Conflict -> approved with the proposed values; ``supersedes`` recorded.

All bundle commits use the source commit's timestamp and a fixed identity, so a
replay is deterministic: repeats produce identical counts, identical bundle
commit SHAs and an identical sidecar dump.

End-of-run checks: every as-of read (by transaction *time*) at every snapshot
for every concept is compared with ``git show <snapshot>:<path>``; the
sidecar's transaction-time reconstruction is compared with the parsed header,
as a whole and field by field (``asof_fields``, the paper's 665/665 count);
a freshly rebuilt sidecar is compared row for row with the incremental one.
"""

from __future__ import annotations

import hashlib
import json
import tempfile
from collections import Counter
from collections.abc import Callable
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any

from . import tokens
from .bundle import Bundle, BundleConfig
from .connectors import Harvest
from .connectors import dbt as dbt_connector
from .connectors import dbt_metrics as metrics_connector
from .gate import DROPPED, PENDING, Gate
from .gitio import Git, Identity
from .okf import canonical
from .serving import Server
from .sidecar import Sidecar

REPLAY_IDENTITY = Identity("contextctl-replay", "replay@contextctl.invalid")
REVIEWER = "scripted-reviewer"

Harvester = Callable[[dict[str, bytes], str, str, str], list[Harvest]]


@dataclass
class StepResult:
    step: int
    source_commit: str
    time: str
    drafts: int
    counts: dict[str, int]
    held: int
    merge: str | None


@dataclass
class ReplayReport:
    source: str
    source_head: str
    config: dict[str, Any] = field(default_factory=dict)
    steps: int = 0
    merges: int = 0
    final_concepts: int = 0
    drafts: int = 0
    counts: dict[str, int] = field(default_factory=dict)
    held: int = 0
    rejected_invalid: int = 0
    asof_checks: int = 0
    asof_matches: int = 0
    asof_field_checks: int = 0
    asof_field_matches: int = 0
    sidecar_tx_checks: int = 0
    sidecar_tx_matches: int = 0
    rebuild_matches: bool = False
    bundle_head: str = ""
    sidecar_digest: str = ""
    tokens: dict[str, Any] = field(default_factory=dict)
    example_history: list[dict[str, Any]] = field(default_factory=list)
    step_log: list[StepResult] = field(default_factory=list)

    def digest(self) -> str:
        core = {k: v for k, v in asdict(self).items() if k not in ("tokens",)}
        return hashlib.sha256(canonical(core).encode()).hexdigest()

    def to_json(self) -> str:
        d = asdict(self)
        d["determinism_digest"] = self.digest()
        return json.dumps(d, indent=2, sort_keys=True, default=str) + "\n"


HARVESTS = ("entries", "metrics")
#: Fields compared per (snapshot, concept) by the field-level as-of check. These
#: are the fields the paper's harness compared (its 665/665 = 133 concept
#: snapshots x 5 fields); ``body_digest`` is our name for the body hash.
ASOF_FIELDS: tuple[str, ...] = ("description", "expr", "filter", "observed_value", "body_digest")


def source_steps(
    src: Git, ref: str, prefix: str, pin: str | None = None, include_pin: bool = False
) -> list[str]:
    """First-parent commits of ``ref`` (up to ``pin``) that touch ``prefix``.
    With ``include_pin`` the pinned commit itself is appended as a final step
    when it does not touch ``prefix`` (the paper's harness did this)."""
    tip = pin or ref
    out = src.run("rev-list", "--first-parent", "--reverse", tip, "--", prefix)
    steps = [x for x in out.splitlines() if x]
    if include_pin:
        full = src.head(tip)
        if full and (not steps or steps[-1] != full):
            steps.append(full)
    return steps


def files_at(
    src: Git, rev: str, prefix: str, exts: tuple[str, ...] = (".yml", ".yaml")
) -> dict[str, bytes]:
    files = {}
    for p in src.ls_tree(rev, prefix):
        if p.endswith(exts):
            files[p] = src.run_bytes("show", f"{rev}:{p}")
    return files


def replay(
    source: Path | str,
    bundle_dir: Path | str,
    source_name: str | None = None,
    ref: str = "HEAD",
    prefix: str = "models",
    pin: str | None = None,
    max_steps: int | None = None,
    harvester: Harvester | None = None,
    measure_tokens: bool = True,
    harvest: str = "entries",
    include_pin: bool = False,
    observed_values: Path | str | None = None,
    held_duplicates: str = "reject",
    similarity_text: str | None = None,
    asof_fields: tuple[str, ...] | list[str] = ASOF_FIELDS,
) -> ReplayReport:
    """Replay ``source`` into a fresh bundle at ``bundle_dir``.

    ``harvest``: ``entries`` (default; every semantic model, measure and metric
    is a concept, :mod:`.connectors.dbt`) or ``metrics`` (one concept per
    metric, the paper's scope, :mod:`.connectors.dbt_metrics`).
    ``include_pin``: also replay the pinned commit when it does not touch
    ``prefix``. ``observed_values``: a directory of MetricFlow results; adds one
    final enrichment step (``metrics`` harvest only). ``held_duplicates``:
    ``reject`` (default) or ``admit`` a held cross-path Duplicate at its own
    path. ``similarity_text``: ``full`` or ``body`` (default: ``full`` for
    ``entries``, ``body`` for ``metrics``).
    """
    if harvest not in HARVESTS:
        raise ValueError(f"harvest must be one of {HARVESTS}")
    if held_duplicates not in ("reject", "admit"):
        raise ValueError("held_duplicates must be 'reject' or 'admit'")
    if observed_values is not None and harvest != "metrics":
        raise ValueError("observed_values needs harvest='metrics'")
    src = Git(Path(source))
    exts: tuple[str, ...]
    if harvest == "metrics":
        default_harvester: Harvester = metrics_connector.harvest_tree
        fact_fields, sim_mode = metrics_connector.FACT_FIELDS, "body"
        exts = (".yml", ".yaml", ".sql")
    else:
        default_harvester = dbt_connector.harvest_tree
        fact_fields, exts, sim_mode = dbt_connector.FACT_FIELDS, (".yml", ".yaml"), "full"
    harvest_fn = harvester or default_harvester
    name = source_name or f"git+{Path(source).name}"
    steps = source_steps(src, ref, prefix, pin, include_pin)
    if max_steps is not None:
        steps = steps[:max_steps]
    if not steps:
        raise ValueError("source has no commits touching the prefix")
    head = src.head(pin or ref) or ""
    first_time = src.commit_time(steps[0])
    bundle = Bundle.init(
        bundle_dir,
        BundleConfig(fact_fields=list(fact_fields), similarity_text=similarity_text or sim_mode),
        when="2000-01-01T00:00:00Z" if first_time > "2000" else first_time,
        who=REPLAY_IDENTITY,
        install_hook=False,
    )
    sidecar = Sidecar(bundle)
    gate = Gate(bundle, sidecar)
    report = ReplayReport(source=name, source_head=head[:7])
    report.config = {
        "harvest": harvest, "include_pin": include_pin,
        "observed_values": observed_values is not None, "held_duplicates": held_duplicates,
        "similarity_text": bundle.config.similarity_text, "prefix": prefix,
        "asof_fields": list(asof_fields),
    }
    totals: Counter[str] = Counter()

    def run_step(i: int, sha: str, when: str, harvests: list[Harvest]) -> None:
        ids = [
            gate.propose(h.concept, h.evidence, proposer=str(h.concept.header.get("actor", "")))
            for h in harvests
        ]
        recs = gate.curate(ids)
        step_counts: Counter[str] = Counter()
        held = 0
        for r in recs:
            if r.reconciliation is None:
                report.rejected_invalid += 1
                continue
            dec = r.reconciliation["decision"]
            step_counts[dec] += 1
            if r.status == DROPPED:
                continue
            if r.status != PENDING:  # pragma: no cover - defensive
                continue
            if r.reconciliation["held"]:
                held += 1
                if dec == "Duplicate" and held_duplicates == "reject":
                    gate.reject(r.id, REVIEWER, "held duplicate of a different concept")
                    continue
                gate.approve(r.id, REVIEWER, as_new=True)
            else:
                gate.approve(r.id, REVIEWER)
        merge = gate.merge(
            message=f"replay step {i}: source {sha[:7]}", when=when, who=REPLAY_IDENTITY
        )
        gate.clear()
        totals.update(step_counts)
        report.held += held
        report.step_log.append(
            StepResult(i, sha[:7], when, len(ids), dict(sorted(step_counts.items())), held,
                       merge[:7] if merge else None)
        )

    for i, sha in enumerate(steps, 1):
        when = src.commit_time(sha)
        run_step(i, sha, when, harvest_fn(files_at(src, sha, prefix, exts), name, sha, when))
    if observed_values is not None:
        last = steps[-1]
        when = src.commit_time(last)
        base = harvest_fn(files_at(src, last, prefix, exts), name, last, when)
        run_step(len(steps) + 1, last, when,
                 metrics_connector.enrich(base, observed_values, name, when))
    report.steps = len(report.step_log)
    report.merges = sum(1 for s in report.step_log if s.merge)
    report.drafts = sum(s.drafts for s in report.step_log)
    report.counts = {k: totals.get(k, 0) for k in ("New", "Duplicate", "Revision", "Conflict")}
    report.final_concepts = len(bundle.concept_paths())
    report.bundle_head = (bundle.head() or "")[:7]
    _check(bundle, sidecar, report, tuple(asof_fields))
    if measure_tokens:
        report.tokens = token_costs(bundle, sidecar, files_at(src, steps[-1], prefix))
    report.example_history = _example_history(sidecar)
    sidecar.close()
    return report


def _check(
    bundle: Bundle, sidecar: Sidecar, report: ReplayReport,
    asof_fields: tuple[str, ...] = ASOF_FIELDS,
) -> None:
    git = bundle.git
    server = Server(bundle, sidecar)
    log = git.first_parent_log(bundle.main)
    for seq, sha in enumerate(log):
        if seq == 0:
            continue
        t = git.commit_time(sha)
        for path in bundle.concept_paths(sha):
            expected = git.show(sha, path)
            report.asof_checks += 1
            if server.read(path, as_of=t) == expected:
                report.asof_matches += 1
            c = bundle.read(path, sha)
            assert c is not None
            want = {k: json.loads(v) for k, v in c.tracked_fields().items()}
            got = sidecar.fields_as_of_tx(path, seq)
            report.sidecar_tx_checks += 1
            if got == want:
                report.sidecar_tx_matches += 1
            for f in asof_fields:  # field-level as-of reads (the paper's count)
                report.asof_field_checks += 1
                if got.get(f) == want.get(f):
                    report.asof_field_matches += 1
    with tempfile.TemporaryDirectory() as td:
        fresh = Sidecar(bundle, Path(td) / "rebuilt.sqlite")
        fresh.rebuild()
        report.rebuild_matches = fresh.canonical_dump() == sidecar.canonical_dump()
        fresh.close()
    report.sidecar_digest = sidecar.dump_digest()[:16]


def _example_history(sidecar: Sidecar, n: int = 3) -> list[dict[str, Any]]:
    rows = sidecar.db.execute(
        "SELECT concept, field, COUNT(*) c FROM belief_history WHERE field NOT IN "
        "('evidence','sources','supersedes','body_digest','actor','confidence') "
        "GROUP BY concept, field HAVING c >= 2 ORDER BY c DESC, concept, field LIMIT ?",
        (n,),
    ).fetchall()
    out = []
    for concept, fname, _ in rows:
        out.append({
            "concept": concept, "field": fname,
            "rows": [
                {"old": r.old_value, "new": r.new_value, "valid_from": r.valid_from,
                 "valid_to": r.valid_to, "snapshot": r.snapshot}
                for r in sidecar.history(concept, fname)
            ],
        })
    return out


def token_costs(bundle: Bundle, sidecar: Sidecar, raw: dict[str, bytes]) -> dict[str, Any]:
    """Disclosure-cost measurements (paper, Appendix C)."""
    server = Server(bundle, sidecar)
    concepts = bundle.concepts()
    if not concepts:
        return {}
    # pick the concept with the most history as the "task" target
    target = max(concepts, key=lambda c: (len(sidecar.history(c.path)), c.path))
    query = target.title.split(" (")[0].replace("_", " ")
    orient = json.dumps(server.orient(), sort_keys=True)
    search = json.dumps(server.search(query), sort_keys=True)
    full_read = server.read(target.path) or ""
    body_read = server.read(target.path, body_only=True) or ""
    full_bundle = "".join(c.dumps() for c in concepts)
    raw_text = "".join(b.decode("utf-8", "replace") for b in raw.values())
    o, s, r, b = (tokens.count(x) for x in (orient, search, full_read, body_read))
    fb, rw = tokens.count(full_bundle), tokens.count(raw_text)
    return {
        "tokenizer": "tiktoken cl100k_base" if tokens.exact() else "estimate chars/4",
        "target": target.path,
        "orient_search_read": o + s + r,
        "orient_search_read_body_only": o + s + b,
        "full_bundle": fb,
        "raw_sources": rw,
        "full_bundle_over_raw": round(fb / rw, 2) if rw else None,
        "full_concept_read": r,
        "body_only_read": b,
    }


def replay_repeats(
    source: Path | str, repeats: int = 3, **kwargs: Any
) -> tuple[ReplayReport, list[str]]:
    """Run the replay ``repeats`` times in fresh bundles; return the first
    report and the determinism digest of every run."""
    digests: list[str] = []
    first: ReplayReport | None = None
    for _ in range(repeats):
        with tempfile.TemporaryDirectory() as td:
            rep = replay(source, Path(td) / "bundle", **kwargs)
        digests.append(rep.digest())
        first = first or rep
    assert first is not None
    return first, digests
