import pytest

from contextctl.evidence import EvidenceStore, sha256_hex


def test_put_idempotent_and_meta(tmp_path):
    s = EvidenceStore(tmp_path / "ev")
    r1 = s.put(b"hello", "u1", "t1")
    r2 = s.put(b"hello", "u2", "t2")
    assert r1.created and not r2.created and r1.digest == r2.digest == sha256_hex(b"hello")
    assert s.meta(r1.digest) == {"sha256": r1.digest, "bytes": 5, "first_uri": "u1",
                                 "fetched_at": "t1"}
    assert r2.first_uri == "u1"
    r3 = s.put(b"hello!", "u1", "t3")  # edited source -> new object, old kept
    assert r3.digest != r1.digest and s.digests() == sorted([r1.digest, r3.digest])
    assert s.verify_all() == {r1.digest: True, r3.digest: True}


def test_tamper_detected(tmp_path):
    s = EvidenceStore(tmp_path / "ev")
    h = s.put(b"data", "u", "t").digest
    s.path(h).write_bytes(b"tampered")
    assert not s.verify(h)
    assert s.meta("b" * 64) == {}
    assert not s.exists("nope")
    with pytest.raises(ValueError):
        s.path("nope")
    assert EvidenceStore(tmp_path / "missing").digests() == []
