# -*- coding: utf-8 -*-
"""模型选型层：把「该用哪套参数/哪套权重」从拍脑袋变成可审查的决策。

两条线各有一套选型对象：

- 短线轨：离场规则组合 `(持有期, 止损, 止盈)`。现有 `exit_model.parameter_scan`
  是**单变量**扫描（固定其余两项，逐项对比），回答不了「组合起来哪个最好」，
  也不知道「在样本内选出的最优，到了样本外还算不算数」。
- 长期轨：六维权重方案。`value_track.historical_replay` 已产出无前视的
  五维分数面板，任何权重方案都能在这个面板上直接算 IC。

日期分组并依据实际标签结束日清除跨界：训练段形成候选，验证段选择，
固定的已结束测试段只比较选中候选与基准。结果仅供研究观察，不自动调参。
测试时间边界由调用方明确提供；跨试验封存与搜索记录属于后续任务。
"""

import json
import os
from datetime import datetime

import numpy as np
import pandas as pd

import quant_agent.application.investing.value_screening as vs
import quant_agent.application.research.value_tracking as vt
import quant_agent.domain.research.exit_model as em
from quant_agent.domain.trading.labels import LabelSpec
from quant_agent.runtime.paths import WORKSPACE_ROOT

ROOT = str(WORKSPACE_ROOT)
STATE = os.path.join(ROOT, "state")
FACTORS_CORE = ["v_quality", "v_growth", "v_cashflow", "v_balance", "v_valuation"]

# 长期轨的候选权重方案
LONG_SCHEMES = {
    "default": dict(vs.DEFAULT_WEIGHTS),
    "quality_first": {
        "quality": 0.40,
        "growth": 0.10,
        "cashflow": 0.15,
        "balance": 0.05,
        "valuation": 0.20,
        "macro": 0.10,
    },
    "valuation_first": {
        "quality": 0.20,
        "growth": 0.08,
        "cashflow": 0.12,
        "balance": 0.05,
        "valuation": 0.45,
        "macro": 0.10,
    },
    "cashflow_first": {
        "quality": 0.24,
        "growth": 0.10,
        "cashflow": 0.34,
        "balance": 0.08,
        "valuation": 0.14,
        "macro": 0.10,
    },
    "equal": {k: 1 / 6 for k in vs.DEFAULT_WEIGHTS},
}
CORE_ONLY = ["quality", "growth", "cashflow", "balance", "valuation"]

# 尾部容忍度：建议参数的 5% 分位比当前配置差超过这个幅度，就不允许切换。
# 低吸策略的核心风险在左尾（接飞刀），「靠加深尾部换日均收益」不是改进，是换了风险偏好。
TAIL_TOL = 0.02


# ---------------------------------------------------------------------------
# 短线：离场规则组合的 walk-forward 选型
# ---------------------------------------------------------------------------
def short_grid(cfg: dict) -> list:
    ms = (cfg.get("model_select", {}) or {}).get("shorts", {}) or {}
    hs = ms.get("hold_days", [3, 5, 8])
    ss = ms.get("stop_loss", [-0.03, -0.05, -0.08, None])
    ts = ms.get("take_profit", [0.05, 0.08, None])
    return [
        {"hold_days": int(h), "stop_loss": s, "take_profit": t} for h in hs for s in ss for t in ts
    ]


def _rule_stats(sigs, horizon, stop, take) -> dict:
    """一组离场规则在信号集上的表现。

    除原始平均收益外，额外给出**日均收益**与日均 Sharpe，作为跨持有期比较的决策量。
    不同持有期的平均收益不可直接比：持有 8 日的组合天然比持有 5 日的多吃 3 天市场
    漂移，看起来更优，实则只是敞口更大。

    日均的定义是 **总收益 / 总持有日数**（= mean(ret)/mean(days)），而不是
    mean(ret_i / days_i)——后者是典型的「平均比率」错误：被 1 日就被止损打掉的
    样本主导（一记 −5% 的 1 日止损会贡献 −500%/日），把有止损的规则算得极度难看。
    前瞻收益就是前瞻收益，风险调整另由 sharpe 承担。
    """
    if not sigs:
        return {"n": 0}
    rets, days = em._simulate_rule(sigs, horizon, stop, take, return_days=True)
    st = em._summ(rets)
    avg_days = float(np.mean(days))
    st["avg_days"] = round(avg_days, 4)
    # avg_per_day 是**选型决策量**，不做四舍五入：
    # 日收益量级约 0.003~0.005，round(...,5) 会把它压成 0.0038 这种只剩两位有效数字的
    # 值，而候选方案之间的日均差异常在 1e-4~1e-5 量级——四舍五入会把真实差异抹平，
    # 让选型退化成靠舍入噪声挑参数。显示层（report）自己控制小数位。
    st["avg_per_day"] = float(np.mean(rets)) / avg_days if avg_days > 1e-9 else None
    # 风险调整跨持有期比较：Sharpe_h ≈ sqrt(h)·Sharpe_日 → 折算回日均口径
    sh = st.get("sharpe")
    st["sharpe_per_day"] = (
        float(sh) / (avg_days**0.5) if sh is not None and avg_days > 1e-9 else None
    )
    return st


def select_short(
    cfg: dict, params: dict, kline_map: dict, splits=(0.55, 0.68, 0.80)
) -> dict | None:
    """Select on earlier dates and report a single disjoint, purged test block."""
    from quant_agent.domain.research.validation import (
        configured_boundaries,
        split_three_way,
        truncate_klines,
    )

    if tuple(splits) != (0.55, 0.68, 0.80):
        raise ValueError(
            "legacy overlapping split fractions are unsupported; provide date boundaries"
        )
    boundaries = configured_boundaries(cfg)
    if boundaries is None:
        return None

    grid = short_grid(cfg)
    if not grid:
        return None
    current = LabelSpec.from_params(params).to_dict()
    limited = truncate_klines(kline_map, boundaries["test_end"])
    max_horizon = max(current["hold_days"], max(g["hold_days"] for g in grid))
    sigs = em._collect_signals(limited, params, max_horizon, with_date=True)
    if len(sigs) < 200 or any(len(item) != 4 for item in sigs):
        return None
    frame = pd.DataFrame(
        {"date": [s[0] for s in sigs], "end": [s[3] for s in sigs], "signal": sigs}
    )
    try:
        parts = split_three_way(frame, date_col="date", end_col="end", **boundaries)
    except ValueError:
        return None
    training = parts.train["signal"].tolist()
    validation = parts.validation["signal"].tolist()
    test = parts.test["signal"].tolist()
    if len(training) < 50 or len(validation) < 20 or len(test) < 20:
        return None
    ranked = []
    for candidate in grid:
        train = _rule_stats(
            training, candidate["hold_days"], candidate["stop_loss"], candidate["take_profit"]
        )
        validate = _rule_stats(
            validation, candidate["hold_days"], candidate["stop_loss"], candidate["take_profit"]
        )
        if train.get("n", 0) >= 50 and validate.get("n", 0) >= 20:
            ranked.append({**candidate, "train": train, "validation": validate})
    if not ranked:
        return None
    ranked.sort(
        key=lambda x: (x["validation"]["avg_per_day"], x["validation"].get("sharpe_per_day") or -9),
        reverse=True,
    )
    winner = ranked[0]
    chosen = {k: winner[k] for k in ("hold_days", "stop_loss", "take_profit")}
    picked = _rule_stats(test, chosen["hold_days"], chosen["stop_loss"], chosen["take_profit"])
    base = _rule_stats(test, current["hold_days"], current["stop_loss"], current["take_profit"])
    train_base = _rule_stats(
        training, current["hold_days"], current["stop_loss"], current["take_profit"]
    )
    gain_is = winner["train"]["avg_per_day"] - train_base["avg_per_day"]
    gain_test = picked["avg_per_day"] - base["avg_per_day"]
    fold = {
        "split": "sealed_test",
        "n_is": len(training),
        "n_validation": len(validation),
        "n_oos": len(test),
        "picked": chosen,
        "is_picked": winner["train"],
        "is_current": train_base,
        "oos_picked": picked,
        "oos_current": base,
        "validation_start": parts.validation_start,
        "test_start": parts.test_start,
    }
    return {
        "n_signals": len(sigs),
        "current": current,
        "recommend": chosen,
        "recommend_stats": winner["validation"],
        "current_stats": _rule_stats(
            validation, current["hold_days"], current["stop_loss"], current["take_profit"]
        ),
        "folds": [fold],
        "gain_unit": "pct/day",
        "gain_in_sample": round(gain_is, 8),
        "gain_out_sample": round(gain_test, 8),
        "decay": None,
        "verdict": "日期分组并清除跨界标签；验证集选择的候选仅观察，封存测试不直接用于调权。",
        "switch_recommended": False,
        "grid_top10": [
            {**{k: r[k] for k in ("hold_days", "stop_loss", "take_profit")}, **r["validation"]}
            for r in ranked[:10]
        ],
        "split_protocol": "date_grouped_label_end_purged_v1",
    }


def _short_verdict(folds, gain_is, gain_oos, decay, recommend, cur_full):
    """把选型结论翻译成一句能直接看的话（并明确该不该切）。

    增量一律按**日均收益差（pct/日）**表述——不同持有期不能比原始均值。
    另加一条**尾部守卫**：低吸策略的核心风险在左尾，若建议参数靠加深 5% 分位换取
    日均收益，即便样本外仍为正也不切（那不是改进，是换了风险偏好）。
    """
    n_oos = sum(f["n_oos"] for f in folds)
    if gain_is <= 0:
        return (
            f"样本内最优组合并未优于当前配置（日均差 {gain_is * 100:+.4f}pct/日），"
            f"调参无增量，**维持当前配置**。"
        ), False
    if recommend and cur_full:
        p_pick, p_cur = recommend.get("p05"), cur_full.get("p05")
        if p_pick is not None and p_cur is not None and p_pick < p_cur - TAIL_TOL:
            return (
                f"建议参数日均收益更高（{gain_oos * 100:+.4f}pct/日），"
                f"但 5% 分位由 {p_cur * 100:+.2f}% 恶化到 {p_pick * 100:+.2f}%"
                f"（持有 {cur_full.get('avg_days')}→{recommend.get('avg_days')} 日），"
                f"以尾部风险换收益，**维持当前配置**。"
            ), False
    if gain_oos > 0 and (decay is None or decay < 0.8):
        return (
            f"样本内日均优势 {gain_is * 100:+.4f}pct/日 在样本外保留 "
            f"{gain_oos * 100:+.4f}pct/日（衰减 {decay * 100:.0f}%），"
            f"且尾部未恶化，选型具备样本外增量，可考虑切换到建议参数。"
        ), True
    if gain_oos > 0:
        return (
            f"样本外略优（{gain_oos * 100:+.4f}pct/日）但衰减达 "
            f"{(decay or 0) * 100:.0f}%，多数优势被拟合掉，"
            f"**维持当前配置**，仅把建议参数作为观察项。"
        ), False
    return (
        f"样本内日均优势 {gain_is * 100:+.4f}pct/日 在样本外**反向**"
        f"（{gain_oos * 100:+.4f}pct/日，{n_oos} 笔），典型的参数过拟合，"
        f"**维持当前配置**。"
    ), False


# ---------------------------------------------------------------------------
# 长期：六维权重方案对比（在无前视面板上算 IC）
# ---------------------------------------------------------------------------
def _select_long_descriptive(
    panel: pd.DataFrame, cfg: dict, milestones=(20, 60, 120)
) -> dict | None:
    """在历史回放面板上比较各权重方案。panel 来自 value_track.historical_replay。"""
    if panel is None or len(panel) < 60:
        return None
    ok = [c for c in FACTORS_CORE if c in panel.columns]
    if len(ok) < 4:
        return None
    rows = []
    for name, w in LONG_SCHEMES.items():
        # 注意：面板列名带 v_ 前缀（v_quality…），权重字典的键不带前缀，
        # 直接用 ww[k] = w.get(k) 会全部取到 0 并导致除零出 NaN。
        ww = {f"v_{k}": float(w.get(k, 0.0)) for k in CORE_ONLY}
        ww = {k: v for k, v in ww.items() if k in ok and v > 0}
        if not ww:
            continue
        tw = sum(ww.values()) or 1.0
        score = sum(pd.to_numeric(panel[k], errors="coerce").fillna(50) * ww[k] for k in ww) / tw
        p = panel.assign(_wscore=score)
        per = {}
        for n in milestones:
            col = f"ret_{n}"
            if col not in p.columns:
                continue
            sub = p.dropna(subset=[col] + list(ww.keys()))
            if len(sub) < 30:
                continue
            ic = vt.cross_section_ic(sub, ["_wscore"], col)
            d = (ic.get("factors") or {}).get("_wscore")
            if d:
                per[int(n)] = d
        if per:
            # 输出权重时去掉 v_ 前缀，与 params.json 里 longterm.weights 的键格式一致，
            # 否则这份结论没法直接拿去更新参数。
            rows.append(
                {
                    "scheme": name,
                    "weights": {
                        k[2:] if k.startswith("v_") else k: round(v / tw, 4) for k, v in ww.items()
                    },
                    "milestones": per,
                    "ic_avg": round(float(np.mean([d["ic_mean"] for d in per.values()])), 4),
                    "icir_avg": round(float(np.mean([d["icir"] or 0 for d in per.values()])), 3),
                }
            )
    if not rows:
        return None
    rows.sort(key=lambda r: (r["ic_avg"], r["icir_avg"]), reverse=True)
    best, cur = rows[0], next((r for r in rows if r["scheme"] == "default"), None)
    spread = round(max(r["ic_avg"] for r in rows) - min(r["ic_avg"] for r in rows), 4)
    if cur:
        cur["_spread"] = spread
    verdict, switch = _long_verdict(best, cur)
    return {
        "n_obs": int(len(panel)),
        "n_periods": int(panel["date"].nunique()),
        "date_range": [str(panel["date"].min()), str(panel["date"].max())],
        "candidates": rows,
        "best": best["scheme"],
        "current_scheme": "default",
        "ic_spread": spread,
        "best_milestones": ({str(k): v for k, v in best["milestones"].items()}),
        "gain_ic": round(best["ic_avg"] - (cur["ic_avg"] if cur else 0.0), 4),
        "verdict": verdict,
        "switch_recommended": switch,
    }


def select_long(panel: pd.DataFrame, cfg: dict, milestones=(20, 60, 120)) -> dict | None:
    """Select weights on validation dates; inspect the sealed test once."""
    from quant_agent.domain.research.validation import configured_boundaries, split_three_way

    boundaries = configured_boundaries(cfg)
    if boundaries is None:
        return None
    if panel is None or panel.empty:
        return None
    ends = [f"label_end_ret_{n}" for n in milestones]
    if any(col not in panel for col in ends):
        return None
    work = panel.copy()
    parsed = work[ends].apply(pd.to_datetime, errors="coerce")
    work["_latest_label_end"] = parsed.max(axis=1).where(parsed.notna().all(axis=1))
    try:
        parts = split_three_way(work, date_col="date", end_col="_latest_label_end", **boundaries)
    except ValueError:
        return None
    if len(parts.train) < 30 or len(parts.validation) < 30 or len(parts.test) < 30:
        return None
    selected = _select_long_descriptive(parts.validation, cfg, milestones)
    if not selected:
        return None

    def score_test(name):
        weights = LONG_SCHEMES[name]
        usable = {
            f"v_{k}": float(weights[k])
            for k in CORE_ONLY
            if f"v_{k}" in parts.test and weights.get(k, 0) > 0
        }
        total = sum(usable.values())
        if not total:
            return {}
        score = (
            sum(
                pd.to_numeric(parts.test[k], errors="coerce").fillna(50) * weight
                for k, weight in usable.items()
            )
            / total
        )
        frame = parts.test.assign(_wscore=score)
        return {
            str(n): vt.cross_section_ic(
                frame.dropna(subset=[f"ret_{n}"] + list(usable)), ["_wscore"], f"ret_{n}"
            )
            for n in milestones
            if f"ret_{n}" in frame
        }

    selected.update(
        test_metrics={"selected": score_test(selected["best"]), "current": score_test("default")},
        validation_start=parts.validation_start,
        test_start=parts.test_start,
        split_protocol="date_grouped_label_end_purged_v1",
        switch_recommended=False,
        verdict="验证集选出的权重仅作观察；封存测试不直接触发参数更新。",
    )
    return selected


def _long_verdict(best, cur):
    if not cur:
        return "缺少基准方案，无法比较。", False
    gain = best["ic_avg"] - cur["ic_avg"]
    # 权重敏感度：所有方案 IC 都为正且极差很小时，说明主要贡献来自信号本身而非权重配比，
    # 此时没有理由为了一点点 IC 差去动权重结构（动了也只是拟合这段样本）。
    if cur.get("_spread") is not None and cur["_spread"] < 0.06 and best["ic_avg"] > 0:
        return (
            f"各权重方案 IC 全为正、极差仅 {cur['_spread']:.4f}，"
            f"说明**预测力主要来自信号本身而非权重配比**；"
            f"当前默认权重（IC 均值 {cur['ic_avg']:+.4f}）与最优方案差距不构成切换理由，"
            f"**维持现配置**，把精力放在扩大样本与验证信号上。"
        ), False
    if best["scheme"] == "default":
        return (
            f"当前默认六维权重已是面板上 IC 最优（均值 IC={best['ic_avg']:+.4f}），**维持现配置**。"
        ), False
    # 一致性要求：最优方案必须在多数里程碑上都优于默认，否则只是某一期的偶然
    n_better = sum(
        1
        for n, d in best["milestones"].items()
        if n in cur["milestones"] and d["ic_mean"] > cur["milestones"][n]["ic_mean"]
    )
    total = len(best["milestones"])
    if gain > 0.005 and n_better >= max(2, total - 1) and best["ic_avg"] > 0:
        return (
            f"方案「{best['scheme']}」在 {n_better}/{total} 个里程碑上优于默认"
            f"（IC 均值 {best['ic_avg']:+.4f} vs {cur['ic_avg']:+.4f}），"
            f"可作为下一期权重方案。"
        ), True
    return (
        f"方案「{best['scheme']}」整体 IC 略高（{gain:+.4f}）但仅在 "
        f"{n_better}/{total} 个里程碑占优，稳定性不足，**维持默认权重**。"
    ), False


# ---------------------------------------------------------------------------
# 落盘
# ---------------------------------------------------------------------------
def save_result(short: dict, long: dict, date_str: str = None) -> str:
    d = date_str or datetime.now().strftime("%Y-%m-%d")
    rec = {
        "date": d,
        "ts": datetime.now().isoformat(),
        "short": short,
        "long": {k: v for k, v in (long or {}).items() if k != "panel"} if long else None,
    }
    path = os.path.join(STATE, f"model_selection_{d}.json")
    with open(path, "w", encoding="utf-8") as f:
        json.dump(rec, f, ensure_ascii=False, indent=2, default=str)
    return path


def load_latest(before: str = None) -> dict | None:
    import glob

    files = sorted(glob.glob(os.path.join(STATE, "model_selection_*.json")))
    if before:
        files = [
            f for f in files if os.path.basename(f).split("model_selection_")[-1][:10] < before
        ]
    if not files:
        return None
    try:
        with open(files[-1], encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return None
