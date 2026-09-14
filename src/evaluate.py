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


def _latest_picks_file(before: str | None = None):
    files = sorted(glob.glob(os.path.join(STATE, "picks_*.json")))
    if before:
        files = [f for f in files
                 if f.split("picks_")[-1][:10] < before]
    return files[-1] if files else None


def evaluate_previous(fetch_quote_fn, today_str: str) -> dict:
    """评估最近一次历史推荐的次日表现。fetch_quote_fn(code, market)->最新价(或None)"""
    pf = _latest_picks_file(before=today_str)
    if not pf:
        return {"skipped": "没有历史推荐可评估"}
    with open(pf, encoding="utf-8") as f:
        rec = json.load(f)
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
        # 追加到滚动结果文件
        rows = [{ "date": rec.get("date"), "code": r["code"], "market": r["market"],
                  "score": r.get("score"), **{k: r.get(k) for k in FACTORS},
                  "ret": r["ret"]} for r in results]
        oc = os.path.join(STATE, "outcomes.csv")
        df = pd.DataFrame(rows)
        df.to_csv(oc, mode="a", header=not os.path.exists(oc), index=False, encoding="utf-8")
    return summary


def _t_value(r: float, n: int) -> float:
    """相关系数 r 在 n 样本下的双尾 t 统计量（用于判断相关性是否统计显著）。"""
    if n < 3:
        return 0.0
    denom = 1.0 - r * r
    if denom <= 1e-12:
        return float("inf") if r > 0 else float("-inf")
    return r * math.sqrt(n - 2) / math.sqrt(denom)


def update_weights(params: dict, min_samples: int = 20, lr: float = 0.25) -> dict:
    """贝叶斯式在线权重更新（借鉴 R&D-Agent(Q) 分析单元的探索-利用自适应思想）。

    关键改进：不再用固定学习率盲调，而是用相关性的统计显著性（t 值）调制学习率——
      - 相关性不显著（|t|<2）时大幅收敛学习率，避免小样本过拟合（保守/利用）；
      - 相关性显著时放大更新幅度（探索新权重结构）；
      - 显著负相关（对收益有害）的因子直接压到下限 0.05（只保留强因子）。
    返回每因子的相关性、t 值与显著性，使“为何调权”可解释。
    """
    oc = os.path.join(STATE, "outcomes.csv")
    if not os.path.exists(oc):
        return {"updated": False, "reason": "样本不足"}
    df = pd.read_csv(oc)
    df = df.dropna(subset=["ret"]).tail(200)
    if len(df) < min_samples:
        return {"updated": False, "reason": f"样本不足({len(df)}/{min_samples})"}
    corrs, tvals, sig = {}, {}, {}
    for f, key in KEY.items():
        sub = df[[f, "ret"]].dropna()
        if len(sub) > 2 and sub[f].std() > 1e-9:
            r = float(np.corrcoef(sub[f].values, sub["ret"].values)[0, 1])
            t = _t_value(r, len(sub))
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
    # 保存版本
    version = datetime.now().strftime("%Y%m%d_%H%M")
    params["version"] = version
    with open(os.path.join(PARAMS_DIR, f"params_{version}.json"), "w", encoding="utf-8") as f:
        json.dump(params, f, ensure_ascii=False, indent=2)
    with open(os.path.join(PARAMS_DIR, "params.json"), "w", encoding="utf-8") as f:
        json.dump(params, f, ensure_ascii=False, indent=2)
    return {"updated": True,
            "correlations": {k: round(v, 3) for k, v in corrs.items()},
            "tvals": tvals, "significant": sig,
            "new_weights": w, "samples": len(df)}


def save_today_picks(picks: list, market_summary: dict) -> str:
    today = date.today().isoformat()
    path = os.path.join(STATE, f"picks_{today}.json")
    with open(path, "w", encoding="utf-8") as f:
        json.dump({"date": today, "ts": datetime.now().isoformat(),
                   "market_summary": market_summary, "picks": picks},
                  f, ensure_ascii=False, indent=2)
    return path
