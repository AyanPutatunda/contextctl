"""Curation: validate, reconcile, review, merge; revert; provenance."""

import pytest
from conftest import WHO, admit, concept

from contextctl.evidence import sha256_hex
from contextctl.gate import APPROVED, DROPPED, MERGED, PENDING, REJECTED, GateError, RevertConflict
from contextctl.serving import Server

BODY = "The nightly payments pipeline is owned by the platform team on rotation.\n"


def note(owner="alex", path="concepts/p.md", body=BODY, data=b"slack thread v1", **h):
    hd = sha256_hex(data)
    c = concept(path, body, id=path, title="Payments owner", owner=owner, evidence=[hd],
                sources=[{"uri": "chat://thread/1", "fetched_at": "t", "evidence": hd}], **h)
    return c, [(data, "chat://thread/1", "2024-01-01T00:00:00Z")]


def test_full_cycle_new_dup_conflict(gate, bundle):
    c, ev = note()
    did = gate.propose(c, ev, proposer="agent")
    assert gate.propose(c, ev) == did  # idempotent
    assert bundle.concept_paths() == []  # staging only
    r = gate.curate([did])[0]
    assert r.status == PENDING and r.reconciliation["decision"] == "New"
    gate.approve(did, "rev")
    assert gate.record(did).status == APPROVED
    sha = gate.merge("admit", when="2024-02-01T00:00:00Z", who=WHO)
    assert sha and len(bundle.git.parents(sha)) == 2  # --no-ff merge
    assert gate.record(did).status == MERGED
    assert bundle.evidence.verify(ev and sha256_hex(ev[0][0]))
    assert gate.merge() is None

    # provenance-only re-harvest: Duplicate, dropped
    c2, ev2 = note(data=b"slack thread v2")
    r2 = gate.curate([gate.propose(c2, ev2)])[0]
    assert r2.status == DROPPED and "duplicate" in r2.note

    # conflict: human resolves; supersedes recorded
    c3, ev3 = note(owner="blair")
    d3 = gate.propose(c3, ev3)
    r3 = gate.curate([d3])[0]
    assert r3.reconciliation["decision"] == "Conflict"
    gate.approve(d3, "rev", resolution={"owner": "casey"})
    gate.merge(when="2024-03-01T00:00:00Z", who=WHO)
    now = bundle.read("concepts/p.md")
    assert now.header["owner"] == "casey"
    assert now.header["supersedes"][0]["fields"] == ["owner"]
    assert now.header["supersedes"][0]["snapshot"] == sha[:7]

    srv = Server(bundle, gate.sidecar)
    prov = srv.provenance("concepts/p.md")
    assert prov.ok and [h.hop for h in prov.hops] == [
        "concept->commit", "commit->evidence", "evidence->source"]
    gate.clear()
    assert gate.records() == []


def test_gate_rejects_bad_drafts(gate, bundle):
    c, _ = note()
    r = gate.curate([gate.propose(c, [])])[0]  # evidence never fetched
    assert r.status == REJECTED and "does not resolve" in r.problems[0]
    c2, ev2 = note(path="concepts/q.md", data=b"x")
    did = gate.propose(c2, ev2)
    bundle.staging_evidence.path(sha256_hex(b"x")).write_bytes(b"tampered")
    r2 = gate.curate([did])[0]
    assert r2.status == REJECTED and "fails digest" in r2.problems[0]
    (gate.dir / "bad.json").write_text(
        '{"id": "bad", "path": "concepts/b.md", "proposer": "a"}')
    (gate.dir / "bad.md").write_text("not okf")
    assert gate.curate(["bad"])[0].status == REJECTED
    with pytest.raises(GateError):
        gate.record("missing")
    with pytest.raises(GateError):
        gate.approve("bad", "rev")
    with pytest.raises(GateError):
        gate.reject("bad", "rev")
    assert gate.curate([r.id])[0].status == REJECTED  # non-staged passthrough


def test_held_draft_review(gate, bundle):
    c, ev = note()
    admit(gate, c, "2024-02-01T00:00:00Z", ev)
    renamed, ev2 = note(path="concepts/p-renamed.md")
    did = gate.propose(renamed, ev2)
    r = gate.curate([did])[0]
    assert r.status == PENDING and r.reconciliation["held"]
    if r.reconciliation["decision"] == "Duplicate":
        with pytest.raises(GateError):
            gate.approve(did, "rev")
    gate.approve(did, "rev", as_new=True)
    gate.merge(when="2024-03-01T00:00:00Z", who=WHO)
    assert "concepts/p-renamed.md" in bundle.concept_paths()
    other, ev3 = note(path="concepts/p-other.md")
    d3 = gate.propose(other, ev3)
    gate.curate([d3])
    assert gate.reject(d3, "rev", "sibling").status == REJECTED


def test_enforce_actor_extension(tmp_path):
    from contextctl.bundle import Bundle, BundleConfig
    from contextctl.gate import Gate

    b = Bundle.init(tmp_path / "b", BundleConfig(extra={"enforce_actor": True}), who=WHO,
                    install_hook=False)
    g = Gate(b)
    c, ev = note(actor="human:ceo")  # draft claims human authorship
    did = g.propose(c, ev, proposer="agent:bot")
    g.curate([did])
    g.approve(did, "rev")
    g.merge(who=WHO)
    assert b.read("concepts/p.md").header["actor"] == "agent:bot"


def test_revert_finding_d(gate, bundle):
    """Reverting one merge conflicts with a later merge of the same file;
    reverting it together with the later merges (newest first) works."""
    m = []
    for i, owner in enumerate(["alex", "blair", "casey", "dana"]):
        c, ev = note(owner=owner)
        m.append(admit(gate, c, f"2024-0{i + 1}-01T00:00:00Z", ev))
    with pytest.raises(RevertConflict):
        gate.revert([m[1]], who=WHO)
    gate.revert([m[3], m[2], m[1]], who=WHO)
    assert bundle.read("concepts/p.md").header["owner"] == "alex"
    with pytest.raises(GateError):
        gate.revert(["deadbeef"], who=WHO)


def test_tamper_finding_b(gate, bundle):
    """A coordinated header-and-blob rewrite passes the digest check; it shows
    only as an uncommitted Git change (paper, finding (b))."""
    c, ev = note()
    admit(gate, c, "2024-02-01T00:00:00Z", ev)
    new_bytes = b"forged thread"
    h = sha256_hex(new_bytes)
    bundle.evidence.root.joinpath(h).write_bytes(new_bytes)
    cur = bundle.read("concepts/p.md")
    cur.header["evidence"] = [h]
    cur.header["sources"] = [{"uri": "chat://thread/1", "evidence": h}]
    (bundle.root / "concepts/p.md").write_text(cur.dumps())
    assert bundle.evidence.verify(h)  # digest check passes ...
    status = bundle.git.run("status", "--porcelain")
    assert "concepts/p.md" in status  # ... detection rests on Git


def test_provenance_failures(gate, bundle):
    srv = Server(bundle, gate.sidecar)
    assert not srv.provenance("concepts/none.md").ok
    admit(gate, concept("concepts/n.md", "no evidence here\n", title="n"),
          "2024-02-01T00:00:00Z")
    p = srv.provenance("concepts/n.md")
    assert not p.ok and p.hops[-1].detail == "no evidence cited"
