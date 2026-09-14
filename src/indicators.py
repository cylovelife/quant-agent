# -*- coding: utf-8 -*-
"""技术指标库（纯 pandas/numpy 实现）。"""
import numpy as np
import pandas as pd


def sma(s: pd.Series, n: int) -> pd.Series:
    return s.rolling(n, min_periods=n).mean()


def ema(s: pd.Series, n: int) -> pd.Series:
    return s.ewm(span=n, adjust=False).mean()


def rsi(close: pd.Series, n: int = 14) -> pd.Series:
    """Wilder RSI。"""
    delta = close.diff()
    gain = delta.clip(lower=0.0)
    loss = (-delta).clip(lower=0.0)
    avg_gain = gain.ewm(alpha=1 / n, adjust=False).mean()
    avg_loss = loss.ewm(alpha=1 / n, adjust=False).mean()
    rs = avg_gain / avg_loss.replace(0, np.nan)
    out = 100 - 100 / (1 + rs)
    return out.fillna(50.0)


def macd(close: pd.Series, fast=12, slow=26, signal=9):
    dif = ema(close, fast) - ema(close, slow)
    dea = ema(dif, signal)
    hist = (dif - dea) * 2
    return dif, dea, hist


def boll(close: pd.Series, n: int = 20, k: float = 2.0):
    mid = sma(close, n)
    std = close.rolling(n, min_periods=n).std()
    upper = mid + k * std
    lower = mid - k * std
    # %B：价格在带内的相对位置，<0 表示跌破下轨
    pb = (close - lower) / (upper - lower).replace(0, np.nan)
    # 带宽
    bw = (upper - lower) / mid
    return mid, upper, lower, pb, bw


def atr(df: pd.DataFrame, n: int = 14) -> pd.Series:
    prev_close = df["close"].shift(1)
    tr = pd.concat([
        df["high"] - df["low"],
        (df["high"] - prev_close).abs(),
        (df["low"] - prev_close).abs(),
    ], axis=1).max(axis=1)
    return tr.ewm(alpha=1 / n, adjust=False).mean()


def volume_ratio(volume: pd.Series, n: int = 5) -> pd.Series:
    """量比：当日成交量 / 近 n 日均量。"""
    return volume / volume.shift(1).rolling(n, min_periods=n).mean()


def add_indicators(df: pd.DataFrame, params: dict) -> pd.DataFrame:
    """在 K 线 DataFrame 上附加全部策略所需指标列。"""
    p = params.get("indicators", {})
    df = df.copy()
    df["ma20"] = sma(df["close"], 20)
    df["ma60"] = sma(df["close"], 60)
    df["rsi"] = rsi(df["close"], p.get("rsi_n", 14))
    df["macd_dif"], df["macd_dea"], df["macd_hist"] = macd(df["close"])
    n_b = p.get("boll_n", 20)
    k_b = p.get("boll_k", 2.0)
    df["boll_mid"], df["boll_up"], df["boll_low"], df["boll_pb"], df["boll_bw"] = \
        boll(df["close"], n_b, k_b)
    df["atr"] = atr(df)
    df["vr"] = volume_ratio(df["volume"], p.get("vr_n", 5))
    df["ret_5"] = df["close"].pct_change(5)
    df["ret_20"] = df["close"].pct_change(20)
    return df


# ---------------------------------------------------------------------------
# 因子共线诊断（借鉴 R&D-Agent(Q) 验证单元“因子去重”思想）
# ---------------------------------------------------------------------------
FACTOR_COLS = ["f_boll", "f_rsi", "f_volume", "f_trend", "f_macd", "f_drawdown"]


def factor_collinearity(matrix: pd.DataFrame, factor_cols=None,
                        flag_thresh: float = 0.7) -> dict:
    """因子共线诊断：基于截面因子得分两两 Pearson 相关。

    目的：识别“讲同一件事”的冗余因子（如 Boll% 与 RSI 都量超卖），
    避免权重叠加放大单一信息造成过拟合。对应文章中“仅保留不相似的强因子”。

    返回 {
      n: 样本数, pairs: [(a,b,r)...]按|r|降序,
      max_abs: 最大共线度, flagged: 高相关冗余对,
    }
    """
    cols = [c for c in (factor_cols or FACTOR_COLS) if c in matrix.columns]
    if len(cols) < 2:
        return {"n": 0, "pairs": [], "max_abs": 0.0, "flagged": []}
    sub = matrix[cols].apply(pd.to_numeric, errors="coerce").dropna()
    if len(sub) < 5:
        return {"n": len(sub), "pairs": [], "max_abs": 0.0, "flagged": []}
    corr = sub.corr()
    pairs = []
    for i in range(len(cols)):
        for j in range(i + 1, len(cols)):
            a, b = cols[i], cols[j]
            r = corr.iloc[i, j]
            if pd.notna(r):
                pairs.append((a, b, round(float(r), 3)))
    pairs.sort(key=lambda x: -abs(x[2]))
    flagged = [(a, b, r) for a, b, r in pairs if abs(r) >= flag_thresh]
    return {"n": len(sub),
            "pairs": pairs,
            "max_abs": max((abs(r) for _, _, r in pairs), default=0.0),
            "flagged": flagged}
