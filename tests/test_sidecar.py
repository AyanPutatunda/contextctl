"""SCD2 belief_history, as-of reads and the rebuild test (paper Sec. 2, App. B)."""

from conftest import WHO, admit, concept
from hypothesis import HealthCheck, given, settings
from hypothesis import strategies as st

from contextctl.okf import Concept
from contextctl.serving import Server
from contextctl.sidecar import Sidecar

P = "concepts/measures/food_revenue.md"
BODY = "Food share of line revenue; zero for drink lines.\n"


def food(expr, **extra):
    return concept(P, BODY, id="food_revenue", title="food_revenue", expr=expr, **extra)


def test_paper_appendix_b_rows_and_valid_time_query(gate, bundle):
    admit(gate, food("= 1"), "2023-08-30T16:00:00Z")
    admit(gate, food("is true"), "2024-09-12T20:00:00Z")
    admit(gate, food("bare boolean"), "2025-07-01T19:00:00Z")
    sc = gate.sidecar
    rows = sc.history(P, "expr")
    assert [(r.old_value, r.new_value, r.valid_from[:10], r.valid_to and r.valid_to[:10])
            for r in rows] == [
        (None, "= 1", "2023-08-30", "2024-09-12"),
        ("= 1", "is true", "2024-09-12", "2025-07-01"),
        ("is true", "bare boolean", "2025-07-01", None),
    ]
    assert sc.value_at(P, "expr", "2024-06-01") == "= 1"
    assert sc.value_at(P, "expr", "2025-01-01") == "is true"
    assert sc.value_at(P, "expr", "2020-01-01") is None
    # the paper's literal SQL returns the same thing
    q = ("SELECT new_value FROM belief_history WHERE concept=? AND field='expr' AND "
         "valid_from<=? AND (valid_to IS NULL OR valid_to>?)")
    assert sc.db.execute(q, (P, "2024-06-01", "2024-06-01")).fetchall() == [('"= 1"',)]


def test_retroactive_correction_two_time_axes(gate, bundle):
    admit(gate, food("= 1"), "2023-08-30T16:00:00Z")
    admit(gate, food("is true", valid_from="2024-07-04"), "2024-09-12T20:00:00Z")
    sc, srv = gate.sidecar, Server(bundle, gate.sidecar)
    rows = sc.history(P, "expr")
    assert rows[0].valid_to == "2024-07-04T00:00:00Z"
    assert rows[1].valid_from == "2024-07-04T00:00:00Z"
    assert rows[1].tx_time == "2024-09-12T20:00:00Z"  # transaction time unchanged
    # valid time says "is true" on 2024-08-01 ...
    assert sc.value_at(P, "expr", "2024-08-01") == "is true"
    # ... but the bundle as of 2024-08-01 still said "= 1"
    text = srv.read(P, as_of="2024-08-01")
    assert text is not None and Concept.parse(P, text).header["expr"] == "= 1"


def test_valid_from_only_edit_not_recorded_gap(gate, bundle):
    admit(gate, food("= 1"), "2023-08-30T16:00:00Z")
    n = len(gate.sidecar.history(P))
    # force a valid_from-only change through Git directly (Conflict-free path)
    c = bundle.read(P)
    c.header["valid_from"] = "2023-01-01"
    (bundle.root / P).write_text(c.dumps())
    bundle.git.run("commit", "-qam", "valid_from only", who=WHO, when="2023-09-01T00:00:00Z")
    gate.sidecar.sync()
    assert len(gate.sidecar.history(P)) == n  # documented gap (App. B)


def test_rebuild_matches_incremental_and_delete(gate, bundle):
    admit(gate, food("= 1"), "2023-08-30T16:00:00Z")
    admit(gate, concept("concepts/x.md", "links to [[concepts/measures/food_revenue.md]]",
                        title="x"), "2023-09-01T00:00:00Z")
    (bundle.root / P).unlink()
    bundle.git.run("commit", "-qam", "delete", who=WHO, when="2023-10-01T00:00:00Z")
    sc = gate.sidecar
    assert sc.sync() == 1
    assert sc.sync() == 0  # idempotent
    assert all(r.valid_to for r in sc.history(P))
    assert sc.links() == [("concepts/x.md", "concepts/measures/food_revenue.md")]
    assert sc.links("concepts/x.md")
    before = sc.canonical_dump()
    sc.close()
    bundle.sidecar_path.unlink()
    fresh = Sidecar(bundle)
    fresh.rebuild()
    assert fresh.canonical_dump() == before
    assert fresh.search("") == [] and fresh.snippet("") == []


def test_history_rewrite_triggers_rebuild(gate, bundle):
    admit(gate, food("= 1"), "2023-08-30T16:00:00Z")
    admit(gate, food("is true"), "2024-09-12T20:00:00Z")
    bundle.git.run("reset", "-q", "--hard", "HEAD~1")
    gate.sidecar.sync()
    assert gate.sidecar.value_at(P, "expr", "2030-01-01") == "= 1"


exprs = st.lists(st.sampled_from(["= 1", "is true", "bare"]), min_size=1, max_size=5)


@settings(max_examples=15, deadline=None,
          suppress_health_check=[HealthCheck.function_scoped_fixture])
@given(exprs)
def test_scd2_invariants_property(tmp_path_factory, seq):
    """For any admission sequence: at most one open row per field, rows of a
    field tile time without overlap, and valid-time lookups match the
    transaction-time as-of read when no retroactive dates are used."""
    from contextctl.bundle import Bundle
    from contextctl.gate import Gate

    b = Bundle.init(tmp_path_factory.mktemp("p") / "b", when="2020-01-01T00:00:00Z", who=WHO,
                    install_hook=False)
    g = Gate(b, Sidecar(b))
    times = [f"202{i + 1}-01-01T00:00:00Z" for i in range(len(seq))]
    for e, t in zip(seq, times, strict=True):
        if b.read(P) is None or b.read(P).header["expr"] != e:
            admit(g, food(e), t)
    rows = g.sidecar.history(P, "expr")
    assert sum(r.valid_to is None for r in rows) == 1
    for a, nxt in zip(rows, rows[1:], strict=False):
        assert a.valid_to == nxt.valid_from
    srv = Server(b, g.sidecar)
    for t in times:
        probe = t.replace("-01-01T", "-06-01T")
        text = srv.read(P, as_of=probe)
        assert text is not None
        assert Concept.parse(P, text).header["expr"] == g.sidecar.value_at(P, "expr", probe)



def test_removed_field_writes_tombstone(gate, bundle):
    p = "concepts/x.md"
    admit(gate, concept(p, "b\n", title="x", owner="ana", supersedes=[{"snapshot": "abc"}]),
          "2024-02-01T00:00:00Z")
    admit(gate, concept(p, "b and more words\n", title="x", owner="ana"), "2024-03-01T00:00:00Z")
    seq = len(bundle.git.first_parent_log(bundle.main)) - 1
    got = gate.sidecar.fields_as_of_tx(p, seq)
    assert "supersedes" not in got and got["owner"] == "ana"
    assert gate.sidecar.fields_as_of_tx(p, seq - 1)["supersedes"] == [{"snapshot": "abc"}]
    assert gate.sidecar.value_at(p, "supersedes", "2024-06-01") is None
