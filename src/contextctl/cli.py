"""``contextctl`` command-line interface."""

from __future__ import annotations

import argparse
import datetime as dt
import json
import os
import sys
from collections.abc import Sequence
from pathlib import Path
from typing import Any

from . import __version__
from .bundle import Bundle
from .connectors import notes
from .gate import PENDING, Gate, GateError
from .gitio import Identity
from .okf import Concept
from .serving import Server
from .sidecar import Sidecar


def _now() -> str:
    return dt.datetime.now(dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _out(obj: Any) -> None:
    print(json.dumps(obj, indent=2, sort_keys=True, default=str))


def _bundle(a: argparse.Namespace) -> Bundle:
    root = Path(a.bundle)
    if not (root / "contextctl.yaml").exists():
        raise SystemExit(f"not a contextctl bundle: {root} (run `contextctl init`)")
    return Bundle(root)


def _who(a: argparse.Namespace) -> Identity:
    name = getattr(a, "reviewer", None) or os.environ.get("CONTEXTCTL_REVIEWER") or "reviewer"
    return Identity(name, f"{name}@contextctl.invalid")


def cmd_init(a: argparse.Namespace) -> int:
    b = Bundle.init(a.bundle)
    print(f"initialized contextctl bundle at {b.root}")
    return 0


def cmd_evidence(a: argparse.Namespace) -> int:
    b = _bundle(a)
    if a.action == "put":
        data = Path(a.file).read_bytes()
        rec = b.staging_evidence.put(data, a.uri or f"file:{a.file}", _now())
        _out(rec.__dict__)
        return 0
    results = b.evidence.verify_all()
    bad = [d for d, ok in results.items() if not ok]
    _out({"checked": len(results), "failed": bad})
    return 1 if bad else 0


def cmd_propose(a: argparse.Namespace) -> int:
    b = _bundle(a)
    text = Path(a.file).read_text()
    c = Concept.parse(a.path or f"concepts/{Path(a.file).name}", text)
    ev = [(Path(f).read_bytes(), f"file:{f}", _now()) for f in a.evidence or []]
    print(Server(b).propose(c, ev, proposer=a.proposer))
    return 0


def cmd_ingest(a: argparse.Namespace) -> int:
    b = _bundle(a)
    gate = Gate(b)
    ids = [gate.propose(h.concept, h.evidence, proposer="connector:notes")
           for h in notes.harvest_dir(Path(a.dir), _now())]
    print(f"staged {len(ids)} drafts")
    return 0


def cmd_curate(a: argparse.Namespace) -> int:
    gate = Gate(_bundle(a))
    recs = gate.curate()
    _out([{"id": r.id, "path": r.path, "status": r.status,
           "decision": (r.reconciliation or {}).get("decision"),
           "held": (r.reconciliation or {}).get("held"), "problems": r.problems,
           "note": r.note} for r in recs])
    return 0


def cmd_review(a: argparse.Namespace) -> int:
    gate = Gate(_bundle(a))
    try:
        if a.action == "list":
            _out([{"id": r.id, "path": r.path, "status": r.status,
                   "reconciliation": r.reconciliation}
                  for r in gate.records(None if a.all else PENDING)])
        elif a.action == "show":
            r = gate.record(a.id)
            _out({"record": r.__dict__, "draft": gate.draft(a.id).dumps()})
        elif a.action == "approve":
            res = json.loads(a.resolve) if a.resolve else None
            r = gate.approve(a.id, _who(a).name, resolution=res, as_new=a.as_new)
            print(f"approved {r.id} -> {r.final_path}")
        elif a.action == "reject":
            gate.reject(a.id, _who(a).name, a.reason or "")
            print(f"rejected {a.id}")
    except GateError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1
    return 0


def cmd_merge(a: argparse.Namespace) -> int:
    sha = Gate(_bundle(a)).merge(a.message, who=_who(a))
    print(sha or "nothing approved")
    return 0


def cmd_orient(a: argparse.Namespace) -> int:
    _out(Server(_bundle(a)).orient(limit=a.limit))
    return 0


def cmd_search(a: argparse.Namespace) -> int:
    _out(Server(_bundle(a)).search(a.query, k=a.k))
    return 0


def cmd_read(a: argparse.Namespace) -> int:
    text = Server(_bundle(a)).read(a.path, as_of=a.as_of, body_only=a.body_only)
    if text is None:
        print("not found", file=sys.stderr)
        return 1
    sys.stdout.write(text)
    return 0


def cmd_history(a: argparse.Namespace) -> int:
    _out(Server(_bundle(a)).history(a.path, a.field, a.valid_at))
    return 0


def cmd_provenance(a: argparse.Namespace) -> int:
    p = Server(_bundle(a)).provenance(a.path)
    _out({"concept": p.concept, "commit": p.commit, "ok": p.ok,
          "hops": [h.__dict__ for h in p.hops]})
    return 0 if p.ok else 1


def cmd_sidecar(a: argparse.Namespace) -> int:
    b = _bundle(a)
    sc = Sidecar(b)
    if a.action == "sync":
        print(f"applied {sc.sync()} commits")
    elif a.action == "rebuild":
        print(f"rebuilt from {sc.rebuild()} commits")
    else:
        import tempfile

        sc.sync()
        with tempfile.TemporaryDirectory() as td:
            fresh = Sidecar(b, Path(td) / "x.sqlite")
            fresh.rebuild()
            same = fresh.canonical_dump() == sc.canonical_dump()
            fresh.close()
        print("sidecar matches rebuild" if same else "sidecar DIFFERS from rebuild")
        return 0 if same else 1
    return 0


def cmd_revert(a: argparse.Namespace) -> int:
    try:
        made = Gate(_bundle(a)).revert(a.commits, who=_who(a))
    except GateError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1
    print("\n".join(made))
    return 0


#: ``--profile paper``: the paper's Jaffle Shop replay configuration (see
#: examples/results/RESULTS.md, "Reproducing the paper's replay").
PAPER_PROFILE: dict[str, Any] = {
    "harvest": "metrics", "include_pin": True, "held_duplicates": "admit",
    "similarity_text": "body",
}


def _replay_options(a: argparse.Namespace) -> dict[str, Any]:
    opts: dict[str, Any] = dict(PAPER_PROFILE) if a.profile == "paper" else {}
    for key in ("harvest", "held_duplicates", "similarity_text", "observed_values"):
        if getattr(a, key) is not None:
            opts[key] = getattr(a, key)
    if a.include_pin is not None:
        opts["include_pin"] = a.include_pin
    if a.asof_fields:
        opts["asof_fields"] = tuple(x for x in a.asof_fields.split(",") if x)
    return opts


def cmd_replay(a: argparse.Namespace) -> int:
    import tempfile

    from .replay import replay_repeats
    from .synthetic import build_dbt_history

    with tempfile.TemporaryDirectory() as td:
        if a.source == "synthetic":
            src = build_dbt_history(Path(td) / "synthetic-src")
            name = "synthetic:synthetic-beans"
        else:
            src, name = Path(a.source), a.source_name or a.source
        opts = _replay_options(a)
        rep, digests = replay_repeats(src, repeats=a.repeats, source_name=name, pin=a.pin,
                                      prefix=a.prefix, **opts)
    data = json.loads(rep.to_json())
    data["repeats"] = a.repeats
    data["repeat_digests_identical"] = len(set(digests)) == 1
    text = json.dumps(data, indent=2, sort_keys=True) + "\n"
    if a.out:
        Path(a.out).write_text(text)
    sys.stdout.write(text if not a.quiet else "")
    return 0 if data["repeat_digests_identical"] else 1


def cmd_selfcheck(a: argparse.Namespace) -> int:
    from .lifecycle import run

    checks = run()
    for i, c in enumerate(checks, 1):
        print(f"[{'PASS' if c.ok else 'FAIL'}] {i}. {c.name} -- {c.detail}")
    ok = all(c.ok for c in checks)
    print(f"{sum(c.ok for c in checks)}/{len(checks)} checks passed")
    return 0 if ok else 1


def cmd_bench(a: argparse.Namespace) -> int:
    from .bench import run

    _out(run(a.sizes, commits=a.commits, repeats=a.repeats))
    return 0


def cmd_mcp(a: argparse.Namespace) -> int:
    from .mcp_server import serve

    try:
        serve()
    except NotImplementedError as exc:
        print(str(exc), file=sys.stderr)
    return 2


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="contextctl", description=__doc__)
    p.add_argument("--version", action="version", version=f"contextctl {__version__}")
    p.add_argument("-C", "--bundle", default=os.environ.get("CONTEXTCTL_BUNDLE", "."),
                   help="bundle directory (default: $CONTEXTCTL_BUNDLE or .)")
    sub = p.add_subparsers(dest="cmd", required=True)

    sub.add_parser("init", help="create a bundle").set_defaults(fn=cmd_init)

    s = sub.add_parser("evidence", help="put / verify evidence")
    s.add_argument("action", choices=["put", "verify"])
    s.add_argument("file", nargs="?")
    s.add_argument("--uri")
    s.set_defaults(fn=cmd_evidence)

    s = sub.add_parser("propose", help="stage an OKF draft (staging only)")
    s.add_argument("file")
    s.add_argument("--path", help="bundle path, e.g. concepts/x.md")
    s.add_argument("--evidence", nargs="*", help="files to store as evidence")
    s.add_argument("--proposer", default="agent")
    s.set_defaults(fn=cmd_propose)

    s = sub.add_parser("ingest", help="run the rule-based notes connector on a folder")
    s.add_argument("dir")
    s.set_defaults(fn=cmd_ingest)

    sub.add_parser("curate", help="validate + reconcile staged drafts").set_defaults(
        fn=cmd_curate)

    s = sub.add_parser("review", help="list / show / approve / reject drafts")
    s.add_argument("action", choices=["list", "show", "approve", "reject"])
    s.add_argument("id", nargs="?")
    s.add_argument("--all", action="store_true")
    s.add_argument("--resolve", help='JSON field->value for a Conflict, e.g. {"owner":"x"}')
    s.add_argument("--as-new", action="store_true", help="admit a held draft at its own path")
    s.add_argument("--reason")
    s.add_argument("--reviewer")
    s.set_defaults(fn=cmd_review)

    s = sub.add_parser("merge", help="merge approved drafts into main (--no-ff)")
    s.add_argument("-m", "--message", default="contextctl: admit reviewed drafts")
    s.add_argument("--reviewer")
    s.set_defaults(fn=cmd_merge)

    s = sub.add_parser("orient", help="serving: bundle overview")
    s.add_argument("--limit", type=int, default=50)
    s.set_defaults(fn=cmd_orient)

    s = sub.add_parser("search", help="serving: full-text search")
    s.add_argument("query")
    s.add_argument("-k", type=int, default=5)
    s.set_defaults(fn=cmd_search)

    s = sub.add_parser("read", help="serving: read a concept (optionally as of a time)")
    s.add_argument("path")
    s.add_argument("--as-of", help="commit-ish or ISO time (transaction time)")
    s.add_argument("--body-only", action="store_true")
    s.set_defaults(fn=cmd_read)

    s = sub.add_parser("history", help="serving: SCD2 belief history / valid-time value")
    s.add_argument("path")
    s.add_argument("--field")
    s.add_argument("--valid-at", help="ISO time (valid time); requires --field")
    s.set_defaults(fn=cmd_history)

    s = sub.add_parser("provenance", help="serving: check concept->commit->evidence->source")
    s.add_argument("path")
    s.set_defaults(fn=cmd_provenance)

    s = sub.add_parser("sidecar", help="sync / rebuild / check the derived sidecar")
    s.add_argument("action", choices=["sync", "rebuild", "check"])
    s.set_defaults(fn=cmd_sidecar)

    s = sub.add_parser("revert", help="revert merges (in the order given)")
    s.add_argument("commits", nargs="+")
    s.add_argument("--reviewer")
    s.set_defaults(fn=cmd_revert)

    s = sub.add_parser("replay", help="run the replay harness")
    s.add_argument("--source", default="synthetic", help="'synthetic' or a path to a git repo")
    s.add_argument("--source-name")
    s.add_argument("--pin", help="replay up to this source commit")
    s.add_argument("--prefix", default="models")
    s.add_argument("--profile", choices=["default", "paper"], default="default",
                   help="'paper': --harvest metrics --include-pin --held-duplicates admit "
                        "--similarity-text body (add --observed-values for the final step)")
    s.add_argument("--harvest", choices=["entries", "metrics"],
                   help="entries (default): every semantic model, measure and metric; "
                        "metrics: one concept per metric (the paper's scope)")
    s.add_argument("--include-pin", dest="include_pin", action="store_true", default=None,
                   help="also replay the pinned commit if it does not touch --prefix")
    s.add_argument("--no-include-pin", dest="include_pin", action="store_false")
    s.add_argument("--observed-values", help="directory with mf_values.json (+ <metric>.csv/.sql):"
                   " adds a final enrichment step (metrics harvest only)")
    s.add_argument("--held-duplicates", choices=["reject", "admit"],
                   help="scripted review of a Duplicate held as a cross-path match")
    s.add_argument("--similarity-text", choices=["full", "body"],
                   help="text compared by TF-IDF: title+facts+body or body only")
    s.add_argument("--asof-fields", help="comma-separated fields for the field-level as-of check")
    s.add_argument("--repeats", type=int, default=3)
    s.add_argument("--out")
    s.add_argument("-q", "--quiet", action="store_true")
    s.set_defaults(fn=cmd_replay)

    sub.add_parser("selfcheck", help="run the nine-check lifecycle").set_defaults(
        fn=cmd_selfcheck)

    s = sub.add_parser("bench", help="microbenchmarks on synthetic bundles")
    s.add_argument("--sizes", type=int, nargs="+", default=[100, 1000])
    s.add_argument("--commits", type=int, default=5)
    s.add_argument("--repeats", type=int, default=5)
    s.set_defaults(fn=cmd_bench)

    sub.add_parser("mcp", help="MCP server (specified, not built)").set_defaults(fn=cmd_mcp)
    return p


def main(argv: Sequence[str] | None = None) -> int:
    a = build_parser().parse_args(argv)
    return int(a.fn(a))


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
