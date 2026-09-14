# -*- coding: utf-8 -*-
"""波段低买评分模型 + 选股筛选。

思路：先在实时快照上做粗筛（流动性、跌幅区间、剔除ST），
再对候选拉K线算指标，按多因子加权打分，找出"超跌企稳、
明日适合低吸"的标的。
"""
import numpy as np
import pandas as pd

from indicators import add_indicators


# ---------------------------------------------------------------------------
# 快照粗筛
# ---------------------------------------------------------------------------
def prefilter(spot: pd.DataFrame, market: str, cfg: dict) -> pd.DataFrame:
    f = cfg["filters"].get(market, {})
    df = spot.copy()
    df = df[df["amount"] >= f.get("min_amount", 1e8)]
    df = df[df["price"] >= f.get("min_price", 2.0)]
    lo, hi = f.get("pct_range", [-7.5, 1.5])
    df = df[(df["pct"] >= lo) & (df["pct"] <= hi)]
    if market in ("cn", "hk"):
        bad = df["name"].str.contains("ST|退", na=False)
        df = df[~bad]
    if market == "etf":
        df = df[~df["name"].str.contains("货币|现金", na=False)]
    # 低买策略目标：当日跌幅居前的活跃标的（后续因子会过滤坠落刀）
    return df.sort_values(["pct", "amount"], ascending=[True, False]).head(
        f.get("top_by_amount", 60))


# ---------------------------------------------------------------------------
# 单标的评分（基于含指标的K线最后一行）
# ---------------------------------------------------------------------------
def _clip01(x):
    return float(max(0.0, min(1.0, x)))


def score_row(row, w: dict) -> dict:
    """返回各因子得分(0~100)与总分。"""
    # 1. 布林位置：%B 越低越接近下轨（超跌）。pb<=-0.2 满分，pb>=1 为 0 分
    pb = row.get("boll_pb", np.nan)
    f_boll = _clip01((1.2 - pb) / 1.4) * 100 if np.isfinite(pb) else 50.0

    # 2. RSI：25~40 区间为"超卖企稳"甜点区；<15 视为坠落刀
    rsi_v = row.get("rsi", 50.0)
    if 25 <= rsi_v <= 40:
        f_rsi = 100.0
    elif 15 <= rsi_v < 25:
        f_rsi = 60.0 + (rsi_v - 15) * 4      # 60~100
    elif 40 < rsi_v <= 55:
        f_rsi = 100.0 - (rsi_v - 40) * 6     # 100~10
    else:
        f_rsi = 10.0

    # 3. 量能：缩量回踩(0.4~0.9)佳，放量下跌(vr>1.5)差；无成交量数据(基金)取中性
    vr = row.get("vr", 1.0)
    if not np.isfinite(vr):
        f_vol = 50.0
    elif 0.4 <= vr <= 0.9:
        f_vol = 100.0
    elif 0.9 < vr <= 1.5:
        f_vol = 70.0 - (vr - 0.9) * 66
    elif vr < 0.4:
        f_vol = 75.0
    else:
        f_vol = 20.0

    # 4. 趋势保护：收盘价相对 ma60 位置，避免坠落刀
    ma60 = row.get("ma60", np.nan)
    close = row.get("close", np.nan)
    if np.isfinite(ma60) and ma60 > 0:
        pos = close / ma60                    # 1.0 表示在年线(60日)上
        f_trend = _clip01((pos - 0.75) / 0.35) * 100   # >=1.1 满分
    else:
        f_trend = 50.0

    # 5. 动能拐点：MACD 柱是否在收敛/翻红
    hist_now = row.get("macd_hist", np.nan)
    hist_prev = row.get("macd_hist_prev", np.nan)
    if np.isfinite(hist_now) and np.isfinite(hist_prev):
        diff = hist_now - hist_prev
        f_macd = _clip01(0.5 + diff / (abs(hist_prev) + 1e-9)) * 100
    else:
        f_macd = 50.0

    # 6. 回撤幅度：近20日跌幅适中(-15%~-5%)好，暴跌>25%危险
    r20 = row.get("ret_20", 0.0)
    if np.isfinite(r20):
        if -0.18 <= r20 <= -0.03:
            f_dd = 100.0
        elif -0.28 <= r20 < -0.18:
            f_dd = 55.0
        elif r20 > -0.03:
            f_dd = 45.0
        else:
            f_dd = 20.0
    else:
        f_dd = 50.0

    total = (w["boll"] * f_boll + w["rsi"] * f_rsi + w["volume"] * f_vol +
             w["trend"] * f_trend + w["macd"] * f_macd + w["drawdown"] * f_dd)
    return {
        "f_boll": round(f_boll, 1), "f_rsi": round(f_rsi, 1),
        "f_volume": round(f_vol, 1), "f_trend": round(f_trend, 1),
        "f_macd": round(f_macd, 1), "f_drawdown": round(f_dd, 1),
        "score": round(total, 1),
    }


# ---------------------------------------------------------------------------
# 对候选标的计算指标与评分
# ---------------------------------------------------------------------------
def score_candidates(candidates: pd.DataFrame, market: str, params: dict,
                     kline_fn, max_n: int = 40, progress=None) -> pd.DataFrame:
    """candidates: 粗筛后的快照；kline_fn(code, market)->K线DF"""
    w = params["weights"]
    thr = params["score_threshold"]
    rows, errors = [], 0
    df = candidates.head(max_n)
    for i, (_, r) in enumerate(df.iterrows()):
        try:
            k = kline_fn(r["code"], market, r.get("mkt_id"))
            if k is None or len(k) < 70:
                continue
            k = add_indicators(k, params)
            last, prev = k.iloc[-1], k.iloc[-2]
            row = dict(last)
            row["macd_hist_prev"] = prev["macd_hist"]
            s = score_row(row, w)
            if s["score"] < thr * 0.5:
                continue
            rows.append({
                "code": r["code"], "name": r["name"], "market": market,
                "mkt_id": r.get("mkt_id"),
                "price": r["price"], "pct": r["pct"],
                "amount": round(r["amount"] / 1e8, 2),  # 亿元
                "boll_pb": round(float(last["boll_pb"]), 3),
                "rsi": round(float(last["rsi"]), 1),
                "vr": round(float(last["vr"]), 2),
                **s,
            })
        except Exception:
            errors += 1
        if progress:
            progress(i + 1, len(df))
    out = pd.DataFrame(rows)
    if errors:
        print(f"  [strategy] {market} K线获取/评分异常 {errors} 只")
    if out.empty:
        return out
    return out.sort_values("score", ascending=False).reset_index(drop=True)
