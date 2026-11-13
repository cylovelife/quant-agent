"""Scientific rejection must preserve unopened synthetic test evidence.

@author cylovelife
"""

import hashlib
import json
from pathlib import Path

import pytest
from research_loop_fixture import make_fixture

from quant_agent.application.research.controlled_loop import ResearchLoop, register


def prepare(tmp_path, *, losing=False, min_sessions=None):
    path = make_fixture(tmp_path / "fixture")
    manifest = json.loads(path.read_text())
    if min_sessions is not None:
        manifest["scientific_policy"]["min_sessions"] = min_sessions
    if losing:
        artifact = Path(manifest["validation"]["path"])
        data = json.loads(artifact.read_text())
        for row in data["rows"]:
            index = data["sessions"].index(row["date"])
            price = 10 if losing == "flat" else 10 - index / 10
            row["open"] = row["close"] = f"{price:.2f}"
        artifact.write_text(json.dumps(data))
        manifest["validation"]["sha256"] = hashlib.sha256(artifact.read_bytes()).hexdigest()
    path.write_text(json.dumps(manifest))
    # Even registration/validation must work when the test file is unavailable.
    Path(manifest["test"]["path"]).unlink()
    registry = tmp_path / "registry.db"
    eid = register(registry, path)["experiment_id"]
    return ResearchLoop(registry), eid, path


@pytest.mark.parametrize("market", ["falling", "flat"])
def test_all_losing_candidates_stop_without_lock_or_test_consumption(tmp_path, monkeypatch, market):
    loop, eid, _ = prepare(tmp_path, losing=market)
    result = loop.validate(eid)
    assert all(item["outcome"] == "succeeded" for item in result["validation"])
    decision = result["scientific_decision"]
    assert decision["verdict"] == "rejected"
    assert all(item["verdict"] == "rejected" for item in decision["candidates"])
    assert result["research_state"] == "stopped_no_eligible_candidate"

    monkeypatch.setattr(ResearchLoop, "_evaluate", lambda *args: pytest.fail("worker rerun"))
    restarted = ResearchLoop(loop.registry.path)
    for result in (restarted.validate(eid), restarted.lock(eid), restarted.test(eid)):
        assert result["research_state"] == "stopped_no_eligible_candidate"
        assert result["scientific_decision"] == decision
        assert result["promotion_allowed"] is False
    state = restarted.status(eid)
    assert state["scientific_decision"] == decision
    assert state["locked_candidate"] is None
    assert len(state["attempts"]) == 3
    assert all(item["phase"] == "validation" for item in state["attempts"])


def test_insufficient_coverage_is_not_rejection_and_does_not_consume_test(tmp_path):
    loop, eid, path = prepare(tmp_path, min_sessions=5)
    # Editing the input manifest after registration cannot relax the frozen gate.
    changed = json.loads(path.read_text())
    changed["scientific_policy"]["min_sessions"] = 2
    path.write_text(json.dumps(changed))
    result = loop.validate(eid)
    assert result["scientific_decision"]["verdict"] == "insufficient_evidence"
    assert all(
        item["verdict"] == "insufficient_evidence"
        for item in result["scientific_decision"]["candidates"]
    )
    assert loop.lock(eid)["research_state"] == "stopped_no_eligible_candidate"
    assert loop.test(eid)["research_state"] == "stopped_no_eligible_candidate"
    assert len(loop.status(eid)["attempts"]) == 3


def test_eligible_candidate_locks_without_releasing_test_early(tmp_path):
    loop, eid, _ = prepare(tmp_path)
    result = loop.validate(eid)
    decision = result["scientific_decision"]
    assert decision["verdict"] == "eligible_for_locked_test"
    eligible = [x for x in decision["candidates"] if x["verdict"] == "eligible_for_locked_test"]
    assert [x["scheme"] for x in eligible] == ["quality_first"]
    assert decision["statistical_significance"] == "not_assessed"
    with pytest.raises(ValueError, match="lock"):
        loop.test(eid)
    assert loop.lock(eid)["scheme"] == "quality_first"
    assert len(loop.status(eid)["attempts"]) == 3


def test_cli_reports_scientific_stop_as_normal_completion(tmp_path, capsys):
    from quant_agent.entrypoints.research_loop import main

    loop, eid, _ = prepare(tmp_path, losing=True)
    request_path = tmp_path / "request.json"
    for operation in ("validate", "lock", "test", "status"):
        request_path.write_text(
            json.dumps(
                {
                    "operation": operation,
                    "registry": str(loop.registry.path),
                    "experiment_id": eid,
                }
            )
        )
        assert main(str(request_path)) == 0
        captured = capsys.readouterr()
        assert not captured.err
        result = json.loads(captured.out)
        assert result["research_state"] == "stopped_no_eligible_candidate"
    assert len(loop.registry.inspect(eid)["attempts"]) == 3


@pytest.mark.parametrize(
    "assets,baseline,sessions,fills,verdict,reason",
    [
        ("9995.00", "9990.00", 4, 1, "rejected", "nonpositive_net_profit"),
        ("10000.00", "9990.00", 4, 1, "rejected", "nonpositive_net_profit"),
        ("10010.00", "10020.00", 4, 1, "rejected", "below_default_net_assets"),
        ("10020.00", "10020.00", 4, 1, "eligible_for_locked_test", None),
        ("10100.00", "10000.00", 3, 1, "insufficient_evidence", "insufficient_sessions"),
        ("10000.00", "10000.00", 4, 0, "insufficient_evidence", "insufficient_filled_orders"),
    ],
)
def test_economic_and_coverage_boundaries(assets, baseline, sessions, fills, verdict, reason):
    from decimal import Decimal

    from quant_agent.domain.research.decision import (
        CandidateEvidence,
        ScientificPolicy,
        judge_candidate,
    )

    result = judge_candidate(
        CandidateEvidence("candidate", "scheme", sessions, fills, Decimal(assets)),
        initial_cash=Decimal("10000.00"),
        default_net_assets=Decimal(baseline),
        policy=ScientificPolicy("synthetic_economic_v1", 4, 1),
    )
    assert result["verdict"] == verdict
    assert result["reasons"] == ([reason] if reason else [])


@pytest.mark.parametrize("mutation", ["missing", "unknown", "version", "bool", "zero"])
def test_invalid_scientific_policy_fails_before_opening_artifacts(tmp_path, mutation):
    path = make_fixture(tmp_path / "fixture")
    manifest = json.loads(path.read_text())
    if mutation == "missing":
        del manifest["scientific_policy"]
    elif mutation == "unknown":
        manifest["scientific_policy"]["allow_promotion"] = True
    elif mutation == "version":
        manifest["scientific_policy"]["version"] = "unknown"
    elif mutation == "bool":
        manifest["scientific_policy"]["min_sessions"] = True
    else:
        manifest["scientific_policy"]["min_filled_orders"] = 0
    path.write_text(json.dumps(manifest))
    for phase in ("validation", "test"):
        Path(manifest[phase]["path"]).unlink()
    with pytest.raises(ValueError, match="scientific|manifest"):
        register(tmp_path / "registry.db", path)
    assert not (tmp_path / "registry.db").exists()
