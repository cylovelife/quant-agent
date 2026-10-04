#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""投资助手 Agent 主控入口（双轨）。

用法：
  python run.py                  # 完整流程：短线低吸 + 长期价值 + 报告
  python run.py --mode short     # 只跑短线低吸轨
  python run.py --mode long      # 只跑长期价值轨
  python run.py --quick          # 快速模式：减少回测标的数量
  python run.py --no-eval        # 跳过昨日推荐评估
  python run.py --no-replay      # 跳过长期历史回放（省约 1 分钟）
  python run.py --market cn      # 短线轨只跑指定市场（cn/etf/hk/us/fund）
  python run.py --brief          # 报告只留操作清单与结论（完整版另存 .full.*）
  python run.py --full           # 强制完整版报告，覆盖 config.json 的 report.brief

两条轨的分工：
  短线轨（short）：超跌反弹，持有 3~8 日，看价格位置与量能，次日即可验证
  长期轨（long）： 企业质量与买入价格，持有数月到数年，按 1/3/6/12 月验证
"""
import argparse
import contextlib
import json
import os
import socket
import sys
import threading
import time
import traceback
from datetime import datetime

socket.setdefaulttimeout(20)  # 全局兜底，防止任何请求无限挂起

import pandas as pd

ROOT = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(ROOT, "src"))

import csc_source  # noqa: E402
from datasources import akshare_events as ake  # noqa: E402
from datasources import akshare_source as aks  # noqa: E402
from datasources import sina_source as ss  # noqa: E402
from datasources import base as dsbase  # noqa: E402
from datasources.base import drain_missing  # noqa: E402
import fetcher  # noqa: E402
import fundamentals as fnd  # noqa: E402
import macro as mcr  # noqa: E402
import ml_eval  # noqa: E402
import model_select as msel  # noqa: E402
import phase_cache as pc  # noqa: E402
import portfolio  # noqa: E402
import report as rpt  # noqa: E402
import store  # noqa: E402
import value_strategy as vstrat  # noqa: E402
import value_track as vtrack  # noqa: E402
from backtest import backtest_candidates, factor_ic  # noqa: E402
from evaluate import evaluate_previous, save_today_picks, update_weights  # noqa: E402
from exit_model import exit_advice, fit_exit_model, parameter_scan  # noqa: E402
from factor_eval import rolling_factor_ic  # noqa: E402
from indicators import factor_collinearity  # noqa: E402
from strategy import prefilter, score_candidates  # noqa: E402

LOG = os.path.join(ROOT, "logs")


def log(msg: str):
    line = f"[{datetime.now().strftime('%H:%M:%S')}] {msg}"
    print(line, flush=True)
    with open(os.path.join(LOG, f"run_{datetime.now().strftime('%Y%m%d')}.log"), "a",
              encoding="utf-8") as f:
        f.write(line + "\n")


@contextlib.contextmanager
def phase(name: str, every: int = 20):
    """长耗时阶段的包裹器：进入即报开始，期间定时报心跳，退出报真实耗时。

    为什么需要心跳：离场模型标定约 155 秒，期间日志一片空白。没有心跳时，
    「在算」与「被冻」（机器休眠/进程挂起）在外部看起来完全一样——
    项目里就曾把一次休眠冻结误判成「walk-forward 卡死 2 小时」。
    有心跳才能一眼区分：日志还在走 = 在算；日志停住 = 有问题。
    """
    t0 = time.time()
    log(f"  ▶ {name} 开始…")
    stop = threading.Event()

    def _beat():
        while not stop.wait(every):
            log(f"    … {name} 仍在计算（已 {int(time.time() - t0)}s）")

    th = threading.Thread(target=_beat, daemon=True)
    th.start()
    try:
        yield
    finally:
        stop.set()
        log(f"  ✔ {name} 完成（{time.time() - t0:.1f}s）")


def cached_phase(name: str, fn, kline_sig: str, params: dict, extra: dict,
                 no_cache: bool = False):
    """带缓存的阶段执行：键 = K线内容签名 + 参数签名，命中则跳过重算。

    命中与未命中都写日志——绝不静默使用历史结果。缓存写入失败不影响主流程
    （缓存是优化，不是正确性依赖）。
    """
    key = pc.cache_key(name, kline_sig, pc.params_signature(params, extra))
    if not no_cache:
        hit = pc.load(name, key)
        if hit is not None:
            log(f"  ⚡ {name}: 命中缓存（K线与参数未变，跳过重算）")
            return hit
    with phase(name):
        val = fn()
    if val is not None and not no_cache:
        if not pc.save(name, key, val):
            log(f"    （{name} 结果未能写入缓存，下次仍会重算）")
    return val


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


def _as_float(v, default=float("inf")):
    """宽松转 float：排序用。上游指数估值字段是**字符串**（如 "13.5012"），
    直接比大小会得到字典序结果（"9" > "13"），把分位排序整个弄反。"""
    try:
        f = float(str(v).replace("%", "").strip())
    except (TypeError, ValueError):
        return default
    return default if f != f else f


def _age_years(pub, today) -> "float | None":
    """标的/指数成立至今的年限；无日期或解析失败返回 None（区别于「0 年」）。

    分位是**窗口统计量**：用 20 年窗口算出的 4% 分位和用 20 个月算出的 4% 分位
    不是一回事。缺了年限就无从判断，所以宁可返回 None 也不要默认 0。
    """
    s = str(pub or "")[:10]
    if len(s) < 10:
        return None
    try:
        d = datetime.strptime(s, "%Y-%m-%d").date()
    except ValueError:
        return None
    return round((today - d).days / 365.25, 1)


def _save_manifest(today_picks, summary, ev, iter_info, params, coll, ic, rolling, elapsed,
                   exit_info=None, advice=None, param_scan=None,
                   longterm=None, model_selection=None, data_health=None):
    """运行快照：把本次运行的版本/权重/相关性/显著性/Top候选/市场概览落盘，
    实现全流程可复现留痕（借鉴 R&D-Agent(Q) 规范单元：假设-代码-回测留痕）。"""
    top = {}
    for mkt, df in today_picks.items():
        if df is None or df.empty:
            continue
        top[mkt] = [{"code": str(r["code"]), "name": str(r["name"]),
                     "score": float(r["score"])} for _, r in df.head(3).iterrows()]
    exit_block = None
    if exit_info:
        v = exit_info.get("validation") or {}
        exit_block = {
            "tau": exit_info.get("tau"),
            "t_min": exit_info.get("t_min"),
            "global_v": exit_info.get("global_v"),
            "n_symbols": exit_info.get("n_symbols"),
            "n_trades": exit_info.get("n_trades"),
            "n_obs": exit_info.get("n_obs"),
            "n_states": len(exit_info.get("states") or {}),
            "date_range": exit_info.get("date_range"),
            "oos": {k: v.get("oos", {}).get(k) for k in ("baseline", "model")} if v else None,
            "lift": v.get("lift") if v else None,
        }
    advice_block = None
    if advice is not None:
        advice_block = {
            "total": len(advice),
            "sell": sum(1 for a in advice if a["advice"] == "SELL"),
            "watch": sum(1 for a in advice if a["advice"] == "WATCH"),
            "hold": sum(1 for a in advice if a["advice"] == "HOLD"),
            "sell_list": [{"code": a["code"], "name": a["name"], "market": a["market"],
                           "days_held": a["days_held"], "ret": a["ret"],
                           "expect_ret": a.get("expect_ret"), "reason": a["reason"]}
                          for a in advice if a["advice"] == "SELL"],
        }
    long_block = None
    if longterm is not None:
        ms = longterm.get("macro") or {}
        pk = longterm.get("picks") or {}
        dfs, dff = pk.get("stocks"), pk.get("funds")
        long_block = {
            "enabled": longterm.get("enabled"),
            "note": longterm.get("note") or longterm.get("error"),
            "macro": {k: ms.get(k) for k in ("score", "label", "equity_stance",
                                             "style_bias", "missing", "date")},
            "macro_items": ms.get("items"),
            "stats": pk.get("stats"),
            "stocks_top": ([{k: (None if pd.isna(r.get(k)) else r.get(k))
                             for k in ("code", "name", "score", "pe_ttm", "pb_pct",
                                       "div_yield", "roe_avg3", "rev_yoy",
                                       "v_quality", "v_growth", "v_cashflow",
                                       "v_balance", "v_valuation", "v_macro",
                                       "bucket", "style")}
                            for _, r in dfs.head(10).iterrows()]
                           if isinstance(dfs, pd.DataFrame) and not dfs.empty else []),
            "funds_top": ([{k: (None if pd.isna(r.get(k)) else r.get(k))
                            for k in ("code", "name", "score", "sharpe", "max_dd",
                                      "ret_1y", "scale", "scale_chg", "manager",
                                      "f_perf", "f_stability", "f_scale", "f_manager")}
                           for _, r in dff.head(5).iterrows()]
                          if isinstance(dff, pd.DataFrame) and not dff.empty else []),
            "etfs": longterm.get("etfs"),
            "industry_rank": longterm.get("industry_rank") or {},
            # 指数估值只留 PE 分位最低的 10 个进快照——全量 200 条会让 manifest
            # 从几十 KB 涨到几百 KB，而回溯时真正会看的只有「最低的那批」。
            "index_valuation_low": sorted(
                (longterm.get("index_valuation") or []),
                key=lambda x: _as_float(x.get("pePercentile")))[:10],
            "track": longterm.get("track"),
            "replay": longterm.get("replay"),
            "selection": (longterm.get("selection") or {}).get("long"),
            "picks_path": longterm.get("picks_path"),
        }
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
        "factor_rolling_ic": rolling,
        "exit_model": exit_block,
        "exit_advice": advice_block,
        "exit_param_scan": ({"n_signals": param_scan["n_signals"],
                             "current": param_scan["current"],
                             "baseline": param_scan["baseline"],
                             "by_stop": param_scan["by_stop"],
                             "by_take": param_scan["by_take"],
                             "by_horizon": param_scan["by_horizon"]}
                            if param_scan else None),
        "model_selection_short": model_selection,
        "longterm": long_block,
        "market_summary": summary,
        "data_health": data_health,
        "top_picks": top,
        "elapsed_sec": round(elapsed, 1),
    }
    path = os.path.join(ROOT, "state", f"run_manifest_{manifest['date']}.json")
    with open(path, "w", encoding="utf-8") as f:
        json.dump(manifest, f, ensure_ascii=False, indent=2, default=str)
    log(f"运行快照已写入 {path}")


def print_db_stats():
    """打印落盘概览：各表行数、行情覆盖口径与日期跨度、近期数据源健康。"""
    st = store.stats()
    if not st.get("enabled"):
        print("存储层已关闭（QUANT_STORE=file）。当前完全走文件缓存。")
        return
    print(f"数据库: {st['path']}（schema v{st['schema']}，{st['size_mb']} MB）")
    print("\n各表行数:")
    for t in ("kline", "kline_meta", "rank_snapshot", "fin_metrics", "fund_profile",
              "index_snapshot", "index_valuation", "industry_rank", "fund_flow",
              "lhb", "lhb_inst", "margin", "restriction", "delisted",
              "macro_snapshot", "ml_eval", "data_health"):
        print(f"  {t:<18} {st.get(t, 0):>8}")
    cov = st.get("kline_coverage") or []
    print("\n行情覆盖（按口径）:")
    if not cov:
        print("  （空）")
    for c in cov:
        label = {"qfq": "前复权（短线/宏观）", "raw": "不复权（估值分位）",
                 "nav": "场外基金净值（预留）"}.get(c["adjust"], c["adjust"])
        print(f"  {label:<22} {c['syms']:>4} 只  {c['rows']:>7} 根  "
              f"{c['f']} ~ {c['l']}")
    hl = store.health_summary(14)
    print("\n近 14 天数据源健康:")
    if not hl:
        print("  （无记录）")
    for h in hl:
        rate = (h["ok_n"] or 0) / h["n"] if h["n"] else 0
        print(f"  {h['source']:<20} {h['market']:<6} 调用 {h['n']:>4} 次  "
              f"成功率 {rate:>5.1%}  均耗时 {h['avg_elapsed']}s  "
              f"累计条目 {h['items'] or 0}")
    anom = store.recent_anomalies(8)
    print("\n最近的归一异常（抓取成功但字段开始解析不出来——上游改版的前兆）:")
    if not anom:
        print("  （无）")
    for r in anom:
        print(f"  {r['run_date']} {r['source']:<22} {r['market']:<5} "
              f"n={r['n_items']}  {r['anomalies']}")

    cov2 = store.index_valuation_coverage()
    print("\n指数估值序列覆盖:")
    if not cov2:
        print("  （无记录）")
    for c in cov2:
        wd = c.get("window_days") or 0
        win = f"{wd / 365:.0f}y" if wd else "未知"
        print(f"  {c['index_code']}.{c['market']} {c['metric']:<3} 窗口 {win:<5} "
              f"{c['n']:>5} 点  {c['first_date']} ~ {c['last_date']}")
    # 新数据源覆盖：与 `--new-sources` 打印的是同一段（单点产出，避免两个入口
    # 各写一份、口径慢慢分叉）。
    print_new_source_coverage()

    mh = store.ml_eval_history(6)
    if mh:
        print("\n走前 ML 验证（最近）:")
        for r in mh:
            print(f"  {r['run_date']} {r['ret_col']:<8} {r['model']:<6} "
                  f"IC {r['ic_mean']} / 规则 {r['rule_ic_mean']}  "
                  f"门禁 {'通过' if r['gate_passed'] else '未过'}")

    hist = store.macro_history(10)
    print("\n宏观分历史（近 10 次运行，新→旧）:")
    if not hist:
        print("  （无记录）")
    for r in hist:
        lo, hi = r.get("stance_lo"), r.get("stance_hi")
        pos = f"{lo * 100:.0f}%~{hi * 100:.0f}%" if lo is not None and hi is not None else "—"
        sc = f"{r['score']:.1f}" if r.get("score") is not None else "—"
        print(f"  {r['run_date']}  {sc:>5}  {r.get('label') or '—':<6}  "
              f"仓位 {pos:<9}  {r.get('style_bias') or ''}")


def _load_replay_panel():
    """读取长期回放缓存里的面板（缺失或损坏返回 None）。

    复用 `_long_replay_and_select` 写下的同一份缓存，避免为验证再跑一次回放。
    """
    path = os.path.join(ROOT, "state", "longterm_replay.json")
    if not os.path.exists(path):
        return None
    try:
        with open(path, encoding="utf-8") as f:
            return pd.DataFrame((json.load(f) or {}).get("panel") or [])
    except Exception:
        return None


def _fmt(v, spec="+.4f"):
    """None 安全的数值格式化：缺失显示 '-'，而不是让 f-string 抛 TypeError。"""
    try:
        return format(float(v), spec)
    except (TypeError, ValueError):
        return "-"


def run_ml_eval(cfg: dict, params: dict, no_replay: bool = False) -> int:
    """走前 ML 验证：把模型的样本外预测与规则分放在**同一把尺子**上比。

    **只读**：不改 params、不动评分、不写 picks。长期轨的 120 日 IC=+0.155
    (t=5.24) 是在「规则分」这一输入下验证出来的，把 ML 分数直接塞进六维评分会让
    已成立的结论失效、收益无法归因。这里只回答一个问题：ML 值不值得单开一次
    接入验证（三关门禁全过才值得）。
    """
    log("走前 ML 验证（长期六维 → 未来收益，expanding window）")
    mc = (cfg.get("ml") or {})
    today = datetime.now().strftime("%Y-%m-%d")
    panel = _load_replay_panel()
    if (panel is None or panel.empty) and not no_replay:
        log("  回放面板缺失，先跑一次长期历史回放…")
        raw_cache = {}

        def _raw(code, market, mkt_id=None):
            key = (market, str(code))
            if key not in raw_cache:
                try:
                    raw_cache[key] = fnd.fetch_kline_raw(code, market)
                except Exception:
                    raw_cache[key] = None
            return raw_cache[key]

        try:
            _long_replay_and_select(cfg, params, _raw, False, today, log)
        except Exception as e:
            log(f"  历史回放失败: {e}")
        panel = _load_replay_panel()
    if panel is None or panel.empty:
        log("  拿不到回放面板，无法评估（先跑 python run.py --mode long）")
        return 1

    horizon = int(mc.get("horizon", ml_eval.DEFAULT_HORIZON))
    ret_col = f"ret_{horizon}"
    res = ml_eval.evaluate_against_rule(
        panel, ret_col=ret_col,
        kinds=tuple(mc.get("models") or ("ridge", "gbr")),
        min_train_periods=int(mc.get("min_train_periods", 6)),
        min_oof_periods=int(mc.get("min_oof_periods", ml_eval.MIN_OOF_PERIODS)),
        n_perm=int(mc.get("n_perm", ml_eval.DEFAULT_N_PERM)),
        purge=bool(mc.get("purge", True)),
        embargo_periods=mc.get("embargo_periods"),
        p_threshold=float(mc.get("p_threshold", 0.10)),
        seed=int(mc.get("seed", 0)))
    if not res.get("rule"):
        log(f"  {res.get('verdict')}")
        return 1

    r = res["rule"]
    log(f"  面板 {res['n_rows']} 观测 / {res['n_periods']} 个截面，目标 {ret_col}，"
        f"特征 {len(res['features'])} 维")
    log(f"  规则分（基准）: IC={_fmt(r['ic_mean'])} ICIR={_fmt(r['icir'], '+.3f')} "
        f"t={_fmt(r['t'], '+.2f')} 去尾部10%={_fmt(r['ic_mean_trimmed'])}")
    for k in sorted(res["candidates"]):
        c = res["candidates"][k]
        mark = "★" if k == res.get("best") else " "
        log(f" {mark} ML·{k:<6} 样本外 {c['n_periods']:>2} 截面: "
            f"IC={_fmt(c['ic_mean'])} ICIR={_fmt(c['icir'], '+.3f')} "
            f"t={_fmt(c['t'], '+.2f')} 去尾部10%={_fmt(c['ic_mean_trimmed'])}")
    log(f"  门禁: {res['gate']}")
    pm = res.get("perm") or {}
    if pm.get("null_mean") is not None:
        log(f"  置换检验 {pm['n_perm']} 次: 原假设 IC {pm['null_mean']:+.4f} ± "
            f"{pm['null_std']:.4f}（最大 {pm['null_max']:+.4f}）"
            f"｜真实 {pm['real_ic']:+.4f} → p={pm['p_value']}")
    log(f"  结论: {res['verdict']}")

    out_dir = os.path.join(ROOT, "reports")
    os.makedirs(out_dir, exist_ok=True)
    path = os.path.join(out_dir, f"ml_eval_{today}.md")
    try:
        with open(path, "w", encoding="utf-8") as f:
            f.write(ml_eval.render_md(res))
        log(f"  报告已写入 {path}")
    except Exception as e:
        log(f"  报告写入失败: {e}")
    n = store.save_ml_eval(today, res)
    if not store.enabled():
        log("  数据层已关闭，结论未落库")
    elif n:
        log(f"  结论已落库 ml_eval：{n} 行")
    else:
        log(f"  结论落库 0 行（err={store.last_error()}）")
    print("\nML_EVAL_MD=" + path)
    return 0


def apply_runtime_config(cfg: dict):
    """把 config.json 的开关落到模块级默认值上。

    只覆盖「未被环境变量显式指定」的项，因此命令行/容器里的
    `QUANT_STORE` / `QUANT_DB` / `QUANT_CSC` / `QUANT_KLINE_TTL_H`
    永远优先于仓库里的配置文件。
    """
    sc = (cfg or {}).get("store") or {}
    store.configure(path=sc.get("path"), enabled=sc.get("enabled"))
    fetcher.configure(kline_ttl_hours=sc.get("kline_ttl_hours"))
    ds = (cfg or {}).get("datasource") or {}
    csc_source.configure(enabled=ds.get("csc_enabled"))
    csc_source.configure_retry(attempts=ds.get("csc_retry_attempts"),
                               base_sec=ds.get("csc_retry_backoff_sec"))
    aks.configure(enabled=ds.get("akshare_enabled"),
                  min_gap=ds.get("akshare_min_gap_sec"),
                  budget_sec=ds.get("akshare_budget_sec"),
                  retries=ds.get("akshare_retries"),
                  breaker_ttl_sec=ds.get("akshare_breaker_ttl_sec"))
    # 现货列表的降级链顺序（改顺序 = 改每日候选池来源，属策略口径变更）
    fetcher.configure_spot_tiers(ds.get("spot_tier_order"))
    ake.configure(enabled=ds.get("akshare_enabled"),
                  min_gap=ds.get("akshare_min_gap_sec"),
                  budget_sec=ds.get("akshare_event_budget_sec"),
                  retries=ds.get("akshare_retries"),
                  breaker_ttl_sec=ds.get("akshare_breaker_ttl_sec"),
                  bse_enabled=ds.get("akshare_margin_bse"))
    # 新浪档（现货降级链第三档）也要接配置：接漏了它会静默用内置默认值，
    # 而「配置写了不生效」比「没有这个配置」更难发现。
    ss.configure(min_gap=ds.get("akshare_min_gap_sec"),
                 budget_sec=ds.get("akshare_sina_budget_sec"),
                 retries=ds.get("akshare_retries"),
                 breaker_ttl_sec=ds.get("akshare_breaker_ttl_sec"))


def latest_trading_date() -> str:
    """最新交易日（账期）。供 `main()` 之外的子命令复用。

    口径与 `main()` 内的 `data_date()` 完全一致：**以行情走到哪一天为账期**，
    而不是墙钟日期。差别只在取数路径——那里先读内存里的 K 线缓存（快），
    这里直接问落盘层，最后退回实时抓一次基准 ETF（510300）。

    为什么这件事必须只有一套口径：资金流是**当日快照**型数据，用墙钟日期当账期，
    周末补跑就会凭空多出一个「没有行情的交易日」，而这一天会作为一个真实的
    观测日流进因子检验。
    """
    d = store.market_latest_date("etf", "qfq")
    if d:
        return str(d)[:10]
    try:
        k = fetcher.fetch_kline("510300", "etf", limit=5)
        if k is not None and len(k):
            return str(k["date"].iloc[-1])[:10]
    except Exception:
        pass
    return datetime.now().strftime("%Y-%m-%d")


def run_fund_flow_only() -> int:
    """独立跑一次资金流采集：抓取 → 归一 → 落库 → 打印画像。

    刻意做成独立子命令，而不是只藏在日循环里：新源上线时先单独观察几天
    （拿到多少行、耗时多少、有没有异常字段），确认稳了再谈「进因子」。
    真出问题时也能在不跑整个日循环（约 15 分钟）的前提下复现。
    """
    log("=" * 60)
    log("资金流采集（Phase 1 新数据源 · 同花顺个股资金流）")
    st = aks.status()
    log(f"  开关 enabled={st['enabled']} / 已安装 installed={st['installed']} / "
        f"节流 {st['min_gap_sec']}s / 预算 {st['budget_sec']}s / 重试 {st['retries']}")
    if st["breaker"]:
        log(f"  ⚠ 熔断中：{st['breaker_reason'][:100]}")
    if not st["enabled"]:
        log("  未启用（QUANT_AKSHARE=off 或 config.datasource.akshare_enabled=false），退出")
        return 1
    today = latest_trading_date()
    log(f"  账期取最新交易日 {today}")
    if store.enabled() and store.connect() is None:
        log(f"  ⚠ 存储层不可用（{store.last_error()}）：本次只抓不落库")
    res = aks.collect_fund_flow(flow_date=today, log=log)
    cov = store.fund_flow_coverage("cn") if store.enabled() else []
    if cov:
        last = cov[-1]
        log(f"  库内覆盖 {len(cov)} 个交易日；最近 {last['flow_date']} "
            f"{last['n']} 只 合计净额 {last['net_yi']} 亿")
    else:
        log("  库内暂无资金流数据")
    miss = drain_missing()
    for m in miss[:5]:
        log(f"  缺失留痕: [{m['source']}/{m['market']}] {m['reason'][:100]}")
    if res.get("reused"):
        log("  结果 复用当日定盘快照（收盘后已抓过，本次未打网络）")
    else:
        log(f"  结果 ok={res['ok']} kind={res['kind'] or '-'}：归一 {res['rows']} 行 / "
            f"写入 {res['written']} 行 / 耗时 {res['elapsed']:.1f}s")
    if res["anomalies"]:
        log(f"  归一异常: {res['anomalies']}")
    return 0 if res["ok"] else 1


def print_new_source_coverage():
    """打印 Phase 1 新数据源的覆盖与源状态（`--db-stats` 与 `--new-sources` 共用）。

    单点产出：同一个覆盖口径在两个入口各写一遍，迟早出现「db-stats 说有一万行、
    new-sources 说三千行」这种自己跟自己打架的输出。
    """
    st_ak = aks.status()
    print("\n资金流截面覆盖（同花顺个股资金流，逐日累积、无法回补）:")
    print(f"  akshare: enabled={st_ak['enabled']} / 已安装={st_ak['installed']} / "
          f"节流 {st_ak['min_gap_sec']}s / 预算 {st_ak['budget_sec']}s"
          + (f" / ⚠ 熔断中: {st_ak['breaker_reason'][:60]}" if st_ak["breaker"] else ""))
    ffc = store.fund_flow_coverage("cn")
    if not ffc:
        print("  （无记录）")
    for c in ffc[-10:]:
        ny = "—" if c["net_yi"] is None else f"{c['net_yi']:+.2f}"
        print(f"  {c['flow_date']}  {c['syms']:>5} 只  {c['n']:>5} 行  "
              f"合计净额 {ny:>10} 亿  末次抓取 {c['last_fetch']}")

    print("\n事件类数据覆盖（龙虎榜 / 两融 / 解禁 / 退市清单）:")
    labels = {"lhb": "龙虎榜明细", "lhb_inst": "龙虎榜机构统计",
              "margin": "个股两融", "restriction": "限售解禁",
              "delisted": "退市清单"}
    hints = {"lhb": "可回补", "lhb_inst": "可回补", "margin": "T+1 公布、可回补",
             "restriction": "可回补", "delisted": "可回补"}
    any_row = False
    for t, label in labels.items():
        cov = store.event_coverage(t)
        if not cov:
            continue
        any_row = True
        c = cov[0]
        span = (f"{c['first_date']} ~ {c['last_date']}"
                if c.get("first_date") else "—")
        print(f"  {label:<14} {c['n']:>7} 行  {c['syms']:>5} 只  {span:<24}"
              f"（{hints[t]}）")
    if not any_row:
        print("  （无记录）")
    for t, label in (("lhb", "龙虎榜明细"), ("margin", "个股两融")):
        recent = store.event_coverage(t, by_date=True, limit=5)
        if recent:
            detail = "  ".join(f"{r['d']}({r['syms']})" for r in recent)
            print(f"  {label}最近交易日: {detail}")
    st = dsbase.sources_status()
    print(f"  akshare 数据源开关: enabled={st['enabled']} / "
          f"已安装={st['installed']}")


def run_spot_probe() -> int:
    """逐档实测现货降级链——回答「这条链今天是哪一档在供数」。

    为什么值得单独一个子命令：降级链最大的风险不是「降级了」，而是
    **长期在降级却没有信号**。东财 clist 在本机成功率只有 9%~15%，
    这件事在每日报告里完全看不见；`--db-stats` 能翻出利用率，但这个探针
    直接给出「此刻每一档通不通、多少行、多少秒」，用来判断该不该调档位顺序。
    """
    log("=" * 60)
    log("现货降级链探针（只读，不落库；每档都实跑一遍，不提前短路）")
    res = fetcher.spot_probe(pages=2)
    for market, r in res.items():
        log(f"  {market}: 档位顺序 {r['tier_order']}"
            f"{'  → 链会由 ' + r['served_by'] + ' 供数' if r['served_by'] else '  → 全部为空'}")
        for tr in r["tiers"]:
            flag = "空" if tr["empty"] else f"{tr['rows']} 行"
            mark = " ← 首选非空" if tr["tier"] == r["served_by"] else ""
            log(f"    {tr['tier']:<10} {flag:>9}  {tr['sec']:>6.1f}s{mark}")
    if not res.get("us", {}).get("served_by"):
        log("  ⚠ 美股仍无可用第二源（新浪美股要逐页抓 911 页，实测不可接受）")
    log("  提示：etf/hk 的新浪档给的是**全量**（1693 / 2811 只），")
    log("        而腾讯档是静态核心池（64 / 48 只）——若要调整优先级，")
    log("        改 config.json 的 datasource.spot_tier_order（会改变每日候选池）")
    return 0


def run_new_sources_only() -> int:
    """独立跑一遍 Phase 1 全部新数据源，并打印各表覆盖。

    与日循环里那一段等价——用它可以在不跑整个日循环（约 15 分钟）的前提下
    单独观察新源、复现问题、或者补跑一次漏掉的采集。
    """
    log("=" * 60)
    log("Phase 1 新数据源采集（资金流 + 事件类）")
    _, cfg = load_configs()
    ds = (cfg or {}).get("datasource") or {}
    st = dsbase.sources_status()
    log(f"  akshare 开关: enabled={st['enabled']} / 已安装={st['installed']}"
        + ("（环境变量 QUANT_AKSHARE 覆盖）" if st["env_override"] else ""))
    if not st["enabled"] or not st["installed"]:
        log("  未启用或未安装 akshare，退出")
        return 1
    today = latest_trading_date()
    log(f"  账期取最新交易日 {today}")
    if store.enabled() and store.connect() is None:
        log(f"  ⚠ 存储层不可用（{store.last_error()}）：本次只抓不落库")

    t0 = time.time()
    log("  ── 资金流 ──")
    ff = aks.collect_fund_flow(flow_date=today, log=log)
    log("  ── 事件类 ──")
    ev = ake.collect_all(
        today,
        lhb_lookback_days=int(ds.get("akshare_lhb_lookback_days", 3) or 0),
        restriction_back_days=int(ds.get("akshare_restriction_back_days", 7) or 0),
        restriction_forward_days=int(
            ds.get("akshare_restriction_forward_days", 120) or 0),
        margin_backfill_days=int(ds.get("akshare_margin_backfill_days", 3) or 0),
        log=log)
    log(f"  合计耗时 {time.time() - t0:.1f}s")

    print_new_source_coverage()
    miss = drain_missing()
    for m in miss[:10]:
        print(f"  缺失留痕: [{m['source']}/{m['market']}] {m['reason'][:100]}")
    ok = bool(ff.get("ok") or ff.get("reused")) or bool(ev.get("ok"))
    return 0 if ok else 1


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--quick", action="store_true")
    ap.add_argument("--no-eval", action="store_true")
    ap.add_argument("--no-replay", action="store_true",
                    help="跳过长期历史回放（省约 1 分钟）")
    ap.add_argument("--no-cache", action="store_true",
                    help="忽略阶段缓存，强制重算（用于验证缓存与原算结果一致）")
    ap.add_argument("--mode", choices=["short", "long", "all"], default="all",
                    help="short=只跑短线低吸轨, long=只跑长期价值轨")
    ap.add_argument("--market", type=str, default=None)
    ap.add_argument("--db-stats", action="store_true",
                    help="打印落盘概览（各表行数 / 行情覆盖 / 数据源健康）后退出")
    ap.add_argument("--csc-probe", action="store_true",
                    help="自检中信建投数据源连通性后退出")
    ap.add_argument("--ml-eval", action="store_true",
                    help="走前 ML 验证：样本外预测 vs 规则分（只读，不接入评分）")
    ap.add_argument("--fund-flow", action="store_true",
                    help="只抓当日个股资金流截面并落库（Phase 1 新数据源，独立可跑）")
    ap.add_argument("--new-sources", action="store_true",
                    help="跑全部 Phase 1 新数据源（资金流 + 龙虎榜/两融/解禁/退市清单）")
    ap.add_argument("--spot-probe", action="store_true",
                    help="逐档实测 ETF/港股/美股现货降级链（只读，不落库）")
    ap.add_argument("--akshare-probe", action="store_true",
                    help="自检 akshare 数据源连通性与字段口径后退出（不写库）")
    ap.add_argument("--brief", action="store_true", default=None,
                    help="报告只留操作清单与结论（完整版另存 report_DATE.full.*）")
    ap.add_argument("--full", action="store_true",
                    help="输出完整版报告，覆盖 config.json 的 report.brief")
    args = ap.parse_args()

    # 配置先行：让 --db-stats / --csc-probe 也认 config.json 里的路径与开关
    try:
        apply_runtime_config(json.load(open(os.path.join(ROOT, "config.json"),
                                           encoding="utf-8")))
    except Exception as e:
        print(f"（读取 config.json 失败，使用内置默认值: {e}）")

    if args.db_stats:
        print_db_stats()
        return
    if args.csc_probe:
        print(json.dumps(csc_source.probe(), ensure_ascii=False, indent=2))
        return
    if args.ml_eval:
        params, cfg = load_configs()
        return run_ml_eval(cfg, params, args.no_replay)
    if args.akshare_probe:
        print(json.dumps(aks.probe(), ensure_ascii=False, indent=2))
        return 0
    if args.fund_flow:
        return run_fund_flow_only()
    if args.new_sources:
        return run_new_sources_only()
    if args.spot_probe:
        return run_spot_probe()

    t0 = time.time()
    params, cfg = load_configs()
    markets = args.market.split(",") if args.market else cfg["markets"]
    bt_top_n = 8 if args.quick else cfg["backtest_top_n"]
    run_short = args.mode in ("short", "all")
    run_long = args.mode in ("long", "all")

    # 共享 K 线缓存：同一标的只拉一次（省请求、抗限流），
    # 同时为滚动因子评估（IC/ICIR/RankIC/RankICIR）提供历史面板。
    kline_cache = {}

    # 数据源保护：按「累计抓取耗时」熔断，而非按失败次数。
    # 为什么不能用失败次数：限流是瞬时的，连续几只失败后往往又能取到；按次数熔断会
    # 把限流误判成源故障，直接掐掉整个市场的行情（实测：某次运行滚动面板从 63 只
    # 掉到 6 只，报告数字整体失真且无任何报错）。耗时预算只拦真正的「源不可用」——
    # 单只标的要依次试 3 个源 + 重试 3 次，最坏 ≈153s，12 只就是半小时的静默等待。
    FETCH_BUDGET_SEC = float(cfg.get("fetch_budget_sec", 180))
    _fetch_budget = {}

    def cached_kline(code, market, mkt_id=None):
        key = (market, str(code))
        if key not in kline_cache:
            bg = _fetch_budget.setdefault(
                market, fetcher.FetchBudget(FETCH_BUDGET_SEC, name=f"{market} 行情", log=log))
            if bg.guard():
                kline_cache[key] = None
                return None
            _t = time.time()
            try:
                k = kline_fn(code, market, mkt_id)
            except Exception:
                k = None
            bg.charge(time.time() - _t)
            kline_cache[key] = k
        return kline_cache[key]

    # 不复权 K 线缓存（长期轨估值分位专用，与上面不可混用：前复权会篡改历史）
    raw_cache = {}

    def cached_raw(code, market, mkt_id=None):
        key = (market, str(code))
        if key not in raw_cache:
            try:
                raw_cache[key] = fnd.fetch_kline_raw(code, market)
            except Exception:
                raw_cache[key] = None
        return raw_cache[key]

    nav_cache = {}

    def cached_nav(code, market="fund"):
        if code not in nav_cache:
            try:
                nav_cache[code] = fetcher.fetch_fund_nav(code, limit=300)
            except Exception:
                nav_cache[code] = None
        return nav_cache[code]

    def data_date() -> str:
        """本次运行所依据的**交易日**（不是墙钟日期）。

        盘中/盘后运行时它就是今天；盘前、周末或节假日补跑时，最新一根 K 线仍是
        上一交易日的。此时若按墙钟日期记账，会得到一份「日期是今天、价格是昨天」
        的推荐：次日复盘会把两天的涨跌当成一天，收益被系统性放大，而这批被污染的
        样本又会流进权重迭代。统一用「行情走到哪一天」作为账期。
        """
        try:
            k = cached_kline("510300", "etf")
            if k is not None and len(k):
                return str(k["date"].iloc[-1])[:10]
        except Exception:
            pass
        return datetime.now().strftime("%Y-%m-%d")

    log("=" * 60)
    log(f"投资助手启动 mode={args.mode} markets={markets if run_short else '-'}")
    if store.enabled():
        if store.connect() is None:
            log(f"  ⚠ 存储层不可用（{store.last_error()}），本次走文件缓存")
        else:
            _st = store.stats()
            log(f"  存储层 {os.path.basename(_st['path'])}: "
                f"行情 {_st.get('kline', 0)} 根 / 财务 {_st.get('fin_metrics', 0)} 期 / "
                f"榜单 {_st.get('rank_snapshot', 0)} 条 / 指数 {_st.get('index_snapshot', 0)} 条")
    else:
        log("  存储层已关闭（QUANT_STORE=file），完全走文件缓存")
    if csc_source.available():
        log("  中信建投数据源: 已启用（财务兜底 / 指数估值 / 行业排名）")
    else:
        log("  中信建投数据源: 未启用（缺 CSC_API_KEY 或 QUANT_CSC=off）")
    today = data_date()
    if today != datetime.now().strftime("%Y-%m-%d"):
        log(f"  账期采用最新交易日 {today}"
            f"（墙钟 {datetime.now():%Y-%m-%d} 尚未产生新行情）")

    # 1) 迭代：评估昨日推荐 + 更新权重
    ev, iter_info = {}, {"updated": False, "reason": "跳过"}
    if run_short and not args.no_eval:
        try:
            log("评估昨日推荐…")
            ev = evaluate_previous(quote_fn, today, kline_fn=cached_kline)
            if ev.get("skipped"):
                log(f"  复盘跳过: {ev['skipped']}")
            else:
                log(f"  复盘结果: {json.dumps({k: v for k, v in ev.items() if k != 'details'}, ensure_ascii=False)}")
            _it = params.get("iteration") or {}
            iter_info = update_weights(params,
                                       min_samples=int(_it.get("min_samples", 20)),
                                       lr=float(_it.get("lr", 0.25)),
                                       min_days=int(_it.get("min_days", 20)))
            # 权重更新后重新加载
            if iter_info.get("updated"):
                with open(os.path.join(ROOT, "params", "params.json"), encoding="utf-8") as f:
                    params = json.load(f)
                log(f"  权重已更新至 v{params['version']}: {params['weights']}")
                log(f"    证据口径: {iter_info.get('n_days')} 个交易日 / 名义 "
                    f"{iter_info.get('samples')} 笔 → 有效样本量 "
                    f"{iter_info.get('n_eff')}（ICC={iter_info.get('icc')}, "
                    f"DEFF={iter_info.get('deff')}）")
            else:
                log(f"  权重未更新: {iter_info.get('reason')}")
                if iter_info.get("n_days"):
                    log(f"    证据口径: {iter_info['n_days']} 个有效交易日 / 名义 "
                        f"{iter_info.get('nominal_samples')} 笔 → 有效样本量 "
                        f"{iter_info.get('n_eff')}（ICC={iter_info.get('icc')}, "
                        f"DEFF={iter_info.get('deff')}）")
                if iter_info.get("degenerate_days"):
                    log(f"    已剔除退化日（全天 ret 恒为 0）: "
                        f"{', '.join(iter_info['degenerate_days'][-5:])}")
        except Exception as e:
            log(f"  迭代评估失败(不影响主流程): {e}")
            traceback.print_exc()

    # 1.5) Phase 1 新数据源。
    # 必须**每个交易日都跑到**——资金流与个股两融只给当日快照、上游不提供历史，
    # 漏一天就永久缺一天；龙虎榜/解禁虽然可回补，但也靠这里逐日刷新（上游会修订）。
    # 它们只写新表、不参与今天的评分，所以放在评分之前还是之后都不影响结论。
    if dsbase.sources_enabled():
        _ds = cfg.get("datasource") or {}
        try:
            log("采集资金流截面（同花顺个股资金流）…")
            ff = aks.collect_fund_flow(flow_date=today, log=log)
            if not ff.get("ok") and not ff.get("reused"):
                log(f"  资金流本次未入库（{ff.get('kind') or '未知'}），已记入 data_health")
        except Exception as e:
            log(f"  资金流采集失败(不影响主流程): {e}")
            traceback.print_exc()
        try:
            log("采集事件类数据（龙虎榜 / 两融 / 解禁 / 退市清单）…")
            ev = ake.collect_all(
                today,
                lhb_lookback_days=int(_ds.get("akshare_lhb_lookback_days", 3) or 0),
                restriction_back_days=int(
                    _ds.get("akshare_restriction_back_days", 7) or 0),
                restriction_forward_days=int(
                    _ds.get("akshare_restriction_forward_days", 120) or 0),
                margin_backfill_days=int(
                    _ds.get("akshare_margin_backfill_days", 3) or 0),
                log=log)
            if not ev.get("ok"):
                log("  事件类数据本次全部未入库，已记入 data_health")
        except Exception as e:
            log(f"  事件类数据采集失败(不影响主流程): {e}")
            traceback.print_exc()
    else:
        log(f"  新数据源未启用（akshare enabled={dsbase.switch_on()} / "
            f"已安装={dsbase.akshare_installed()}），跳过")

    # 2) 短线轨：抓取各市场快照 + 粗筛 + K线评分
    today_picks, summary = {}, {}
    for mkt in (markets if run_short else []):
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
            _t_k = time.time()

            def _prog(i, n, _m=mkt, _t=_t_k):
                # 抓取循环必须留进度：源变慢时「在抓」与「卡死」外观相同，
                # 逐标的输出 + 已耗时才能一眼区分。
                if i == 1 or i == n or i % 5 == 0:
                    log(f"    抓取K线 {i}/{n}（已 {time.time() - _t:.0f}s）")

            scored = score_candidates(
                cand, mkt, params, cached_kline,
                max_n=budget, progress=_prog)
            log(f"  评分通过 {len(scored)} 只")
            if scored.empty:
                today_picks[mkt] = scored
                continue
            scored = backtest_candidates(scored, cached_kline, params, top_n=bt_top_n)
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
    if run_short:
        save_today_picks(all_picks, summary, data_date=today)
        log(f"已保存今日推荐 {len(all_picks)} 条到 state/（账期 {today}）")

    # 3.45) K 线内容签名：供后面三段重计算阶段做缓存寻址。
    # 签名来自 K 线内容本身（而非墙钟日期），所以当天数据被修订时会自动换 key。
    kline_sig = pc.kline_signature(kline_cache)
    log(f"  K线内容签名 {kline_sig}（{len(kline_cache)} 只标的）")

    # 3.5) 诊断：因子共线 + 截面因子预测力（借鉴 R&D-Agent(Q) 验证单元）
    coll, ic, rolling = None, None, None
    diag_frames = [df for df in today_picks.values() if df is not None and not df.empty]
    if run_short and diag_frames:
        coll = factor_collinearity(pd.concat(diag_frames, ignore_index=True))
        ic = factor_ic(pd.concat(diag_frames, ignore_index=True))
        if coll:
            log(f"  因子共线诊断: 最大|r|={coll['max_abs']:.2f}, 冗余对={len(coll['flagged'])}")
        if ic:
            log(f"  截面因子预测力: IC={ic['ic']:+.2f}, RankIC={ic['rank_ic']:+.2f}")

    # 3.6) 滚动因子评估：IC/ICIR/Rank IC/Rank ICIR（复用 K 线缓存，不额外请求）
    if run_short and kline_cache:
        try:
            log(f"滚动因子评估（{len(kline_cache)} 只标的缓存）…")
            rolling = rolling_factor_ic(
                kline_cache, params, horizon=params["backtest"].get("hold_days", 5))
            if rolling:
                sc = rolling["factors"].get("score", {}).get("ic") or {}
                log(f"  滚动面板: {rolling['n_symbols']}只 × {rolling['n_periods']}截面 "
                    f"({rolling['date_range'][0]}~{rolling['date_range'][1]})")
                log(f"  综合评分: IC均值={sc.get('mean')} ICIR={sc.get('icir')} "
                    f"t={sc.get('t')} 胜率={sc.get('winrate')}")
            else:
                log("  样本不足，跳过")
        except Exception as e:
            log(f"  滚动因子评估失败(不影响主流程): {e}")
            traceback.print_exc()

    # 3.7) 持仓账本 + 离场模型：结算 → 建仓 → 卖出建议（复用同一份 K 线缓存）
    # today 已在启动时按「行情最新交易日」确定，此处不再用墙钟覆盖
    exit_cfg = cfg.get("exit_model", {}) or {}
    book, advice, exit_info, scan = None, [], None, None
    if run_short and exit_cfg.get("enabled", True):
        try:
            book = portfolio.load_book()
            cal = portfolio.trading_calendar(kline_cache)
            max_new = int(exit_cfg.get("max_new_positions_per_market", 5))

            # 首次运行：从最近一份历史推荐回填，让账本与离场模型立刻可用
            if not book.get("positions"):
                pf = portfolio.latest_picks_file(before=today)
                if pf:
                    st = portfolio.backfill_from_picks(book, pf, cached_kline, params, today, cal,
                                                       max_per_market=max_new)
                    log(f"  账本首次回填 {os.path.basename(pf)}: 开仓 {st['opened']}、"
                        f"已平 {st['closed']}、跳过 {st['skipped']}")

            # 结算：批量行情刷新最新价（腾讯批量接口，数十只一次请求）
            held = portfolio.open_positions(book)
            quotes = fetcher.fetch_quotes_batch(
                [(p["code"], p["market"], p.get("mkt_id")) for p in held]) if held else {}
            closed = portfolio.settle_book(book, quotes, cal, params, today)
            log(f"  持仓结算: 未平仓 {len(portfolio.open_positions(book))} 只, "
                f"本次平仓 {len(closed)} 只, 行情命中 {len(quotes)}/{len(held)}")

            # 建仓：今日推荐按评分降序登记（同标的已持有则跳过）
            picks_by_market = {}
            for mkt, df in today_picks.items():
                if df is None or df.empty:
                    continue
                picks_by_market[mkt] = [
                    {"code": str(r["code"]), "name": r["name"], "score": float(r["score"]),
                     "entry_price": float(r["price"]), "mkt_id": r.get("mkt_id"),
                     **{f: (float(r[f]) if f in r.index and pd.notna(r[f]) else None)
                        for f in ("f_boll", "f_rsi", "f_volume", "f_trend",
                                  "f_macd", "f_drawdown")}}
                    for _, r in df.iterrows()]
            added = portfolio.add_positions(book, picks_by_market, params, today, max_new)
            log(f"  新建仓 {len(added)} 只（每市场上限 {max_new}）")

            # 离场模型：用历史面板标定状态表 + 样本外验证（≈155s，输入未变则命中缓存）
            _mbn = int(exit_cfg.get("min_bucket_n", 20))
            _oof = float(exit_cfg.get("oos_frac", 0.3))
            _shk = float(exit_cfg.get("shrink_k", 25))
            exit_info = cached_phase(
                "exit_model",
                lambda: fit_exit_model(kline_cache, params, min_bucket_n=_mbn,
                                       oos_frac=_oof, shrink_k=_shk),
                kline_sig, params,
                {"min_bucket_n": _mbn, "oos_frac": _oof, "shrink_k": _shk},
                no_cache=args.no_cache)
            if exit_info:
                v = exit_info.get("validation")
                log(f"  离场模型: {exit_info['n_symbols']}只/{exit_info['n_trades']}笔路径, "
                    f"状态桶 {len(exit_info['states'])}, τ*={exit_info['tau'] * 100:+.1f}%"
                    + (f", OOS 均收益 {v['oos']['model']['avg_ret'] * 100:+.3f}% vs "
                       f"基线 {v['oos']['baseline']['avg_ret'] * 100:+.3f}%" if v else ""))
            else:
                log("  离场模型: 样本不足，跳过")

            advice = exit_advice(portfolio.open_positions(book), exit_info, params)
            sells = [a for a in advice if a["advice"] == "SELL"]
            watches = [a for a in advice if a["advice"] == "WATCH"]
            log(f"  卖出建议: 卖出 {len(sells)} / 观察 {len(watches)} / 持仓 {len(advice)}")

            # 离场规则体检：止损 / 止盈 / 持有期的历史表现对比（≈60s，复用同一信号遍历）
            scan = cached_phase(
                "param_scan",
                lambda: parameter_scan(kline_cache, params),
                kline_sig, params, {}, no_cache=args.no_cache)
            if scan:
                b = scan["baseline"]
                log(f"  离场规则体检: 信号 {scan['n_signals']} 笔, 当前配置 "
                    f"均收益 {b['avg'] * 100:+.3f}% / Sharpe {b.get('sharpe')}")

            portfolio.save_book(book)
        except Exception as e:
            log(f"  离场模型失败(不影响主流程): {e}")
            traceback.print_exc()

    # 3.8) 短线模型选型：离场规则组合的 walk-forward（样本外是否支持切换）
    short_sel = None
    if run_short and kline_cache and (cfg.get("model_select", {}) or {}).get("enabled", True):
        try:
            log("短线模型选型（离场规则组合 walk-forward）…")
            short_sel = cached_phase(
                "select_short",
                lambda: msel.select_short(cfg, params, kline_cache),
                kline_sig, params,
                {"grid": (cfg.get("model_select") or {}).get("shorts")},
                no_cache=args.no_cache)
            if short_sel:
                r = short_sel.get("recommend") or {}
                log(f"  信号 {short_sel['n_signals']} 笔，样本内增益 "
                    f"{short_sel['gain_in_sample'] * 100:+.3f}pct → 样本外 "
                    f"{short_sel['gain_out_sample'] * 100:+.3f}pct"
                    + (f"（衰减 {short_sel['decay'] * 100:.0f}%）"
                       if short_sel.get("decay") is not None else ""))
                log(f"  建议参数: 持有 {r.get('hold_days')} 日 / 止损 {r.get('stop_loss')} "
                    f"/ 止盈 {r.get('take_profit')}")
                log(f"  结论: {short_sel['verdict']}")
            else:
                log("  样本不足，跳过")
        except Exception as e:
            log(f"  短线选型失败: {e}")
            traceback.print_exc()

    # 3.9) 长期价值轨
    long_bundle = {"enabled": False, "note": "mode=short，未运行"}
    if run_long:
        try:
            long_bundle = run_longterm(cfg, params, cached_kline, cached_raw,
                                       cached_nav, today, args.no_replay)
        except Exception as e:
            log(f"长期价值轨失败: {e}")
            traceback.print_exc()
            long_bundle = {"enabled": False, "error": str(e)}

    # 3.95) 模型选型落盘
    try:
        sel_path = msel.save_result(short_sel, (long_bundle or {}).get("selection", {}).get("long"),
                                    today)
        log(f"模型选型结论已写入 {sel_path}")
    except Exception as e:
        log(f"  选型落盘失败: {e}")

    # 3.97) 数据完整性汇总：把各市场的抓取预算状态与被跳过的标的数一起交给报告。
    # 必须进报告而不是只进日志——超预算会整片跳过某市场的标的，报告却照常生成，
    # 用户会误读成「今天这个市场没有符合条件的标的」。
    data_health = {}
    for _mkt, _bg in _fetch_budget.items():
        _st = _bg.state()
        _st["missing"] = sum(1 for (_m, _c), _v in kline_cache.items()
                             if _m == _mkt and (_v is None or len(_v) == 0))
        data_health[_mkt] = _st
        store.record_health("kline_tencent", _mkt, not _st["tripped"],
                            _st["spent"], len(kline_cache),
                            f"未取到 {_st['missing']} 只" if _st["missing"] else "")
    for _mkt, _st in data_health.items():
        if _st["tripped"] or _st["missing"]:
            log(f"  数据完整性 {_mkt}: 抓取 {_st['spent']}s/{_st['budget']}s"
                f"{'（已超预算）' if _st['tripped'] else ''}，未取到 {_st['missing']} 只")

    # 降级取数留痕：财务/行情改用备用源或库内旧数据的记录。
    # 和上面一样，必须进报告——降级后的口径与主源不同（例如财务公告日退化为
    # 法定披露截止日），只写日志等于让这批数字失去来历。
    _fallbacks = fetcher.drain_fallbacks()
    if _fallbacks:
        data_health["_fallbacks"] = _fallbacks
        log(f"  降级取数 {len(_fallbacks)} 笔（已在报告数据完整性段落披露）")

    # Phase 1 新数据源的缺失留痕：与上面的「降级取数」分开记，因为影响面不同——
    # 行情降级影响今天的结论，新表缺口只影响将来因子检验的样本完整性。
    _missing_src = drain_missing()
    if _missing_src:
        data_health["_missing_sources"] = _missing_src
        log(f"  新数据源缺失 {len(_missing_src)} 条（已在报告数据完整性段落披露）")

    # 4) 生成报告
    # 精简版是默认：每天只有几分钟看盘的人要的是「做什么」，不是「为什么」。
    # 完整版照旧生成（report_DATE.full.*），需要查证据时再翻。
    _cfg_brief = bool(((cfg or {}).get("report") or {}).get("brief", False))
    brief_report = True if args.brief else (False if args.full else _cfg_brief)
    if brief_report:
        log("  报告模式：精简版（完整版另存 .full）")
    md_path, html_path = rpt.generate_report(today_picks, summary, ev, iter_info, params,
                                             coll, ic, rolling,
                                             exit_advice_list=advice,
                                             exit_model=exit_info,
                                             book=book,
                                             param_scan=scan,
                                             longterm=long_bundle,
                                             model_selection=short_sel,
                                             cfg=cfg,
                                             data_health=data_health,
                                             mode=args.mode,
                                             brief=brief_report)
    log(f"报告已生成: {md_path}")
    print("\nREPORT_HTML=" + html_path)
    print("REPORT_MD=" + md_path)

    # 5) 运行快照（全流程留痕，可复现复盘）
    _save_manifest(today_picks, summary, ev, iter_info, params, coll, ic, rolling,
                   time.time() - t0, exit_info=exit_info, advice=advice,
                   param_scan=scan, longterm=long_bundle, model_selection=short_sel,
                   data_health=data_health)


def run_longterm(cfg, params, cached_kline, cached_raw, nav_fn, today,
                 no_replay: bool = False) -> dict:
    """长期价值轨：宏观环境 → 标的池 → 六维评分 → 推荐 → 跟踪与模型选型。

    与短线轨共用 K 线缓存（宏观代理用前复权，估值分位用不复权，两者不可混用）。
    """
    out = {"enabled": True}
    lt_cfg = cfg.get("longterm", {}) or {}
    if not lt_cfg.get("enabled", True):
        return {"enabled": False, "note": "config.longterm.enabled=false"}

    # ---- 1) 宏观环境 -------------------------------------------------------
    try:
        snap = mcr.build_snapshot(kline_fn=cached_kline)
        ms = mcr.macro_score(snap)
        out["macro"] = ms
        # 判断类数据落库：原始指标能重抓，但「当时打了几分、建议多少仓位」抓不回来。
        try:
            n = store.upsert_macro_snapshot(today, ms)
            if n:
                log(f"  宏观快照已落库 macro_snapshot({today})")
        except Exception as e:
            log(f"  宏观快照落库失败（不影响主流程）: {e}")
        stance = ms.get("equity_stance")
        log(f"  宏观环境: {ms['score']} 分「{ms['label']}」"
            + (f"，建议权益仓位 {stance[0] * 100:.0f}%~{stance[1] * 100:.0f}%"
               if stance else "")
            + f"，风格偏向 {'/'.join(ms['style_bias'])}")
        for it in ms["items"]:
            log(f"    · {it['name']}: {it['score'] if it['score'] is not None else '—'} "
                f"— {it['note']}")
        if ms.get("missing"):
            log(f"    数据缺口（不参与总分）: {'、'.join(ms['missing'])}")
    except Exception as e:
        log(f"  宏观环境失败(长期轨终止): {e}")
        traceback.print_exc()
        return {"enabled": False, "error": str(e)}

    # ---- 2) 标的池 + 六维评分 ---------------------------------------------
    market_data = {}
    try:
        market_data["cn_spot"] = fetcher.fetch_cn_rank_by_amount(
            int(lt_cfg.get("amount_rank_top", 400)))
        log(f"  A股成交额榜 {len(market_data['cn_spot'])} 条（长期标的池来源）")
    except Exception as e:
        log(f"  成交额榜失败: {e}")
    try:
        market_data["etf_spot"] = fetcher.fetch_spot_list("etf", pages=2)
    except Exception:
        market_data["etf_spot"] = None

    try:
        progress = lambda msg, i, n: log(f"    {msg} ({i}/{n})") if n and i % 10 == 0 else None
        picks = vstrat.pick_longterm(cfg, params, ms, market_data,
                                     kline_raw_fn=cached_raw, nav_fn=nav_fn,
                                     progress=progress)
        out["picks"] = picks
        st = picks.get("stats") or {}
        log(f"  长期标的池 {st.get('universe')} 只 → 完成分析 {st.get('analyzed')} 只"
            f"（失败 {st.get('failed')}），达到推荐线 {st.get('passed')} 只")
        dfs = picks.get("stocks")
        if isinstance(dfs, pd.DataFrame) and not dfs.empty:
            top = dfs.head(3)
            log("  长期 Top3: " + "、".join(
                f"{r['name']}({r['score']})" for _, r in top.iterrows()))
        dff = picks.get("funds")
        if isinstance(dff, pd.DataFrame) and not dff.empty:
            log("  基金 Top3: " + "、".join(
                f"{r['name']}({r['score']})" for _, r in dff.head(3).iterrows()))
    except Exception as e:
        log(f"  长期评分失败: {e}")
        traceback.print_exc()
        out["picks"] = {"stocks": pd.DataFrame(), "funds": pd.DataFrame(),
                        "stats": {"error": str(e)}}

    # ETF 配置载体（无财报数据，不参与价值评分，只给分类与近期表现）
    try:
        etfs = vstrat.build_etf_universe(
            market_data.get("etf_spot"), cfg,
            per_group=int(lt_cfg.get("etf_per_group", 2)))
        for e in etfs:
            k = cached_kline(e["code"], "etf")
            if k is not None and len(k) > 61:
                c = k["close"].to_numpy(dtype=float)
                e["mom_20"] = round((c[-1] / c[-21] - 1) * 100, 2)
                e["mom_60"] = round((c[-1] / c[-61] - 1) * 100, 2)
        out["etfs"] = etfs
        log(f"  ETF 配置载体 {len(etfs)} 只（按主题分组抽样）")
    except Exception as e:
        log(f"  ETF 载体失败: {e}")
        out["etfs"] = []

    # ---- 2.5) 指数估值（中信建投）：补上长期轨缺失的估值锚 --------------------
    # 现有链路对 ETF 完全没有估值数据——腾讯行情接口的 PE/PB 字段对 ETF 为空，
    # fundamentals 里明确把它降级成「无」。中信建投的指数估值接口正好补这块。
    #
    # ⚠️ 只做**展示**，不接入六维评分。理由：长期轨的 120 日 IC=+0.155(t=5.24)
    # 是在「没有这一项输入」的条件下验证出来的；中途往评分里塞新因子，会让已
    # 验证的结论失效，而收益无法归因。要接入必须先做一次完整的前向验证。
    out["index_valuation"] = []
    ds_cfg = (cfg.get("datasource") or {})
    if csc_source.available() and ds_cfg.get("csc_index_valuation", True):
        try:
            rows = csc_source.index_list(ds_cfg.get("csc_index_type", "broad"),
                                        page_size=200)
            out["index_valuation"] = rows
            # 「已落库」这句话必须来自写入结果，而不是来自「我调了写库函数」。
            # 实测写入失败时（老库缺列）旧写法照样打印「已落库」。
            _w = csc_source.drain_persist().get("index_snapshot", 0)
            # 同源指数对（全收益版 / 价格版）：估值五项逐一相同 ⇒ 同一个敞口。
            # 只标注不删除（两个代码各有 ETF 跟踪），展示层合并成一行。
            _dups = csc_source.mark_same_exposure(rows)
            if _dups:
                _names = [f"{x.get('securityCode')}≡{'/'.join(x['_same_exposure'])}"
                          for x in rows if x.get("_exposure_primary")
                          and x.get("_same_exposure")]
                log(f"  指数估值（中信建投）: {len(rows)} 个指数"
                    f"（已落库 index_snapshot {_w} 行）；识别到 {_dups} 行"
                    f"估值同源（全收益/价格版）：{'、'.join(_names[:4])}")
            elif not rows:
                log("  指数估值（中信建投）: 上游无数据")
            elif _w:
                log(f"  指数估值（中信建投）: {len(rows)} 个指数（已落库 index_snapshot {_w} 行）")
            elif not store.enabled():
                log(f"  指数估值（中信建投）: {len(rows)} 个指数（数据层已关闭，未落库）")
            else:
                log(f"  指数估值（中信建投）: {len(rows)} 个指数取到，但落库 0 行"
                    f"（err={store.last_error()}）")
            # 标出成立年限：报告里「PE 分位 4%」必须能看出这是 20 年窗口的数字
            # 还是 20 个月窗口的数字。实测 PE=141 的指数也挂着 4% 分位——把这种
            # 行当「便宜」读会直接读反。
            _today_d = datetime.strptime(today, "%Y-%m-%d").date()
            for _x in rows:
                _x["_age_years"] = _age_years(_x.get("publishDate"), _today_d)

            # 给分位最低的少数指数补一次历史序列：分位只是一个数字，
            # 拉出曲线才能区分「长期就在低位」与「刚从高位跌下来」。
            #
            # 窗口由 `index_valuation` 自适应：上游对「窗口长于指数成立年限」的
            # 请求返回 rc=102，模块内按 5y→3y→1y 降级。所以这里**不再用成立
            # 年限预先筛掉新指数**——那是之前把 rc=102 误当成「无数据」时的
            # 权宜之计，代价是最便宜的那几个指数一条曲线都拿不到。
            # 实际窗口随结果带回来（`_window_used`），报告里照实标。
            _hist_n = int(ds_cfg.get("csc_index_valuation_history", 3))
            if rows and _hist_n > 0:
                _cap = max(_hist_n, int(ds_cfg.get("csc_index_valuation_attempts", 8)))
                _pool = [x for x in sorted(
                    rows, key=lambda x: _as_float(x.get("pePercentile")))
                    if x.get("_exposure_primary")    # 同源对只取代表行，省一次请求
                    and str(x.get("securityCode") or "")]
                _ok, _tried, _win = 0, 0, {}
                for _x in _pool:
                    if _ok >= _hist_n or _tried >= _cap:
                        break
                    _c = str(_x.get("securityCode"))
                    _m = str(_x.get("marketCode") or "")
                    _tried += 1
                    _v = csc_source.index_valuation(_c, _m, period="5y", metric="pe")
                    if _v.get("history"):
                        _ok += 1
                        _x["_hist_points"] = len(_v["history"])
                        _x["_hist_window"] = _v.get("_window_used")
                        _w_used = _v.get("_window_used") or "?"
                        _win[_w_used] = _win.get(_w_used, 0) + 1
                _miss = csc_source.drain_valuation_miss()
                if _ok:
                    _mix = "、".join(f"{k}×{v}" for k, v in sorted(_win.items()))
                    log(f"  指数估值历史（中信建投）: {_ok} 条序列已落库 index_valuation"
                        f"（尝试 {_tried} 个，窗口 {_mix}，写入 "
                        f"{csc_source.drain_persist().get('index_valuation', 0)} 行）"
                        + (f"；{len(_miss)} 个未取到" if _miss else ""))
                elif _tried:
                    log(f"  指数估值历史（中信建投）: {_tried} 个均未取到"
                        f"｜上游原因: {csc_source.last_error()}")
                # 逐条原因单独列：都表现为「返回空」，但「该指数没那么长历史」
                # 与「这次没打通」要能一眼分开。
                for _k, _v in list(_miss.items())[:3]:
                    log(f"    · {_k}: {_v}")
        except Exception as e:
            log(f"  指数估值失败(不影响主流程): {e}")

    # ---- 2.6) 长期推荐的行业相对位置（中信建投） -----------------------------
    # 同样只做展示：回答「这只股所属行业整体便宜，还是它自己便宜」。
    # 逐只一次请求，因此只对最终推荐的 Top N 取，避免拖慢主流程。
    out["industry_rank"] = {}
    if csc_source.available() and ds_cfg.get("csc_industry_rank", True):
        try:
            dfs_ = (out.get("picks") or {}).get("stocks")
            if isinstance(dfs_, pd.DataFrame) and not dfs_.empty:
                _lim = int(ds_cfg.get("csc_industry_rank_top", 10))
                _targets = list(dfs_.head(_lim).itertuples(index=False))
                got = {}
                for _r in _targets:
                    _code = str(getattr(_r, "code"))
                    _mkt = str(getattr(_r, "market", "cn") or "cn")
                    _ir = csc_source.industry_rank(_code, "pe")
                    if _ir.get("rank") or _ir.get("industry"):
                        got[_code] = {"code": _code, "market": _mkt, **_ir}
                out["industry_rank"] = got
                log(f"  行业排名（中信建投）: {len(got)}/{len(_targets)} 只取到"
                    f"（已落库 industry_rank "
                    f"{csc_source.drain_persist().get('industry_rank', 0)} 行）")
        except Exception as e:
            log(f"  行业排名失败(不影响主流程): {e}")

    # ---- 3) 保存今日长期推荐（供未来跟踪验证） ------------------------------
    try:
        out["picks_path"] = vtrack.save_picks(out.get("picks") or {}, ms, today)
    except Exception as e:
        log(f"  长期推荐落盘失败: {e}")

    # ---- 4) 跟踪结算（按 5/20/60/120/250 交易日 + 基准超额） -----------------
    try:
        track = vtrack.settle_tracking(cached_kline, cfg, today)
        out["track"] = track
        if track.get("summary"):
            parts = [f"{n}日 {d['avg_ret']:+.2f}%/胜率{d['win_rate']:.0f}%"
                     f"/超额{(d['avg_excess'] or 0):+.2f}%"
                     for n, d in sorted(track["summary"].items())]
            log(f"  长期跟踪（{track['batches']} 批）: " + "；".join(parts))
        else:
            log(f"  长期跟踪: {track.get('note', '暂无到期里程碑')}")
    except Exception as e:
        log(f"  长期跟踪失败: {e}")
        traceback.print_exc()
        out["track"] = None

    # ---- 5) 历史回放 + 长期模型选型（结果按 TTL 缓存，默认 3 天） -----------
    out["replay"], out["selection"] = None, {}
    try:
        sel, rep = _long_replay_and_select(cfg, params, cached_raw, no_replay, today, log)
        out["selection"]["long"] = sel
        out["replay"] = rep
    except Exception as e:
        log(f"  长期回放/选型失败: {e}")
        traceback.print_exc()

    return out


def _long_replay_and_select(cfg, params, cached_raw, no_replay, today, log):
    """历史回放 + 长期权重方案选型。回放结果带 TTL 缓存（默认 3 天）。"""
    lt_cfg = cfg.get("longterm", {}) or {}
    ttl_days = float(lt_cfg.get("replay_ttl_days", 3))
    cache_path = os.path.join(ROOT, "state", "longterm_replay.json")
    rep, panel = None, None
    if not no_replay and os.path.exists(cache_path):
        age = (time.time() - os.path.getmtime(cache_path)) / 86400
        if age < ttl_days:
            try:
                with open(cache_path, encoding="utf-8") as f:
                    cached = json.load(f)
                rep = cached.get("summary")
                panel = pd.DataFrame(cached.get("panel") or [])
                log(f"  历史回放缓存命中（{age:.1f} 天前，TTL {ttl_days:.0f} 天）")
            except Exception:
                rep, panel = None, None

    if rep is None or panel is None or panel.empty:
        if no_replay:
            log("  历史回放已跳过（--no-replay）")
            return None, None
        # 回放样本：用当日池子里市值靠前的一批，控制请求量
        n_rep = int(lt_cfg.get("replay_symbols", 24))
        try:
            spot = fetcher.fetch_cn_rank_by_amount(400)
            uni = vstrat.build_stock_universe(spot, cfg)[:n_rep]
        except Exception:
            uni = []
        if len(uni) < 8:
            log("  历史回放样本不足，跳过")
            return None, None
        log(f"  历史回放（{len(uni)} 只 × 每 20 交易日一个时点）…")
        t0 = time.time()
        res = vtrack.historical_replay(
            uni, cached_raw, {}, cfg, params,
            n_periods=int(lt_cfg.get("replay_periods", 18)),
            step=int(lt_cfg.get("replay_step", 20)),
            min_history=int(lt_cfg.get("replay_min_history", 280)))
        panel = res.pop("panel", None)
        rep = res
        log(f"  历史回放完成 {rep.get('n_obs')} 观测 / {rep.get('n_periods')} 截面 "
            f"（{time.time() - t0:.1f}s）")
        if panel is not None and not panel.empty:
            try:
                with open(cache_path, "w", encoding="utf-8") as f:
                    json.dump({"summary": rep,
                               "panel": panel.to_dict("records")},
                              f, ensure_ascii=False, default=str)
            except Exception as e:
                log(f"  回放缓存写入失败: {e}")

    if rep and rep.get("milestones"):
        for n, h in sorted(rep["milestones"].items()):
            d = (h.get("factors") or {}).get("score")
            if d:
                log(f"  回放 {n} 日: 综合评分 IC={d['ic_mean']:+.4f} "
                    f"ICIR={d['icir']:+.3f} t={d['t']:+.2f} "
                    f"正比例={d['positive_rate']:.2f}")
    sel = msel.select_long(panel, cfg) if panel is not None and not panel.empty else None
    if sel:
        log(f"  长期权重选型: 最优方案「{sel['best']}」"
            f"（IC 均值 {sel['candidates'][0]['ic_avg']:+.4f}，"
            f"方案间极差 {sel['ic_spread']:.4f}）")
        log(f"    结论: {sel['verdict']}")
    return sel, rep


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
