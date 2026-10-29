"""Fixed-signal risk factorial and self-financing transaction-cost scenarios.

@author cylovelife
"""

import argparse
import hashlib
import importlib.metadata
import json
import logging
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
from quant_agent.competitions.hull.published_portfolio import PublishedRiskOverlay, RiskSettings
from quant_agent.competitions.hull.reversal import new_policy
from quant_agent.competitions.hull.reversal_study import (
    COMPARATORS,
    RISK,
    SIGNAL,
    causal_oof_weights,
)
from quant_agent.competitions.hull.study import write_new_json

ARMS = ["base", "stabilizer_only", "target_only", "stabilizer_and_target"]
COST_BPS = [0, 1, 2, 5, 10, 20]


def validate_plan(plan: dict) -> None:
    if plan["schema_version"] != 1 or plan["risk"] != RISK:
        raise ValueError("fixed risk settings required")
    if any(plan["signal"][key] != value for key, value in SIGNAL.items()):
        raise ValueError("fixed signal required")
    if plan["arms"] != ARMS or plan["comparators"] != COMPARATORS:
        raise ValueError("fixed factorial required")
    if (
        plan["cost_bps"] != COST_BPS
        or plan["expected_scored_cases"] != 32
        or plan["expected_cost_cases"] != 192
        or plan["new_signal_configurations"] != 0
        or plan["max_predictor_fits"] != 0
        or plan["bootstrap_scale_min_observations"] != 63
    ):
        raise ValueError("fixed execution budget required")
    if any(
        plan[k]
        for k in (
            "reserved_evaluation_allowed",
            "reserved_scoring_authorized",
            "promotion_allowed",
            "selection_on_reserved_allowed",
            "model_predictions_allowed",
            "selection_allowed",
            "platform_submission_allowed",
        )
    ):
        raise ValueError("development-only frozen diagnosis required")
    windows = plan["development_windows"]
    a, b = plan["reserved_window"]
    if (
        a >= b
        or len(windows) != 2
        or any(s >= e or e > a for s, e in windows)
        or windows[0][1] > windows[1][0]
    ):
        raise ValueError("invalid development/reserved boundaries")
    files = {"frozen.json", "results.json"} | {
        f"development-{i}-{m}-{kind}.csv"
        for i in (1, 2)
        for m in ("ridge", "tree")
        for kind in ("predictions", "oof")
    }
    prior = {"frozen.json", "results.json"} | {
        f"development-{i}-{m}-allocations.csv" for i in (1, 2) for m in COMPARATORS
    }
    if set(plan["reference_files"]) != files or set(plan["prior_reversal_files"]) != prior:
        raise ValueError("exact frozen references required")
    if plan["cost_accounting"] != {
        "initial_risky_weight": 0.0,
        "entry_cost_included": True,
        "terminal_liquidation": False,
        "target_basis": "post_fee_wealth",
        "turnover_basis": "one_way_actual_traded_notional_over_pretrade_wealth_with_return_drift",
        "funding": "official risk_free_rate for cash and borrowing; no extra spread",
        "risk_feedback": "gross frozen causal risk states; costs never reoptimize allocations",
    }:
        raise ValueError("fixed cost accounting required")


def cost_path(
    market: np.ndarray, cash: np.ndarray, weights: np.ndarray, cost_bps: float
) -> pd.DataFrame:
    """Rebalance to post-fee targets from cash; final holdings marked, not liquidated.

    With pretrade risky fraction d, target w and fee rate c, solve
    fee=c*abs(w*(1-fee)-d). Position drift includes cash/borrowing returns.
    """
    market, cash, weights = (np.asarray(x, dtype=float) for x in (market, cash, weights))
    if (
        market.ndim != 1
        or not len(market)
        or cash.shape != market.shape
        or weights.shape != market.shape
        or not np.isfinite(np.r_[market, cash, weights, cost_bps]).all()
        or (market <= -1).any()
        or (cash <= -1).any()
        or (weights < 0).any()
        or (weights > 2).any()
        or not 0 <= cost_bps < 5000
    ):
        raise ValueError("finite aligned returns, 0..2 weights and valid nonnegative fee required")
    rate = cost_bps / 10000
    drift = 0.0
    records = []
    for r, f, w in zip(market, cash, weights, strict=True):
        fee = rate * abs(w - drift) / (1 + rate * w if w >= drift else 1 - rate * w)
        growth = 1 + f + w * (r - f)
        if fee >= 1 or growth <= 0:
            raise ValueError("insolvent cost scenario")
        traded = abs(w * (1 - fee) - drift)
        records.append(
            {
                "target_weight": w,
                "pretrade_risky_weight": drift,
                "traded_fraction": traded,
                "fee_fraction": fee,
                "gross_return": growth - 1,
                "net_return": (1 - fee) * growth - 1,
            }
        )
        drift = w * (1 + r) / growth
    return pd.DataFrame(records)


def cost_report(solution: pd.DataFrame, weights: np.ndarray, cost_bps: float) -> dict:
    path = cost_path(
        solution.forward_returns.to_numpy(), solution.risk_free_rate.to_numpy(), weights, cost_bps
    )
    returns = path.net_return.to_numpy()
    excess = returns - solution.risk_free_rate.to_numpy()
    excess_vol = float(np.std(excess, ddof=1))
    equity = np.r_[1.0, np.cumprod(1 + returns)]
    vol = float(np.std(returns, ddof=1))
    return {
        "cost_bps": cost_bps,
        "net_total_return": float(equity[-1] - 1),
        "annualized_volatility": vol * float(np.sqrt(252)),
        "max_drawdown": float(np.min(equity / np.maximum.accumulate(equity) - 1)),
        "arithmetic_excess_sharpe": float(np.mean(excess) / excess_vol * np.sqrt(252))
        if excess_vol > 0
        else None,
        "mean_daily_traded_fraction": float(path.traded_fraction.mean()),
        "summed_traded_fractions": float(path.traded_fraction.sum()),
        "mean_daily_fee_fraction": float(path.fee_fraction.mean()),
        "entry_fee_fraction": float(path.fee_fraction.iloc[0]),
        "official_score_computed_on_net_returns": False,
    }


def evaluate_window(
    history: pd.DataFrame,
    predictions: dict[str, pd.DataFrame],
    oofs: dict[str, pd.DataFrame],
    start: int,
    end: int,
    first_boot: int,
    costs: list[int],
) -> tuple[dict, dict[str, pd.DataFrame]]:
    valid = mature_rows(history, start, end)
    eval_ids, boot_ids = set(valid.date_id), set(oofs["ridge"].date_id)
    if not np.array_equal(oofs["ridge"].date_id, oofs["tree"].date_id):
        raise ValueError("OOF cohorts differ")
    selected = boot_ids | eval_ids
    exposures = {
        m: pd.concat(
            [
                causal_oof_weights(oofs[m]),
                pd.Series(
                    predictions[m].allocation.to_numpy().astype(np.float32).astype(float),
                    index=predictions[m].date_id.to_numpy(),
                ),
            ]
        )
        for m in ("ridge", "tree")
    }
    settings = RiskSettings(**RISK)
    policies = {m: new_policy(history, first_boot, settings) for m in COMPARATORS}
    targets = {m: PublishedRiskOverlay(settings, 1) for m in COMPARATORS}
    trace = {m: [] for m in COMPARATORS}
    lag_r, lag_f = history.forward_returns.shift(1), history.risk_free_rate.shift(1)
    for idx in history.index[(history.date_id >= first_boot) & (history.date_id < end)]:
        date_id = int(history.loc[idx, "date_id"])
        decide = date_id in selected
        for m, policy in policies.items():
            override = (
                float(exposures[m].loc[date_id])
                if decide and m in exposures
                else (1.0 if m == "market" else None)
            )
            decision = policy.step(
                date_id,
                float(lag_r.loc[idx]),
                float(lag_f.loc[idx]),
                base_override=override,
                decide=decide,
            )
            if not decide:
                continue
            # Same mature candidate PnL dates, but no stabilizer in the risk estimate.
            one = pd.DataFrame(
                list(policy.risk_history),
                columns=[
                    "date_id",
                    "label_end_id",
                    "signal_return_0",
                    "signal_return_1",
                    "portfolio_return",
                ],
            ).drop(columns="signal_return_1")
            one = one.astype({"date_id": "int64", "label_end_id": "int64"})
            one["portfolio_return"] = one.signal_return_0
            target = targets[m].allocate(date_id, np.array([decision.base_position]), one)
            if date_id in eval_ids:
                trace[m].append(
                    {
                        "date_id": date_id,
                        "base": decision.base_position,
                        "stabilizer_only": float(np.float32(decision.pre_overlay_position)),
                        "target_only": target.position,
                        "stabilizer_and_target": decision.position,
                        "candidate_mix_weight": decision.risk_decision.signal_weights[0],
                        "risk_label_end_max": decision.risk_decision.risk_label_end_max,
                        "target_label_end_max": target.risk_label_end_max,
                    }
                )
    frames = {m: pd.DataFrame(rows) for m, rows in trace.items()}
    record = {
        "window": [start, end],
        "rows": len(valid),
        "bootstrap_decisions": len(boot_ids),
        "comparators": {},
    }
    for m, frame in frames.items():
        if not np.array_equal(frame.date_id, valid.date_id):
            raise ValueError("evaluation IDs changed")
        arms = {}
        for arm in ARMS:
            w = frame[arm].to_numpy()
            arms[arm] = {
                "gross": risk_report(valid, w),
                "costs": {str(c): cost_report(valid, w, c) for c in costs},
            }
            for c in costs:
                arms[arm]["costs"][str(c)]["net_return_difference_vs_market_same_cost"] = (
                    arms[arm]["costs"][str(c)]["net_total_return"]
                    - cost_report(valid, np.ones(len(valid)), c)["net_total_return"]
                )
        # 2x2 contrasts are descriptive and metric-specific, not causal inference.
        contrasts = {}
        for metric in ("official_score", "gross_total_return", "annualized_volatility"):
            b, s, t, st = (arms[a]["gross"][metric] for a in ARMS)
            contrasts[metric] = {
                "stabilizer_without_target": s - b,
                "target_without_stabilizer": t - b,
                "stabilizer_with_target": st - t,
                "target_with_stabilizer": st - s,
                "interaction": st - s - t + b,
            }
        record["comparators"][m] = {"arms": arms, "factorial_contrasts": contrasts}
    return record, frames


def copy_verified(source: Path, destination: Path, files: dict[str, str]) -> None:
    destination.mkdir()
    for name, expected in files.items():
        contents = (source / name).read_bytes()
        if hashlib.sha256(contents).hexdigest() != expected:
            raise ValueError(f"frozen reference hash mismatch: {name}")
        with (destination / name).open("xb") as handle:
            handle.write(contents)


def run_study(
    raw: Path, manifest: Path, protocol: Path, reference: Path, prior: Path, output: Path
) -> dict:
    output.mkdir(parents=True, exist_ok=False)
    try:
        plan = json.loads(protocol.read_text())
        validate_plan(plan)
        copy_verified(reference, output / "inputs", plan["reference_files"])
        copy_verified(prior, output / "prior", plan["prior_reversal_files"])
        parent = json.loads((output / "inputs/frozen.json").read_text())["protocol"]
        old = json.loads((output / "inputs/results.json").read_text())
        old_reversal = json.loads((output / "prior/results.json").read_text())
        prior_plan = json.loads((output / "prior/frozen.json").read_text())["protocol"]
        for k in ("expected_train_sha256", "development_windows", "reserved_window"):
            if plan[k] != parent[k] or plan[k] != prior_plan[k]:
                raise ValueError(f"reference identity changed: {k}")
        if any(
            x[k]
            for x in (old, old_reversal)
            for k in ("reserved_predictions", "reserved_evaluations")
        ):
            raise ValueError("reference consumed reserved rows")
        history = read_history(raw, manifest, plan)
        sources = {p.name: sha256(p) for p in sorted(Path(__file__).parent.glob("*.py"))}
        if sources["_official_metric_v3.py"] != plan["official_scorer_sha256"]:
            raise ValueError("official scorer changed")
        write_new_json(
            output / "frozen.json",
            {
                "protocol": plan,
                "protocol_sha256": sha256(protocol),
                "source_sha256": sources,
                "packages": {
                    p: importlib.metadata.version(p) for p in ("numpy", "pandas", "scikit-learn")
                },
                "parsed_rows": len(history),
                "parsed_max_id": int(history.date_id.max()),
            },
        )
        windows = []
        for i, (start, end) in enumerate(plan["development_windows"], 1):
            preds, oofs = {}, {}
            for m in ("ridge", "tree"):
                preds[m] = pd.read_csv(
                    output / f"inputs/development-{i}-{m}-predictions.csv",
                    float_precision="round_trip",
                )
                oofs[m] = pd.read_csv(
                    output / f"inputs/development-{i}-{m}-oof.csv", float_precision="round_trip"
                )
                validate_forecasts(
                    history,
                    preds[m],
                    oofs[m],
                    start,
                    end,
                    parent["calibration"],
                    old["windows"][i - 1][m],
                )
            first = start - parent["calibration"]["blocks"] * parent["calibration"]["block_rows"]
            record, frames = evaluate_window(history, preds, oofs, start, end, first, COST_BPS)
            for m, frame in frames.items():
                before = pd.read_csv(
                    output / f"prior/development-{i}-{m}-allocations.csv",
                    float_precision="round_trip",
                )
                if (
                    not np.array_equal(frame.date_id, before.date_id)
                    or not np.array_equal(frame.base, before.base_position)
                    or not np.array_equal(frame.stabilizer_and_target, before.position)
                ):
                    raise ValueError("prior daily behavior not reproduced")
                frame.to_csv(output / f"development-{i}-{m}-allocations.csv", index=False, mode="x")
            windows.append(record)
        for folder, hashes in (
            (reference, plan["reference_files"]),
            (prior, plan["prior_reversal_files"]),
        ):
            if any(sha256(folder / n) != h for n, h in hashes.items()):
                raise ValueError("source reference changed")
        result = {
            "task": plan["task"],
            "windows": windows,
            "scored_cases": 32,
            "cost_cases": 192,
            "new_signal_configurations": 0,
            "predictor_fits": 0,
            "model_predictions": 0,
            "reserved_predictions": 0,
            "reserved_evaluations": 0,
            "prior_daily_allocations_reproduced": True,
            "selected_for_promotion": False,
            "online_submitted": False,
            "online_rank": None,
        }
        write_new_json(output / "results.json", result)
        return result
    except Exception as exc:
        write_new_json(output / "failure.json", {"type": type(exc).__name__, "message": str(exc)})
        raise


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    for name in ("raw", "manifest", "protocol", "reference", "prior", "output"):
        parser.add_argument("--" + name, type=Path, required=True)
    args = parser.parse_args()
    result = run_study(
        args.raw, args.manifest, args.protocol, args.reference, args.prior, args.output
    )
    logging.getLogger(__name__).info(
        "gross=%d cost=%d reserved=0", result["scored_cases"], result["cost_cases"]
    )


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO)
    main()
