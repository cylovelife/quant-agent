#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""投资助手 Agent 主控入口。

用法：
  python run.py              # 完整流程：迭代评估 -> 抓数据 -> 评分 -> 回测 -> 报告
  python run.py --quick      # 快速模式：减少回测标的数量
  python run.py --no-eval    # 跳过昨日推荐评估
  python run.py --market cn  # 只跑指定市场（cn/etf/hk/us/fund，可逗号分隔）
"""
import argparse
import json
import os
import socket
import sys
import time
import traceback
from datetime import datetime

socket.setdefaulttimeout(20)  # 全局兜底，防止任何请求无限挂起

import pandas as pd

ROOT = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(ROOT, "src"))

import fetcher  # noqa: E402
import report as rpt  # noqa: E402
from backtest import backtest_candidates, factor_ic  # noqa: E402
from evaluate import evaluate_previous, save_today_picks, update_weights  # noqa: E402
from indicators import factor_collinearity  # noqa: E402
from strategy import prefilter, score_candidates  # noqa: E402

LOG = os.path.join(ROOT, "logs")


def log(msg: str):
    line = f"[{datetime.now().strftime('%H:%M:%S')}] {msg}"
    print(line, flush=True)
    with open(os.path.join(LOG, f"run_{datetime.now().strftime('%Y%m%d')}.log"), "a",
              encoding="utf-8") as f:
        f.write(line + "\n")


def load_configs():
    with open(os.path.join(ROOT, "params", "params.json"), encoding="utf-8") as f:
        params = json.load(f)
    with open(os.path.join(ROOT, "config.json"), encoding="utf-8") as f:
        cfg = json.load(f)
    return params, cfg


def quote_fn(code: str, market: str, mkt_id=None):
    """供迭代评估用：取某标的最新价。"""
    try:
        if market == "fund":
            nav = fetcher.fetch_fund_nav(code, limit=3)
            return float(nav.iloc[-1]["nav"]) if len(nav) else None
        return fetcher.fetch_quote(code, market, mkt_id)
    except Exception:
        return None


def kline_fn(code: str, market: str, mkt_id=None):
    if market == "fund":
        nav = fetcher.fetch_fund_nav(code, limit=300)
        if nav.empty:
            return None
        nav = nav.rename(columns={"nav": "close"})
        nav["open"] = nav["high"] = nav["low"] = nav["close"]
        nav["volume"] = 1.0
        nav["amount"] = 0.0
        return nav
    return fetcher.fetch_kline(code, market, mkt_id=mkt_id)


def market_summary(spot: pd.DataFrame, market: str) -> dict:
    return {
        "count": int(len(spot)),
        "up_ratio": float((spot["pct"] > 0).mean()) if len(spot) else 0.0,
        "median_pct": float(spot["pct"].median()) if len(spot) else 0.0,
        "total_amount": float(spot["amount"].sum() / 1e8) if len(spot) else 0.0,
    }


def _save_manifest(today_picks, summary, ev, iter_info, params, coll, ic, elapsed):
    """运行快照：把本次运行的版本/权重/相关性/显著性/Top候选/市场概览落盘，
    实现全流程可复现留痕（借鉴 R&D-Agent(Q) 规范单元：假设-代码-回测留痕）。"""
    top = {}
    for mkt, df in today_picks.items():
        if df is None or df.empty:
            continue
        top[mkt] = [{"code": str(r["code"]), "name": str(r["name"]),
                     "score": float(r["score"])} for _, r in df.head(3).iterrows()]
    manifest = {
        "date": datetime.now().strftime("%Y-%m-%d"),
        "version": params.get("version"),
        "weights": params.get("weights"),
        "iteration": {
            "updated": iter_info.get("updated"),
            "correlations": iter_info.get("correlations"),
            "tvals": iter_info.get("tvals"),
            "significant": iter_info.get("significant"),
            "samples": iter_info.get("samples"),
        },
        "factor_collinearity": {
            "max_abs": round(coll["max_abs"], 3) if coll else None,
            "flagged": coll["flagged"] if coll else [],
            "n": coll["n"] if coll else 0,
        },
        "factor_ic": ic,
        "market_summary": summary,
        "top_picks": top,
        "elapsed_sec": round(elapsed, 1),
    }
    path = os.path.join(ROOT, "state", f"run_manifest_{manifest['date']}.json")
    with open(path, "w", encoding="utf-8") as f:
        json.dump(manifest, f, ensure_ascii=False, indent=2)
    log(f"运行快照已写入 {path}")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--quick", action="store_true")
    ap.add_argument("--no-eval", action="store_true")
    ap.add_argument("--market", type=str, default=None)
    args = ap.parse_args()

    t0 = time.time()
    params, cfg = load_configs()
    markets = args.market.split(",") if args.market else cfg["markets"]
    bt_top_n = 8 if args.quick else cfg["backtest_top_n"]

    log("=" * 60)
    log(f"投资助手启动 mode={'quick' if args.quick else 'full'} markets={markets}")

    # 1) 迭代：评估昨日推荐 + 更新权重
    ev, iter_info = {}, {"updated": False, "reason": "跳过"}
    if not args.no_eval:
        try:
            log("评估昨日推荐…")
            ev = evaluate_previous(quote_fn, datetime.now().strftime("%Y-%m-%d"))
            log(f"  复盘结果: {json.dumps({k: v for k, v in ev.items() if k != 'details'}, ensure_ascii=False)}")
            iter_info = update_weights(params, **params["iteration"])
            # 权重更新后重新加载
            if iter_info.get("updated"):
                with open(os.path.join(ROOT, "params", "params.json"), encoding="utf-8") as f:
                    params = json.load(f)
                log(f"  权重已更新至 v{params['version']}: {params['weights']}")
        except Exception as e:
            log(f"  迭代评估失败(不影响主流程): {e}")
            traceback.print_exc()

    # 2) 抓取各市场快照 + 粗筛 + K线评分
    today_picks, summary = {}, {}
    for mkt in markets:
        try:
            log(f"处理市场: {mkt}")
            if mkt == "fund":
                picks = run_funds(cfg, params)
                today_picks["fund"] = picks
                continue
            spot = (fetcher.fetch_cn_rank(cfg["filters"]["cn"].get("rank_top", 400))
                    if mkt == "cn" else fetcher.fetch_spot_list(mkt, pages=2))
            if spot.empty:
                log(f"  {mkt} 快照为空，跳过")
                continue
            summary[mkt] = market_summary(spot, mkt)
            if mkt == "cn":
                summary[mkt]["note"] = "跌幅榜Top400样本"
            log(f"  快照 {len(spot)} 条; 上涨占比 {summary[mkt]['up_ratio']:.1%}")
            cand = prefilter(spot, mkt, cfg)
            log(f"  粗筛后候选 {len(cand)} 只")
            budget = cfg["kline_budget"].get(mkt, 20)
            if args.quick:
                budget = min(budget, 10)
            scored = score_candidates(
                cand, mkt, params,
                lambda c, m, mid=None: kline_fn(c, m, mid),
                max_n=budget, progress=lambda i, n: None)
            log(f"  评分通过 {len(scored)} 只")
            if scored.empty:
                today_picks[mkt] = scored
                continue
            scored = backtest_candidates(scored, kline_fn, params, top_n=bt_top_n)
            today_picks[mkt] = scored.head(cfg["top_picks_per_market"])
            if len(today_picks[mkt]):
                log(f"  Top1: {today_picks[mkt].iloc[0]['name']} "
                    f"score={today_picks[mkt].iloc[0]['score']}")
        except Exception as e:
            log(f"  市场处理失败: {e}")
            traceback.print_exc()

    # 3) 保存今日推荐（含因子分数，供明日迭代评估）
    all_picks = []
    for mkt, df in today_picks.items():
        if df is None or df.empty:
            continue
        for _, r in df.iterrows():
            pick = {"code": str(r["code"]), "name": str(r["name"]),
                    "market": mkt, "score": float(r["score"]),
                    "mkt_id": r.get("mkt_id"),
                    "entry_price": float(r["price"])}
            for f in ("f_boll", "f_rsi", "f_volume", "f_trend", "f_macd", "f_drawdown"):
                if f in r and pd.notna(r[f]):
                    pick[f] = float(r[f])
            all_picks.append(pick)
    save_today_picks(all_picks, summary)
    log(f"已保存今日推荐 {len(all_picks)} 条到 state/")

    # 3.5) 诊断：因子共线 + 截面因子预测力（借鉴 R&D-Agent(Q) 验证单元）
    diag_frames = [df for df in today_picks.values() if df is not None and not df.empty]
    coll = factor_collinearity(pd.concat(diag_frames, ignore_index=True)) if diag_frames else None
    ic = factor_ic(pd.concat(diag_frames, ignore_index=True)) if diag_frames else None
    if coll:
        log(f"  因子共线诊断: 最大|r|={coll['max_abs']:.2f}, 冗余对={len(coll['flagged'])}")
    if ic:
        log(f"  截面因子预测力: IC={ic['ic']:+.2f}, RankIC={ic['rank_ic']:+.2f}")

    # 4) 生成报告
    md_path, html_path = rpt.generate_report(today_picks, summary, ev, iter_info, params, coll, ic)
    log(f"报告已生成: {md_path}")
    print("\nREPORT_HTML=" + html_path)
    print("REPORT_MD=" + md_path)

    # 5) 运行快照（全流程留痕，可复现复盘）
    _save_manifest(today_picks, summary, ev, iter_info, params, coll, ic, time.time() - t0)


def run_funds(cfg, params) -> pd.DataFrame:
    """场外基金：按净值序列评分（T-1 数据）。"""
    rows = []
    codes = cfg["fund_watchlist"][: 12]
    for code in codes:
        try:
            k = kline_fn(code, "fund")
            if k is None or len(k) < 70:
                continue
            from indicators import add_indicators
            from strategy import score_row
            k = add_indicators(k, params)
            last, prev = k.iloc[-1], k.iloc[-2]
            d = dict(last)
            d["macd_hist_prev"] = prev["macd_hist"]
            s = score_row(d, params["weights"])
            if s["score"] < params["score_threshold"]:
                continue
            rows.append({
                "code": code, "name": fetcher.fetch_fund_name(code), "market": "fund",
                "price": float(last["close"]), "pct": float(k["close"].pct_change().iloc[-1]) * 100,
                "amount": 0.0, "boll_pb": round(float(last["boll_pb"]), 3),
                "rsi": round(float(last["rsi"]), 1), "vr": float("nan"),
                **s,
            })
        except Exception:
            continue
    df = pd.DataFrame(rows)
    if df.empty:
        return df
    return df.sort_values("score", ascending=False).head(cfg.get("top_fund_picks", 5))


if __name__ == "__main__":
    os.makedirs(LOG, exist_ok=True)
    t0 = time.time()
    try:
        main()
    finally:
        log(f"总耗时 {time.time() - t0:.1f}s")
