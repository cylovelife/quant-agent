# -*- coding: utf-8 -*-
"""滚动因子评估：IC / Rank IC 及其信息比率（ICIR / Rank ICIR）。

对接 R&D-Agent(Q) 的「统一可复现评测」思想：因子好不好，不只看某一期
截面的预测力，更要看这种预测力在时间序列上是否**稳定**。

方法（与实盘回测口径对齐）：
1. 对参与评估的标的历史 K 线逐日计算 6 因子分与综合 score；
2. 前向收益 = 次日开盘买入、持有 horizon 日收盘卖出（与 backtest 一致）；
3. 每个「市场 × 交易日」构成一个横截面，计算 因子 vs 前向收益 的
   Pearson IC 与 Spearman Rank IC；
4. 得到 IC 时间序列 → 均值 / 标准差 / ICIR / 胜率 / t 值。

关键指标：
- IC       : 截面预测力的中枢，>0 表示因子方向正确；
- ICIR     : IC 均值 / IC 标准差，衡量预测力的稳定性（信噪比）；
- Rank IC  : 秩相关，对极端值更鲁棒；
- Rank ICIR: Rank IC 的稳定性；
- t 值     : IC 均值是否显著异于 0（重叠窗口使其偏乐观，判读以 ICIR/胜率为主）。
"""
import numpy as np
import pandas as pd

from indicators import add_indicators
from strategy import score_row

FACTOR_COLS = ["f_boll", "f_rsi", "f_volume", "f_trend", "f_macd", "f_drawdown"]
EVAL_COLS = ["score"] + FACTOR_COLS

WARMUP = 70      # 与回测一致的指标预热期
MIN_OBS = 5      # 单个截面至少 5 只标的才计算 IC


def forward_return(k: pd.DataFrame, horizon: int = 5) -> pd.Series:
    """t 日信号 → t+1 开盘买入 → 持有 horizon 日收盘卖出（与 backtest 口径一致）。"""
    entry = k["open"].shift(-1)
    exit_ = k["close"].shift(-horizon)
    return exit_ / entry - 1


def _corr(x: np.ndarray, y: np.ndarray) -> float:
    """皮尔逊相关（numpy 手算，避免 pandas 在小截面上的开销）。"""
    x = x.astype(float) - float(np.mean(x))
    y = y.astype(float) - float(np.mean(y))
    denom = np.sqrt(float(np.dot(x, x)) * float(np.dot(y, y)))
    return float(np.dot(x, y) / denom) if denom > 1e-12 else float("nan")


def build_panel(kline_map: dict, params: dict, horizon: int = 5) -> pd.DataFrame:
    """把 K 线集合摊成「市场 × 日期 × 标的」的因子面板（含前向收益）。"""
    rows = []
    w = params["weights"]
    for key, k in kline_map.items():
        if k is None or len(k) < WARMUP + horizon + 10:
            continue
        market, code = key if isinstance(key, tuple) else ("", key)
        kk = add_indicators(k, params).reset_index(drop=True)
        if "date" not in kk.columns:
            continue
        kk["macd_hist_prev"] = kk["macd_hist"].shift(1)
        kk["fwd_ret"] = forward_return(kk, horizon)
        # 截取：预热期之后、且留有完整前向窗口
        seg = kk.iloc[WARMUP: len(kk) - horizon]
        for d in seg.to_dict("records"):
            fwd = d.get("fwd_ret", np.nan)
            if not np.isfinite(fwd):
                continue
            s = score_row(d, w)
            rec = {"market": market, "date": str(d.get("date"))[:10],
                   "code": code, "fwd_ret": float(fwd)}
            rec.update(s)
            rows.append(rec)
    return pd.DataFrame(rows)


def _summarize(series: list, horizon: int) -> "dict | None":
    """把 IC（或 Rank IC）时间序列汇总成 均值/标准差/ICIR/胜率/t。"""
    a = np.asarray([x for x in series if x is not None and np.isfinite(x)], dtype=float)
    if len(a) < 3:
        return None
    mean = float(a.mean())
    std = float(a.std(ddof=1))
    winrate = float((a > 0).mean())
    if std <= 1e-12:
        return {"n": len(a), "mean": round(mean, 4), "std": 0.0,
                "icir": None, "icir_ann": None, "t": None,
                "winrate": round(winrate, 3)}
    icir = mean / std
    ann = float(np.sqrt(252.0 / max(horizon, 1)))   # 按持有周期折算到年化
    t = mean / (std / np.sqrt(len(a)))
    return {"n": len(a), "mean": round(mean, 4), "std": round(std, 4),
            "icir": round(icir, 3), "icir_ann": round(icir * ann, 3),
            "t": round(float(t), 2), "winrate": round(winrate, 3)}


def rolling_factor_ic(kline_map: dict, params: dict,
                      horizon: "int | None" = None,
                      min_obs: int = MIN_OBS) -> "dict | None":
    """滚动因子预测力评估。kline_map: {(market, code): K线DF} 或 {code: DF}。"""
    if not kline_map:
        return None
    horizon = int(horizon or params.get("backtest", {}).get("hold_days", 5))
    panel = build_panel(kline_map, params, horizon)
    if panel.empty:
        return None

    groups = [(d, g) for (m, d), g in panel.groupby(["market", "date"], sort=True)
              if len(g) >= min_obs]
    series = {f: {"ic": [], "rank_ic": []} for f in EVAL_COLS if f in panel.columns}
    for _, g in groups:
        fr = g["fwd_ret"].values
        if np.std(fr) < 1e-12:
            continue
        rank_fr = pd.Series(fr).rank().values
        for f, acc in series.items():
            fv = g[f].values
            mask = np.isfinite(fv) & np.isfinite(fr)
            if mask.sum() < min_obs:
                continue
            x = fv[mask]
            if np.std(x) < 1e-12:
                continue
            acc["ic"].append(_corr(x, fr[mask]))
            acc["rank_ic"].append(_corr(pd.Series(x).rank().values, rank_fr[mask]))

    factors = {}
    for f, acc in series.items():
        ic_s = _summarize(acc["ic"], horizon)
        ric_s = _summarize(acc["rank_ic"], horizon)
        if ic_s or ric_s:
            factors[f] = {"ic": ic_s, "rank_ic": ric_s,
                          "n_periods": len([x for x in acc["ic"] if np.isfinite(x)])}

    if not factors:
        return None
    return {
        "horizon": horizon,
        "min_obs": min_obs,
        "n_periods": len(groups),
        "n_obs": int(len(panel)),
        "n_symbols": int(panel["code"].nunique()),
        "markets": sorted(panel["market"].unique().tolist()),
        "date_range": [str(panel["date"].min()), str(panel["date"].max())],
        "factors": factors,
    }
