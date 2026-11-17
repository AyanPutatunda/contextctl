# contextctl

**Auditable, open-format context infrastructure for AI agents.**

[![CI](https://github.com/AyanPutatunda/contextctl/actions/workflows/ci.yml/badge.svg)](https://github.com/AyanPutatunda/contextctl/actions/workflows/ci.yml)
License: Apache-2.0 · Python 3.10+ · no services, nothing paid

contextctl manages what AI agents remember as governed data: markdown concept
files in the Open Knowledge Format (OKF), Git as the write gate and the
transaction-time record, a content-addressed evidence folder, and a rebuildable
SQLite sidecar holding slowly changing dimension (SCD) Type 2 history.

This repository is the reference implementation of the demo paper
*contextctl: Auditable, Open-Format Context Infrastructure for AI Agents*
(Demo Paper, AAI-BD workshop, IEEE BigData 2026). The paper is in [`paper/`](paper/).

## Quickstart

```bash
git clone https://github.com/AyanPutatunda/contextctl.git && cd contextctl
python -m venv .venv && . .venv/bin/activate
pip install -e '.[tokens]'            # tokens = tiktoken, optional

contextctl selfcheck                  # nine-check no-model lifecycle
bash examples/quickstart.sh           # the four layers on synthetic notes
contextctl replay --repeats 3         # replay harness on a synthetic dbt history
python examples/reproduce_paper.py    # regenerate examples/results/RESULTS.md
```

## The four layers

```
 4. Serving ............ orient → search → read → history → provenance; propose only
 3. Metadata (derived) . SQLite sidecar: SCD2 belief_history, FTS5 search, links
 2. Curation (trust) ... validate → reconcile → review → merge (git --no-ff)
 1. Generation ......... connectors fetch evidence bytes and draft concept files
```

* **Generation is untrusted.** Connectors store raw bytes at `evidence/<sha256>`
  (digest of the bytes only; URI and fetch time live beside it) and draft OKF files
  into `.contextctl/staging/`.
* **Curation is the only path into trusted state.** The gate validates the header,
  rejects any evidence digest that does not resolve and re-hash, reconciles the draft
  (below), waits for review, then admits it with a `--no-ff` Git merge.
* **The sidecar is a cache.** Delete it and rebuild from Git; the rebuild must match
  the incremental one row for row (`contextctl sidecar check`).
* **Serving may only propose.** `propose` writes to staging, never to trusted state.

### Reconciliation (Table I)

| Rule | Condition | Action |
|---|---|---|
| Conflict | a fact field differs from the same-path concept | human resolves; `supersedes` recorded |
| Duplicate | max sim ≥ 0.9 and no new facts | drop draft; log match |
| Revision | 0.5 ≤ max sim < 0.9 | rewrite as edit of best match |
| New | max sim < 0.5 | add as new concept |

`sim` is TF-IDF cosine against the top-10 FTS5 matches plus the same-path concept.
Provenance fields (`actor`, `evidence`, `sources`, `confidence`, `supersedes`,
validity dates) never raise a Conflict. A Duplicate/Revision whose best match is a
different concept is **held** for review.

### Two time axes

* **Valid time** (from headers): `contextctl history PATH --field expr --valid-at 2024-06-01`
  runs the paper's range query on `belief_history`.
* **Transaction time** (from Git): `contextctl read PATH --as-of 2024-06-01` reads the
  file at the last main-line snapshot on or before that time.

## CLI

```
contextctl init                           create a bundle (git repo + layout)
contextctl ingest DIR                     rule-based notes connector -> staging
contextctl propose FILE [--evidence F..]  stage one OKF draft
contextctl curate                         validate + reconcile staged drafts
contextctl review list|show|approve|reject ID [--resolve JSON] [--as-new]
contextctl merge                          admit approved drafts (git merge --no-ff)
contextctl orient | search Q | read PATH [--as-of T] [--body-only]
contextctl history PATH [--field F] [--valid-at T]
contextctl provenance PATH                concept -> commit -> evidence -> source
contextctl evidence put FILE | verify
contextctl sidecar sync|rebuild|check
contextctl revert MERGE...                revert merges (newest first)
contextctl replay [--source PATH|synthetic] [--pin SHA] [--repeats N] [--profile paper]
                  [--harvest entries|metrics] [--include-pin] [--observed-values DIR]
                  [--held-duplicates reject|admit] [--similarity-text full|body]
contextctl selfcheck | bench | mcp
```

Global: `-C/--bundle DIR` (or `CONTEXTCTL_BUNDLE`). Reviewer name:
`--reviewer` or `CONTEXTCTL_REVIEWER`.

## Paper → code

| Paper | Code |
|---|---|
| Sec. 2, OKF-MEM header profile | `src/contextctl/okf.py` |
| Sec. 2, evidence object and digest scope | `src/contextctl/evidence.py` |
| Sec. 2, Git as write gate | `src/contextctl/gitio.py`, `bundle.py`, `gate.py` |
| Sec. 2, reconciliation, Table I | `src/contextctl/reconcile.py`, `similarity.py` |
| Sec. 2, curation (validate, reconcile, review, merge) | `src/contextctl/gate.py` |
| Sec. 2, metadata and time (SCD2, FTS5, links, post-merge hook) | `src/contextctl/sidecar.py` |
| Sec. 2, serving (progressive disclosure, propose) | `src/contextctl/serving.py`, `cli.py` |
| Sec. 2, P1/P2 and their gaps | `serving.provenance`, `tests/test_gate.py` (`test_tamper_finding_b`) |
| Sec. 3, built connector (notes) / specified connectors | `connectors/notes.py` / `connectors/specified.py` |
| Sec. 3, nine-check lifecycle | `src/contextctl/lifecycle.py` |
| Sec. 3 + App. C, replay harness | `src/contextctl/replay.py`, `connectors/dbt.py`, `connectors/dbt_metrics.py` (paper scope) |
| Sec. 3, finding (d) revert conflict | `gate.revert`, `tests/test_gate.py` (`test_revert_finding_d`) |
| App. B, worked bitemporal example | `tests/test_sidecar.py` |
| App. C, microbenchmarks | `src/contextctl/bench.py` |
| MCP server, plugin (specified only) | `src/contextctl/mcp_server.py` (stub) |

Built vs specified, and every interpretation choice, is listed in
[`docs/design-notes.md`](docs/design-notes.md).

## Results

[`examples/results/RESULTS.md`](examples/results/RESULTS.md) compares the paper's
measured numbers with this implementation on (a) a fully synthetic dbt-style
project and (b) the same public dbt Jaffle Shop history the paper replays (cloned
at runtime; no file from it is committed). All checks pass (as-of reads, sidecar
rebuild, identical repeats) and the Appendix B rows reproduce.

The paper's replay counts reproduce exactly with `--profile paper`:

```bash
git clone https://github.com/dbt-labs/jaffle-shop /tmp/jaffle-shop
contextctl replay --source /tmp/jaffle-shop --pin 7be2c58 --profile paper \
    --observed-values examples/jaffle-shop-metricflow --repeats 7
# 25 steps, 7 merges, 22 concepts, 373 drafts = 16 New / 322 Duplicate /
# 24 Revision / 11 Conflict, field-level as-of reads 665/665, 7/7 identical repeats
```

`--profile paper` means `--harvest metrics --include-pin --held-duplicates admit
--similarity-text body`; `--observed-values` supplies the MetricFlow values for the
paper's final enrichment step. The default configuration (`--harvest entries`)
harvests every semantic model, measure and metric and gives 764 drafts on the same
history (302 whole-file / 1,510 field-level as-of reads); `contextctl replay` with
no `--source` runs the synthetic project (531 drafts). RESULTS.md, "Differences
from the paper", attributes each difference to one setting.

## Development

```bash
pip install -e '.[dev,tokens]'
ruff check . && python -m mypy && pytest --cov
```

CI (GitHub Actions) runs ruff, mypy and pytest on Python 3.10/3.12/3.13, the
lifecycle, a deterministic replay, and a site build check. Pushes to `main` that pass
then deploy the site to Netlify production (https://contextctl.sh); pull requests get a
draft deploy. The project website
lives in [`site/`](site/) (static HTML, `python3 site/build.py`).

## Data

Synthetic data only. The `examples/` notes and the generated dbt project are
fictional. The optional public replay reads the public dbt Jaffle Shop repository;
`examples/jaffle-shop-metricflow/` holds the overall metric values MetricFlow computed
on that public project (see its README).

## Citation

```bibtex
@inproceedings{putatunda2026contextctl,
  title     = {contextctl: Auditable, Open-Format Context Infrastructure for AI Agents},
  author    = {Putatunda, Ayan and Jangoan, Suhas},
  booktitle = {2026 IEEE International Conference on Big Data (BigData),
               AAI-BD Workshop},
  year      = {2026},
  note      = {Demo paper}
}
```

## License

Apache-2.0. See [LICENSE](LICENSE).
