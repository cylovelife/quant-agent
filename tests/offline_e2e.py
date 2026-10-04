#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""离线端到端自检：不写真实 state/，用临时目录跑通「账本 → 离场模型 → 体检 → 报告」。

用法：
    python tests/offline_e2e.py            # 有缓存则离线跑
    python tests/offline_e2e.py --refresh  # 强制重新拉 K 线（需要网络）

覆盖的回归点（每一条都曾真实出过问题）：
1. 建仓幂等：同一天重复建仓必须新增 0 只；
2. 卖出分层：噪声级负期望（|t| 很小）不得被判为「建议卖出」；
3. 报告渲染：章节齐全、止损/止盈标签不串（不止损 ≠ 不止盈）、收缩系数不为空。
4. 迭代完整性：复盘记账按日期幂等（重复运行不翻倍样本）、无新样本时不重复调权、
   outcomes 的 code 按字符串保存（前导零不丢）、算出的新权重真的写回并落盘。
5. 复盘新鲜度：行情未推进到推荐日之后时不得复盘（否则灌入一批 ret=0 的退化样本）。
6. 账期按「行情最新交易日」而非墙钟日期命名（否则盘前运行会产出「日期今天、价格昨天」
   的推荐，把两天的涨跌记成一天）。
7. 调权证据门禁：有效交易日不足不调权、全天 ret 恒 0 的退化日被剔除、
   t 值按 DEFF 修正后的有效样本量计算（否则同日相关性会把 t 放大 sqrt(DEFF) 倍）。
8. 阶段缓存 + 心跳：K线签名顺序无关且对内容敏感（否则旧结果会被喂给新数据）、
   numpy 标量经 JSON 往返后仍是原生类型（否则数值比较静默失效）、
   配置变更与 --no-cache 必须绕过缓存、长阶段必须留下心跳（否则无法区分在算与被冻）。
9. 选型层口径：跨持有期只比日均收益（总收益/总暴露日数，不是日均比率的平均）、
   尾部恶化超容忍度时不得建议切换。
10. 数据完整性披露：抓取超预算导致整片跳过某市场时，报告必须显式提示缺口
   （否则用户会把「没取到数据」误读成「今天没有符合条件的标的」）；
   健康市场不得误报。
"""
import argparse
import json
import os
import pickle
import shutil
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, "src"))

CACHE = "/tmp/quant_exit_e2e_klines.pkl"
TMP = "/tmp/quant_exit_e2e"


def build_kline_map(refresh: bool = False) -> dict:
    if not refresh and os.path.exists(CACHE):
        with open(CACHE, "rb") as f:
            kmap = pickle.load(f)
        if kmap:
            print(f"[cache] 复用 K 线缓存 {CACHE}（{len(kmap)} 只）")
            return kmap
    import glob
    import fetcher
    files = sorted(glob.glob(os.path.join(ROOT, "state", "picks_*.json")))
    if not files:
        raise SystemExit("state/ 下没有 picks_*.json，先跑一次 run.py")
    with open(files[-1], encoding="utf-8") as f:
        rec = json.load(f)
    kmap = {}
    for pk in rec.get("picks", []):
        code, market = str(pk["code"]), pk["market"]
        try:
            if market == "fund":
                nav = fetcher.fetch_fund_nav(code, limit=300)
                if nav.empty:
                    continue
                nav = nav.rename(columns={"nav": "close"})
                for c in ("open", "high", "low"):
                    nav[c] = nav["close"]
                nav["volume"] = 1.0
                nav["amount"] = 0.0
                kmap[(market, code)] = nav
            else:
                k = fetcher.fetch_kline(code, market, mkt_id=pk.get("mkt_id"))
                if k is not None and len(k):
                    kmap[(market, code)] = k
        except Exception:
            continue
    with open(CACHE, "wb") as f:
        pickle.dump(kmap, f)
    print(f"[fetch] 已抓取并缓存 {len(kmap)} 只标的 K 线 → {CACHE}")
    return kmap


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--refresh", action="store_true")
    args = ap.parse_args()

    import pandas as pd  # noqa: F401  (确保依赖可用)

    import portfolio
    import report as rpt
    from exit_model import (NEG_T_WEAK, exit_advice, fit_exit_model,
                            parameter_scan)

    shutil.rmtree(TMP, ignore_errors=True)
    os.makedirs(TMP, exist_ok=True)
    portfolio.BOOK_PATH = os.path.join(TMP, "positions.json")
    portfolio.TRADES_PATH = os.path.join(TMP, "trades.csv")
    rpt.REPORT_DIR = os.path.join(TMP, "reports")
    os.makedirs(rpt.REPORT_DIR, exist_ok=True)

    kmap = build_kline_map(args.refresh)
    params = json.load(open(os.path.join(ROOT, "params", "params.json"), encoding="utf-8"))
    cfg = json.load(open(os.path.join(ROOT, "config.json"), encoding="utf-8"))
    today = "2026-09-20"
    cap = int(cfg.get("exit_model", {}).get("max_new_positions_per_market", 5))
    fails = []

    # ---- 1) 账本：回填 → 结算 → 建仓 → 幂等性 ----
    book = portfolio.load_book()
    cal = portfolio.trading_calendar(kmap)
    pf = portfolio.latest_picks_file(before=today)

    def cached_kline(code, market, mkt_id=None):
        return kmap.get((market, str(code)))

    st = portfolio.backfill_from_picks(book, pf, cached_kline, params, today, cal,
                                       max_per_market=cap)
    print(f"[1] 回填 {os.path.basename(pf)}: 开仓 {st['opened']} / 已平 {st['closed']} "
          f"/ 跳过 {st['skipped']}")
    held = portfolio.open_positions(book)
    per_mkt = {}
    for p in held:
        per_mkt[p["market"]] = per_mkt.get(p["market"], 0) + 1
    print(f"    未平仓 {len(held)} 只，每市场 {per_mkt}（上限 {cap}）")
    if any(v > cap for v in per_mkt.values()):
        fails.append(f"回填超过每市场上限 {cap}: {per_mkt}")

    quotes = {(p["market"], str(p["code"])): {"price": float(kmap[(p["market"], str(p["code"]))].iloc[-1]["close"]),
                                             "pct": 0.0,
                                             "date": str(kmap[(p["market"], str(p["code"]))].iloc[-1]["date"])[:10]}
              for p in held if (p["market"], str(p["code"])) in kmap}
    closed = portfolio.settle_book(book, quotes, cal, params, today)
    print(f"    结算：行情命中 {len(quotes)}/{len(held)}，平仓 {len(closed)} 只")

    picks_json = json.load(open(os.path.join(ROOT, "state", "picks_2026-09-20.json"),
                                encoding="utf-8"))
    pbm = {}
    for p in picks_json["picks"]:
        pbm.setdefault(p["market"], []).append(p)
    added1 = portfolio.add_positions(book, pbm, params, today, cap)
    added2 = portfolio.add_positions(book, pbm, params, today, cap)
    print(f"[2] 建仓 {len(added1)} 只；重复建仓 {len(added2)} 只（期望 0）")
    if added2:
        fails.append(f"建仓不幂等：重复运行为 {len(added2)} 只")
    per_mkt = {}
    for p in portfolio.open_positions(book):
        per_mkt[p["market"]] = per_mkt.get(p["market"], 0) + 1
    if any(v > 2 * cap for v in per_mkt.values()):
        fails.append(f"当日建仓超过 2×上限 {cap}: {per_mkt}")

    # ---- 2) 离场模型 + 卖出建议 ----
    model = fit_exit_model(kmap, params)
    val = model["validation"]
    print(f"[3] 离场模型 {model['n_symbols']}只/{model['n_trades']}笔，状态桶 "
          f"{len(model['states'])}，τ*={val['tau'] * 100:+.1f}%，t_min*={val['t_min']}")
    print(f"    OOS 基线 均{val['oos']['baseline']['avg_ret'] * 100:+.3f}% / "
          f"模型 均{val['oos']['model']['avg_ret'] * 100:+.3f}% "
          f"(提前离场 {val['oos']['model']['early_exit_ratio'] * 100:.0f}%)")

    advice = exit_advice(portfolio.open_positions(book), model, params)
    from collections import Counter
    dist = Counter(a["advice"] for a in advice)
    print(f"[4] 卖出建议 {dict(dist)}")
    for a in advice:
        if a["advice"] == "SELL" and a.get("expect_t") is not None \
                and a["expect_t"] > NEG_T_WEAK:
            fails.append(f"噪声级负期望被判卖出: {a['code']} t={a['expect_t']}")

    # ---- 3) 参数体检 ----
    scan = parameter_scan(kmap, params)
    print(f"[5] 规则体检 {scan['n_signals']} 笔；止损档 "
          f"{[(it['stop'], round(it['sharpe'], 3)) for it in scan['by_stop']]}")

    # ---- 4) 报告 ----
    md_path, html_path = rpt.generate_report(
        {}, {}, {}, {}, params, None, None, None,
        exit_advice_list=advice, exit_model=model, book=book, param_scan=scan)
    txt = open(md_path, encoding="utf-8").read()
    heads = [l for l in txt.split("\n") if l.startswith("## ")]
    print(f"[6] 报告 {len(txt)} 字符，章节 {len(heads)} 个: {heads}")
    for want in ("五、卖出建议", "六、持仓账本", "七、离场模型"):
        if not any(want in h for h in heads):
            fails.append(f"报告缺少章节：{want}")
    if "**止盈线对比" in txt and "**持有期对比" in txt:
        take_block = txt.split("**止盈线对比")[1].split("**持有期对比")[0]
        if "不止损" in take_block:
            fails.append("止盈线表格误用了止损标签（不止损 ≠ 不止盈）")
    if "k=）" in txt or "（k=-）" in txt:
        fails.append("收缩系数显示为空")

    # ---- 5) 结论分支回归：避免「未跑赢基线」与正增益数字自相矛盾 ----
    def _v(avg_model, win_model, p05_model, exit_ratio):
        base = {"n": 100, "avg_ret": 0.0, "median_ret": 0.0, "win_rate": 0.45,
                "p05": -0.09, "worst": -0.2, "avg_days": 5.0, "early_exit_ratio": 0.0}
        mod = dict(base, avg_ret=avg_model, win_rate=win_model, p05=p05_model,
                   avg_days=4.4, early_exit_ratio=exit_ratio)
        return {"horizon": 5, "cut_date": "2026-06-01", "score_edges": [69, 75],
                "tau": 0.0, "t_min": -1.2, "tau_scan": [],
                "is": {"baseline": base, "model": mod},
                "oos": {"baseline": base, "model": mod, "model_loose": mod},
                "lift": {"avg_ret": avg_model, "win_rate": win_model - 0.45,
                         "p05": p05_model + 0.09, "max_dd": 0.0}}

    cases = [
        # (名称, validation, 必须出现的措辞, 禁止出现的措辞)
        ("收益正但胜率降、回撤变差", _v(0.0012, 0.418, -0.1150, 0.21),
         "未能取得一致改善", "未跑赢基线"),
        ("收益负但回撤改善", _v(-0.0003, 0.464, -0.0773, 0.57),
         "收益未跑赢基线", None),
        ("模型未触发", _v(0.0, 0.45, -0.09, 0.0), "一次都没触发", None),
    ]
    for name, vm, must_have, forbid in cases:
        block = rpt.exit_model_md({"horizon": vm["horizon"], "min_bucket_n": 20,
                                   "shrink_k": 25.0, "score_edges": vm["score_edges"],
                                   "tau": vm["tau"], "t_min": vm["t_min"],
                                   "states": {"x": {}}, "coarse": {}, "global_v": 0.0,
                                   "n_obs": 1, "n_trades": 1, "n_symbols": 1,
                                   "date_range": ["2025-01-01", "2026-01-01"],
                                   "validation": vm})
        ok = must_have in block
        bad = forbid is not None and forbid in block
        print(f"[7] 结论分支「{name}」: {'OK' if ok and not bad else 'FAIL'}")
        if not ok:
            fails.append(f"结论分支「{name}」缺少预期措辞：{must_have}")
        if bad:
            fails.append(f"结论分支「{name}」出现矛盾的措辞：{forbid}")

    # ---- 6) 迭代完整性：复盘记账幂等 + 无新样本不调权 + 新权重真的落盘 ----
    # 这三条都曾真实出问题：盲 append 把同一批样本记两次（名义 N 翻倍 → t 值假显著）；
    # 同日复跑让权重在无新信息时连续漂移；算出的新权重没写回 params，
    # 导致版本号照升而数字一动不动，整个调权闭环静默失效。
    import tempfile
    import evaluate as ev

    itmp = tempfile.mkdtemp(prefix="qt_iter_e2e_")
    ev.STATE, ev.PARAMS_DIR = itmp, itmp
    json.dump({"date": "2026-09-20", "picks": [
        {"code": "600519", "name": "贵州茅台", "market": "cn", "score": 70,
         "entry_price": 100.0, "f_boll": 60, "f_rsi": 50, "f_volume": 40,
         "f_trend": 30, "f_macd": 20, "f_drawdown": 10},
        {"code": "000333", "name": "美的集团", "market": "cn", "score": 68,
         "entry_price": 50.0, "f_boll": 55, "f_rsi": 45, "f_volume": 35,
         "f_trend": 25, "f_macd": 15, "f_drawdown": 8}]},
        open(os.path.join(itmp, "picks_2026-09-20.json"), "w"), ensure_ascii=False)
    qfn = lambda c, m, i=None: {"600519": 102.0, "000333": 49.0}[c]  # noqa: E731
    oc = os.path.join(itmp, "outcomes.csv")
    for _ in range(3):
        ev.evaluate_previous(qfn, "2026-09-21")
    n_rows = len(pd.read_csv(oc))
    print(f"[8] 复盘记账幂等：3 次复盘后 outcomes 行数 {n_rows}（期望 2）")
    if n_rows != 2:
        fails.append(f"复盘记账非幂等：3 次复盘记了 {n_rows} 行（期望 2）")
    saved_codes = [str(c) for c in pd.read_csv(oc, dtype={"code": str})["code"]]
    if saved_codes != ["600519", "000333"]:
        fails.append(f"outcomes 的 code 未按字符串保存（前导零会丢）：{saved_codes}")

    # 6b) 构造一批「boll 强预测收益、其余因子无信息」的样本，
    #     调权后 boll 权重必须真的变大，且落盘的 params.json 与之一致。
    utmp = tempfile.mkdtemp(prefix="qt_iter_write_")
    ev.STATE, ev.PARAMS_DIR = utmp, utmp
    urows = []
    for i in range(40):
        fb = 20 + i * 1.5                       # 单调递增
        urows.append({"date": "2026-09-20", "code": f"S{i:03d}", "market": "cn",
                      "score": 60, "f_boll": fb, "f_rsi": 50.0, "f_volume": 30.0,
                      "f_trend": 40.0, "f_macd": 25.0, "f_drawdown": 15.0,
                      "ret": (fb - 50) / 1000.0})   # 与 f_boll 完全共变
    pd.DataFrame(urows).to_csv(os.path.join(utmp, "outcomes.csv"), index=False)
    uparams = {"weights": {"boll": .25, "rsi": .25, "volume": .15,
                           "trend": .1, "macd": .15, "drawdown": .1},
               "iteration": {"min_samples": 20, "lr": 0.25}}
    r1 = ev.update_weights(uparams, min_samples=20, lr=0.25, min_days=1)
    r2 = ev.update_weights(uparams, min_samples=20, lr=0.25, min_days=1)
    print(f"[8] 无新样本调权守卫：首次 {r1['updated']} / 复跑 {r2['updated']}（期望 True / False）")
    if not r1["updated"] or r2["updated"]:
        fails.append(f"调权守卫失效：首次 {r1['updated']}，复跑 {r2['updated']}（应 True/False）")
    if r1.get("data_through") != "2026-09-20":
        fails.append(f"调权基线日期不对：{r1.get('data_through')}")
    # 新权重必须写回 params 内存对象
    if uparams["weights"]["boll"] <= 0.25:
        fails.append(f"新权重未写回 params：boll={uparams['weights']['boll']}（应 >0.25）")
    # 且必须真的落盘（旧 bug：算完丢掉，盘上还是旧数字）
    on_disk = json.load(open(os.path.join(utmp, "params.json"), encoding="utf-8"))
    print(f"[8] 权重重算落盘：boll {0.25} → 内存 {uparams['weights']['boll']} / 盘上 {on_disk['weights']['boll']}")
    if on_disk["weights"]["boll"] <= 0.25:
        fails.append(f"新权重未落盘：盘上 boll={on_disk['weights']['boll']}（应 >0.25）")
    if on_disk["weights"] != uparams["weights"]:
        fails.append("落盘权重与内存不一致")
    if on_disk.get("iteration", {}).get("last_used_date") != "2026-09-20":
        fails.append(f"last_used_date 未落盘：{on_disk.get('iteration')}")
    shutil.rmtree(itmp, ignore_errors=True)
    shutil.rmtree(utmp, ignore_errors=True)

    # ---- 7) 复盘新鲜度守卫：行情没推进到推荐日之后，不得复盘 ----
    # 曾真实发生：推荐当天深夜跑第二次、或次日凌晨盘前补跑，取到的最新价
    # 仍是推荐日自己的收盘价 → 一整批 ret=0 的退化样本进 outcomes.csv，
    # 把相关性与 t 值一起稀释向 0，等于用假数据做迭代。
    ftmp = tempfile.mkdtemp(prefix="qt_fresh_")
    ev.STATE, ev.PARAMS_DIR = ftmp, ftmp
    json.dump({"date": "2026-09-20", "picks": [
        {"code": "600519", "name": "贵州茅台", "market": "cn", "score": 70,
         "entry_price": 100.0, "f_boll": 60, "f_rsi": 50, "f_volume": 40,
         "f_trend": 30, "f_macd": 20, "f_drawdown": 10}]},
        open(os.path.join(ftmp, "picks_2026-09-20.json"), "w"), ensure_ascii=False)
    q1 = lambda c, m, i=None: 102.0  # noqa: E731

    def _kfn(dates):
        def f(code, market, mkt_id=None):
            return pd.DataFrame({"date": pd.to_datetime(dates),
                                 "close": [1.0] * len(dates)})
        return f

    fpath = os.path.join(ftmp, "outcomes.csv")
    s_stale = ev.evaluate_previous(q1, "2026-09-21", kline_fn=_kfn(["2026-09-18", "2026-09-20"]))
    n_stale = len(pd.read_csv(fpath)) if os.path.exists(fpath) else 0
    print(f"[9] 新鲜度守卫（行情未推进）：skipped={bool(s_stale.get('skipped'))} "
          f"outcomes 行数={n_stale}（期望 True / 0）")
    if not s_stale.get("skipped") or n_stale != 0:
        fails.append(f"新鲜度守卫失效：行情未推进仍复盘"
                     f"（skipped={s_stale.get('skipped')}, rows={n_stale}）")

    s_fresh = ev.evaluate_previous(q1, "2026-09-21", kline_fn=_kfn(["2026-09-20", "2026-09-21"]))
    n_fresh = len(pd.read_csv(fpath)) if os.path.exists(fpath) else 0
    print(f"[9] 新鲜度守卫（行情已推进）：skipped={bool(s_fresh.get('skipped'))} "
          f"outcomes 行数={n_fresh}（期望 False / 1）")
    if s_fresh.get("skipped") or n_fresh != 1:
        fails.append(f"新鲜度守卫过严：行情已推进仍拒评"
                     f"（skipped={s_fresh.get('skipped')}, rows={n_fresh}）")
    shutil.rmtree(ftmp, ignore_errors=True)

    # ---- 8) 账期取「行情最新交易日」，不取墙钟日期 ----
    # 盘前/非交易日运行时最新 K 线仍是上一交易日的。若按墙钟日期给推荐命名，
    # 会得到一份「日期是今天、价格是昨天」的推荐 → 次日复盘把两天的涨跌记成一天。
    dtmp = tempfile.mkdtemp(prefix="qt_date_")
    ev.STATE = dtmp
    p = ev.save_today_picks([{"code": "600519"}], {}, data_date="2026-09-21")
    wrote = os.path.basename(p)
    d = json.load(open(p, encoding="utf-8"))
    print(f"[10] 账期落盘：文件名 {wrote} / 内嵌 date={d.get('date')}（期望 picks_2026-09-21.json / 2026-09-21）")
    if wrote != "picks_2026-09-21.json" or d.get("date") != "2026-09-21":
        fails.append(f"账期未按行情交易日命名：{wrote} / {d.get('date')}")
    shutil.rmtree(dtmp, ignore_errors=True)

    # ---- 9) 调权的三道证据门禁：交易日数 / 退化日 / DEFF 修正 ----
    # 曾真实发生：9 个交易日的样本被当成 220+ 笔独立观测，t 值被放大
    # sqrt(DEFF)≈3.2 倍，把 rsi 的纯噪声（按日分组 IC=+0.017、t=+0.11）
    # 判成「显著负相关 t=-3.82」并压到权重下限 0.05。
    gtmp = tempfile.mkdtemp(prefix="qt_gate_")
    ev.STATE, ev.PARAMS_DIR = gtmp, gtmp
    grows = []
    for di, dstr in enumerate(["2026-09-17", "2026-09-18"]):
        for i in range(20):
            grows.append({"date": dstr, "code": f"G{di}{i:03d}", "market": "cn",
                          "score": 60, "f_boll": 20 + i * 1.5, "f_rsi": 50.0,
                          "f_volume": 30.0, "f_trend": 40.0, "f_macd": 25.0,
                          "f_drawdown": 15.0,
                          "ret": (i - 10) / 500.0 + (0.02 if di == 0 else -0.02)})
    # 再塞一个退化日：全天 ret 恒为 0（实测 2026-09-11 就是这种）
    grows += [{"date": "2026-09-19", "code": f"Z{i:03d}", "market": "cn", "score": 60,
               "f_boll": 40.0, "f_rsi": 50.0, "f_volume": 30.0, "f_trend": 40.0,
               "f_macd": 25.0, "f_drawdown": 15.0, "ret": 0.0} for i in range(20)]
    gpath = os.path.join(gtmp, "outcomes.csv")
    pd.DataFrame(grows).to_csv(gpath, index=False)

    gparams = {"weights": {"boll": .25, "rsi": .25, "volume": .15,
                           "trend": .1, "macd": .15, "drawdown": .1},
               "iteration": {}}
    rg = ev.update_weights(gparams, min_samples=20, lr=0.25, min_days=20)
    print(f"[11] 交易日门槛：updated={rg['updated']} / {rg.get('reason')}")
    if rg["updated"]:
        fails.append("有效交易日不足 20 天仍调权")
    if "有效交易日不足" not in str(rg.get("reason")):
        fails.append(f"门槛理由不明确：{rg.get('reason')}")

    clean, qinfo = ev._drop_degenerate(pd.read_csv(gpath))
    print(f"[11] 退化日剔除：识别 {qinfo.get('degenerate_days')}，样本 "
          f"{len(grows)} → {len(clean)}（期望 ['2026-09-19'] / 40）")
    if qinfo.get("degenerate_days") != ["2026-09-19"] or len(clean) != 40:
        fails.append(f"退化日未被正确剔除：{qinfo.get('degenerate_days')} / {len(clean)}")

    deff, ndays, icc = ev._design_effect(clean)
    print(f"[11] DEFF 修正：{ndays} 个交易日 ICC={icc:.3f} → DEFF={deff:.2f}（期望 >1）")
    if deff <= 1.0:
        fails.append(f"DEFF 未生效：{deff}")
    shutil.rmtree(gtmp, ignore_errors=True)

    # ---- 10) 选型层两道纪律：跨持有期比「日均」+ 尾部守卫 ----
    # 网格里持有期不同（3/5/8 日）。用原始平均收益比较，等于把「敞口更大」当成
    # 「策略更优」：持有 8 日的组合多吃 3 天漂移，原始均值必然更高。
    import numpy as np
    import model_select as msel

    rng = np.random.RandomState(7)
    sigs = [("2026-01-%02d" % (k % 28 + 1), 10.0,
             10.0 * np.cumprod(1 + rng.normal(0.004, 0.010, 8)))
            for k in range(300)]
    s5 = msel._rule_stats(sigs, 5, None, None)
    s8 = msel._rule_stats(sigs, 8, None, None)
    print(f"[12] 日均折算：持有5日 avg={s5['avg'] * 100:+.3f}% / "
          f"日均={s5['avg_per_day'] * 100:+.4f}% | 持有8日 "
          f"avg={s8['avg'] * 100:+.3f}% / 日均={s8['avg_per_day'] * 100:+.4f}%")
    if s8["avg"] <= s5["avg"]:
        fails.append("样本构造有误：持有更久却未取得更高原始均值")
    if abs(s8["avg_per_day"] - s5["avg_per_day"]) > 0.001:
        fails.append(f"日均折算未消除持有期差异：{s5['avg_per_day']} vs "
                     f"{s8['avg_per_day']}")
    if s5.get("avg_days") != 5.0 or s8.get("avg_days") != 8.0:
        fails.append(f"实际持有日数不对：{s5.get('avg_days')} / {s8.get('avg_days')}")

    # (a2) 口径必须锁定「总收益 / 总持有日数」，不能退化成「平均比率」——
    # 后者被 1 日就被止损打掉的样本主导（−5% 的 1 日止损贡献 −500%/日），
    # 会把带止损的规则算得极度难看。用带止损的信号集来区分两种口径。
    import exit_model as em
    rets, days = em._simulate_rule(sigs, 8, -0.03, None, return_days=True)
    proper = float(rets.mean()) / float(days.mean())
    naive = float(np.mean(rets / np.maximum(days, 1.0)))
    s_stop = msel._rule_stats(sigs, 8, -0.03, None)
    print(f"[12] 口径锁定（含止损，最短持有 {int(days.min())} 日）："
          f"实现={s_stop['avg_per_day'] * 100:+.4f}% | "
          f"总收益/总日数={proper * 100:+.4f}% | 平均比率={naive * 100:+.4f}%")
    # 容差取 1e-12 而非 1e-6：1e-6 会放过 round(...,5) 这种四舍五入。
    # 日收益量级只有 1e-3，round(...,5) 会把决策量压成 0.0038（两位有效数字），
    # 而候选方案之间的日均差异常在 1e-4~1e-5——四舍五入正好把真实差异抹平，
    # 选型就退化成靠舍入噪声挑参数。该断言同时守住「口径正确」与「精度够用」。
    if abs(s_stop["avg_per_day"] - proper) > 1e-12:
        fails.append(f"日均口径未用「总收益/总日数」或精度被舍入："
                     f"{s_stop['avg_per_day']!r} vs {proper!r}")
    if abs(proper - naive) < 1e-6:
        fails.append("样本无法区分两种日均口径，该断言失去意义")
    if int(days.min()) >= 8:
        fails.append(f"止损未生效（最短持有 {int(days.min())} 日），无法检验口径差异")

    # 尾部守卫：建议参数靠加深 5% 分位换日均收益 → 必须维持现配置
    rec = {"hold_days": 8, "stop_loss": None, "take_profit": None, "avg": 0.0064,
           "avg_per_day": 0.00080, "avg_days": 8.0, "p05": -0.1052}
    curf = {"hold_days": 5, "stop_loss": -0.05, "take_profit": 0.08, "avg": 0.0025,
            "avg_per_day": 0.00050, "avg_days": 5.0, "p05": -0.0721}
    v1, sw1 = msel._short_verdict([{"n_oos": 100}], 0.0003, 0.0003, 0.0, rec, curf)
    print(f"[12] 尾部守卫（5%分位恶化 3.3pct）：switch={sw1} | {v1[:40]}…")
    if sw1 or "维持当前配置" not in v1:
        fails.append(f"尾部守卫失效：仍建议切换（{v1}）")

    # 反向：尾部未恶化时不应误伤（该切就切）
    v2, sw2 = msel._short_verdict([{"n_oos": 100}], 0.0003, 0.0003, 0.0,
                                  dict(rec, p05=-0.0700), curf)
    print(f"[12] 尾部守卫（5%分位未恶化）：switch={sw2}（期望 True）")
    if not sw2:
        fails.append(f"尾部未恶化却未建议切换（守卫过严）：{v2}")

    # ---- 13) 阶段缓存 + 心跳 ----
    # 缓存省掉三段重算（离场模型 ≈155s / 体检 ≈60s / 选型 ≈48s），但它必须满足两条：
    #   (a) 内容一变就失效——否则会把旧结果喂给新数据；
    #   (b) 数值经 JSON 往返后仍是原生类型——否则 numpy 标量被 default=str 变成字符串，
    #       下游 `it['stop'] == cur` 这类数值比较会静默失效（不报错，只是结果悄悄错）。
    # 心跳解决另一个问题：长阶段日志一片空白时，「在算」与「被冻」看起来完全一样，
    # 项目里曾把一次机器休眠误判成「卡死 2 小时」。这三条都属于「错了不会自己暴露」，
    # 必须由测试兜住。
    import tempfile
    import time as _time
    import phase_cache as pc
    sys.path.insert(0, ROOT)
    import run as R

    ptmp = tempfile.mkdtemp(prefix="qt_cache_e2e_")
    _old_cache_dir = pc.CACHE_DIR
    _captured = []
    _old_log = R.log
    pc.CACHE_DIR = ptmp
    R.log = lambda msg: _captured.append(str(msg))
    try:
        # (a) 签名：与字典顺序无关，与内容强相关
        def _mk(seed, n=30):
            rng = np.random.default_rng(seed)
            cl = 10 + np.cumsum(rng.normal(0, .2, n))
            return pd.DataFrame({"date": [f"2026-08-{i + 1:02d}" for i in range(n)],
                                 "open": cl, "high": cl * 1.01, "low": cl * .99,
                                 "close": cl, "volume": rng.integers(1e5, 1e6, n)})

        km_a = {("cn", "600519"): _mk(1), ("cn", "000333"): _mk(2)}
        km_b = {("cn", "000333"): _mk(2), ("cn", "600519"): _mk(1)}
        km_c = {("cn", "600519"): _mk(1), ("cn", "000333"): _mk(99)}
        s_a, s_b, s_c = (pc.kline_signature(x) for x in (km_a, km_b, km_c))
        print(f"[13] K线签名：顺序无关={s_a == s_b} 内容敏感={s_a != s_c}")
        if s_a != s_b:
            fails.append("K线签名受字典顺序影响，缓存会无谓失效")
        if s_a == s_c:
            fails.append("K线内容变了签名却没变，缓存会把旧结果喂给新数据")

        # (b) numpy 标量必须降级为原生类型
        pc.save("t13", "types", {"stop": np.float64(-0.05), "n": np.int64(7),
                                 "flag": np.bool_(True), "nan": float("nan")})
        bk = pc.load("t13", "types")
        ok_types = (type(bk["stop"]) is float and type(bk["n"]) is int
                    and bk["flag"] is True and bk["nan"] is None)
        print(f"[13] JSON 往返类型：stop={type(bk['stop']).__name__} "
              f"n={type(bk['n']).__name__} flag={type(bk['flag']).__name__} "
              f"nan→{bk['nan']}")
        if not ok_types:
            fails.append(f"缓存往返后类型不安全（数值比较会静默失效）：{bk}")

        # (c) 命中 / 配置变更失效 / 强制重算
        calls = []

        def _fn():
            calls.append(1)
            return {"v": 42}

        pp = {"weights": {}, "score_threshold": 60, "backtest": {}}
        pc.clear("t13")
        R.cached_phase("t13", _fn, "sig", pp, {"x": 1})                 # 冷 → 计算
        hit = R.cached_phase("t13", _fn, "sig", pp, {"x": 1})           # 暖 → 命中
        R.cached_phase("t13", _fn, "sig", pp, {"x": 2})                 # 配置变 → 重算
        R.cached_phase("t13", _fn, "sig", pp, {"x": 1}, no_cache=True)  # 强制重算
        print(f"[13] 缓存 fn 调用次数 {len(calls)}（期望 3：冷启/配置变/强制重算）"
              f" 命中返回 {hit}")
        if len(calls) != 3:
            fails.append(f"缓存命中逻辑错误：fn 被调用 {len(calls)} 次（期望 3）")
        if hit != {"v": 42}:
            fails.append(f"命中未返回缓存值：{hit}")

        # (d) 损坏缓存必须当作未命中，不能把流程带崩
        with open(pc.path_for("t13", "corrupt"), "w") as f:
            f.write("{ 坏 json")
        try:
            bad = pc.load("t13", "corrupt")
            print(f"[13] 损坏缓存 → {bad}（期望 None 且不抛错）")
            if bad is not None:
                fails.append("损坏缓存未被当作未命中")
        except Exception as e:
            fails.append(f"损坏缓存导致异常：{e}")

        # (e) 心跳：长阶段必须留下「开始 / 仍在计算 / 完成」三类痕迹
        _captured.clear()
        with R.phase("自检阶段", every=1):
            _time.sleep(1.3)
        joined = "\n".join(_captured)
        has_start = "开始…" in joined
        has_beat = "仍在计算（已" in joined
        has_done = "完成（" in joined
        print(f"[13] 阶段心跳：开始={has_start} 心跳={has_beat} 完成={has_done}")
        if not (has_start and has_beat and has_done):
            fails.append(f"阶段心跳不完整：开始={has_start} 心跳={has_beat} "
                         f"完成={has_done}（无法区分「在算」与「被冻」）")
    finally:
        pc.CACHE_DIR = _old_cache_dir
        R.log = _old_log
        shutil.rmtree(ptmp, ignore_errors=True)

    # ---- 14) 数据完整性披露 ----
    # 抓取超预算会整片跳过某市场的标的，而报告照常生成——曾出现 cn / etf 两个市场被
    # 完全掐掉（评分通过 0 只）而报告看不出任何异常。用户会误读成「今天这个市场没有
    # 符合条件的标的」。缺数据而不自知比跑得慢危险得多，所以必须由报告显式披露。
    from report import MARKET_NAMES, data_health_md

    if data_health_md(None) != "" or data_health_md({}) != "":
        fails.append("data_health_md 对空输入应返回空串")
    healthy = {"cn": {"name": "cn 行情", "spent": 12.0, "budget": 180.0,
                      "tripped": False, "missing": 0}}
    if data_health_md(healthy) != "":
        fails.append("data_health_md 对健康数据不应报警（否则会变成狼来了）")
    bad = {"cn": {"name": "cn 行情", "spent": 193.0, "budget": 180.0,
                  "tripped": True, "missing": 18},
           "hk": {"name": "hk 行情", "spent": 6.0, "budget": 180.0,
                  "tripped": False, "missing": 0}}
    warn = data_health_md(bad)
    has_bits = all(s in warn for s in ("193", "180", "18", MARKET_NAMES.get("cn", "cn")))
    only_bad = warn.count("- **") == 1
    print(f"[14] 数据完整性披露：关键数字齐={has_bits} "
          f"只列问题市场={only_bad}（列出 {warn.count('- **')} 个）")
    if not has_bits:
        fails.append(f"数据完整性披露缺关键数字：{warn!r}")
    if not only_bad:
        fails.append("健康的 hk 市场被误报进数据完整性提示")
    # 局部缺口（未熔断但缺了几只）也要报
    if data_health_md({"us": {"tripped": False, "missing": 5}}) == "":
        fails.append("未熔断但缺 5 只时未提示")

    # 新数据源（资金流/龙虎榜/…）的缺失留痕必须单独披露，且措辞要说明
    # 「不影响今日操作、但会让将来的因子样品缺一块」——与行情缺口混为一谈，
    # 要么把「今天照样能操作」说成「今天数据不可信」，要么把永久缺样本淹没掉。
    src_warn = data_health_md({
        "_missing_sources": [
            {"source": "akshare_margin_sse", "market": "cn",
             "reason": "上游无数据（nodata）"},
            {"source": "akshare_lhb", "market": "cn",
             "reason": "blocked: ConnectionError"},
        ]})
    src_ok = all(s in src_warn for s in ("新数据源", "不影响今日操作",
                                        "akshare_margin_sse", "akshare_lhb"))
    src_counts = src_warn.count("- **") == 1
    print(f"[14b] 新数据源缺失披露：措辞与来源齐={src_ok} 计为独立一类={src_counts}")
    if not src_ok:
        fails.append(f"新数据源缺失披露不完整：{src_warn!r}")
    if not src_counts:
        fails.append("新数据源缺失被混进了行情缺口那一类")
    # 只有新源缺失时也必须出这段（否则「今天没采到」就没人知道）
    if data_health_md({"_missing_sources": [{"source": "x", "market": "cn",
                                             "reason": "y"}]}) == "":
        fails.append("仅有新数据源缺失时未提示")

    print(f"\n报告: {md_path}\n      {html_path}")
    if fails:
        print("\n❌ 自检失败：")
        for f in fails:
            print("  - " + f)
        raise SystemExit(1)
    print("\n✅ 离线端到端自检全部通过")


if __name__ == "__main__":
    main()
