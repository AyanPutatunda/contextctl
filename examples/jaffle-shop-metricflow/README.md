# MetricFlow values for the paper's enrichment step

Input for `contextctl replay --profile paper --observed-values examples/jaffle-shop-metricflow`
(the 25th step of the paper's Jaffle Shop replay; see examples/results/RESULTS.md).

* `mf_values.json`: for each of the 19 metrics declared in the public
  dbt-labs/jaffle-shop project at commit `7be2c58`, the overall value returned by
  `mf query --metrics <metric>` (no grouping, no filter), or the error MetricFlow
  returned (`revenue_growth_mom`, whose time-offset input is not supported without
  a time grouping). `duckdb_crosscheck` recomputes nine of them with plain SQL on
  the built tables; all nine agree.
* `<metric>.csv` / `<metric>.sql`: the query result and the SQL MetricFlow
  generated (`mf query --explain`); the replay stores both as evidence.

How they were produced: the public project at `7be2c58` built into DuckDB with its
own seed data (`dbt seed --vars '{load_source_data: true}'` then `dbt build`) using
dbt-core 1.12.5, dbt-duckdb 1.11.0 and dbt-metricflow 0.15.0, then one
`mf query` per metric. They are not derived from the Git history, which is why they
are shipped as data rather than recomputed by the replay. No file of the
jaffle-shop repository itself is included.
