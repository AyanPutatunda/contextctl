# Design notes: where the paper is ambiguous and what we chose

This implementation follows the paper's text (Sec. 2-3, Appendices B-C). Where the
text leaves a choice open, we picked the option below and pinned it with a test.

## Status labels

The paper labels claims *measured*, *demonstrated*, *specified* or *planned*. This
repository mirrors the paper's **Built** / **Specified only** split (Sec. 3):

| Component | Paper | This repo |
|---|---|---|
| Bundle (OKF files + Git) | built | built (`bundle.py`, `okf.py`) |
| Evidence store + digest verification | built | built (`evidence.py`) |
| Minimal reconciler + gate | built (min.) | built (`reconcile.py`, `gate.py`) |
| SQLite sidecar (SCD2, FTS5, links) | built (min.) | built (`sidecar.py`) |
| Rule-based notes connector | built | built (`connectors/notes.py`) |
| Replay harness (scripted admission, no model) | built | built (`replay.py`, `connectors/dbt.py`) |
| Nine-check lifecycle | built | built (`lifecycle.py`) |
| Microbenchmarks | built | built (`bench.py`) |
| Progressive-disclosure serving | specified; "only part of the CLI is built" | `orient/search/read/history/provenance/propose` built in Python API + CLI |
| Repository, document, Confluence, Slack, email connectors | specified | stubs that raise `NotImplementedError` (`connectors/specified.py`) |
| MCP server, plugin | specified | stub (`mcp_server.py`), no plugin |
| P1/P2 integrity properties | hypotheses | not proven; tests demonstrate the documented gaps |
| Setting `actor` from an authenticated identity | "not implemented" | optional extension, off by default (`enforce_actor: true` in `contextctl.yaml`) |
| Accuracy, gate efficacy on model drafts (RQ1-RQ6) | planned | not addressed |

## OKF-MEM header

* Header fields are split into identity (`id`, `title`), provenance (`actor`,
  `evidence`, `sources`/`source`, `confidence`, `supersedes`, `valid_from`,
  `valid_to`) and facts (everything else, or `fact_fields` in `contextctl.yaml`).
* `evidence` is a list of SHA-256 hex digests; `sources` entries are
  `{uri, fetched_at, evidence}`. A digest cited in `sources` counts as cited.
* Headers are written with sorted keys so files are byte-deterministic.

## Evidence object

* `evidence/<h>` holds the bytes; `h = sha256(bytes)` only. `evidence/<h>.meta.json`
  (`sha256, bytes, first_uri, fetched_at`) is written only when the blob is new, so
  a retried fetch keeps the *first* URI/time (paper: "written beside the blob when it
  is first stored").
* Connectors store fetched evidence in the **staging** evidence store
  (`.contextctl/staging/evidence/`). The gate copies verified blobs into the trusted
  `evidence/` folder in the same merge as the concept that cites them.
* Source deletion is unspecified in the paper and unhandled here.

## Reconciliation (Table I)

* Candidates: top-k FTS5 matches (k = 10, BM25 order, OR of the draft's terms, max
  64 terms) **plus** the same-path concept `c0`.
* `sim`: TF-IDF cosine with smoothed IDF `ln((1+N)/(1+df)) + 1`, IDF fitted over the
  trusted corpus plus the draft. Text = title + fact fields + body. Provenance is not
  part of the text, so a provenance-only update has `sim = 1`.
* **Conflict** is checked against `c0` only (the replay finding (a) scoping).
* **"No new facts"** (Duplicate): the draft asserts no fact field that the best match
  lacks or holds with a different value. A draft with `sim >= theta_dup` that does
  carry new facts falls to **Revision** (`sim >= theta_rev` still holds).
* Ties in `sim` prefer the same-path concept, then path order (determinism).
* **Held**: a Duplicate or Revision whose best match is a different path. Held
  drafts stay `pending`; a reviewer can admit them at their own path (`--as-new`) or
  reject them. A held Duplicate cannot be approved as an edit.
* A **New** draft at a path that already exists (low similarity, no conflicting
  facts) replaces that file when approved; it is still counted as New, as Table I
  says.
* The provenance-only-update gap is kept on purpose: the newer citation is dropped
  with the Duplicate (tested in `test_reconcile.py`).
* **Conflict resolution** defaults to the proposed values; `--resolve` overrides per
  field. The admitted header appends `supersedes: {snapshot, fields}` where
  `snapshot` is the first-parent main commit that last changed the concept.

## Gate and merge

* Validate: header schema, path under `concepts/`, confidence in [0, 1], ISO dates,
  every cited digest resolves (staging or trusted store) **and** re-hashes to its
  name. Unverifiable digests are rejected (what P2 relies on).
* Merge: all approved drafts in one batch go onto a temporary branch as one commit,
  then `git merge --no-ff` into `main`. Each merge is one snapshot (transaction time).
* The post-merge hook (`.git/hooks/post-merge`) runs `contextctl sidecar sync` for
  humans merging with plain Git; contextctl's own merges disable hooks and sync in
  process. `sync` is idempotent and rebuilds automatically if history was rewritten.
* Branch protection (assumption A2) is the hosting platform's job and is not
  enforced here.

## Sidecar and time

* `belief_history` tracks every header field **except** `valid_from`/`valid_to`
  (they are the SCD2 axis) plus `body_digest`. Values are canonical JSON.
* Effective (valid) time of a change = the concept's `valid_from` if that commit set
  or changed it, else the merge's commit time. This reproduces Appendix B, including
  the retroactive correction (rows split at 2024-07-04 while the snapshot stays
  2024-09-12).
* A commit that edits only `valid_from` writes no row: the paper's flagged gap
  (tested).
* A retroactive `valid_from` earlier than the open row's `valid_from` produces a
  zero/negative-length closed row; we keep it for audit rather than rewriting history.
* Times are normalized to `YYYY-MM-DDTHH:MM:SSZ` (UTC); a bare date means midnight UTC.
* Deleting a concept closes its open rows at the commit time.
* `tx_seq` (first-parent commit index) lets the sidecar reconstruct header fields as
  of any snapshot; the replay checks that reconstruction against `git show`.
* Transaction-time as-of reads resolve an ISO time with
  `git rev-list --first-parent --before=<t> main` and read the file at that commit.

## Replay harness

* Source steps = first-parent commits of the source repository that touch `models/`
  (up to an optional pin). The paper reports 25 steps for Jaffle Shop at `7be2c58`;
  this selection rule yields 23, so the paper's harness evidently selected steps
  differently (not stated in the paper).
* Every step re-harvests every YAML entry (semantic models, their measures, metrics),
  which is why most drafts are Duplicates. The paper's 22 final concepts imply a
  narrower harvest scope than ours (we get 43 on the same project); the paper does
  not list it.
* Scripted admission: Duplicate dropped; held Duplicate rejected; Revision/New
  approved (held Revision admitted at its own path); Conflict approved with proposed
  values.
* Determinism: fixed identities and source commit timestamps make bundle commit SHAs,
  counts and the sidecar dump identical across repeats (digest compared).

## Tokens

`tiktoken` `cl100k_base` when installed (as in the paper), else a `chars/4`
estimate that is labelled as such in every report.
