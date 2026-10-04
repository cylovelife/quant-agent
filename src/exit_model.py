# -*- coding: utf-8 -*-
"""离场决策模型：离散状态马尔可夫链 + 条件期望值函数。

回答的问题：**已持仓的标的，明天该卖还是继续持有？**

与买入端（6 因子评分）不同，离场端的有效信号来自「持仓路径」而非「因子取值」。
本模型把持仓路径抽象成离散状态，用项目自身历史 K 线标定，无外部依赖。

模型分三层
----------
1. **状态定义**（马尔可夫链的状态空间）
       S = (持有天数 d, 累计收益桶 r, 买入评分桶 s)
   - `d` 由时间序列推进（d -> d+1，确定性）；
   - `r` 由**昨天的实盘**决定（相对建仓价的累计收益分桶）；
   - `s` 由**前天的预测**决定（信号日综合评分分桶）。
2. **值函数** V(S) = E[ 从当前价继续持有到期（T+hold_days）的收益 | S ]
   - 立即卖出收益记为 0，继续持有期望为 V(S)；
   - 故 V(S) < τ 时应离场。这是对行为策略的一步贪心改进（policy improvement）。
3. **短周期项** μ1(S) = E[ 次日收益 | S ]，作为动量确认。

之所以用「持有到固定期限」而不是「持有到原策略退出」定义值函数，是为了避免
止损/止盈造成的路径依赖与幸存者偏差，同时让基线（固定持有到期）可直接对标。

估计与稳健性
------------
- 状态桶样本稀疏，采用两层**贝叶斯收缩**：(d,r,s) 向 (d,r) 收缩，(d,r) 向全局均值收缩；
- 样本内/样本外按建仓日切分：IS 段标定状态表与阈值 τ*，OOS 段验证
  「模型提前离场」相对「固定持有到期」的真实增益——防止样本内自欺欺人；
- 路径样本按「每个信号独立」采样（不做交易去重），样本量更大，
  但相邻信号路径高度重叠，会使 t 值偏乐观，**判读以样本外回测为准**。
"""
import numpy as np
import pandas as pd

from indicators import add_indicators
from strategy import score_row

WARMUP = 70                     # 指标预热期（与 backtest / factor_eval 一致）
R_EDGES = (-0.05, -0.02, 0.0, 0.02, 0.05)
R_LABELS = ("≤-5%", "-5~-2%", "-2~0%", "0~+2%", "+2~+5%", "≥+5%")
N_SCORE_BUCKETS = 3
MIN_BUCKET_N = 20               # 低于此样本量的状态桶不作为主要依据
SHRINK_K = 25.0                 # 收缩强度（等价于先验样本数）
TAU_GRID = (-0.03, -0.02, -0.012, -0.006, 0.0, 0.008, 0.02)
OOS_FRAC = 0.3
NEG_T_STRONG = -2.0             # 显著为负 → 独立卖出依据
NEG_T_WEAK = -1.0               # 弱显著 → 减仓/收紧止损提醒（不构成独立卖出）
T_MIN_GRID = (None, -0.8, -1.2, -2.0)   # 显著性门槛候选（None = 不设门槛）
# 注意：扫描时凡比 NEG_T_WEAK(-1.0) 更松的候选会被剔除（见 fit_exit_model），
# 即 -0.8 永远不会被选中——保留它在网格里只是为了把这条约束显式化。


# ---------------------------------------------------------------------------
# 分桶
# ---------------------------------------------------------------------------
def _rbucket(r: float) -> int:
    """累计收益桶：<=-5% / -5~-2% / -2~0% / 0~2% / 2~5% / >=5%。"""
    x = float(r)
    if not np.isfinite(x):
        return 3
    x = max(min(x, 0.5), -0.5)
    return int(np.digitize(x, R_EDGES))


def _score_edges(scores) -> list:
    """评分桶边界（默认三分位），由标定集决定，决策时复用同一组边界。"""
    a = np.asarray([s for s in scores if s is not None and np.isfinite(s)], dtype=float)
    if len(a) < 3 * N_SCORE_BUCKETS:
        return []
    qs = np.linspace(0.0, 1.0, N_SCORE_BUCKETS + 1)[1:-1]
    return [round(float(q), 3) for q in np.quantile(a, qs)]


def _sbucket(score, edges: list) -> int:
    if score is None or not np.isfinite(score) or not edges:
        return 0
    return int(np.digitize(float(score), edges))


def state_key(d: int, rb: int, sb: int) -> str:
    return f"d{int(d)}|r{int(rb)}|s{int(sb)}"


def coarse_key(d: int, rb: int) -> str:
    return f"d{int(d)}|r{int(rb)}"


def r_label(rb: int) -> str:
    return R_LABELS[int(rb)] if 0 <= int(rb) < len(R_LABELS) else "?"


# ---------------------------------------------------------------------------
# 1) 从历史 K 线重建持仓路径面板
# ---------------------------------------------------------------------------
def _symbol_paths(k: pd.DataFrame, market: str, code: str,
                  params: dict, horizon: int) -> list:
    """回放单只标的的低吸信号，展开成逐日持仓路径（每条 = 一个决策时点）。"""
    thr = float(params["score_threshold"])
    w = params["weights"]
    kk = add_indicators(k, params).reset_index(drop=True)
    if "date" not in kk.columns:
        return []
    n = len(kk)
    if n < WARMUP + horizon + 5:
        return []

    prev_hist = kk["macd_hist"].shift(1).to_numpy(dtype=float)
    close = kk["close"].to_numpy(dtype=float)
    open_ = kk["open"].to_numpy(dtype=float)
    dates = kk["date"].astype(str).str.slice(0, 10).to_numpy()
    base_cols = [c for c in kk.columns if c != "date"]

    rows = []
    i = WARMUP
    while i < n - horizon:
        hist_prev = prev_hist[i]
        if not np.isfinite(hist_prev):
            i += 1
            continue
        row = {c: kk.iloc[i][c] for c in base_cols}
        row["macd_hist_prev"] = float(hist_prev)
        s = score_row(row, w)
        if s["score"] < thr:
            i += 1
            continue
        entry = float(open_[i + 1])
        expire_c = float(close[i + horizon])
        if not (np.isfinite(entry) and entry > 0 and np.isfinite(expire_c) and expire_c > 0):
            i += 1
            continue
        ret_expire = expire_c / entry - 1
        # 决策时点：d = 1 .. horizon-1（到 d = horizon 时是硬性到期，无需决策）
        peak = 0.0                                  # 建仓价即 open 成交价，故初值 0
        for j in range(i + 1, i + horizon):
            c = float(close[j])
            if not (np.isfinite(c) and c > 0):
                continue
            ret0 = float(c / entry - 1)
            peak = max(peak, ret0)
            rows.append({
                "market": market, "code": str(code),
                "signal_date": dates[i], "entry_date": dates[i + 1],
                "date": dates[j], "d": int(j - i),
                "ret0": ret0,                         # 相对建仓价的累计收益（昨日实盘）
                "peak_ret": float(peak),              # 持仓期最大浮盈
                "giveback": float(peak - ret0),       # 浮盈回吐幅度
                "ret_expire": float(ret_expire),      # 固定持有到期的基线收益
                "mu1": float(close[j + 1] / c - 1),   # 次日收益（时间序列项）
                "score": float(s["score"]),           # 买入时评分（前天预测）
            })
        i += 1
    return rows


def build_exit_panel(kline_map: dict, params: dict, horizon: "int | None" = None) -> pd.DataFrame:
    """把 K 线集合摊成「持仓决策时点」面板。"""
    horizon = int(horizon or params.get("backtest", {}).get("hold_days", 5))
    rows = []
    for key, k in (kline_map or {}).items():
        if k is None or len(k) == 0:
            continue
        market, code = key if isinstance(key, tuple) else ("", key)
        try:
            rows.extend(_symbol_paths(k, market, code, params, horizon))
        except Exception:
            continue
    return pd.DataFrame(rows)


# ---------------------------------------------------------------------------
# 2) 标定状态表（两层收缩）
# ---------------------------------------------------------------------------
def _shrink(n: int, mean: float, parent: float, k: float = SHRINK_K) -> float:
    return (n * mean + k * parent) / (n + k)


def calibrate_states(panel: pd.DataFrame, score_edges: list,
                     min_bucket_n: int = MIN_BUCKET_N,
                     shrink_k: float = SHRINK_K) -> dict:
    """用面板标定 (d,r,s) 与 (d,r) 两层期望值 + 次日动量。"""
    if panel.empty:
        return {}
    df = panel.copy()
    df["rb"] = df["ret0"].map(_rbucket)
    df["sb"] = df["score"].map(lambda x: _sbucket(x, score_edges))
    # v = 从当前价继续持有到期的收益（值函数监督信号）
    df = df[np.isfinite(df["ret0"]) & np.isfinite(df["ret_expire"])]
    df["v"] = (1.0 + df["ret_expire"]) / (1.0 + df["ret0"]) - 1.0
    df = df[np.isfinite(df["v"])]
    if df.empty:
        return {"states": {}, "coarse": {}, "global_v": 0.0, "n_obs": 0}

    v_all = float(df["v"].mean())

    coarse = {}
    for (d, rb), g in df.groupby(["d", "rb"]):
        v = g["v"].to_numpy(dtype=float)
        mu = g["mu1"].to_numpy(dtype=float)
        mu = mu[np.isfinite(mu)]
        coarse[coarse_key(d, rb)] = {
            "d": int(d), "rb": int(rb), "n": int(len(v)),
            "v": round(_shrink(len(v), float(v.mean()), v_all, shrink_k), 4),
            "v_raw": round(float(v.mean()), 4),
            "var5": round(float(np.percentile(v, 5)), 4) if len(v) >= 20 else None,
            "p_bad": round(float((v <= -0.05).mean()), 3) if len(v) >= 20 else None,
            "mu1": round(float(mu.mean()), 4) if len(mu) else None,
        }

    states = {}
    for (d, rb, sb), g in df.groupby(["d", "rb", "sb"]):
        v = g["v"].to_numpy(dtype=float)
        mu = g["mu1"].to_numpy(dtype=float)
        mu = mu[np.isfinite(mu)]
        n = len(v)
        raw = float(v.mean())
        sd = float(v.std(ddof=1)) if n > 1 else 0.0
        t = raw / (sd / np.sqrt(n)) if n > 1 and sd > 1e-12 else None
        parent = coarse.get(coarse_key(d, rb), {}).get("v", v_all)
        states[state_key(d, rb, sb)] = {
            "d": int(d), "rb": int(rb), "sb": int(sb), "n": int(n),
            "v": round(_shrink(n, raw, parent, shrink_k), 4),
            "v_raw": round(raw, 4),
            "v_t": round(float(t), 2) if t is not None else None,
            "var5": round(float(np.percentile(v, 5)), 4) if n >= 20 else None,
            "p_bad": round(float((v <= -0.05).mean()), 3) if n >= 20 else None,
            "sd": round(sd, 4) if n > 1 else None,
            "mu1": round(float(mu.mean()), 4) if len(mu) else None,
            "p_neg": round(float((v < 0).mean()), 3),
        }
    return {"states": states, "coarse": coarse, "global_v": round(v_all, 4),
            "n_obs": int(len(df)), "n_trades": int(df.groupby(["code", "entry_date"]).ngroups)}


# ---------------------------------------------------------------------------
# 3) 策略评估：模型提前离场 vs 固定持有到期
# ---------------------------------------------------------------------------
def _lookup(d: int, ret0: float, score, states: dict, coarse: dict,
            score_edges: list, min_bucket_n: int) -> tuple:
    """查状态表：优先细桶，样本不足回退到 (d,r) 粗桶。返回 (状态dict, 层级)。"""
    rb = _rbucket(ret0)
    sb = _sbucket(score, score_edges)
    st = states.get(state_key(d, rb, sb))
    if st and st.get("n", 0) >= min_bucket_n:
        return st, "fine"
    ct = coarse.get(coarse_key(d, rb))
    if ct and ct.get("n", 0) >= min_bucket_n:
        return ct, "coarse"
    if st:
        return st, "fine-thin"
    return ct, "coarse-thin"


def _simulate(paths_by_trade: list, states: dict, coarse: dict, score_edges: list,
              tau: float, min_bucket_n: int, use_model: bool,
              horizon: int, t_min: "float | None" = None) -> dict:
    """对每笔交易应用「V(S) < τ 即卖出」规则，返回组合层面统计。

    `t_min` 为显著性门槛：不为 None 时额外要求该状态的期望收益
    `t ≤ t_min`，避免把噪声级偏差（如 E=-0.04%，t=-0.30）当成卖出依据。
    """
    rets, days, stopped = [], [], 0
    for g in paths_by_trade:
        g = g.sort_values("d")
        picked = None
        if use_model:
            for _, row in g.iterrows():
                st, _lvl = _lookup(int(row["d"]), float(row["ret0"]), row["score"],
                                   states, coarse, score_edges, min_bucket_n)
                if not st or st.get("v") is None or st.get("n", 0) < min_bucket_n:
                    continue
                if st["v"] >= tau:
                    continue
                if t_min is not None and not (st.get("v_t") is not None
                                              and st["v_t"] <= t_min):
                    continue
                picked = row
                break
        if picked is not None:
            rets.append(float(picked["ret0"]))
            days.append(int(picked["d"]))
            stopped += 1
        else:
            last = g.iloc[-1]
            rets.append(float(last["ret_expire"]))
            days.append(horizon)
    if not rets:
        return {"n": 0}
    a = np.asarray(rets, dtype=float)
    equity = np.cumprod(1.0 + a)
    peak = np.maximum.accumulate(equity)
    return {
        "n": int(len(a)),
        "avg_ret": round(float(a.mean()), 4),
        "median_ret": round(float(np.median(a)), 4),
        "win_rate": round(float((a > 0).mean()), 3),
        "p05": round(float(np.percentile(a, 5)), 4),
        "worst": round(float(a.min()), 4),
        "avg_days": round(float(np.mean(days)), 2),
        "max_dd": round(float((equity / peak - 1.0).min()), 4),
        "early_exit_ratio": round(stopped / len(a), 3),
    }


def evaluate_exit_policy(panel: pd.DataFrame, horizon: int,
                         oos_frac: float = OOS_FRAC,
                         min_bucket_n: int = MIN_BUCKET_N,
                         shrink_k: float = SHRINK_K) -> "dict | None":
    """按建仓日切分 IS/OOS，IS 选 τ*，OOS 验证相对基线的增益。

    ⚠️ 状态表只用 IS 段标定、τ 只在 IS 段选，OOS 段完全不参与拟合，
    因此 OOS 的对比是本模型的真实样本外表现。
    """
    if panel is None or panel.empty or "entry_date" not in panel.columns:
        return None
    df = panel.copy()
    trades = df.groupby(["code", "entry_date"]).size().reset_index(name="m")
    if len(trades) < 12:
        return None
    cut_date = sorted(trades["entry_date"].unique())[
        max(1, int(len(sorted(trades["entry_date"].unique())) * (1 - oos_frac)) - 1)]
    is_df = df[df["entry_date"] <= cut_date]
    oos_df = df[df["entry_date"] > cut_date]
    if is_df.empty or oos_df.empty:
        return None

    edges = _score_edges(is_df["score"].tolist())
    is_cal = calibrate_states(is_df, edges, min_bucket_n, shrink_k)
    if not is_cal.get("states"):
        return None
    states, coarse = is_cal["states"], is_cal["coarse"]

    is_trades = [g for _, g in is_df.groupby(["code", "entry_date"], sort=False)]
    oos_trades = [g for _, g in oos_df.groupby(["code", "entry_date"], sort=False)]

    scan = []
    for tau in TAU_GRID:
        for tm in T_MIN_GRID:
            # 不变量：卖出门槛不得松于噪声线 NEG_T_WEAK。
            # 否则扫描会选出 t_min*=-0.8 这类比噪声线(-1.0)更松的门槛，
            # 把 t≈-0.85 的纯噪声判成「建议卖出」——正是这个模块最该避免的事。
            if tm is not None and tm > NEG_T_WEAK:
                continue
            r = _simulate(is_trades, states, coarse, edges, tau, min_bucket_n,
                          True, horizon, tm)
            if r.get("n"):
                scan.append({"tau": tau, "t_min": tm, **r})
    if not scan:
        return None
    # 样本内选择：平均收益最高者优先，并列时取「离场更少」的组合
    # （更保守：同等收益下不鼓励频繁交易，也降低过拟合面）。
    best = max(scan, key=lambda x: (x["avg_ret"], -x["early_exit_ratio"],
                                    -abs(x["tau"]),
                                    -(x["t_min"] or 0.0)))
    tau = float(best["tau"])
    t_min = best["t_min"]

    baseline_is = _simulate(is_trades, states, coarse, edges, tau, min_bucket_n,
                            False, horizon, None)
    baseline_oos = _simulate(oos_trades, states, coarse, edges, tau, min_bucket_n,
                             False, horizon, None)
    model_oos = _simulate(oos_trades, states, coarse, edges, tau, min_bucket_n,
                          True, horizon, t_min)
    loose_oos = _simulate(oos_trades, states, coarse, edges, tau, min_bucket_n,
                          True, horizon, None)

    return {
        "horizon": horizon,
        "cut_date": cut_date,
        "score_edges": edges,
        "tau": tau,
        "t_min": t_min,
        "tau_scan": scan,
        "is": {"baseline": baseline_is, "model": best},
        "oos": {"baseline": baseline_oos, "model": model_oos, "model_loose": loose_oos},
        "lift": {
            "avg_ret": round((model_oos.get("avg_ret", 0.0) or 0.0)
                             - (baseline_oos.get("avg_ret", 0.0) or 0.0), 4),
            "win_rate": round((model_oos.get("win_rate", 0.0) or 0.0)
                              - (baseline_oos.get("win_rate", 0.0) or 0.0), 3),
            "p05": round((model_oos.get("p05", 0.0) or 0.0)
                         - (baseline_oos.get("p05", 0.0) or 0.0), 4),
            "max_dd": round((model_oos.get("max_dd", 0.0) or 0.0)
                            - (baseline_oos.get("max_dd", 0.0) or 0.0), 4),
        },
    }


# ---------------------------------------------------------------------------
# 4) 对外入口：标定完整模型
# ---------------------------------------------------------------------------
def fit_exit_model(kline_map: dict, params: dict,
                   horizon: "int | None" = None,
                   min_bucket_n: int = MIN_BUCKET_N,
                   oos_frac: float = OOS_FRAC,
                   shrink_k: float = SHRINK_K) -> "dict | None":
    """标定离场模型：状态表（全样本）+ 样本外验证结论。"""
    horizon = int(horizon or params.get("backtest", {}).get("hold_days", 5))
    panel = build_exit_panel(kline_map, params, horizon)
    if panel.empty:
        return None
    edges = _score_edges(panel["score"].tolist())
    cal = calibrate_states(panel, edges, min_bucket_n, shrink_k)
    if not cal.get("states"):
        return None
    val = evaluate_exit_policy(panel, horizon, oos_frac, min_bucket_n, shrink_k)
    tau = float(val["tau"]) if val else 0.0
    t_min = (val.get("t_min") if val else None)
    symbols = panel.groupby(["market", "code"]).ngroups
    out = {
        "horizon": horizon,
        "min_bucket_n": min_bucket_n,
        "shrink_k": shrink_k,
        "score_edges": edges,
        "tau": tau,
        "t_min": t_min,
        "states": cal["states"],
        "coarse": cal["coarse"],
        "global_v": cal["global_v"],
        "n_obs": cal["n_obs"],
        "n_trades": cal["n_trades"],
        "n_symbols": int(symbols),
        "date_range": [str(panel["date"].min()), str(panel["date"].max())],
        "validation": val,
    }
    return out


# ---------------------------------------------------------------------------
# 6) 离场规则体检：止损 / 止盈 / 持有期的参数敏感性
# ---------------------------------------------------------------------------
def _collect_signals(kline_map: dict, params: dict, max_h: int,
                     with_date: bool = False) -> list:
    """一次遍历 K 线，收集所有低吸信号及其建仓后的收盘价序列（供多组参数复用）。

    with_date=True 时返回 (信号日, 建仓价, 后续收盘价序列)——
    做 walk-forward 参数选型必须有日期才能切样本内/外。
    """
    thr = float(params["score_threshold"])
    w = params["weights"]
    sigs = []
    for key, k in (kline_map or {}).items():
        if k is None or len(k) < WARMUP + max_h + 5:
            continue
        try:
            kk = add_indicators(k, params).reset_index(drop=True)
        except Exception:
            continue
        if "date" not in kk.columns:
            continue
        prev_hist = kk["macd_hist"].shift(1).to_numpy(dtype=float)
        close = kk["close"].to_numpy(dtype=float)
        open_ = kk["open"].to_numpy(dtype=float)
        n = len(kk)
        cols = [c for c in kk.columns if c != "date"]
        i = WARMUP
        while i < n - max_h - 1:
            hp = prev_hist[i]
            if not np.isfinite(hp):
                i += 1
                continue
            row = {c: kk.iloc[i][c] for c in cols}
            row["macd_hist_prev"] = float(hp)
            if score_row(row, w)["score"] < thr:
                i += 1
                continue
            entry = float(open_[i + 1])
            if not (np.isfinite(entry) and entry > 0):
                i += 1
                continue
            seg = close[i + 1: i + 1 + max_h]
            if len(seg) == max_h and np.all(np.isfinite(seg)):
                if with_date:
                    sigs.append((str(kk.iloc[i]["date"])[:10], entry, seg))
                else:
                    sigs.append((entry, seg))
            i += 1
    return sigs


def _summ(a) -> dict:
    a = np.asarray(a, dtype=float)
    sd = float(a.std(ddof=1)) if len(a) > 1 else 0.0
    return {"n": int(len(a)), "avg": round(float(a.mean()), 4),
            "med": round(float(np.median(a)), 4),
            "win": round(float((a > 0).mean()), 3),
            "p05": round(float(np.percentile(a, 5)), 4),
            "worst": round(float(a.min()), 4),
            "sd": round(sd, 4),
            "sharpe": round(float(a.mean()) / sd, 3) if sd > 1e-9 else None}


def _simulate_rule(sigs: list, horizon: int, stop, take, return_days: bool = False):
    """对信号集应用「止损/止盈/持有到期」规则，返回每笔收益。

    兼容两种信号格式：(entry, seg) 与 (date, entry, seg)。

    `return_days=True` 时额外返回每笔的**实际持有交易日数**。这是跨持有期比较的前提：
    不同持有期的「平均收益」不可直接比——持有 8 日的组合天然比持有 5 日的多吃 3 天
    市场漂移，看起来更优，实则只是敞口更大。必须折算成日均收益再比。
    """
    n = len(sigs)
    out = np.empty(n, dtype=float)
    held = np.empty(n, dtype=float)
    for idx, item in enumerate(sigs):
        if len(item) == 3:
            _d, entry, seg = item
        else:
            entry, seg = item
        px, nd = None, 0
        for j, c in enumerate(seg[:horizon]):
            r = c / entry - 1.0
            if stop is not None and r <= stop:
                px, nd = c, j + 1
                break
            if take is not None and r >= take:
                px, nd = c, j + 1
                break
        if px is None:
            nd = min(horizon, len(seg))
            px = seg[nd - 1]
        out[idx] = px / entry - 1.0
        held[idx] = max(1, nd)
    return (out, held) if return_days else out


def parameter_scan(kline_map: dict, params: dict,
                   horizons=(5, 8, 10),
                   stops=(-0.03, -0.05, -0.08, None),
                   takes=(0.05, 0.08, 0.15, None)) -> "dict | None":
    """离场规则体检：单次信号遍历 + 多组参数复用，回答「止损/止盈/持有期该怎么设」。

    返回结构：
      n_signals / baseline(当前参数) / by_horizon / by_stop / by_take
    口径说明：信号独立采样（不做交易去重），因此是**相对比较**，
    不代表实盘组合收益；所有组合使用同一信号集，横向可比。
    """
    bt = params.get("backtest", {})
    cur_h = int(bt.get("hold_days", 5))
    cur_s = float(bt.get("stop_loss", -0.05))
    cur_t = float(bt.get("take_profit", 0.08))
    sigs = _collect_signals(kline_map, params, max(horizons))
    if len(sigs) < 50:
        return None
    base = _summ(_simulate_rule(sigs, cur_h, cur_s, cur_t))
    return {
        "n_signals": len(sigs),
        "current": {"horizon": cur_h, "stop": cur_s, "take": cur_t},
        "baseline": base,
        "by_horizon": [
            {"horizon": h, "stop": cur_s, "take": cur_t,
             **_summ(_simulate_rule(sigs, h, cur_s, cur_t))} for h in horizons],
        "by_stop": [
            {"stop": s, "horizon": cur_h, "take": cur_t,
             **_summ(_simulate_rule(sigs, cur_h, s, cur_t))} for s in stops],
        "by_take": [
            {"take": t, "horizon": cur_h, "stop": cur_s,
             **_summ(_simulate_rule(sigs, cur_h, cur_s, t))} for t in takes],
    }


# ---------------------------------------------------------------------------
# 7) 对当前持仓给出卖出建议
# ---------------------------------------------------------------------------
ADVICE_LABEL = {
    "SELL": "建议卖出",
    "WATCH": "观察/减仓",
    "HOLD": "继续持有",
}


def exit_advice(positions: list, model: "dict | None", params: dict) -> list:
    """对当前持仓逐只给出离场建议。

    positions: [{"code","name","market","days_held","ret","entry_score",...}]
    返回同长度列表，追加 advice / reason / expect_ret / expect_n / expect_t / level。
    """
    bt = params.get("backtest", {})
    horizon = int(bt.get("hold_days", 5))
    stop = float(bt.get("stop_loss", -0.05))
    take = float(bt.get("take_profit", 0.08))
    states = (model or {}).get("states") or {}
    coarse = (model or {}).get("coarse") or {}
    edges = (model or {}).get("score_edges") or []
    tau = float((model or {}).get("tau", 0.0) or 0.0)
    t_min = (model or {}).get("t_min")
    min_n = int((model or {}).get("min_bucket_n", MIN_BUCKET_N))

    out = []
    for p in positions or []:
        d = int(p.get("days_held") or 0)
        r = float(p.get("ret") or 0.0)
        sc = p.get("entry_score")
        st, lvl = _lookup(d, r, sc, states, coarse, edges, min_n) if states else (None, None)
        rec = {
            "code": p.get("code"), "name": p.get("name"), "market": p.get("market"),
            "days_held": d, "ret": round(r, 4),
            "entry_score": sc,
            "last_price": p.get("last_price"), "entry_price": p.get("entry_price"),
            "entry_date": p.get("entry_date"),
            "r_bucket": r_label(_rbucket(r)),
            "expect_ret": st.get("v") if st else None,
            "expect_n": st.get("n") if st else None,
            "expect_t": st.get("v_t") if st else None,
            "expect_mu1": st.get("mu1") if st else None,
            "level": lvl,
            "tau": tau,
        }
        if d >= horizon:
            rec.update(advice="SELL", reason=f"持有到期（{d}≥{horizon} 个交易日）")
        elif d <= 0:
            rec.update(advice="HOLD", reason="新开仓（T+0），自下一交易日起进入离场评估")
        elif r <= stop:
            rec.update(advice="SELL", reason=f"触发止损（{r * 100:.1f}% ≤ {stop * 100:.0f}%）")
        elif r >= take:
            rec.update(advice="SELL", reason=f"达成止盈（{r * 100:.1f}% ≥ {take * 100:.0f}%）")
        elif st is None:
            rec.update(advice="HOLD", reason="无可用状态样本，维持持有")
        elif st["n"] < min_n:
            rec.update(advice="HOLD",
                       reason=f"状态样本不足（n={st['n']}<{min_n}），不下提前离场结论"
                              f"（参考 E={st['v'] * 100:+.2f}%）")
        else:
            # 分层判定：**幅度**（E < τ）决定是否要动，**可信度**（t 值）决定动到什么程度。
            # 两者必须同时看：E 在 ±0.05% 量级的偏差全是噪声，
            # 只按 E<τ 判卖会出现「浮盈 +1.8% 也建议清仓」的荒谬结论。
            # t_min 由样本内扫描选出、样本外验证（见 report 第七章），不写死。
            v, t = st["v"], st.get("v_t")
            tn = "t 不可算" if t is None else f"t={t:+.2f}"
            if v >= tau:
                rec.update(advice="HOLD",
                           reason=f"继续持有期望为正（E={v * 100:+.2f}%，"
                                  f"τ={tau * 100:+.1f}%，n={st['n']}）")
            elif t is not None and t_min is not None and t <= t_min:
                rec.update(advice="SELL",
                           reason=f"继续持有期望为负且达可信门槛（E={v * 100:+.2f}% < "
                                  f"τ={tau * 100:+.1f}%，{tn}≤{t_min:+.1f}，n={st['n']}）")
            elif t is not None and t <= NEG_T_WEAK:
                rec.update(advice="SELL",
                           reason=f"继续持有期望为负且弱显著（E={v * 100:+.2f}% < "
                                  f"τ={tau * 100:+.1f}%，{tn}≤{NEG_T_WEAK:+.1f}，n={st['n']}）")
            else:
                rec.update(advice="WATCH",
                           reason=f"期望为负但可信度不足（E={v * 100:+.2f}%，{tn}，"
                                  f"未达门槛 {t_min if t_min is not None else NEG_T_WEAK:+.1f}，"
                                  f"n={st['n']}）——建议盯盘/收紧止损，不构成独立卖出依据")
        out.append(rec)

    order = {"SELL": 0, "WATCH": 1, "HOLD": 2}
    out.sort(key=lambda x: (order.get(x["advice"], 3),
                            x["expect_ret"] if x["expect_ret"] is not None else 0.0))
    return out
