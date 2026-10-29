"""One preregistered reversal, frozen models and common risk framework comparison.

@author cylovelife
"""

import argparse
import hashlib
import json
import logging
import time
from pathlib import Path

import numpy as np
import pandas as pd

from quant_agent.competitions.hull.mapping import risk_report
from quant_agent.competitions.hull.mapping_study import (
    mature_rows,
    read_history,
    sha256,
    validate_forecasts,
)
from quant_agent.competitions.hull.published_portfolio import RiskSettings
from quant_agent.competitions.hull.reversal import new_policy
from quant_agent.competitions.hull.study import write_new_json

LOGGER = logging.getLogger(__name__)
RISK = {
    "signal_window": 252,
    "portfolio_window": 252,
    "min_observations": 126,
    "update_every": 21,
    "target_annual_volatility": 0.15,
    "daily_volatility_floor": 1e-6,
    "warmup_position": 1.0,
    "portfolio_return_basis": "pre_overlay",
}
SIGNAL = {
    "sum_window": 5,
    "vol_window": 21,
    "threshold": 1.0,
    "strength": 0.25,
    "max_active_position": 0.5,
    "daily_volatility_floor": 1e-6,
}
COMPARATORS = ["reversal", "ridge", "tree", "market"]


def validate_plan(plan: dict) -> None:
    if plan["schema_version"] != 1 or plan["risk"] != RISK:
        raise ValueError("fixed risk settings required")
    if any(plan["signal"][k] != v for k, v in SIGNAL.items()):
        raise ValueError("only the fixed transparent signal is allowed")
    if plan["comparators"] != COMPARATORS or plan["arms"] != ["base", "public_risk_overlay"]:
        raise ValueError("fixed comparison matrix required")
    if (
        plan["expected_scored_cases"] != 16
        or plan["new_signal_configurations"] != 1
        or plan["max_predictor_fits"] != 0
        or plan["bootstrap_scale_min_observations"] != 63
        or plan["rowwise_api_replay_rows"] != 16
    ):
        raise ValueError("fixed execution budget required")
    if (
        plan["reserved_evaluation_allowed"]
        or plan["reserved_scoring_authorized"]
        or plan["promotion_allowed"]
        or plan["selection_on_reserved_allowed"]
    ):
        raise ValueError("reserved evaluation/promotion forbidden")
    windows = plan["development_windows"]
    if (
        len(windows) != 2
        or any(a >= b or b > plan["reserved_window"][0] for a, b in windows)
        or windows[0][1] > windows[1][0]
    ):
        raise ValueError("invalid development/reserved windows")
    expected = {"frozen.json", "results.json"}
    expected.update(
        f"development-{i}-{m}-{kind}.csv"
        for i in (1, 2)
        for m in ("ridge", "tree")
        for kind in ("predictions", "oof")
    )
    if set(plan["reference_files"]) != expected:
        raise ValueError("exact frozen reference set required")


def causal_oof_weights(oof: pd.DataFrame, min_rows: int = 63) -> pd.Series:
    """Earlier visible forecasts only; never use full-OOF RMS for earlier decisions."""
    p = oof.forecast.to_numpy()
    values = []
    for i, value in enumerate(p):
        rms = float(np.sqrt(np.mean(p[:i] ** 2))) if i >= min_rows else 0.0
        values.append(
            float(np.float32(np.clip(1 + 0.25 * value / rms, 0, 2))) if rms >= 1e-8 else 1.0
        )
    return pd.Series(values, index=oof.date_id.to_numpy())


def evaluate_window(
    history: pd.DataFrame,
    predictions: dict[str, pd.DataFrame],
    oofs: dict[str, pd.DataFrame],
    start: int,
    end: int,
    first_boot: int,
) -> tuple[dict, dict[str, pd.DataFrame]]:
    valid = mature_rows(history, start, end)
    evaluation_ids = set(valid.date_id)
    boot_ids = set(oofs["ridge"].date_id)
    if not np.array_equal(oofs["ridge"].date_id, oofs["tree"].date_id):
        raise ValueError("OOF risk-bootstrap cohorts differ")
    selected = boot_ids | evaluation_ids
    exposures = {}
    for name in ("ridge", "tree"):
        past = causal_oof_weights(oofs[name])
        current = pd.Series(
            predictions[name].allocation.to_numpy().astype(np.float32).astype(float),
            index=predictions[name].date_id.to_numpy(),
        )
        exposures[name] = pd.concat([past, current])
    settings = RiskSettings(**RISK)
    policies = {name: new_policy(history, first_boot, settings) for name in COMPARATORS}
    trace = {name: [] for name in COMPARATORS}
    lag_r, lag_f = history.forward_returns.shift(1), history.risk_free_rate.shift(1)
    for idx in history.index[(history.date_id >= first_boot) & (history.date_id < end)]:
        date_id = int(history.loc[idx, "date_id"])
        decide = date_id in selected
        for name, policy in policies.items():
            override = (
                float(exposures[name].loc[date_id])
                if decide and name in exposures
                else (1.0 if name == "market" else None)
            )
            decision = policy.step(
                date_id,
                float(lag_r.loc[idx]),
                float(lag_f.loc[idx]),
                base_override=override,
                decide=decide,
            )
            if date_id in evaluation_ids:
                risk = decision.risk_decision
                trace[name].append(
                    {
                        "date_id": date_id,
                        "base_position": decision.base_position,
                        "position": decision.position,
                        "standardized_move": decision.standardized_move,
                        "pre_overlay_position": decision.pre_overlay_position,
                        "candidate_weight": risk.signal_weights[0] if risk.signal_weights else None,
                        "market_stabilizer_weight": risk.signal_weights[1]
                        if risk.signal_weights
                        else None,
                        "risk_updated": risk.risk_updated,
                        "risk_label_end_max": risk.risk_label_end_max,
                        "estimated_portfolio_annual_volatility": risk.annualized_portfolio_volatility,
                    }
                )
    frames = {name: pd.DataFrame(rows) for name, rows in trace.items()}
    record = {
        "window": [start, end],
        "evaluation_rows": len(valid),
        "evaluation_id_range": [int(valid.date_id.min()), int(valid.date_id.max())],
        "bootstrap_decisions": len(boot_ids),
        "comparators": {},
    }
    for name, frame in frames.items():
        if not np.array_equal(frame.date_id, valid.date_id):
            raise ValueError("evaluation IDs changed")
        record["comparators"][name] = {
            "base": risk_report(valid, frame.base_position.to_numpy()),
            "public_risk_overlay": risk_report(valid, frame.position.to_numpy()),
            "risk_updates_in_evaluation": int(frame.risk_updated.sum()),
            "base_nonmarket_fraction": float(np.mean(frame.base_position != 1)),
        }
    return record, frames


def run_study(raw: Path, manifest: Path, protocol: Path, reference: Path, output: Path) -> dict:
    output = output.resolve()
    output.mkdir(parents=True, exist_ok=False)
    started = time.perf_counter()
    try:
        plan = json.loads(protocol.read_text())
        validate_plan(plan)
        inputs = output / "inputs"
        inputs.mkdir()
        for name, expected in plan["reference_files"].items():
            contents = (reference / name).read_bytes()
            if hashlib.sha256(contents).hexdigest() != expected:
                raise ValueError(f"frozen reference hash mismatch: {name}")
            with (inputs / name).open("xb") as h:
                h.write(contents)
        parent = json.loads((inputs / "frozen.json").read_text())["protocol"]
        previous = json.loads((inputs / "results.json").read_text())
        for key in ("expected_train_sha256", "development_windows", "reserved_window"):
            if parent[key] != plan[key]:
                raise ValueError(f"parent identity changed: {key}")
        if previous["reserved_predictions"] or previous["reserved_evaluations"]:
            raise ValueError("reference consumed reserved data")
        history = read_history(raw, manifest, plan)
        source = {p.name: sha256(p) for p in sorted(Path(__file__).parent.glob("*.py"))}
        if source["_official_metric_v3.py"] != plan["official_scorer_sha256"]:
            raise ValueError("official metric changed")
        write_new_json(
            output / "frozen.json",
            {
                "protocol": plan,
                "protocol_sha256": sha256(protocol),
                "source_sha256": source,
                "parsed_rows": len(history),
                "parsed_max_id": int(history.date_id.max()),
            },
        )
        records = []
        for number, (start, end) in enumerate(plan["development_windows"], 1):
            preds = {}
            oofs = {}
            for name in ("ridge", "tree"):
                preds[name] = pd.read_csv(
                    inputs / f"development-{number}-{name}-predictions.csv",
                    float_precision="round_trip",
                )
                oofs[name] = pd.read_csv(
                    inputs / f"development-{number}-{name}-oof.csv", float_precision="round_trip"
                )
                validate_forecasts(
                    history,
                    preds[name],
                    oofs[name],
                    start,
                    end,
                    parent["calibration"],
                    previous["windows"][number - 1][name],
                )
            first = start - parent["calibration"]["blocks"] * parent["calibration"]["block_rows"]
            record, frames = evaluate_window(history, preds, oofs, start, end, first)
            for name in ("ridge", "tree"):
                score = record["comparators"][name]["base"]["official_score"]
                if not np.isclose(
                    score,
                    previous["windows"][number - 1][name]["portfolio"]["official_score"],
                    rtol=0,
                    atol=1e-10,
                ):
                    raise ValueError("frozen baseline score reproduction failed")
            for name, frame in frames.items():
                frame.to_csv(
                    output / f"development-{number}-{name}-allocations.csv", index=False, mode="x"
                )
            records.append(record)
        gains = []
        for record in records:
            comps = record["comparators"]
            primary = comps["reversal"]["public_risk_overlay"]
            market = comps["market"]["base"]
            gains.append(
                primary["official_score"]
                > max(comps[m]["public_risk_overlay"]["official_score"] for m in ("ridge", "tree"))
                and primary["gross_total_return"] >= market["gross_total_return"]
                and primary["max_drawdown"] >= market["max_drawdown"]
            )
        if any(sha256(reference / n) != h for n, h in plan["reference_files"].items()):
            raise ValueError("frozen source changed")
        result = {
            "task": "transparent_reversal_common_overlay_control",
            "windows": records,
            "scored_cases": sum(len(x["comparators"]) * 2 for x in records),
            "new_signal_configurations": 1,
            "predictor_fits": 0,
            "frozen_model_allocations_reproduced": True,
            "reserved_predictions": 0,
            "reserved_evaluations": 0,
            "consistent_development_gain": bool(all(gains)),
            "original_fourth_strategy_reproduced": False,
            "selected_for_promotion": False,
            "online_submitted": False,
            "online_rank": None,
            "elapsed_seconds": time.perf_counter() - started,
        }
        if result["scored_cases"] != plan["expected_scored_cases"]:
            raise ValueError("scoring budget mismatch")
        write_new_json(output / "results.json", result)
        return result
    except Exception as exc:
        write_new_json(
            output / "failure.json",
            {"type": type(exc).__name__, "message": str(exc), "predictor_fits": 0},
        )
        raise


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    for name in ("raw", "manifest", "protocol", "reference", "output"):
        parser.add_argument("--" + name, type=Path, required=True)
    args = parser.parse_args()
    result = run_study(args.raw, args.manifest, args.protocol, args.reference, args.output)
    LOGGER.info("cases=%d fits=0 reserved_scores=0", result["scored_cases"])


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO)
    main()
