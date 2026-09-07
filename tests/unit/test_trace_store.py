"""
Unit tests for utils/trace_store — Observability v2 Phase 2 retention.

The retention contract: keep only the newest N trace.zip files run-wide,
save_trace writes into the failure folder and prunes, and every path is
defensive (a tracing/pruning failure never fails a test).
"""
from __future__ import annotations

import os

import utils.trace_store as ts


class _FakeTracing:
    def __init__(self): self.stopped_with = "UNSET"
    def stop(self, path=None):
        self.stopped_with = path
        if path:
            open(path, "wb").write(b"PK\x03\x04 fake-trace")   # minimal zip-ish bytes


class _FakeContext:
    def __init__(self): self.tracing = _FakeTracing()


def _make_trace(root, name, mtime):
    d = root / name
    d.mkdir(parents=True)
    p = d / ts.TRACE_NAME
    p.write_bytes(b"x")
    os.utime(p, (mtime, mtime))
    return p


def test_prune_keeps_newest_n(tmp_path):
    for i in range(6):
        _make_trace(tmp_path, f"fail_{i}", mtime=1000 + i)   # newest = fail_5
    deleted = ts.prune_traces(tmp_path, keep=3)
    assert len(deleted) == 3
    remaining = {p.parent.name for p in tmp_path.glob(f"*/{ts.TRACE_NAME}")}
    assert remaining == {"fail_3", "fail_4", "fail_5"}       # oldest 0/1/2 pruned


def test_prune_noop_under_cap(tmp_path):
    _make_trace(tmp_path, "fail_0", 1000)
    assert ts.prune_traces(tmp_path, keep=20) == []
    assert (tmp_path / "fail_0" / ts.TRACE_NAME).is_file()


def test_save_trace_writes_and_prunes(tmp_path, monkeypatch):
    monkeypatch.setattr(ts, "FAILURES_ROOT", tmp_path)
    monkeypatch.setenv("FLOWGUARD_TRACE_MAX", "2")
    # two pre-existing older traces
    _make_trace(tmp_path, "old_a", 1000)
    _make_trace(tmp_path, "old_b", 1001)
    ctx = _FakeContext()
    folder = tmp_path / "new_fail"
    p = ts.save_trace(ctx, folder)
    assert p is not None and p.is_file()
    assert ctx.tracing.stopped_with == str(p)                # saved WITH a path
    # cap=2, newest is the just-saved one → 2 kept total, one old pruned
    kept = {x.parent.name for x in tmp_path.glob(f"*/{ts.TRACE_NAME}")}
    assert "new_fail" in kept and len(kept) == 2


def test_save_trace_defensive_on_stop_error(tmp_path):
    class Boom:
        class tracing:
            @staticmethod
            def stop(path=None):
                if path:
                    raise RuntimeError("disk full")
                # discard path (no-arg) succeeds
    p = ts.save_trace(Boom(), tmp_path / "f")
    assert p is None                                          # failure surfaced as None, not raise


def test_max_traces_env_override(monkeypatch):
    monkeypatch.setenv("FLOWGUARD_TRACE_MAX", "7")
    assert ts._max_traces() == 7
    monkeypatch.setenv("FLOWGUARD_TRACE_MAX", "garbage")
    assert ts._max_traces() == ts.DEFAULT_MAX_TRACES
