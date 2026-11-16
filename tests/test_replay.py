"""Replay harness: end-to-end on the synthetic dbt history, determinism."""

import json

import pytest

from contextctl.replay import replay, replay_repeats
from contextctl.synthetic import build_dbt_history


@pytest.fixture(scope="module")
def src(tmp_path_factory):
    return build_dbt_history(tmp_path_factory.mktemp("src") / "synthetic")


@pytest.fixture(scope="module")
def report(src, tmp_path_factory):
    return replay(src, tmp_path_factory.mktemp("b") / "bundle", source_name="synthetic")


def test_replay_counts_and_checks(report):
    assert report.steps == 25
    assert sum(report.counts.values()) == report.drafts
    assert all(report.counts[k] > 0 for k in ("New", "Duplicate", "Revision", "Conflict"))
    assert report.counts["Duplicate"] > report.drafts * 0.8  # dedup before humans
    assert report.asof_checks > 0 and report.asof_matches == report.asof_checks
    assert report.sidecar_tx_matches == report.sidecar_tx_checks
    assert report.rebuild_matches
    assert report.held > 0 and report.rejected_invalid == 0
    assert report.tokens["full_bundle"] > report.tokens["raw_sources"]
    assert json.loads(report.to_json())["determinism_digest"] == report.digest()


def test_replay_reproduces_appendix_b(report):
    rows = {(h["concept"], h["field"]): h["rows"] for h in report.example_history}
    food = rows[("concepts/measures/food_revenue.md", "expr")]
    assert [(r["valid_from"][:10], r["valid_to"] and r["valid_to"][:10]) for r in food] == [
        ("2023-08-30", "2024-09-12"), ("2024-09-12", "2025-07-01"), ("2025-07-01", None)]
    assert "= 1" in food[0]["new"] and "is true" in food[1]["new"]
    assert "is true" not in food[2]["new"]


def test_replay_deterministic(src):
    first, digests = replay_repeats(src, repeats=2, source_name="synthetic", max_steps=8,
                                    measure_tokens=False)
    assert len(set(digests)) == 1 and first.steps == 8


def test_replay_empty_prefix(src, tmp_path):
    with pytest.raises(ValueError):
        replay(src, tmp_path / "b", prefix="does-not-exist")
