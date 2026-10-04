# -*- coding: utf-8 -*-
"""迭代模块：评估昨日推荐的实际表现，在线更新评分权重。

机制：
1. 每次运行把当日推荐写入 state/picks_YYYYMMDD.json（含买入参考价=当日收盘）。
2. 下次运行时，读取最近一份历史推荐，用最新快照/净值计算实际收益。
3. 收益记录滚动追加到 state/outcomes.csv。
4. 当最近 N 笔样本 >= 20 时，按"各因子得分与实际收益的滚动相关性"微调权重：
   w_i <- clip(w_i * (1 + lr * corr_i))，然后归一化，并保存版本历史。
"""
import glob
import json
import os
from datetime import date, datetime

import math

import numpy as np
import pandas as pd

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
STATE = os.path.join(ROOT, "state")
PARAMS_DIR = os.path.join(ROOT, "params")
FACTORS = ["f_boll", "f_rsi", "f_volume", "f_trend", "f_macd", "f_drawdown"]
KEY = {"f_boll": "boll", "f_rsi": "rsi", "f_volume": "volume",
       "f_trend": "trend", "f_macd": "macd", "f_drawdown": "drawdown"}


def _read_outcomes(path: str) -> pd.DataFrame:
    """读滚动结果文件，保证 code/date 按字符串读（避免 002026 被解析成 2026 丢前导零）。

    额外的防重复守卫：历史上若有重复记账（早期版本会盲 append），按 (date, code)
    去重保留最后一条，避免重复样本把 t 值抬到假显著。
    """
    df = pd.read_csv(path, dtype={"code": str, "date": str, "market": str})
    if "date" in df.columns and "code" in df.columns:
        df = df.drop_duplicates(subset=["date", "code"], keep="last").reset_index(drop=True)
    return df


def _latest_picks_file(before: str | None = None):
    files = sorted(glob.glob(os.path.join(STATE, "picks_*.json")))
    if before:
        files = [f for f in files
                 if f.split("picks_")[-1][:10] < before]
    return files[-1] if files else None


def _latest_trade_date(kline_fn) -> str | None:
    """用基准标的（沪深300ETF 510300）的最新 K 线日期，代表「行情已更新到哪一天」。

    这是判断「明天的实际数据是否已经存在」的唯一可靠依据：
    快照里的最新价在盘中/隔夜都可能仍是上一交易日的收盘价，光看它分不出真假。
    """
    if kline_fn is None:
        return None
    try:
        k = kline_fn("510300", "etf")
        if k is None or len(k) == 0:
            return None
        return str(k["date"].iloc[-1])[:10]
    except Exception:
        return None


def evaluate_previous(fetch_quote_fn, today_str: str, kline_fn=None) -> dict:
    """评估最近一次历史推荐的次日表现。fetch_quote_fn(code, market)->最新价(或None)

    新鲜度守卫：只有当行情已经推进到**推荐日之后**才允许复盘。
    否则（例如推荐当天 23:00，或次日凌晨盘前）取到的最新价仍是推荐日自己的收盘价，
    会算出一整批 ret=0 的退化样本进 outcomes.csv —— 这些零样本会把相关性和 t 值
    稀释向 0，是「用假数据做迭代」的典型来源。
    """
    pf = _latest_picks_file(before=today_str)
    if not pf:
        return {"skipped": "没有历史推荐可评估"}
    with open(pf, encoding="utf-8") as f:
        rec = json.load(f)
    picks_date = str(rec.get("date") or "")[:10]
    last_td = _latest_trade_date(kline_fn)
    if last_td and picks_date and last_td <= picks_date:
        return {"skipped": f"行情尚未推进（最新交易日 {last_td} ≤ 推荐日 {picks_date}），"
                           f"无可验证的新数据，跳过复盘"}
    picks = rec.get("picks", [])
    results = []
    for p in picks:
        px = fetch_quote_fn(p["code"], p["market"], p.get("mkt_id"))
        if px is None or not p.get("entry_price"):
            continue
        ret = px / p["entry_price"] - 1
        results.append({**p, "exit_price": px, "ret": round(ret, 4)})
    summary = {"picks_date": rec.get("date"), "count": len(results)}
    if results:
        rets = [r["ret"] for r in results]
        summary.update({
            "avg_ret": round(float(np.mean(rets)), 4),
            "win_rate": round(float(np.mean([r > 0 for r in rets])), 3),
            "best": max(results, key=lambda r: r["ret"]),
            "worst": min(results, key=lambda r: r["ret"]),
            "details": results,
        })
        # 追加到滚动结果文件——按 picks_date 幂等重写：同一天重复运行不会重复记账。
        # 盲目 append 会让同一批样本进两次，把名义 N 翻倍，进而把 t 值抬到「假显著」。
        rows = [{"date": rec.get("date"), "code": str(r["code"]), "market": r["market"],
                 "score": r.get("score"), **{k: r.get(k) for k in FACTORS},
                 "ret": r["ret"]} for r in results]
        oc = os.path.join(STATE, "outcomes.csv")
        new = pd.DataFrame(rows)
        if os.path.exists(oc):
            old = _read_outcomes(oc)
            old = old[old["date"].astype(str) != str(rec.get("date"))]
            out = pd.concat([old, new], ignore_index=True) if not old.empty else new
        else:
            out = new
        out.to_csv(oc, index=False, encoding="utf-8")
    return summary


def _t_value(r: float, n: int) -> float:
    """相关系数 r 在 n 样本下的双尾 t 统计量（用于判断相关性是否统计显著）。

    注意 n 必须是**有效样本量**而非名义笔数：同日推荐同涨同跌，名义 N 里真正独立的
    信息只有 N/DEFF。传名义 N 会把 t 值放大 sqrt(DEFF) 倍。
    """
    if n < 3:
        return 0.0
    denom = 1.0 - r * r
    if denom <= 1e-12:
        return float("inf") if r > 0 else float("-inf")
    return r * math.sqrt(n - 2) / math.sqrt(denom)


def _drop_degenerate(df: pd.DataFrame) -> tuple[pd.DataFrame, dict]:
    """剔除无法提供信息的样本，返回 (清洗后样本, 剔除统计)。

    两类必须剔除：
    1. **恒 0 收益日**：某交易日全部标注的 ret 都恰好为 0（实测出现过整日 29/29），
       说明当天取到的不是「次日实际成交价」而是同一时点价格。它们不是「0 收益观测」，
       是**缺失**；留着会把相关性往 0 稀释。
    2. 因子分缺失的行（早期版本的推荐没写因子列）。
    """
    info = {}
    if df.empty:
        return df, info
    if "date" in df.columns:
        flag = df.groupby("date")["ret"].apply(
            lambda s: bool((s.fillna(0) == 0).all()))
        dead = [d for d, v in flag.items() if v]
        if dead:
            info["degenerate_days"] = sorted(str(d) for d in dead)
            df = df[~df["date"].astype(str).isin({str(d) for d in dead})]
    fcols = [c for c in FACTORS if c in df.columns]
    if fcols:
        before = len(df)
        df = df.dropna(subset=fcols + ["ret"])
        if before != len(df):
            info["dropped_incomplete"] = before - len(df)
    return df.reset_index(drop=True), info


def _design_effect(df: pd.DataFrame) -> tuple[float, int, float]:
    """按交易日分组估设计效应 DEFF = 1 + (m̄-1)·ICC，返回 (deff, 天数, icc)。

    同日推荐同涨同跌，样本高度非独立。实测本项目 ICC≈0.37 → DEFF≈10，
    即 226 笔名义样本里只有约 23 笔的独立信息量。不修正就会把 t 值放大 3.15 倍，
    把纯噪声判成「显著」——这正是本模块历史上反复踩的坑（见 Qlib 引入研究：
    同名结论 ICC=0.390 / DEFF=13.0）。

    组数为 1 或组内无方差时无法估计，退化为 DEFF=1；此时由 min_days 门槛兜底。
    """
    if df.empty or "date" not in df.columns or "ret" not in df.columns:
        return 1.0, int(df["date"].nunique()) if "date" in df.columns else 1, 0.0
    g = df.groupby("date")["ret"]
    sizes = g.size()
    k, n = int(len(sizes)), int(sizes.sum())
    if k < 2 or n <= k:
        return 1.0, k, 0.0
    grand = float(df["ret"].mean())
    ss_between = float(((g.mean() - grand) ** 2 * sizes).sum())
    ss_within = float(g.apply(lambda s: float(((s - s.mean()) ** 2).sum())).sum())
    m = n / k
    ms_between = ss_between / (k - 1)
    ms_within = ss_within / (n - k)
    if ms_within <= 1e-15:
        icc = 1.0
    else:
        var_between = max(0.0, (ms_between - ms_within) / m)
        icc = float(np.clip(var_between / (var_between + ms_within), 0.0, 0.99))
    return float(max(1.0, 1.0 + (m - 1.0) * icc)), k, icc


def update_weights(params: dict, min_samples: int = 20, lr: float = 0.25,
                   min_days: int = 20) -> dict:
    """贝叶斯式在线权重更新（借鉴 R&D-Agent(Q) 分析单元的探索-利用自适应思想）。

    关键改进：不再用固定学习率盲调，而是用相关性的统计显著性（t 值）调制学习率——
      - 相关性不显著（|t|<2）时大幅收敛学习率，避免小样本过拟合（保守/利用）；
      - 相关性显著时放大更新幅度（探索新权重结构）；
      - 显著负相关（对收益有害）的因子直接压到下限 0.05（只保留强因子）。
    返回每因子的相关性、t 值与显著性，使“为何调权”可解释。

    三道证据门禁（缺一条就会让噪声变成「显著」）：
      ① `min_days`：有效交易日数不足则**直接不调权**。9 个交易日里各因子只有各 9 个
         独立观测，任何相关性都不足以支撑改权重。
      ② **退化日剔除**：整天 ret 恒为 0 的交易日是缺失而非观测。
      ③ **DEFF 修正**：t 值用有效样本量 n/DEFF，而不是名义笔数。
    """
    oc = os.path.join(STATE, "outcomes.csv")
    if not os.path.exists(oc):
        return {"updated": False, "reason": "样本不足"}
    df = _read_outcomes(oc)
    df = df.dropna(subset=["ret"])
    if df.empty:
        return {"updated": False, "reason": "样本不足"}
    # 证据诊断先算出来：不论最终是否调权都要能回答「凭多少样本说话」。
    # 只在调权成功时才给出诊断，等于把「为什么不调」变成黑箱。
    full, qinfo = _drop_degenerate(df)
    d_deff, d_days, d_icc = _design_effect(full)
    diag = {"n_days": d_days, "deff": round(d_deff, 2), "icc": round(d_icc, 3),
            "n_eff": int(round(max(3.0, len(full) / d_deff))), "samples": len(full),
            "nominal_samples": len(df)}
    # 幂等守卫：只在「有新样本」时调权。同一天重复运行 / 复跑不会二次调权，
    # 避免权重在没有新信息的情况下连续漂移（调权应对应新证据，而非新的一次执行）。
    max_date = str(df["date"].max())
    last_used = (params.get("iteration") or {}).get("last_used_date")
    if last_used and max_date <= str(last_used):
        return {"updated": False, "data_through": max_date, **diag, **qinfo,
                "reason": f"无新样本（最新 {max_date} 已用于上次迭代），维持现权重"}
    df = full.tail(200)
    if len(df) < min_samples:
        return {"updated": False, "data_through": max_date, **diag, **qinfo,
                "reason": f"样本不足({len(df)}/{min_samples})"}
    deff, n_days, icc = _design_effect(df)
    if n_days < min_days:
        return {"updated": False, "data_through": max_date, **diag, **qinfo,
                "reason": f"有效交易日不足({n_days}/{min_days})，"
                          f"不足以支撑调权（维持现权重）"}
    n_eff = max(3.0, len(df) / deff)
    corrs, tvals, sig = {}, {}, {}
    for f, key in KEY.items():
        sub = df[[f, "ret"]].dropna()
        if len(sub) > 2 and sub[f].std() > 1e-9:
            r = float(np.corrcoef(sub[f].values, sub["ret"].values)[0, 1])
            # 用有效样本量算 t，而不是名义笔数
            t = _t_value(r, int(round(n_eff)))
            corrs[key] = round(float(np.clip(r, -0.5, 0.5)), 3)
            tvals[key] = round(t, 2)
            sig[key] = abs(t) >= 2.0
        else:
            corrs[key] = 0.0
            tvals[key] = 0.0
            sig[key] = False
    w = {k: float(v) for k, v in params["weights"].items()}
    for key in w:
        r = corrs[key]
        t = abs(tvals[key])
        conf = math.tanh(t / 2.0)            # 置信度 0~1：|t| 越大越接近 1
        eff_lr = lr * conf                    # 贝叶斯式调制：不显著则保守
        if r < 0 and abs(tvals[key]) >= 2.0:
            w[key] = 0.05                     # 显著负相关：因子有害，压到下限
        else:
            w[key] = max(0.05, min(0.45, w[key] * (1 + eff_lr * r)))
    s = sum(w.values())
    for key in w:
        w[key] = round(w[key] / s, 4)
    # 关键：把算出的新权重写回 params，否则落盘的永远是旧权重——
    # 版本号会照升，但数字一动不动，整个调权闭环静默失效。
    params["weights"] = w
    # 保存版本：记录本次用于迭代的最新样本日期，作为下次的幂等基线
    version = datetime.now().strftime("%Y%m%d_%H%M")
    params["version"] = version
    it = params.setdefault("iteration", {})
    it["last_used_date"] = max_date
    it["last_diag"] = {"n_days": n_days, "deff": round(deff, 2),
                       "icc": round(icc, 3), "n_eff": int(round(n_eff)),
                       "samples": len(df)}
    with open(os.path.join(PARAMS_DIR, f"params_{version}.json"), "w", encoding="utf-8") as f:
        json.dump(params, f, ensure_ascii=False, indent=2)
    with open(os.path.join(PARAMS_DIR, "params.json"), "w", encoding="utf-8") as f:
        json.dump(params, f, ensure_ascii=False, indent=2)
    return {"updated": True,
            "correlations": {k: round(v, 3) for k, v in corrs.items()},
            "tvals": tvals, "significant": sig,
            "new_weights": w, "samples": len(df), "data_through": max_date,
            "n_days": n_days, "deff": round(deff, 2), "icc": round(icc, 3),
            "n_eff": int(round(n_eff)), "nominal_samples": diag["nominal_samples"],
            **qinfo}


def save_today_picks(picks: list, market_summary: dict, data_date: str | None = None) -> str:
    """落盘当日推荐。

    `data_date` 是**行情数据对应的交易日**，不是墙钟日期。盘前或非交易日运行时
    最新 K 线仍是上一交易日的，若用墙钟日期命名，会得到一份「日期是今天、价格是
    昨天」的推荐——次日复盘时把两天的涨跌当成一天记，收益被系统性放大。
    """
    today = data_date or date.today().isoformat()
    path = os.path.join(STATE, f"picks_{today}.json")
    with open(path, "w", encoding="utf-8") as f:
        json.dump({"date": today, "ts": datetime.now().isoformat(),
                   "market_summary": market_summary, "picks": picks},
                  f, ensure_ascii=False, indent=2)
    return path
