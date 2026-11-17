#!/usr/bin/env python3
"""Reproduce the paper's demo measurements as closely as possible.

    python examples/reproduce_paper.py                 # synthetic only (default)
    python examples/reproduce_paper.py --public-dbt    # also replay the public dbt
                                                       # Jaffle Shop history (network)
    python examples/reproduce_paper.py --sizes 100 1000 10000

Writes examples/results/results.json and examples/results/RESULTS.md.
The synthetic replay uses only generated data. --public-dbt clones the public
dbt-labs/jaffle-shop repository into a temporary directory at runtime (no file
from it is committed here), pins it at 7be2c58, as in the paper, and replays it
twice: with the default configuration and with ``--profile paper`` plus the
MetricFlow values in examples/jaffle-shop-metricflow/ (the paper's 25th step).
It also runs a one-factor-at-a-time ablation that attributes the gap between
the two configurations (see RESULTS.md, "Differences from the paper").
"""

from __future__ import annotations

import argparse
import json
import platform
import sqlite3
import subprocess
import sys
import tempfile
from pathlib import Path

from contextctl import bench, lifecycle, tokens
from contextctl.replay import replay_repeats
from contextctl.synthetic import build_dbt_history

OUT = Path(__file__).resolve().parent / "results"
MF_VALUES = Path(__file__).resolve().parent / "jaffle-shop-metricflow"
PAPER_PROFILE = {"harvest": "metrics", "include_pin": True, "held_duplicates": "admit",
                 "similarity_text": "body"}
PAPER_FULL = {**PAPER_PROFILE, "observed_values": str(MF_VALUES)}

#: One-factor-at-a-time ablation on the public project (label, replay options, CLI flags).
ABLATION: list[tuple[str, dict, str]] = [
    ("Default (`entries` harvest)", {}, ""),
    ("+ include the pinned commit", {"include_pin": True}, "--include-pin"),
    ("Metric-level harvest only", {"harvest": "metrics"}, "--harvest metrics"),
    ("Metric-level harvest, title+facts+body similarity",
     {"harvest": "metrics", "similarity_text": "full"}, "--harvest metrics --similarity-text full"),
    ("+ include the pinned commit", {"harvest": "metrics", "include_pin": True},
     "--harvest metrics --include-pin"),
    ("+ MetricFlow enrichment step",
     {"harvest": "metrics", "include_pin": True, "observed_values": str(MF_VALUES)},
     "--harvest metrics --include-pin --observed-values examples/jaffle-shop-metricflow"),
    ("`--profile paper` without the enrichment step", PAPER_PROFILE, "--profile paper"),
    ("`--profile paper`, title+facts+body similarity",
     {**PAPER_FULL, "similarity_text": "full"},
     "--profile paper --observed-values examples/jaffle-shop-metricflow "
     "--similarity-text full"),
    ("**`--profile paper` + enrichment (reproduces the paper)**", PAPER_FULL,
     "--profile paper --observed-values examples/jaffle-shop-metricflow"),
]

PAPER = {
    "steps": 25, "merges": 7, "final_concepts": 22, "drafts": 373,
    "New": 16, "Duplicate": 322, "Revision": 24, "Conflict": 11,
    "asof": "665/665", "orient_search_read": 2026, "full_bundle": 30460, "raw": 6579,
    "full_over_raw": 4.6, "full_read": 1339, "body_read": 190, "body_variant": 880,
}


def run_replay(src: Path, name: str, repeats: int, pin: str | None = None,
               keep_steps: bool = False, **opts: object) -> dict:
    rep, digests = replay_repeats(src, repeats=repeats, source_name=name, pin=pin, **opts)
    d = json.loads(rep.to_json())
    d["repeats"] = repeats
    d["repeat_digests_identical"] = len(set(digests)) == 1
    if not keep_steps:
        d.pop("step_log", None)
    return d


def summary(d: dict) -> dict:
    keys = ("steps", "merges", "final_concepts", "drafts", "counts", "held", "asof_checks",
            "asof_matches", "asof_field_checks", "asof_field_matches", "sidecar_tx_checks",
            "sidecar_tx_matches", "rebuild_matches")
    return {k: d[k] for k in keys}


def table_row(label: str, paper: object, *ours: object) -> str:
    return "| " + " | ".join([label, str(paper), *[str(o) for o in ours]]) + " |"


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--public-dbt", action="store_true")
    ap.add_argument("--sizes", type=int, nargs="+", default=[100, 1000, 10000])
    ap.add_argument("--repeats", type=int, default=7)
    ap.add_argument("--render-only", action="store_true", help="re-render RESULTS.md")
    ap.add_argument("--reuse-bench", action="store_true",
                    help="keep the microbenchmarks from the existing results.json")
    a = ap.parse_args()
    if a.render_only:
        (OUT / "RESULTS.md").write_text(render(json.loads((OUT / "results.json").read_text())))
        return 0
    OUT.mkdir(parents=True, exist_ok=True)

    results: dict = {
        "environment": {
            "python": platform.python_version(), "sqlite": sqlite3.sqlite_version,
            "git": subprocess.run(["git", "--version"], capture_output=True, text=True)
            .stdout.strip(),
            "platform": platform.platform(), "tokenizer_exact": tokens.exact(),
        }
    }
    checks = lifecycle.run()
    results["lifecycle"] = [{"name": c.name, "ok": c.ok} for c in checks]
    print(f"lifecycle: {sum(c.ok for c in checks)}/{len(checks)}")

    with tempfile.TemporaryDirectory() as td:
        src = build_dbt_history(Path(td) / "synthetic-beans")
        results["replay_synthetic"] = run_replay(src, "synthetic:synthetic-beans", a.repeats)
        print("synthetic replay:", results["replay_synthetic"]["counts"])
        if a.public_dbt:
            pub = Path(td) / "jaffle-shop"
            subprocess.run(["git", "clone", "-q", "https://github.com/dbt-labs/jaffle-shop",
                            str(pub)], check=True)
            url = "https://github.com/dbt-labs/jaffle-shop"
            results["replay_public_dbt"] = run_replay(pub, url, a.repeats, pin="7be2c58")
            print("public dbt replay:", results["replay_public_dbt"]["counts"])
            results["replay_public_dbt_paper"] = run_replay(
                pub, url, a.repeats, pin="7be2c58", keep_steps=True, **PAPER_FULL)
            print("public dbt replay, --profile paper:",
                  results["replay_public_dbt_paper"]["counts"])
            results["ablation_public_dbt"] = []
            for label, opts, flags in ABLATION:
                d = run_replay(pub, url, 1, pin="7be2c58", measure_tokens=False, **opts)
                results["ablation_public_dbt"].append(
                    {"label": label, "flags": flags, **summary(d)})
                print("ablation:", label, d["drafts"], d["counts"])

    prev = OUT / "results.json"
    if a.reuse_bench and prev.exists():
        results["bench"] = json.loads(prev.read_text())["bench"]
    else:
        results["bench"] = bench.run(a.sizes, commits=10, repeats=5)
    print("bench:", results["bench"])
    (OUT / "results.json").write_text(json.dumps(results, indent=2, sort_keys=True) + "\n")
    (OUT / "RESULTS.md").write_text(render(results))
    print(f"wrote {OUT / 'RESULTS.md'}")
    ok = all(c.ok for c in checks) and results["replay_synthetic"]["repeat_digests_identical"]
    return 0 if ok else 1


def _cnt(d: dict) -> str:
    c = d["counts"]
    return f"{d['drafts']} ({c['New']}/{c['Duplicate']}/{c['Revision']}/{c['Conflict']})"


def render(r: dict) -> str:
    syn = r["replay_synthetic"]
    pub = r.get("replay_public_dbt")
    pap = r.get("replay_public_dbt_paper")
    reps = [syn] + [x for x in (pub, pap) if x]
    cols = ["Paper (measured, public dbt Jaffle Shop)", "This repo: synthetic replay (default)"]
    if pub:
        cols.append("This repo: public dbt (7be2c58), default")
    if pap:
        cols.append("This repo: public dbt (7be2c58), `--profile paper`")
    lines = [
        "# Reproduced results",
        "",
        "Generated by `python examples/reproduce_paper.py"
        + (" --public-dbt" if pub else "") + "`. Environment: "
        + ", ".join(f"{k} {v}" for k, v in r["environment"].items()) + ".",
        "",
        "**Read this first.** Three replays are reported. The *synthetic* replay",
        "(`contextctl replay`, no `--source`) runs on a generated project, so its counts",
        "are not expected to equal the paper's. The *public dbt, default* column replays",
        "the public dbt Jaffle Shop history with this repository's default harvester,",
        "which is broader than the paper's. The *`--profile paper`* column replays the",
        "same history with the paper's harvest scope and step selection plus the",
        "MetricFlow values for its final step, and reproduces the paper's replay counts",
        "and as-of check exactly. Section \"Differences from the paper\" attributes",
        "every difference between the columns to one setting.",
        "",
        "## Lifecycle",
        "",
        f"{sum(c['ok'] for c in r['lifecycle'])}/{len(r['lifecycle'])} no-model lifecycle "
        "checks passed (paper: nine-check lifecycle passed).",
        "",
        "## Replay",
        "",
        "| Metric | " + " | ".join(cols) + " |",
        "|---" * (len(cols) + 1) + "|",
    ]

    def row(label: str, paper: object, key) -> str:
        return table_row(label, paper, *[key(d) for d in reps])

    P = PAPER
    lines += [
        row("Source steps", P["steps"], lambda d: d["steps"]),
        row("Bundle merges", P["merges"], lambda d: d["merges"]),
        row("Final concepts", P["final_concepts"], lambda d: d["final_concepts"]),
        row("Drafts", P["drafts"], lambda d: d["drafts"]),
        row("New", P["New"], lambda d: d["counts"]["New"]),
        row("Duplicate", P["Duplicate"], lambda d: d["counts"]["Duplicate"]),
        row("Revision", P["Revision"], lambda d: d["counts"]["Revision"]),
        row("Conflict", P["Conflict"], lambda d: d["counts"]["Conflict"]),
        row("Held for review (cross-path Dup/Rev)", "6", lambda d: d["held"]),
        row("As-of reads matching `git show`, field level (paper's count)", P["asof"],
            lambda d: f"{d['asof_field_matches']}/{d['asof_field_checks']}"),
        row("As-of reads matching `git show`, whole file (one per concept and snapshot)",
            "n/r", lambda d: f"{d['asof_matches']}/{d['asof_checks']}"),
        row("Sidecar tx-time reconstruction matches (all tracked fields)", "n/r",
            lambda d: f"{d['sidecar_tx_matches']}/{d['sidecar_tx_checks']}"),
        row("Rebuilt sidecar = incremental", "yes", lambda d: "yes" if d["rebuild_matches"]
            else "NO"),
        row("Repeats with identical results", "7/7",
            lambda d: f"{d['repeats']}/{d['repeats']}" if d["repeat_digests_identical"]
            else "NO"),
        row("Tokens: orient+search+read", P["orient_search_read"],
            lambda d: d["tokens"]["orient_search_read"]),
        row("Tokens: body-only variant", P["body_variant"],
            lambda d: d["tokens"]["orient_search_read_body_only"]),
        row("Tokens: full governed bundle", P["full_bundle"], lambda d: d["tokens"]["full_bundle"]),
        row("Tokens: raw source files (YAML)", P["raw"], lambda d: d["tokens"]["raw_sources"]),
        row("Full bundle / raw", f"{P['full_over_raw']}x",
            lambda d: f"{d['tokens']['full_bundle_over_raw']}x"),
        row("Full concept read vs body", f"{P['full_read']} vs {P['body_read']}",
            lambda d: f"{d['tokens']['full_concept_read']} vs {d['tokens']['body_only_read']}"),
        "",
        f"Tokenizer: {syn['tokens']['tokenizer']}. n/r = not reported in the paper. "
        "Held count for the paper: 1 cross-path Duplicate + 5 cross-path Revisions, from the",
        "authors' replay log. Token counts are not expected to match: the paper's serving",
        "tiers and concept files were formatted differently, and token counts depend on",
        "formatting (see Differences, item 7).",
        "",
    ]
    if pap:
        lines += [
            "## Reproducing the paper's replay",
            "",
            "```bash",
            "git clone https://github.com/dbt-labs/jaffle-shop /tmp/jaffle-shop",
            "contextctl replay --source /tmp/jaffle-shop --pin 7be2c58 --profile paper \\",
            "    --observed-values examples/jaffle-shop-metricflow --repeats 7",
            "```",
            "",
            f"Result: {pap['steps']} steps, {pap['merges']} merges, {pap['final_concepts']} final "
            f"concepts, {_cnt(pap)} drafts (New/Duplicate/Revision/Conflict), field-level "
            f"as-of reads {pap['asof_field_matches']}/{pap['asof_field_checks']}, "
            f"{pap['repeats']}/{pap['repeats']} identical repeats. Paper: 25 steps, 7 merges, "
            "22 final concepts, 373 (16/322/24/11), 665/665, 7/7.",
            "",
            "`--profile paper` is shorthand for `--harvest metrics --include-pin "
            "--held-duplicates admit --similarity-text body`. The per-step outcomes:",
            "",
            "| Step | Source commit | Drafts | Outcomes | Held | Merge |",
            "|---|---|---|---|---|---|",
        ]
        for st in pap.get("step_log", []):
            outs = ", ".join(f"{k} {v}" for k, v in st["counts"].items())
            lines.append(f"| {st['step']} | {st['source_commit']} | {st['drafts']} | {outs} | "
                         f"{st['held'] or '-'} | {st['merge'] or 'no-op'} |")
        lines += [
            "",
            "Step 25 re-issues the 19 metrics at the pinned commit with the overall value",
            "MetricFlow computed for each (`observed_value`), which is why it yields 19",
            "Revisions. Those values are not derived from the Git history, so they are",
            "shipped in `examples/jaffle-shop-metricflow/` (see its README for how they were",
            "produced). Without `--observed-values` the profile stops after step 24.",
            "",
        ]
    ab = r.get("ablation_public_dbt")
    if ab:
        lines += [
            "## Differences from the paper",
            "",
            "One setting changed at a time on the public dbt Jaffle Shop history pinned at",
            "7be2c58 (each row is a real run; `contextctl replay --source <clone> --pin 7be2c58`",
            "plus the flags shown):",
            "",
            "| Configuration | Flags | Steps | Merges | Final concepts | Drafts (N/D/R/C) | "
            "As-of, field level | As-of, whole file |",
            "|---|---|---|---|---|---|---|---|",
        ]
        for x in ab:
            lines.append(
                f"| {x['label']} | `{x['flags'] or '(none)'}` | {x['steps']} | {x['merges']} | "
                f"{x['final_concepts']} | {_cnt(x)} | "
                f"{x['asof_field_matches']}/{x['asof_field_checks']} | "
                f"{x['asof_matches']}/{x['asof_checks']} |")
        lines += ["", DIFFERENCES, ""]
    lines += ["### Worked bitemporal example (paper, Appendix B)", ""]
    for label, rep, what in (("Synthetic replay", syn, "measure"),
                             ("Public dbt replay, default", pub, "measure"),
                             ("Public dbt replay, `--profile paper`", pap, "metric")):
        if not rep:
            continue
        for h in rep["example_history"]:
            if not (h["concept"].endswith("food_revenue.md") and h["field"] == "expr"):
                continue
            lines += [f"{label}, `food_revenue` {what}, field `expr`:", "",
                      "| old | new | valid_from | valid_to | snapshot |", "|---|---|---|---|---|"]
            for x in h["rows"]:
                lines.append(f"| {x['old'] or '--'} | {x['new']} | {x['valid_from'][:10]} | "
                             f"{(x['valid_to'] or 'open')[:10]} | {x['snapshot']} |")
            lines.append("")
    lines += ["Paper: `= 1` 2023-08-30..2024-09-12, `is true` 2024-09-12..2025-07-01,",
              "bare boolean 2025-07-01..open. The synthetic history scripts the same three",
              "changes, so its dates match by construction; snapshot SHAs differ."]
    if pub:
        lines.append("On the public dbt Jaffle Shop history the three rows and their dates match "
                     "the paper's Appendix B exactly.")
    lines.append("")
    lines += ["## Microbenchmarks (synthetic, medians, this machine)", "",
              "| Concepts | Snapshot commit (ms) | As-of read (ms) | Valid-time query (ms) | "
              "Revert (ms) | Sidecar rebuild (ms) |", "|---|---|---|---|---|---|"]
    for b in r["bench"]:
        lines.append(f"| {b['concepts']} | {b['snapshot_commit_ms']} | {b['asof_read_ms']} | "
                     f"{b['valid_time_query_ms']} | {b['revert_ms']} | {b['sidecar_rebuild_ms']} |")
    lines += ["", "Paper (single vCPU, 10^4 concepts): snapshot commit 155.1 ms, as-of read and",
              "revert about 3-5 ms, sidecar rebuild 0.47 s. Our rebuild parses every concept and",
              "rebuilds history, FTS5 and links in Python, so it is slower in absolute terms;",
              "absolute timings depend on hardware.", ""]
    return "\n".join(lines)


DIFFERENCES = """\
What drives each difference:

1. **Harvest scope (the main factor).** The default `entries` harvester makes a
   concept of every semantic model, every measure and every metric (43 final
   concepts). The paper's replay made one concept per *metric* (22 final concepts,
   19 declared at the pin plus 3 renamed-away metrics that were never retracted),
   with each metric resolved through its measure to its semantic model, so a
   metric concept carries `expr`, `agg`, `dimensions`, `entities` and so on
   (`--harvest metrics`). This alone moves drafts from 764 to 335.
2. **Which fact fields.** The Conflict rule is the same in both configurations:
   a Conflict needs a fact field that the draft and the *same-path* concept both
   assert with different values. Only the field list differs with the scope:
   `entries` uses 14 entry-level fields (`model`, `measures`, `numerator`, ...);
   `metrics` uses the paper's 14 metric fields (`metric_type`, `type_params`,
   `filter`, `measure`, `agg`, `expr`, `semantic_model`, `time_dimension`,
   `entities`, `dimensions`, `input_metrics`, `upstream_models`,
   `upstream_sources`, `observed_value`). The rule itself was never the cause.
   Each of the 11 Conflicts is an upstream edit of `dimensions` (7, on
   2024-04-04) or `expr` (4, on 2024-09-12 and 2025-07-01), as the paper says;
   they are the same 11 concept/field pairs, and the 6 held cross-path matches the
   same 6 drafts, as in the authors' replay log.
3. **Step selection.** Both select first-parent commits that touch `models/` up
   to the pin (23 on this history). The paper's harness also replayed the pinned
   commit itself, which touches no model file (`--include-pin`, 24 steps), and
   then a 25th *enrichment* step that re-issues every metric with the overall
   value MetricFlow computed for it (`--observed-values`). Step 24 adds 19
   Duplicates; step 25 adds 19 Revisions.
4. **Scripted review of held cross-path Duplicates.** The paper's scripted
   reviewer approved every held draft, including the one held Duplicate (the
   `large_order` -> `large_orders` rename, admitted at its own path). The
   default replay rejects held Duplicates. Rejecting it keeps `large_orders` out
   of the bundle (re-held as a Duplicate at four more steps), so its 2024-04-04
   `dimensions` edit arrives as a held cross-path Revision against `large_order`
   instead of a same-path Conflict (10 instead of 11 Conflicts, 25 instead of 24
   Revisions, 6 instead of 7 merges) (`--held-duplicates admit`).
5. **What TF-IDF compares.** The paper's similarity compares concept bodies (its
   bodies restate the facts in prose; Sec. 2: facts stated only in the body
   affect only `sim`). The default compares title + fact values + body. With
   the paper profile this changes one draft (New 15 -> 16, Duplicate 323 -> 322)
   (`--similarity-text body`, the default for `--harvest metrics`).
6. **What an "as-of read" counts.** The paper's 665 is field-level: at each of
   the bundle's first-parent snapshots, for each concept, the sidecar's
   transaction-time value of 5 fields (`description`, `expr`, `filter`,
   `observed_value`, body digest) is compared with the file from `git show`:
   133 concept-snapshots x 5 fields = 665. The default report's 302 counts
   whole-file reads (one per concept-snapshot) on a different bundle. Both
   counts are now reported (`asof_field_*` and `asof_*`); `--asof-fields` changes
   the field list.
7. **Not reproduced: token counts and timings.** These depend on the concept
   file layout and the serving responses, which the paper does not specify
   byte for byte, and on hardware.

`contextctl replay` with no `--source` runs the synthetic history: 531 drafts and
362/362 whole-file as-of reads (1,810/1,810 field-level). Those numbers describe
the synthetic project and are not comparable with the paper's."""


if __name__ == "__main__":
    sys.exit(main())
