import json

import pytest

from contextctl import bench, lifecycle, tokens
from contextctl.cli import main
from contextctl.connectors import dbt, notes, specified
from contextctl.mcp_server import serve


def test_notes_connector(tmp_path):
    (tmp_path / "a.md").write_text("# Pipeline owner\nOwner: alex\nOn Call: yes\n\nBody [[x]]\n")
    (tmp_path / "b.txt").write_text("no heading\n")
    (tmp_path / "skip.bin").write_bytes(b"\0")
    hs = notes.harvest_dir(tmp_path, "2024-01-01T00:00:00Z")
    assert [h.concept.path for h in hs] == ["concepts/notes/pipeline-owner.md",
                                           "concepts/notes/b.md"]
    h = hs[0].concept.header
    assert h["owner"] == "alex" and h["on_call"] == "yes" and h["evidence"]
    assert notes.slugify("!!!") == "untitled"


def test_dbt_connector_shapes():
    doc = b"""
semantic_models:
  - name: orders
    model: ref('orders')
    defaults: {agg_time_dimension: ordered_at}
    entities: [{name: order_id, type: primary}]
    measures:
      - {name: total, agg: sum, expr: amount}
      - {agg: sum}
metrics:
  - {name: m1, type: simple, label: M1, type_params: {measure: {name: total, filter: "x = 1"}}}
  - {name: m2, type: ratio, type_params: {numerator: {name: a}, denominator: b}}
  - {name: m3, type: simple, type_params: {measure: total}, filter: "y"}
  - just-a-string
"""
    hs = dbt.harvest_yaml(doc, "models/x.yml", "src", "abcdef0", "2024-01-01T00:00:00Z")
    by = {h.concept.path: h.concept.header for h in hs}
    assert by["concepts/measures/total.md"]["expr"] == "amount"
    assert by["concepts/metrics/m1.md"]["filter"] == "x = 1"
    assert by["concepts/metrics/m2.md"]["numerator"] == "a"
    assert by["concepts/metrics/m3.md"]["filter"] == "y"
    assert by["concepts/semantic_models/orders.md"]["entities"] == ["order_id type=primary"]
    assert dbt.harvest_yaml(b": : :", "x.yml", "s", "r", "t") == []
    assert dbt.harvest_yaml(b"- a", "x.yml", "s", "r", "t") == []
    assert len(dbt.FACT_FIELDS) == 14


def test_specified_stubs():
    with pytest.raises(NotImplementedError):
        specified.connector("slack")
    with pytest.raises(KeyError):
        specified.connector("nope")
    with pytest.raises(NotImplementedError):
        serve()


def test_lifecycle_nine_checks():
    checks = lifecycle.run()
    assert len(checks) == 9 and all(c.ok for c in checks), [c for c in checks if not c.ok]


def test_bench_small():
    (r,) = bench.run([20], commits=2, repeats=1)
    assert r["concepts"] == 20 and r["sidecar_rebuild_ms"] >= 0


def test_tokens():
    assert tokens.count("hello world") >= 2


def test_cli_end_to_end(tmp_path, capsys):
    b = str(tmp_path / "bundle")
    notes_dir = tmp_path / "notes"
    notes_dir.mkdir()
    (notes_dir / "p.md").write_text("# Payments owner\nOwner: alex\n\nOwned by alex.\n")
    assert main(["-C", b, "init"]) == 0
    assert main(["-C", b, "ingest", str(notes_dir)]) == 0
    assert main(["-C", b, "curate"]) == 0
    capsys.readouterr()
    assert main(["-C", b, "review", "list"]) == 0
    items = json.loads(capsys.readouterr().out)
    assert len(items) == 1 and items[0]["reconciliation"]["decision"] == "New"
    did = items[0]["id"]
    assert main(["-C", b, "review", "show", did]) == 0
    assert main(["-C", b, "review", "approve", did, "--reviewer", "ayan"]) == 0
    assert main(["-C", b, "merge"]) == 0
    assert main(["-C", b, "merge"]) == 0  # nothing approved
    capsys.readouterr()
    path = "concepts/notes/payments-owner.md"
    for argv in (["orient"], ["search", "payments"], ["read", path], ["read", path, "--body-only"],
                 ["history", path], ["history", path, "--field", "owner", "--valid-at",
                                     "2999-01-01"], ["provenance", path],
                 ["sidecar", "sync"], ["sidecar", "rebuild"], ["sidecar", "check"],
                 ["evidence", "verify"]):
        assert main(["-C", b, *argv]) == 0, argv
    out = capsys.readouterr().out
    assert "payments" in out.lower()
    assert main(["-C", b, "read", "concepts/none.md"]) == 1
    assert main(["-C", b, "review", "approve", "nope"]) == 1
    assert main(["-C", b, "revert", "nope"]) == 1
    draft = tmp_path / "d.md"
    draft.write_text("---\ntitle: D\n---\nbody\n")
    ev = tmp_path / "ev.txt"
    ev.write_text("evidence")
    assert main(["-C", b, "propose", str(draft), "--path", "concepts/d.md",
                 "--evidence", str(ev)]) == 0
    assert main(["-C", b, "evidence", "put", str(ev)]) == 0
    capsys.readouterr()
    assert main(["-C", b, "review", "list", "--all"]) == 0
    recs = json.loads(capsys.readouterr().out)
    staged = [r["id"] for r in recs if r["status"] == "staged"][0]
    assert main(["-C", b, "review", "reject", staged, "--reason", "test"]) == 0
    assert main(["-C", b, "mcp"]) == 2
    with pytest.raises(SystemExit):
        main(["-C", str(tmp_path / "nobundle"), "orient"])


def test_cli_selfcheck_bench_replay(tmp_path, capsys):
    assert main(["selfcheck"]) == 0
    assert main(["bench", "--sizes", "10", "--commits", "1", "--repeats", "1"]) == 0
    out = tmp_path / "r.json"
    assert main(["replay", "--repeats", "1", "--out", str(out), "-q"]) == 0
    data = json.loads(out.read_text())
    assert data["repeat_digests_identical"] and data["steps"] == 25


def test_cli_back_to_back_ingest_is_duplicate(tmp_path, capsys):
    """Regression for the per-second draft-ID collision: ingest, admit, and
    re-ingest the same notes immediately (no sleep); the re-ingest must get a
    new draft ID and be classified Duplicate."""
    b = str(tmp_path / "bundle")
    notes_dir = tmp_path / "notes"
    notes_dir.mkdir()
    (notes_dir / "p.md").write_text("# Payments owner\nOwner: alex\n\nOwned by alex.\n")
    assert main(["-C", b, "init"]) == 0
    assert main(["-C", b, "ingest", str(notes_dir)]) == 0
    assert main(["-C", b, "curate"]) == 0
    capsys.readouterr()
    assert main(["-C", b, "review", "list"]) == 0
    first = json.loads(capsys.readouterr().out)[0]
    assert main(["-C", b, "review", "approve", first["id"], "--reviewer", "r"]) == 0
    assert main(["-C", b, "merge"]) == 0
    seen = {first["id"]}
    for _ in range(5):
        assert main(["-C", b, "ingest", str(notes_dir)]) == 0
        capsys.readouterr()
        assert main(["-C", b, "curate"]) == 0
        recs = json.loads(capsys.readouterr().out)
        assert len(recs) == 1 and recs[0]["id"] not in seen
        assert recs[0]["decision"] == "Duplicate" and recs[0]["status"] == "dropped"
        seen.add(recs[0]["id"])
