"""Metric-level harvester (the paper's replay scope), enrichment, replay options.
All inputs are synthetic."""

import json

import pytest
import yaml

from contextctl.bundle import Bundle, BundleConfig
from contextctl.cli import main
from contextctl.connectors import dbt_metrics as dm
from contextctl.replay import replay
from contextctl.synthetic import build_dbt_history

SEM = {
    "semantic_models": [{
        "name": "orders", "model": "ref('orders')",
        "defaults": {"agg_time_dimension": "ordered_at"},
        "entities": [{"name": "order_id", "type": "primary"}],
        "dimensions": [{"name": "ordered_at", "type": "time"},
                       {"name": "is_large", "type": "categorical"}],
        "measures": [{"name": "order_total", "agg": "sum", "expr": "order_total"},
                     {"name": "order_count", "agg": "sum", "expr": "1"}],
    }],
}
METRICS = {
    "metrics": [
        {"name": "order_total", "label": "Order Total", "type": "simple",
         "description": "Sum of   order totals.", "type_params": {"measure": "order_total"}},
        {"name": "large_orders", "type": "simple", "filter": "{{ Dimension('is_large') }}",
         "type_params": {"measure": {"name": "order_count"}}},
        {"name": "rolling_total", "type": "cumulative",
         "type_params": {"measure": "order_total", "window": "7 days"}},
        {"name": "large_share", "type": "ratio",
         "type_params": {"numerator": {"name": "large_orders"}, "denominator": "orders_n"}},
        {"name": "avg_total", "type": "derived",
         "type_params": {"expr": "order_total / orders_n",
                         "metrics": [{"name": "order_total"}, "orders_n"]}},
        {"name": "orders_n", "type": "simple", "type_params": {"measure": "missing_measure"}},
        {"label": "no name, ignored"},
    ],
}
FILES = {
    "models/marts/orders.yml": yaml.safe_dump(SEM).encode(),
    "models/marts/metrics.yml": yaml.safe_dump(METRICS).encode(),
    "models/marts/broken.yml": b"metrics: [unclosed",
    "models/marts/orders.sql": b"select * from {{ ref('stg_orders') }}",
    "models/staging/stg_orders.sql": b"select * from {{ source('ecom', 'raw_orders') }}",
}


def _by_name(harvests):
    return {h.concept.path.rsplit("/", 1)[-1][:-3]: h for h in harvests}


def test_harvest_metrics_resolves_facts_and_evidence():
    hs = _by_name(dm.harvest_tree(FILES, "synthetic", "a" * 40, "2024-01-02T03:04:05Z"))
    assert sorted(hs) == ["avg_total", "large_orders", "large_share", "order_total",
                          "orders_n", "rolling_total"]
    ot = hs["order_total"].concept
    assert ot.path == "concepts/metrics/order_total.md"
    h = ot.header
    assert h["expr"] == "order_total" and h["agg"] == "sum" and h["semantic_model"] == "orders"
    assert h["dimensions"] == ["is_large:categorical", "ordered_at:time"]
    assert h["upstream_models"] == ["orders", "stg_orders"]
    assert h["upstream_sources"] == ["ecom.raw_orders"]
    assert h["description"] == "Sum of order totals." and h["valid_from"] == "2024-01-02"
    assert "filter" not in h  # unasserted facts are omitted
    assert len(hs["order_total"].evidence) == len(h["evidence"]) == 4
    assert "aggregates measure `order_total` as sum(order_total)" in ot.body
    assert set(ot.facts(dm.FACT_FIELDS)) <= set(dm.FACT_FIELDS)
    assert "Filter: {{ Dimension('is_large') }}." in hs["large_orders"].concept.body
    assert "window: 7 days." in hs["rolling_total"].concept.body
    ls = hs["large_share"].concept
    assert ls.header["input_metrics"] == ["large_orders", "orders_n"]
    assert "ratio of metric `large_orders` to metric `orders_n`" in ls.body
    assert "[large_orders](large_orders.md)" in ls.body
    av = hs["avg_total"].concept
    assert "derived as expression `order_total / orders_n`" in av.body
    assert av.header["semantic_model"] == "orders"
    assert "semantic_model" not in hs["orders_n"].concept.header  # measure not found


def test_enrich_adds_observed_value(tmp_path):
    vals = {"metrics": {"order_total": {"status": "ok", "value": "12.5"},
                        "large_orders": {"status": "error", "error_message": "boom"}}}
    (tmp_path / "mf_values.json").write_text(json.dumps(vals))
    (tmp_path / "order_total.csv").write_text("order_total\n12.5\n")
    base = dm.harvest_tree(FILES, "synthetic", "a" * 40, "2024-01-02T03:04:05Z")
    out = _by_name(dm.enrich(base, tmp_path, "synthetic", "2024-01-02T03:04:05Z"))
    ot = out["order_total"].concept
    assert ot.header["observed_value"] == "12.5" and ot.header["actor"] == dm.ENRICH_ACTOR
    assert "Observed overall value (MetricFlow" in ot.body
    assert len(out["order_total"].evidence) == len(base[3].evidence) + 2
    lo = out["large_orders"].concept
    assert "observed_value" not in lo.header and "returned an error (boom)" in lo.body
    assert out["rolling_total"].concept.body == _by_name(base)["rolling_total"].concept.body


def test_similarity_text_config(tmp_path):
    with pytest.raises(ValueError):
        BundleConfig(similarity_text="headers").check()
    b = Bundle.init(tmp_path / "b", BundleConfig(similarity_text="body"), install_hook=False)
    assert Bundle(b.root).config.similarity_text == "body"
    assert "similarity_text: body" in b.config.dump()


@pytest.fixture(scope="module")
def src(tmp_path_factory):
    return build_dbt_history(tmp_path_factory.mktemp("src") / "synthetic")


@pytest.fixture(scope="module")
def values(tmp_path_factory):
    d = tmp_path_factory.mktemp("mf")
    (d / "mf_values.json").write_text(json.dumps(
        {"metrics": {"revenue": {"status": "ok", "value": "100.0"}}}))
    return d


def test_replay_paper_profile_on_synthetic(src, values, tmp_path):
    r = replay(src, tmp_path / "b", source_name="synthetic", harvest="metrics",
               include_pin=True, held_duplicates="admit", observed_values=values,
               measure_tokens=False)
    assert r.config["harvest"] == "metrics" and r.config["similarity_text"] == "body"
    assert r.steps == 26 and r.step_log[-1].counts == {"Duplicate": r.step_log[-1].drafts - 1,
                                                       "Revision": 1}
    assert sum(r.counts.values()) == r.drafts and r.counts["Conflict"] > 0
    assert r.asof_field_checks == 5 * r.sidecar_tx_checks > 0
    assert r.asof_field_matches == r.asof_field_checks
    assert r.sidecar_tx_matches == r.sidecar_tx_checks and r.rebuild_matches


@pytest.mark.parametrize("kw", [{"harvest": "rows"}, {"held_duplicates": "maybe"},
                                {"observed_values": "x"}])
def test_replay_rejects_bad_options(src, tmp_path, kw):
    with pytest.raises(ValueError):
        replay(src, tmp_path / "b", **kw)


def test_cli_replay_profile_paper(src, values, tmp_path, capsys):
    out = tmp_path / "r.json"
    assert main(["replay", "--source", str(src), "--profile", "paper", "--observed-values",
                 str(values), "--no-include-pin", "--asof-fields", "expr,body_digest",
                 "--repeats", "1", "--out", str(out), "-q"]) == 0
    d = json.loads(out.read_text())
    assert d["config"]["include_pin"] is False and d["config"]["asof_fields"] == [
        "expr", "body_digest"]
    assert d["asof_field_checks"] == 2 * d["sidecar_tx_checks"]
    assert d["config"]["held_duplicates"] == "admit"
