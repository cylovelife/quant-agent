"""Research experiment evidence, candidate limits and complete-day weight updates.

@author cylovelife
"""

import sys
from pathlib import Path

import pandas as pd

import quant_agent.application.investing.evaluation as evaluate  # noqa: E402


def test_weight_window_preserves_every_row_of_the_boundary_day():
    data = pd.DataFrame(
        [
            {"date": f"2026-01-{d:02d}", "code": str(i), "ret": i}
            for d in range(1, 11)
            for i in range(25)
        ]
    )
    window = evaluate._recent_complete_dates(data, 200)
    assert len(window) == 200 and window.date.nunique() == 8
    uneven = pd.concat(
        [
            data,
            pd.DataFrame([{"date": "2026-01-11", "code": str(i), "ret": i} for i in range(30)]),
        ],
        ignore_index=True,
    )
    window = evaluate._recent_complete_dates(uneven.sample(frac=1, random_state=4), 200)
    counts = window.groupby("date").size().to_dict()
    original = uneven.groupby("date").size().to_dict()
    assert len(window) >= 200 and all(counts[d] == original[d] for d in counts)


def fixture_snapshot(tmp_path):
    from quant_agent.infrastructure.persistence.snapshots import publish_snapshot

    dates = pd.bdate_range("2025-01-01", periods=15)
    frame = pd.DataFrame(
        {
            "date": dates,
            "open": [10.0] * 15,
            "high": [11.0] * 15,
            "low": [9.0] * 15,
            "close": [10.5] * 15,
            "volume": [100.0] * 15,
            "amount": [1000.0] * 15,
            "src": ["fixture"] * 15,
        }
    )
    return publish_snapshot({("cn", "000001", "qfq"): frame}, tmp_path / "snapshots")


def protocol():
    return {
        "universe": [["cn", "000001", "qfq"]],
        "parameters": {"weights": {"boll": 0.5, "rsi": 0.5}},
        "features": ["boll", "rsi"],
        "label": {"name": "return", "horizon": 5, "version": "v2"},
        "cost": {"model": "explicit", "commission": "0.0003"},
        "split": {
            "validation_start": "2024-01-01",
            "test_start": "2025-01-01",
            "test_end": "2025-12-31",
        },
        "seed": 42,
        "budget": {"max_candidates": 2, "max_attempts": 4, "max_attempt_seconds": 60},
    }


def test_frozen_identity_candidates_and_single_use_test(tmp_path):
    import pytest

    from quant_agent.application.research.experiments import ExperimentRegistry

    snapshot = fixture_snapshot(tmp_path)
    registry = ExperimentRegistry.create(tmp_path / "experiments.db")
    first = registry.register(snapshot, protocol())
    eid = first["experiment_id"]
    again = registry.register(snapshot, protocol())
    assert again["experiment_id"] == eid and again["replayed"]
    c1 = registry.add_candidate(eid, {"weights": {"boll": 0.4, "rsi": 0.6}})["candidate_id"]
    assert registry.add_candidate(eid, {"weights": {"boll": 0.4, "rsi": 0.6}})["replayed"]
    c2 = registry.add_candidate(eid, {"weights": {"boll": 0.6, "rsi": 0.4}})["candidate_id"]
    with pytest.raises(ValueError, match="budget"):
        registry.add_candidate(eid, {"weights": {"boll": 0.7, "rsi": 0.3}})
    with pytest.raises(ValueError, match="previously locked"):
        registry.begin(eid, c1, "test-too-soon", "test")
    registry.begin(eid, c1, "val-1", "validation")
    assert registry.begin(eid, c1, "val-1", "validation")["replayed"]
    registry.finish("val-1", "failed", {"error": "fixture"})
    registry.begin(eid, c1, "val-2", "validation")
    registry.finish("val-2", "succeeded", {"ic": 0.1})
    registry.lock_candidate(eid, c1)
    with pytest.raises(ValueError, match="closed"):
        registry.begin(eid, c2, "late", "validation")
    with pytest.raises(ValueError, match="previously locked"):
        registry.begin(eid, c2, "wrong-test", "test")
    registry.begin(eid, c1, "test-1", "test")
    registry.finish("test-1", "succeeded", {"ic": 0.2})
    with pytest.raises(ValueError, match="consumed"):
        registry.begin(eid, c1, "test-2", "test")
    record = registry.inspect(eid)
    assert len(record["attempts"]) == 3 and record["attempts"][0]["outcome"] == "failed"
    assert record["locked_candidate"] == c1 and record["independent_new_samples"] == 0
    assert record["promotion_allowed"] is False
    changed = protocol()
    changed["seed"] = 43
    with pytest.raises(ValueError, match="overlaps"):
        registry.register(snapshot, changed)


def test_failed_or_interrupted_attempts_are_not_erased(tmp_path):
    import pytest

    from quant_agent.application.research.experiments import ExperimentRegistry

    registry = ExperimentRegistry.create(tmp_path / "registry.db")
    eid = registry.register(fixture_snapshot(tmp_path), protocol())["experiment_id"]
    cid = registry.add_candidate(eid, {"model": "ridge"})["candidate_id"]
    registry.begin(eid, cid, "unfinished", "validation")
    with pytest.raises(ValueError, match="unfinished"):
        registry.begin(eid, cid, "other", "validation")
    with pytest.raises(ValueError, match="successful"):
        registry.lock_candidate(eid, cid)
    registry.finish("unfinished", "failed", {"error": "interrupted"})
    assert registry.finish("unfinished", "failed", {"error": "interrupted"})["replayed"]
    with pytest.raises(ValueError, match="conflicts"):
        registry.finish("unfinished", "succeeded", {})
    assert registry.inspect(eid)["attempts"][0]["outcome"] == "failed"


def test_damaged_data_and_runtime_drift_fail_closed(tmp_path, monkeypatch):
    import pytest

    import quant_agent.application.research.experiments as module

    registry = module.ExperimentRegistry.create(tmp_path / "registry.db")
    snapshot = fixture_snapshot(tmp_path)
    eid = registry.register(snapshot, protocol())["experiment_id"]
    monkeypatch.setattr(module, "code_signature", lambda: "changed")
    with pytest.raises(ValueError, match="code or environment"):
        registry.add_candidate(eid, {"model": "ridge"})
    monkeypatch.undo()
    candidate = registry.add_candidate(eid, {"model": "ridge"})["candidate_id"]
    (snapshot / "bars.json").write_text("corrupt")
    with pytest.raises(ValueError, match="integrity"):
        registry.begin(eid, candidate, "run", "validation")
    assert registry.inspect(eid)["attempts"] == []


def test_registry_refuses_existing_files_and_wrong_universe(tmp_path):
    import pytest

    from quant_agent.application.research.experiments import ExperimentRegistry

    target = tmp_path / "existing.db"
    target.write_bytes(b"user file")
    with pytest.raises(FileExistsError):
        ExperimentRegistry.create(target)
    assert target.read_bytes() == b"user file"
    registry = ExperimentRegistry.create(tmp_path / "registry.db")
    bad = protocol()
    bad["universe"] = [["cn", "000002", "qfq"]]
    with pytest.raises(ValueError, match="universe"):
        registry.register(fixture_snapshot(tmp_path), bad)


def test_explicit_cli_registers_and_preserves_failures(tmp_path):
    import json
    import subprocess

    root = Path(__file__).resolve().parents[1]
    snapshot = fixture_snapshot(tmp_path)
    request = tmp_path / "request.json"

    def call(payload, ok=True):
        request.write_text(json.dumps(payload), encoding="utf-8")
        result = subprocess.run(
            [
                sys.executable,
                str(root / "run.py"),
                "--research-experiment",
                str(request),
            ],
            capture_output=True,
            text=True,
            cwd=root,
            check=False,
        )
        assert result.returncode == (0 if ok else 2), result.stderr
        return json.loads(result.stdout if ok else result.stderr)

    base = {"registry_path": "registry.db"}
    eid = call(
        {
            **base,
            "operation": "register",
            "snapshot_path": str(snapshot),
            "spec": protocol(),
        }
    )["experiment_id"]
    cid = call(
        {
            **base,
            "operation": "candidate",
            "experiment_id": eid,
            "definition": {"model": "ridge"},
        }
    )["candidate_id"]
    call(
        {
            **base,
            "operation": "begin",
            "experiment_id": eid,
            "candidate_id": cid,
            "attempt_id": "failed-run",
            "phase": "validation",
        }
    )
    call(
        {
            **base,
            "operation": "finish",
            "attempt_id": "failed-run",
            "outcome": "failed",
            "details": {"error": "fixture"},
        }
    )
    result = call({**base, "operation": "status", "experiment_id": eid})
    assert result["attempts"][0]["outcome"] == "failed"
    assert result["promotion_allowed"] is False
    assert (
        "successful"
        in call(
            {**base, "operation": "lock", "experiment_id": eid, "candidate_id": cid},
            False,
        )["error"]
    )


def test_raw_etf_proxy_and_cached_source_remain_distinct(tmp_path, monkeypatch):
    import json

    import quant_agent.infrastructure.market_data.fundamentals as fundamentals

    class Response:
        def json(self):
            return {
                "data": {
                    "sh510300": {
                        "day": [["2026-10-26", "4.155", "4.179", "4.187", "4.130", "1000"]]
                    }
                }
            }

    calls, saved = [], []
    monkeypatch.setattr(fundamentals, "KLINE_DIR", tmp_path)
    monkeypatch.setattr(fundamentals.store, "enabled", lambda: False)
    monkeypatch.setattr(
        fundamentals.store,
        "upsert_kline",
        lambda market, code, adjust, df, src: saved.append((adjust, src)),
    )
    monkeypatch.setattr(
        fundamentals.fetcher,
        "_get",
        lambda url, params, **kw: calls.append((url, params)) or Response(),
    )
    data = fundamentals.fetch_kline_raw("510300", "etf", limit=1, ttl_days=1)
    assert data.iloc[0]["close"] == 4.179
    assert calls[0][0] == fundamentals.fetcher._CN_KLINE_FALLBACK
    assert calls[0][1]["param"].endswith(",1,")
    assert saved == [("raw", "tencent_proxy_ifzq_raw")]
    assert (
        json.loads((tmp_path / "etf_510300_1.json").read_text())["source"]
        == "tencent_proxy_ifzq_raw"
    )
    calls.clear()
    fundamentals.fetch_kline_raw("510300", "etf", limit=1, ttl_days=1)
    assert calls == [] and saved[-1] == ("raw", "tencent_proxy_ifzq_raw")


def test_managed_attempt_runs_once_and_records_exceptions(tmp_path):
    import pytest

    from quant_agent.application.research.experiments import ExperimentRegistry

    registry = ExperimentRegistry.create(tmp_path / "managed.db")
    eid = registry.register(fixture_snapshot(tmp_path), protocol())["experiment_id"]
    cid = registry.add_candidate(eid, {"model": "ridge"})["candidate_id"]
    calls = []

    def succeeds():
        calls.append("run")
        return {"selection": "ridge"}

    first = registry.run_attempt(eid, cid, "managed-1", "validation", succeeds)
    again = registry.run_attempt(eid, cid, "managed-1", "validation", succeeds)
    assert calls == ["run"]
    assert first["details"] == again["details"] == {"selection": "ridge"}
    assert again["replayed"] is True

    def fails():
        raise ValueError("invalid actual input")

    with pytest.raises(ValueError, match="invalid actual input"):
        registry.run_attempt(eid, cid, "managed-failed", "validation", fails)
    record = registry.inspect(eid)
    assert [row["outcome"] for row in record["attempts"]] == ["succeeded", "failed"]
    assert "invalid actual input" in record["attempts"][1]["result_json"]


def test_managed_runner_does_not_replay_a_manual_result(tmp_path):
    import pytest

    from quant_agent.application.research.experiments import ExperimentRegistry

    registry = ExperimentRegistry.create(tmp_path / "managed.db")
    eid = registry.register(fixture_snapshot(tmp_path), protocol())["experiment_id"]
    cid = registry.add_candidate(eid, {"model": "ridge"})["candidate_id"]
    registry.begin(eid, cid, "manual", "validation")
    registry.finish("manual", "succeeded", {"selection": "ridge"})
    with pytest.raises(ValueError, match="not a managed"):
        registry.run_attempt(eid, cid, "manual", "validation", lambda: {})


def test_managed_attempt_rechecks_frozen_runtime_before_recording_success(tmp_path, monkeypatch):
    import pytest

    import quant_agent.application.research.experiments as module

    registry = module.ExperimentRegistry.create(tmp_path / "managed.db")
    eid = registry.register(fixture_snapshot(tmp_path), protocol())["experiment_id"]
    cid = registry.add_candidate(eid, {"model": "ridge"})["candidate_id"]

    def changes_code():
        monkeypatch.setattr(module, "code_signature", lambda: "different")
        return {"selection": "ridge"}

    with pytest.raises(ValueError, match="code or environment"):
        registry.run_attempt(eid, cid, "drift", "validation", changes_code)
    assert registry.inspect(eid)["attempts"][0]["outcome"] == "failed"
