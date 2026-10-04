# -*- coding: utf-8 -*-
"""长期价值轨：推荐存档、前向跟踪、无前视历史回放。

三件事：

1. **推荐存档**（save_picks）——每日长期推荐连六维分数一起落盘，
   供未来验证。分数必须存下来，否则明天无法回答「得分高的到底跑赢了没有」。

2. **前向跟踪**（settle_tracking）——对每批历史推荐，按里程碑
   5/20/60/120/250 个交易日计算收益，并同时算基准（沪深300ETF 510300）
   同期收益，得到**超额收益**。长期投资只看绝对收益会被市场 beta 骗，
   必须看超额。

3. **无前视历史回放**（historical_replay）——不等一年才知道模型行不行。
   在过去三年里取若干历史时点，只用**当时已经能看到的数据**重算六维分，
   再看后续 20/60/120 日收益，算截面 IC。
   无前视的三条硬约束（这是整个项目最容易出错的地方）：
     a. 财务只取 `NOTICE_DATE <= t` 的报告期——用公告日而非报告期截止日，
        否则会用上还没公布的业绩；
     b. 估值分位只用 t 之前的 K 线，且用**不复权**价（前复权会篡改历史）；
     c. IC 必须**先按交易日分组**再算相关，直接对混合样本求相关会把
        截面差异误当预测力（qlib/contrib/eva/alpha.py::calc_ic 的口径）。

历史回放的宏观维度刻意不参与：宏观择时对长期选股的增量贡献远小于选股本身，
且公布值序列的时点对齐容易出错，与其做一个半可信的版本，不如边界划干净。
"""
import glob
import json
import os
from datetime import date, datetime

import numpy as np
import pandas as pd

import fundamentals as fd
import value_strategy as vs

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
STATE = os.path.join(ROOT, "state")
LONGS_DIR = os.path.join(STATE, "longterm")
os.makedirs(LONGS_DIR, exist_ok=True)

OUTCOMES = os.path.join(STATE, "longterm_outcomes.csv")
FACTORS_6 = ["v_quality", "v_growth", "v_cashflow", "v_balance",
             "v_valuation", "v_macro"]
FACTORS_CORE = ["v_quality", "v_growth", "v_cashflow", "v_balance", "v_valuation"]
MILESTONES_DEFAULT = [5, 20, 60, 120, 250]


# ---------------------------------------------------------------------------
# 1) 推荐存档
# ---------------------------------------------------------------------------
def save_picks(picks: dict, macro_env: dict, date_str: str = None) -> str:
    """落盘当日长期推荐（含宏观环境与全部因子分）。"""
    d = date_str or date.today().isoformat()
    path = os.path.join(STATE, f"longterm_picks_{d}.json")
    stocks = picks.get("stocks")
    funds = picks.get("funds")
    rec = {
        "date": d, "ts": datetime.now().isoformat(),
        "macro": {k: macro_env.get(k) for k in
                  ("score", "label", "equity_stance", "style_bias", "missing",
                   "items", "pmi", "cpi", "money", "market")},
        "stats": picks.get("stats"),
        "stocks": (stocks.to_dict("records") if isinstance(stocks, pd.DataFrame)
                   and not stocks.empty else []),
        "funds": (funds.to_dict("records") if isinstance(funds, pd.DataFrame)
                  and not funds.empty else []),
    }
    with open(path, "w", encoding="utf-8") as f:
        json.dump(rec, f, ensure_ascii=False, indent=2, default=str)
    return path


def load_history(before: str = None) -> list:
    files = sorted(glob.glob(os.path.join(STATE, "longterm_picks_*.json")))
    out = []
    for p in files:
        d = os.path.basename(p).split("longterm_picks_")[-1][:10]
        if before and d >= before:
            continue
        try:
            with open(p, encoding="utf-8") as f:
                out.append(json.load(f))
        except Exception:
            continue
    return out


# ---------------------------------------------------------------------------
# 2) 前向跟踪结算
# ---------------------------------------------------------------------------
def _index_on_or_after(k: pd.DataFrame, d: str):
    if k is None or k.empty:
        return None
    dates = k["date"].astype(str).str.slice(0, 10)
    hit = dates[dates >= d]
    return int(hit.index[0]) if len(hit) else None


def settle_tracking(kline_fn, cfg: dict, today: str = None,
                    progress=None) -> dict:
    """对历史长期推荐按里程碑结算收益与超额。返回汇总统计。"""
    today = today or date.today().isoformat()
    miles = (cfg.get("longterm", {}) or {}).get("track_milestones",
                                                MILESTONES_DEFAULT)
    bench_code = (cfg.get("longterm", {}) or {}).get("benchmark", "510300")
    hist = load_history(before=today)
    if not hist:
        return {"batches": 0, "rows": [], "summary": {},
                "note": "尚无历史长期推荐（本次为首批，从下一个交易日起可结算 5 日里程碑）"}

    try:
        bk = kline_fn(bench_code, "etf")
    except Exception:
        bk = None
    bk_dates = (bk["date"].astype(str).str.slice(0, 10).tolist()
                if bk is not None and not bk.empty else [])

    rows = []
    # 已完成结算的里程碑不重复写
    done = set()
    if os.path.exists(OUTCOMES):
        try:
            prev = pd.read_csv(OUTCOMES)
            if {"date", "code", "milestone"} <= set(prev.columns):
                done = {(str(a), str(b), int(c)) for a, b, c in
                        zip(prev["date"], prev["code"], prev["milestone"])}
        except Exception:
            done = set()

    for batch in hist:
        bdate = batch.get("date")
        items = (batch.get("stocks") or []) + (batch.get("funds") or [])
        for it in items:
            code, market = str(it.get("code")), it.get("market")
            if market == "fund":
                continue                          # 场外基金按净值单独跟踪，见下
            try:
                k = kline_fn(code, market)
            except Exception:
                k = None
            if k is None or k.empty:
                continue
            i0 = _index_on_or_after(k, bdate)
            if i0 is None:
                continue
            entry = float(k.iloc[i0]["close"])
            if entry <= 0:
                continue
            for n in miles:
                if i0 + n >= len(k):
                    continue                      # 里程碑尚未到期
                key = (bdate, code, int(n))
                if key in done:
                    continue
                px = float(k.iloc[i0 + n]["close"])
                ret = px / entry - 1
                b_ret = None
                if bk_dates:
                    j0, j1 = _index_on_or_after(bk, bdate), None
                    if j0 is not None and j0 + n < len(bk):
                        j1 = j0 + n
                        b_entry = float(bk.iloc[j0]["close"])
                        if b_entry > 0:
                            b_ret = float(bk.iloc[j1]["close"]) / b_entry - 1
                rows.append({
                    "date": bdate, "code": code, "market": market,
                    "name": it.get("name"), "milestone": int(n),
                    "score": it.get("score"),
                    **{f: it.get(f) for f in FACTORS_6},
                    "ret": round(ret, 5),
                    "bench_ret": (round(b_ret, 5) if b_ret is not None else None),
                    "excess": (round(ret - b_ret, 5) if b_ret is not None else None),
                })
            if progress:
                progress(f"长期跟踪结算 {it.get('name')}", 0, 1)

    if rows:
        df = pd.DataFrame(rows)
        df.to_csv(OUTCOMES, mode="a", header=not os.path.exists(OUTCOMES),
                  index=False, encoding="utf-8")
    else:
        df = pd.DataFrame()

    # 汇总（含历史已落盘的记录）
    summary = {}
    if os.path.exists(OUTCOMES):
        try:
            allrows = pd.read_csv(OUTCOMES)
            for n, g in allrows.groupby("milestone"):
                r = pd.to_numeric(g["ret"], errors="coerce").dropna()
                e = pd.to_numeric(g.get("excess"), errors="coerce").dropna() \
                    if "excess" in g.columns else pd.Series(dtype=float)
                if not len(r):
                    continue
                summary[int(n)] = {
                    "n": int(len(r)),
                    "avg_ret": round(float(r.mean()) * 100, 2),
                    "win_rate": round(float((r > 0).mean()) * 100, 1),
                    "avg_excess": (round(float(e.mean()) * 100, 2) if len(e) else None),
                    "excess_win": (round(float((e > 0).mean()) * 100, 1)
                                   if len(e) else None),
                }
        except Exception:
            pass
    return {"batches": len(hist), "new_rows": len(rows),
            "covered": len(df), "summary": summary}


# ---------------------------------------------------------------------------
# 3) 无前视历史回放
# ---------------------------------------------------------------------------
def _asof_row(rows: list, t: str):
    """取「公告日 <= t 的最近一期」财务行（无前视的关键）。"""
    best = None
    for r in rows or []:
        nd = str(r.get("NOTICE_DATE") or r.get("REPORT_DATE") or "")[:10]
        if not nd or nd > t:
            continue
        if best is None or nd > best[0]:
            best = (nd, r)
    return best[1] if best else None, (best[0] if best else None)


def _rows_upto(rows: list, t: str) -> list:
    return [r for r in (rows or [])
            if str(r.get("NOTICE_DATE") or r.get("REPORT_DATE") or "")[:10] <= t]


def replay_score(rows: list, k_raw: pd.DataFrame, t: str,
                 weights: dict = None) -> dict | None:
    """在历史时点 t 重算五维分（不含宏观）。只用 t 之前可得的信息。"""
    rows_t = _rows_upto(rows, t)
    if len(rows_t) < 4:
        return None
    m = fd.derived_metrics(rows_t)
    if not m or (m.get("n_annual") or 0) < 1:
        return None
    if k_raw is None or k_raw.empty:
        return None
    k = k_raw[k_raw["date"] <= pd.Timestamp(t)]
    if len(k) < 120:
        return None
    price = float(k["close"].iloc[-1])
    if price <= 0:
        return None
    vp = fd.valuation_percentile(k, rows_t)
    m["pb_pct"] = vp.get("pb_pct")
    m["px_pct"] = vp.get("px_pct")
    eps_ttm = m.get("eps_ttm")
    m["pe_ttm"] = (price / eps_ttm) if (eps_ttm and eps_ttm > 0) else None
    m["price"] = price
    m["div_yield"] = None          # 历史股息率需按时点截断分红记录，回放中不参与
    s = vs.score_longterm(m, {}, weights)
    # 回放不含宏观维度：把宏观的权重按比例分给其余五维
    five = {k2: s[f"v_{k2}"] for k2 in ("quality", "growth", "cashflow",
                                        "balance", "valuation")}
    avail = {k2: v for k2, v in five.items() if v is not None}
    if not avail:
        return None
    base_w = dict(vs.DEFAULT_WEIGHTS)
    base_w.pop("macro", None)
    tw = sum(base_w[k2] for k2 in avail)
    total = sum(avail[k2] * base_w[k2] for k2 in avail) / tw if tw else None
    return {"score": round(total, 1) if total is not None else None,
            **{f"v_{k2}": round(v, 1) for k2, v in five.items()},
            "price": price, "pe_ttm": m["pe_ttm"], "pb_pct": m["pb_pct"],
            "report_date": m.get("report_date")}


def historical_replay(items: list, kline_raw_fn, fin_cache: dict,
                      cfg: dict, params: dict, n_periods: int = 26,
                      step: int = 20, min_history: int = 260,
                      progress=None) -> dict:
    """历史时点回放：产出「因子分 vs 未来收益」的截面 IC 表。

    items: [{"code","name","market"}]；fin_cache: {code: rows}（未命中则现拉）
    """
    miles = [20, 60, 120]
    records = []
    for idx, it in enumerate(items):
        code, market = str(it["code"]), it.get("market", "cn")
        try:
            k = kline_raw_fn(code, market)
        except Exception:
            k = None
        if k is None or len(k) < min_history + max(miles) + 5:
            continue
        rows = fin_cache.get(code)
        if rows is None:
            rows = fd.fetch_financial(code, market)
            fin_cache[code] = rows
        if not rows:
            continue
        k = k.sort_values("date").reset_index(drop=True)
        # 时点：从 min_history 起，每 step 个交易日一个，取最近 n_periods 个
        idxs = list(range(min_history, len(k) - max(miles), step))[-n_periods:]
        for i in idxs:
            t = str(k.iloc[i]["date"])[:10]
            sc = replay_score(rows, k.iloc[: i + 1], t, params.get("longterm", {}).get("weights"))
            if not sc or sc.get("score") is None:
                continue
            entry = float(k.iloc[i]["close"])
            if entry <= 0:
                continue
            rec = {"date": t, "code": code, "name": it.get("name"),
                   "market": market, **{f: sc.get(f) for f in FACTORS_CORE},
                   "score": sc["score"]}
            ok = True
            for n in miles:
                j = i + n
                if j >= len(k):
                    ok = False
                    break
                rec[f"ret_{n}"] = round(float(k.iloc[j]["close"]) / entry - 1, 5)
            if ok:
                records.append(rec)
        if progress:
            progress(f"历史回放 {it.get('name')}", idx + 1, len(items))
    if not records:
        return {"n_obs": 0, "n_periods": 0, "factors": {}, "milestones": {}}
    df = pd.DataFrame(records)
    horizons = {}
    for n in miles:
        col = f"ret_{n}"
        sub = df.dropna(subset=[col])
        if len(sub) < 30:
            continue
        horizons[n] = cross_section_ic(sub, FACTORS_CORE + ["score"], col)
    # 全局 IC（所有里程碑的合并视角，用 60 日为代表）
    return {"n_obs": int(len(df)), "n_periods": int(df["date"].nunique()),
            "n_symbols": int(df["code"].nunique()),
            "date_range": [str(df["date"].min()), str(df["date"].max())],
            "milestones": horizons,
            "panel": df}


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


def cross_section_ic(df: pd.DataFrame, factors: list, ret_col: str,
                     min_n: int = 5) -> dict:
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
        out[f] = {"ic_mean": round(mean, 4),
                  "ic_std": round(sd, 4),
                  "icir": round(mean / sd, 3) if sd > 1e-12 else None,
                  "t": round(t, 2) if t is not None else None,
                  "positive_rate": round(float((a > 0).mean()), 3),
                  "n_periods": len(a)}
    return {"factors": out, "n_periods": n_periods}


# ---------------------------------------------------------------------------
# 4) 长期权重迭代（与短线同纪律：只认统计显著）
# ---------------------------------------------------------------------------
def update_longterm_weights(params: dict, min_samples: int = 40,
                            lr: float = 0.20) -> dict:
    """按「因子分 vs 超额收益」的截面 IC 均值与 t 值更新六维权重。

    与短线轨的 update_weights 不同，这里用**超额收益**（减掉沪深300ETF）
    做标签：长期投资里，靠市场普涨赚的钱不该记在选股模型头上。
    显著性不达标时只做极小幅度调整（tanh 调制），避免用小样本噪声改结构。
    """
    if not os.path.exists(OUTCOMES):
        return {"updated": False, "reason": "尚无长期跟踪样本"}
    try:
        df = pd.read_csv(OUTCOMES)
    except Exception:
        return {"updated": False, "reason": "读取 outcomes 失败"}
    if "excess" not in df.columns:
        return {"updated": False, "reason": "缺少超额收益列"}
    df = df.dropna(subset=["excess"])
    if len(df) < min_samples:
        return {"updated": False, "reason": f"样本不足({len(df)}/{min_samples})"}
    ics = cross_section_ic(df, FACTORS_6, "excess")
    fac = ics.get("factors") or {}
    if not fac:
        return {"updated": False, "reason": "截面样本不足，无法算 IC"}

    lt = params.setdefault("longterm", {})
    w = {k: float(v) for k, v in (lt.get("weights") or vs.DEFAULT_WEIGHTS).items()}
    detail = {}
    for key in list(w.keys()):
        col = f"v_{key}"
        d = fac.get(col)
        if not d:
            detail[key] = {"ic": None, "t": None, "action": "无样本"}
            continue
        ic, t = d["ic_mean"], (d["t"] or 0.0)
        conf = float(np.tanh(abs(t) / 2.0))
        if ic < 0 and abs(t) >= 2.0:
            w[key] = max(0.03, w[key] * 0.5)
            act = "显著负 IC，权重减半"
        else:
            w[key] = max(0.03, min(0.45, w[key] * (1 + lr * conf * np.sign(ic) * abs(ic) * 10)))
            act = f"按 IC 微调（IC={ic:+.3f}, t={t:+.2f}, 置信={conf:.2f}）"
        detail[key] = {"ic": ic, "icir": d.get("icir"), "t": t,
                       "positive_rate": d.get("positive_rate"), "action": act}
    s = sum(w.values())
    w = {k: round(v / s, 4) for k, v in w.items()}
    lt["weights"] = w
    lt["version"] = datetime.now().strftime("%Y%m%d_%H%M")
    params["longterm"] = lt
    pdir = os.path.join(ROOT, "params")
    ver = lt["version"]
    with open(os.path.join(pdir, f"params_longterm_{ver}.json"), "w",
              encoding="utf-8") as f:
        json.dump(params, f, ensure_ascii=False, indent=2)
    with open(os.path.join(pdir, "params.json"), "w", encoding="utf-8") as f:
        json.dump(params, f, ensure_ascii=False, indent=2)
    return {"updated": True, "weights": w, "detail": detail,
            "samples": int(len(df)), "version": ver}


if __name__ == "__main__":
    import json as _json
    cfg = _json.load(open(os.path.join(ROOT, "config.json"), encoding="utf-8"))
    print("里程碑:", cfg["longterm"]["track_milestones"])
    r = settle_tracking(lambda c, m: None, cfg)
    print(_json.dumps({k: v for k, v in r.items() if k != "rows"},
                      ensure_ascii=False, default=str)[:600])
