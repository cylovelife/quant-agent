"""Controlled research must fail closed without opening real data or accounts.

@author cylovelife
"""

import json
from decimal import Decimal
from pathlib import Path

import pytest
from research_loop_fixture import make_fixture

from quant_agent.application.research.controlled_loop import ResearchLoop, register


def setup_loop(tmp_path):
    manifest = make_fixture(tmp_path / "fixture")
    registry = tmp_path / "research.db"
    result = register(registry, manifest)
    return ResearchLoop(registry), result["experiment_id"], manifest


def test_complete_loop_locks_validation_winner_and_replays_test(tmp_path, monkeypatch):
    loop, eid, manifest = setup_loop(tmp_path)
    test_path = Path(json.loads(manifest.read_text())["test"]["path"])
    sealed = test_path.with_suffix(".sealed")
    test_path.rename(sealed)
    with pytest.raises(ValueError, match="lock"):
        loop.test(eid)
    results = loop.validate(eid)
    assert len(results["validation"]) == 3
    selected = loop.lock(eid)
    assert selected["scheme"] == "quality_first"
    sealed.rename(test_path)
    outcome = loop.test(eid)
    detail = outcome["details"]
    assert detail["promotion_allowed"] is False
    assert detail["independent_new_samples"] == 0
    account = detail["selected"]
    assert account["total_return"] is None
    assert Decimal(account["fees"]) > 0
    # 100 shares at 10.11 after adverse tick rounding, fees 5.01, last close 10.35.
    assert account["net_assets"] == "10018.99"
    assert account["fees"] == "5.01"
    assert detail["default_baseline"]["net_assets"] == "9978.99"
    assert account["orders"][0]["order"]["signal_date"] == "2026-08-10"
    assert account["orders"][0]["execution_date"] == "2026-08-11"
    last = account["curve"][-1]
    assert Decimal(last["net_assets"]) == Decimal(last["cash"]) + Decimal(last["market_value"])
    monkeypatch.setattr(loop, "_evaluate", lambda *a: pytest.fail("replayed worker"))
    replay = loop.test(eid)
    assert replay["replayed"] and replay["details"] == detail
    assert len(loop.registry.inspect(eid)["attempts"]) == 4


def test_registration_never_opens_test_and_validation_rejects_drift(tmp_path):
    manifest = make_fixture(tmp_path / "fixture")
    data = json.loads(manifest.read_text())
    Path(data["test"]["path"]).unlink()
    registry = tmp_path / "research.db"
    eid = register(registry, manifest)["experiment_id"]
    loop = ResearchLoop(registry)
    Path(data["validation"]["path"]).write_text("{}")
    with pytest.raises(ValueError, match="digest"):
        loop.validate(eid)
    assert loop.registry.inspect(eid)["attempts"][0]["outcome"] == "failed"


def test_failed_test_is_consumed_and_cannot_be_retried(tmp_path):
    loop, eid, manifest = setup_loop(tmp_path)
    loop.validate(eid)
    loop.lock(eid)
    test_path = Path(json.loads(manifest.read_text())["test"]["path"])
    original = test_path.read_bytes()
    test_path.write_text("{}")
    with pytest.raises(ValueError, match="digest"):
        loop.test(eid)
    test_path.write_bytes(original)
    replay = loop.test(eid)
    assert replay["replayed"] and replay["outcome"] == "failed"
    assert len([a for a in loop.registry.inspect(eid)["attempts"] if a["phase"] == "test"]) == 1


def test_manual_validation_cannot_lock(tmp_path):
    loop, eid, _ = setup_loop(tmp_path)
    for item in loop.registry.inspect(eid)["candidates"]:
        cid = item["candidate_id"]
        loop.registry.begin(eid, cid, loop.attempt_id(eid, cid, "validation"), "validation")
        loop.registry.finish(
            loop.attempt_id(eid, cid, "validation"), "succeeded", {"net_assets": "999999"}
        )
    with pytest.raises(ValueError, match="managed"):
        loop.lock(eid)


def test_timeout_records_failure_and_prevents_lock(tmp_path, monkeypatch):
    import subprocess

    loop, eid, _ = setup_loop(tmp_path)

    def timeout(*args, **kwargs):
        raise subprocess.TimeoutExpired("fixture-worker", 0.01)

    monkeypatch.setattr("quant_agent.application.research.controlled_loop.subprocess.run", timeout)
    with pytest.raises(subprocess.TimeoutExpired):
        loop.validate(eid)
    assert loop.registry.inspect(eid)["attempts"][0]["outcome"] == "failed"
    with pytest.raises(ValueError):
        loop.lock(eid)


def test_future_factors_do_not_change_earlier_orders(tmp_path):
    from copy import deepcopy

    from quant_agent.application.research.loop_account import evaluate_account
    from quant_agent.application.research.model_selection import LONG_SCHEMES

    manifest = json.loads(make_fixture(tmp_path / "fixture").read_text())
    data = json.loads(Path(manifest["validation"]["path"]).read_text())
    candidate = {"scheme": "quality_first", "weights": LONG_SCHEMES["quality_first"]}
    full = evaluate_account(data, candidate, manifest["policy"], manifest["initial_cash"])
    changed = deepcopy(data)
    for row in changed["rows"]:
        if row["date"] >= data["sessions"][2]:
            row["factors"] = {key: 100 - value for key, value in row["factors"].items()}
    mutated = evaluate_account(changed, candidate, manifest["policy"], manifest["initial_cash"])
    assert full["curve"][:3] == mutated["curve"][:3]
    assert full["orders"][0] == mutated["orders"][0]
    assert len(mutated["orders"]) == 3


def test_unfilled_exit_does_not_open_second_position(tmp_path):
    from quant_agent.application.research.loop_account import evaluate_account
    from quant_agent.application.research.model_selection import LONG_SCHEMES

    manifest = json.loads(make_fixture(tmp_path / "fixture").read_text())
    data = json.loads(Path(manifest["validation"]["path"]).read_text())
    for row in data["rows"]:
        if row["date"] == data["sessions"][1]:
            row["factors"] = {key: 100 - value for key, value in row["factors"].items()}
        if row["date"] == data["sessions"][2] and row["code"] == "600001":
            row["facts"]["trading_status"] = "suspended"
    result = evaluate_account(
        data,
        {"scheme": "quality_first", "weights": LONG_SCHEMES["quality_first"]},
        manifest["policy"],
        manifest["initial_cash"],
    )
    assert all(len(point["positions"]) <= 1 for point in result["curve"])
    assert any(order["reason"] == "suspended" for order in result["orders"])


@pytest.mark.parametrize(
    "change,match",
    [
        (lambda data: data.update(kind="historical"), "synthetic"),
        (lambda data: data["rows"][0].update(adjust="qfq"), "raw"),
        (lambda data: data["rows"].pop(), "complete"),
        (lambda data: data["sessions"].__setitem__(0, "2026-08-10"), "ordered|boundary"),
    ],
)
def test_invalid_phase_contract_rejected(tmp_path, change, match):
    from quant_agent.application.research.loop_data import check_phase

    manifest = json.loads(make_fixture(tmp_path / "fixture").read_text())
    data = json.loads(Path(manifest["validation"]["path"]).read_text())
    change(data)
    with pytest.raises(ValueError, match=match):
        check_phase(data, "validation", manifest["split"])


def test_cli_dispatch_and_failure_exit_code(tmp_path, capsys, monkeypatch):
    from quant_agent.entrypoints.cli import main

    manifest = make_fixture(tmp_path / "fixture")
    request = tmp_path / "request.json"
    request.write_text(
        json.dumps({"operation": "register", "registry": "research.db", "manifest": str(manifest)})
    )
    assert main(["--research-loop", str(request)]) == 0
    eid = json.loads(capsys.readouterr().out)["experiment_id"]
    request.write_text(
        json.dumps({"operation": "test", "registry": "research.db", "experiment_id": eid})
    )
    assert main(["--research-loop", str(request)]) == 2
    assert "lock" in json.loads(capsys.readouterr().err)["error"]


def test_completed_test_over_total_attempt_budget_cannot_report_success(tmp_path, monkeypatch):
    from datetime import datetime, timedelta, timezone

    loop, eid, _ = setup_loop(tmp_path)
    loop.validate(eid)
    loop.lock(eid)
    now = datetime.now(timezone.utc)
    timestamps = iter([now.isoformat(), (now + timedelta(seconds=31)).isoformat()])
    monkeypatch.setattr(
        "quant_agent.application.research.experiments._now", lambda: next(timestamps)
    )
    with pytest.raises(ValueError, match="budget"):
        loop.test(eid)
    with pytest.raises(ValueError, match="budget"):
        loop.test(eid)
    assert len(loop.registry.inspect(eid)["attempts"]) == 4
