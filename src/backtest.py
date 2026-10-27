# -*- coding: utf-8 -*-
"""向量化/事件驱动混合回测器：检验"低吸信号"的历史胜率。

研究模拟规则（尚未纳入成本及真实成交约束）：
- 信号日收盘触发评分 >= 阈值
- 次日开盘价买入（承认无法精确抄到收盘价）
- 持有 hold_days 日后收盘卖出；期间若收盘跌破止损线或达到止盈线则提前离场
"""
import numpy as np
import pandas as pd

from indicators import add_indicators
from trade_semantics import LabelSpec, resolve_trade
from execution import AccountView, ExecutionPolicy, Instrument, Order, SessionFacts, match_order
from execution_data import opening_quote


def replay_paper_order(order: Order, instrument: Instrument, raw: pd.DataFrame,
                       session: SessionFacts, account: AccountView,
                       policy: ExecutionPolicy) -> dict:
    """Preview one explicit historical order; no qfq conversion or portfolio accounting."""
    quote = opening_quote(raw, instrument, order.execution_date, session)
    return match_order(order, instrument, quote, account, policy)


def backtest_symbol(k: pd.DataFrame, params: dict) -> dict:
    """对单只标的的历史K线回测低吸策略，返回统计指标。"""
    bt = params["backtest"]
    w = params["weights"]
    spec = LabelSpec.from_dict(bt)
    thr = params["score_threshold"]

    k = add_indicators(k, params)
    k = k.reset_index(drop=True)
    prev_hist = k["macd_hist"].shift(1)

    trades = []
    i = 70  # 跳过指标预热期
    n = len(k)
    while i < n - 1:
        row = k.iloc[i]
        s = score_row_fast(row, prev_hist.iloc[i], w)
        if s >= thr:
            trade = resolve_trade(k, str(k.iloc[i]["date"])[:10], spec)
            if trade["status"] != "mature":
                i += 1
                continue
            trades.append({**trade, "days": trade["days_held"]})
            exit_i = i + trade["days_held"]
            i = exit_i + 1  # 一笔交易结束后才寻找下一个信号
        else:
            i += 1

    if not trades:
        return {"signals": 0}
    rets = np.array([t["ret"] for t in trades])
    wins = rets > 0
    # 加入初始净值，首笔亏损也必须计入回撤；仍仅观察已平仓交易端点，非每日净值。
    equity = np.concatenate(([1.0], np.cumprod(1 + rets)))
    peak = np.maximum.accumulate(equity)
    mean = float(rets.mean())
    sd = float(rets.std())
    avg_days = float(np.mean([t["days"] for t in trades]))
    max_dd = float((equity / peak - 1).min())
    # 可复现策略指标（借鉴 R&D-Agent(Q) 统一评测体系：IR / Calmar / ARR）
    # 仅当信号样本足够时才计算，避免小样本误报
    MIN_SIG = 5
    ir = calmar = arr = None
    if len(trades) >= MIN_SIG and sd > 1e-9 and avg_days > 0:
        ann_factor = 252.0 / avg_days
        ir = round(mean / sd * np.sqrt(ann_factor), 3)          # 信息比率（年化）
        arr = round((1 + mean) ** ann_factor - 1, 4)            # 年化收益 ARR
        calmar = round(arr / abs(max_dd), 3) if max_dd < -1e-9 else None
    return {
        "signals": len(trades),
        "win_rate": float(wins.mean()),
        "avg_ret": mean,
        "best_ret": float(rets.max()),
        "worst_ret": float(rets.min()),
        "total_ret": float(equity[-1] - 1),
        "max_dd": max_dd,
        "avg_days": avg_days,
        "ir": ir, "arr": arr, "calmar": calmar,
    }


def score_row_fast(row, prev_hist, w) -> float:
    """轻量版评分（回测用，只算总分，避免字典开销）。"""
    from strategy import score_row  # 延迟导入复用同一套逻辑
    d = dict(row)
    d["macd_hist_prev"] = prev_hist
    return score_row(d, w)["score"]


def backtest_candidates(scored: pd.DataFrame, kline_fn, params: dict,
                        top_n: int = 20) -> pd.DataFrame:
    """对评分最高的 top_n 只标的做历史回测，把回测指标并回结果表。"""
    rows = []
    for _, r in scored.head(top_n).iterrows():
        try:
            k = kline_fn(r["code"], r["market"], r.get("mkt_id"))
            if k is None or len(k) < 100:
                continue
            stat = backtest_symbol(k, params)
            row = r.to_dict()
            row.update({
                "bt_signals": stat.get("signals", 0),
                "bt_win_rate": round(stat["win_rate"], 3) if stat.get("signals") else None,
                "bt_avg_ret": round(stat["avg_ret"], 4) if stat.get("signals") else None,
                "bt_total_ret": round(stat["total_ret"], 3) if stat.get("signals") else None,
                "bt_max_dd": round(stat["max_dd"], 3) if stat.get("signals") else None,
                "bt_ir": stat.get("ir"),
                "bt_arr": stat.get("arr"),
                "bt_calmar": stat.get("calmar"),
            })
            rows.append(row)
        except Exception:
            continue
    return pd.DataFrame(rows)


def factor_ic(scored: pd.DataFrame) -> dict | None:
    """截面因子预测力（借鉴 R&D-Agent(Q) 统一评测：IC / Rank IC）。

    用当日候选的评分与回测场均收益做截面相关：
      - IC      = corr(score, bt_avg_ret)        （Pearson）
      - Rank IC = corr(rank(score), rank(bt_avg_ret))（Spearman 近似）
    代表“分数越高、历史低吸表现越好”的单调关系。样本不足返回 None。
    """
    need = ["score", "bt_avg_ret"]
    if not all(c in scored.columns for c in need):
        return None
    d = scored.dropna(subset=need)
    if len(d) < 5:
        return None
    ic = float(d["score"].corr(d["bt_avg_ret"]))
    rank_ic = float(d["score"].rank().corr(d["bt_avg_ret"].rank()))
    return {"n": len(d), "ic": round(ic, 3), "rank_ic": round(rank_ic, 3)}
