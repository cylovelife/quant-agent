"""Pure value track rules extracted without changing calculations.

@author cylovelife
"""

import numpy as np
import pandas as pd


def _rank_corr(x, y):
    """Spearman 秩相关（自实现，不引 scipy）。

    pandas 的 method="spearman" 需要 scipy，本环境未安装；
    平均秩 + Pearson 与 scipy.stats.spearmanr 等价（含并列情形）。
    """
    rx = pd.Series(np.asarray(x, dtype=float)).rank().to_numpy()
    ry = pd.Series(np.asarray(y, dtype=float)).rank().to_numpy()
    if rx.std() < 1e-12 or ry.std() < 1e-12:
        return None
    return float(np.corrcoef(rx, ry)[0, 1])


def cross_section_ic(df: pd.DataFrame, factors: list, ret_col: str, min_n: int = 5) -> dict:
    """先按交易日分组算截面相关，再对时间序列求均值/ICIR/t。

    不分组直接对全部样本求相关是错的：同一交易日的标的共享市场 beta，
    混合样本会把「当日涨跌」误读成「因子预测力」。
    ICIR = IC均值 / IC标准差（未年化，与 factor_eval 口径一致）。
    用秩相关（Rank IC），对极端值更稳健。
    """
    out = {}
    if df.empty:
        return out
    grp = df.groupby("date")
    n_periods = 0
    for f in factors:
        ics = []
        for _, g in grp:
            sub = g[[f, ret_col]].dropna()
            if len(sub) < min_n:
                continue
            if sub[f].nunique() < 3 or sub[ret_col].nunique() < 3:
                continue
            r = _rank_corr(sub[f].to_numpy(), sub[ret_col].to_numpy())
            if r is not None:
                ics.append(r)
        if len(ics) < 3:
            continue
        a = np.asarray(ics, dtype=float)
        mean, sd = float(a.mean()), float(a.std(ddof=1))
        t = float(mean / (sd / np.sqrt(len(a)))) if sd > 1e-12 else None
        n_periods = max(n_periods, len(a))
        out[f] = {
            "ic_mean": round(mean, 4),
            "ic_std": round(sd, 4),
            "icir": round(mean / sd, 3) if sd > 1e-12 else None,
            "t": round(t, 2) if t is not None else None,
            "positive_rate": round(float((a > 0).mean()), 3),
            "n_periods": len(a),
        }
    return {"factors": out, "n_periods": n_periods}
