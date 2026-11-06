"""Cross-entry and process-boundary regressions for controlled research gates.

@author cylovelife
"""

import json
import os
import subprocess
import sys
from copy import deepcopy
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest
from research_loop_fixture import make_fixture

from quant_agent.application.research.controlled_loop import ResearchLoop, register
from quant_agent.application.research.experiments import (
    _CONTROLLED_REGISTRATION,
    ExperimentRegistry,
)


@pytest.mark.parametrize(
    "mutation",
    ["split", "test_as_validation", "budget", "weights", "policy", "quantity", "features"],
)
def test_controlled_context_rejects_inconsistent_identity(tmp_path, mutation):
    manifest = make_fixture(tmp_path / "fixture")
    result = register(tmp_path / "valid.db", manifest)
    identity = result["identity"]
    spec = json.loads(
        json.dumps(
            {
                key: identity[key]
                for key in (
                    "universe",
                    "parameters",
                    "features",
                    "label",
                    "cost",
                    "split",
                    "seed",
                    "budget",
                )
            }
        )
    )
    if mutation == "split":
        spec["split"]["test_start"] = "2026-08-04"
    elif mutation == "test_as_validation":
        protocol = spec["parameters"]["protocol"]
        protocol["validation"] = deepcopy(protocol["test"])
        protocol["test"]["path"] += ".other"
        protocol["split"] = {
            "validation_start": "2026-08-10",
            "test_start": "2026-08-17",
            "test_end": "2026-08-20",
        }
    elif mutation == "budget":
        spec["parameters"]["protocol"]["budget"]["max_attempt_seconds"] = 300
    elif mutation == "weights":
        spec["parameters"]["definitions"][0]["weights"] = {"quality": 1}
    elif mutation == "policy":
        spec["parameters"]["protocol"]["policy"]["profiles"]["cn_mainboard"]["commission_min"] = "0"
    elif mutation == "quantity":
        spec["parameters"]["quantity"] = 1000000
    else:
        spec["features"] = ["future_return"]
    registry = ExperimentRegistry.create(tmp_path / "generic.db")
    # Fault injection into the internal registrar; public generic registration
    # is independently forbidden from claiming this reserved protocol.
    eid = registry.register(
        identity["snapshot_path"], spec, _registration_token=_CONTROLLED_REGISTRATION
    )["experiment_id"]
    for candidate in spec["parameters"]["definitions"]:
        registry.add_candidate(eid, candidate)
    with pytest.raises(ValueError, match="frozen|protocol|candidate"):
        ResearchLoop(registry.path).validate(eid)
    assert registry.inspect(eid)["attempts"] == []


def test_generic_and_legacy_records_cannot_claim_controlled_provenance(tmp_path, monkeypatch):
    import quant_agent.application.research.experiments as experiments

    result = register(tmp_path / "valid.db", make_fixture(tmp_path / "fixture"))
    identity = result["identity"]
    spec = {
        key: identity[key]
        for key in (
            "universe",
            "parameters",
            "features",
            "label",
            "cost",
            "split",
            "seed",
            "budget",
        )
    }
    registry = ExperimentRegistry.create(tmp_path / "generic.db")
    with pytest.raises(ValueError, match="dedicated"):
        registry.register(tmp_path / "must-not-open-this-snapshot", spec)
    # Reproduce a generic record written by the old, permissive implementation.
    with monkeypatch.context() as context:
        context.setattr(experiments, "CONTROLLED_FORMAT", "different-reserved-format")
        eid = registry.register(identity["snapshot_path"], spec)["experiment_id"]
    with pytest.raises(ValueError, match="controlled registration"):
        ResearchLoop(registry.path).validate(eid)
    with pytest.raises(ValueError, match="provenance"):
        registry.register(
            identity["snapshot_path"], spec, _registration_token=_CONTROLLED_REGISTRATION
        )


def test_last_validation_over_budget_rejected_on_first_call_and_replay(tmp_path, monkeypatch):
    manifest_path = make_fixture(tmp_path / "fixture")
    manifest = json.loads(manifest_path.read_text())
    manifest["candidates"] = ["default"]
    manifest["budget"].update(max_candidates=1, max_attempts=2)
    manifest_path.write_text(json.dumps(manifest))
    eid = register(tmp_path / "registry.db", manifest_path)["experiment_id"]
    loop = ResearchLoop(tmp_path / "registry.db")
    now = datetime.now(timezone.utc)
    times = iter([now.isoformat(), (now + timedelta(seconds=31)).isoformat()])
    monkeypatch.setattr("quant_agent.application.research.experiments._now", lambda: next(times))
    with pytest.raises(ValueError, match="budget"):
        loop.validate(eid)
    with pytest.raises(ValueError, match="budget"):
        loop.validate(eid)
    assert len(loop.registry.inspect(eid)["attempts"]) == 1


def test_blocked_test_input_is_timed_out_and_consumed(tmp_path):
    manifest_path = make_fixture(tmp_path / "fixture")
    manifest = json.loads(manifest_path.read_text())
    manifest["budget"]["max_attempt_seconds"] = 2
    manifest_path.write_text(json.dumps(manifest))
    registry_path = tmp_path / "registry.db"
    eid = register(registry_path, manifest_path)["experiment_id"]
    loop = ResearchLoop(registry_path)
    loop.validate(eid)
    loop.lock(eid)
    phase_path = Path(manifest["test"]["path"])
    phase_path.unlink()
    os.mkfifo(phase_path)
    request = tmp_path / "test-request.json"
    request.write_text(
        json.dumps({"operation": "test", "registry": str(registry_path), "experiment_id": eid})
    )
    root = Path(__file__).resolve().parents[1]
    completed = subprocess.run(
        [sys.executable, str(root / "run.py"), "--research-loop", str(request)],
        text=True,
        capture_output=True,
        timeout=6,
        env=dict(os.environ, PYTHONPATH=str(root / "src")),
    )
    assert completed.returncode == 2
    assert json.loads(completed.stderr)["error_type"] == "TimeoutExpired"
    attempts = loop.registry.inspect(eid)["attempts"]
    assert attempts[-1]["phase"] == "test" and attempts[-1]["outcome"] == "failed"
    assert loop.test(eid)["replayed"]
