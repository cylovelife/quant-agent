# -*- coding: utf-8 -*-
"""长期价值轨离线自检（不写真实 state/，不依赖网络）。

覆盖：
  [1] 分段线性打分原语 _band / _mix 的边界与缺失处理
  [2] 金融类标的（无毛利率 + 高负债率）的现金流/财务安全维度取中性而非罚分
  [3] 权重方案选型的列名映射（面板列带 v_ 前缀、方案字典键不带前缀）
  [4] 无前视对齐：财务只用「公告日 ≤ 时点」的报告期
  [5] 里程碑跟踪的超额收益计算
  [6] 报告渲染：长期章节完整、mode 切换时章节编号连续且无空章节

用法：/path/to/python tests/offline_long.py
"""
import os
import sys

import numpy as np
import pandas as pd

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, "src"))

import macro as mc            # noqa: E402
import model_select as msel   # noqa: E402
import report as rpt          # noqa: E402
import value_strategy as vs   # noqa: E402
import value_track as vt      # noqa: E402

fails = []


def check(name, ok, detail=""):
    print(f"[{'OK' if ok else 'FAIL'}] {name}" + (f" — {detail}" if detail else ""))
    if not ok:
        fails.append(name)


# --------------------------------------------------------------------------- [1]
check("_band 端点截断", vs._band(999, [(0, 0), (10, 100)]) == 100.0)
check("_band 线性插值", abs(vs._band(5, [(0, 0), (10, 100)]) - 50.0) < 1e-9)
check("_band 缺失返回 None", vs._band(None, [(0, 0), (10, 1)]) is None)
check("_band NaN 返回 None", vs._band(float("nan"), [(0, 0), (10, 1)]) is None)
check("_mix 按可得项归一",
      abs(vs._mix([(80.0, 0.5), (None, 0.5)]) - 80.0) < 1e-9)
check("_mix 全缺失返回 None", vs._mix([(None, 0.5)]) is None)

# --------------------------------------------------------------------------- [2]
fin_like = {"name": "某银行", "gross_margin": None, "debt_ratio": 92.0,
            "roe_avg3": 11.0, "roe_min3": 10.0, "current_ratio": None,
            "ocf_to_profit": 0.1, "pb_pct": 0.5, "pe_ttm": 6.0, "div_yield": 4.0,
            "rev_yoy": 3.0, "profit_yoy": 4.0, "gross_margin_avg3": None}
s_fin = vs.score_longterm(fin_like, {})
check("金融类：现金流维度取中性 60", s_fin["v_cashflow"] == 60.0,
      f"实际 {s_fin['v_cashflow']}")
check("金融类：财务安全维度取中性 60", s_fin["v_balance"] == 60.0,
      f"实际 {s_fin['v_balance']}")
check("金融类：标注了口径修正", bool(s_fin.get("balance_note")))
check("金融类：总分可用", s_fin["score"] is not None)
mfg = {"name": "某制造", "gross_margin": 35.0, "debt_ratio": 30.0,
       "roe_avg3": 18.0, "roe_min3": 16.0, "gross_margin_avg3": 34.0,
       "current_ratio": 2.2, "ocf_to_profit": 1.25, "pb_pct": 0.2, "pe_ttm": 15.0,
       "div_yield": 2.0, "rev_yoy": 12.0, "profit_yoy": 15.0, "rev_cagr3": 0.12}
s_mfg = vs.score_longterm(mfg, {})
check("制造业：财务安全为真实评分（非中性 60）", s_mfg["v_balance"] != 60.0,
      f"实际 {s_mfg['v_balance']}")
check("制造业：现金流为真实评分（非中性 60）", s_mfg["v_cashflow"] != 60.0)
check("高 ROE 低估值 > 银行同口径", s_mfg["score"] > s_fin["score"],
      f"{s_mfg['score']} vs {s_fin['score']}")

# --------------------------------------------------------------------------- [3]
rng = np.random.default_rng(7)
rows = []
for d in range(24):
    for i in range(10):
        f = {k: float(rng.random() * 100) for k in
             ["v_quality", "v_growth", "v_cashflow", "v_balance", "v_valuation"]}
        rows.append({"date": f"2025-01-{d + 1:02d}", "code": f"{i:06d}", **f,
                     "score": float(np.mean(list(f.values()))),
                     "ret_20": float(rng.normal(0, 0.05)),
                     "ret_60": float(rng.normal(0, 0.1)),
                     "ret_120": float(rng.normal(0, 0.15))})
panel = pd.DataFrame(rows)
sel = msel.select_long(panel, {})
check("select_long 产出结论（列名映射正确）", sel is not None)
if sel:
    check("候选方案覆盖全部权重方案", len(sel["candidates"]) == len(msel.LONG_SCHEMES),
          f"{len(sel['candidates'])}/{len(msel.LONG_SCHEMES)}")
    check("每个方案都有里程碑 IC", all(c["milestones"] for c in sel["candidates"]))
    check("IC 不是除以 0 得到的 NaN",
          all(np.isfinite(c["ic_avg"]) for c in sel["candidates"]))
    check("给出是否切换的判断", isinstance(sel["switch_recommended"], bool))
    check("权重不含 v_ 前缀键",
          all(not k.startswith("v_") for k in sel["candidates"][0]["weights"]))

# --------------------------------------------------------------------------- [4]
fin_rows = [
    {"REPORT_DATE": "2025-03-31 00:00:00", "NOTICE_DATE": "2025-04-28 00:00:00",
     "REPORT_TYPE": "一季报", "EPSJB": 1.0, "ROEJQ": 5.0},
    {"REPORT_DATE": "2024-12-31 00:00:00", "NOTICE_DATE": "2025-03-20 00:00:00",
     "REPORT_TYPE": "年报", "EPSJB": 4.0, "ROEJQ": 20.0},
]
picked, notice = vt._asof_row(fin_rows, "2025-04-15")
check("无前视：4/15 只能看到 3/20 公告的年报",
      picked is not None and picked.get("REPORT_TYPE") == "年报",
      f"取到 {picked.get('REPORT_TYPE') if picked else None}")
picked2, _ = vt._asof_row(fin_rows, "2025-05-01")
check("无前视：5/1 可看到 4/28 公告的一季报",
      picked2 is not None and picked2.get("REPORT_TYPE") == "一季报")
upto = vt._rows_upto(fin_rows, "2025-04-15")
check("_rows_upto 排除未公告期", len(upto) == 1, f"实际 {len(upto)}")

# --------------------------------------------------------------------------- [5]
check("里程碑默认值", vt.MILESTONES_DEFAULT == [5, 20, 60, 120, 250])
tl = vt.settle_tracking(lambda c, m: None, {"longterm": {}}, "2026-01-01")
check("无历史推荐时不报错并给出说明", "note" in tl or tl.get("batches") == 0)

# --------------------------------------------------------------------------- [6]
stocks = pd.DataFrame([
    {"code": "600519", "name": "贵州茅台", "score": 84.2, "price": 1252.57,
     "pe_ttm": 19.2, "pb_pct": 0.051, "div_yield": 4.15, "roe_avg3": 34.2,
     "rev_yoy": 1.3, "bucket": "龙头", "v_quality": 98.2, "v_growth": 46.2,
     "v_cashflow": 94.2, "v_balance": 95.2, "v_valuation": 85.3, "v_macro": 75.0,
     "style": "value/defensive", "macro_fit": "契合当前宏观偏向「高股息/低估值」"},
    {"code": "601398", "name": "工商银行", "score": 68.3, "price": 8.11,
     "pe_ttm": 7.7, "pb_pct": 0.914, "div_yield": 3.83, "roe_avg3": 10.5,
     "rev_yoy": 2.0, "bucket": "低估值", "v_quality": 64.4, "v_growth": 48.4,
     "v_cashflow": 60.0, "v_balance": 60.0, "v_valuation": 62.3, "v_macro": 75.0,
     "style": "value/defensive", "macro_fit": "契合", "balance_note": "金融类取中性"},
])
funds = pd.DataFrame([
    {"code": "110011", "name": "易方达优质精选", "score": 61.0, "ret_1y": -29.43,
     "ann_ret": -5.2, "ann_vol": 18.0, "max_dd": -35.0, "sharpe": -0.29,
     "scale": 67.77, "scale_chg": -0.29, "manager": "张坤",
     "f_perf": 40.0, "f_stability": 45.0, "f_scale": 55.0, "f_manager": 80.0},
])
ms = mc.macro_score({"date": "2026-09-21",
                     "pmi": {"period": "2026年08月份", "mfg": 49.8, "mfg_prev": 49.2,
                             "non_mfg": 49.0},
                     "cpi": {"period": "2026年08月份", "yoy": 0.8, "mom": 0.4},
                     "money": {"period": "2026年08月份", "m2_yoy": 7.5, "m1_yoy": 4.1,
                               "m1_minus_m2": -3.4},
                     "market": {"bond_10y": {"mom_20": 0.14, "vol_60": 0.049},
                                "bond_short": {"mom_20": 0.0, "vol_60": 0.031},
                                "equity_cn": {"mom_60": -7.04},
                                "equity_us": {"mom_60": 4.68},
                                "gold": {"mom_60": 5.69}}})
check("macro_score 五维齐全", len(ms["items"]) == 5)
check("macro_score 无缺失维", ms["missing"] == [], str(ms["missing"]))
check("macro_score 给出权益仓位区间", isinstance(ms["equity_stance"], list))

lt = {"enabled": True,
      "macro": ms,
      "picks": {"stocks": stocks, "funds": funds,
                "stats": {"universe": 56, "analyzed": 52, "failed": 4, "passed": 18}},
      "etfs": [{"group": "宽基", "code": "510300", "name": "沪深300ETF", "mom_20": 1.27,
                "mom_60": -7.04}],
      # 指数估值：一个成立 21 年的老指数 + 一个成立 1.7 年的新指数（PE=141 却挂着
      # 4% 分位，且 PB 分位 83%）。后者是这张表最容易误导人的形态，必须被点出来。
      "index_valuation": [
          {"securityName": "沪深300指数", "securityCode": "000300", "pe": "13.5173",
           "pePercentile": "55.479", "pb": "1.4277", "pbPercentile": "29.6722",
           "roe": "10.5600", "dividendRatio": "2.5946", "change1Year": "0.8362",
           "_age_years": 21.5},
          {"securityName": "上证科创板综合指数", "securityCode": "000680",
           "pe": "141.3644", "pePercentile": "4.2553", "pb": "6.3776",
           "pbPercentile": "83.3061", "roe": "4.5100", "dividendRatio": "0.3079",
           "change1Year": "20.6139", "_age_years": 1.7}],
      "industry_rank": {"600519": {"code": "600519", "market": "cn", "industry": "酿酒饮料",
                                   "rank": "12/44", "industry_avg": "32.99"}},
      "track": {"batches": 1, "note": "首批已登记", "summary": {
          20: {"n": 12, "avg_ret": 1.2, "win_rate": 58.3, "avg_excess": 0.4,
               "excess_win": 50.0}}},
      "replay": {"n_obs": 432, "n_periods": 29, "n_symbols": 24,
                 "date_range": ["2024-09-24", "2026-03-03"],
                 "milestones": {60: {"factors": {"score": {
                     "ic_mean": 0.1149, "icir": 0.586, "t": 2.49,
                     "positive_rate": 0.67, "n_periods": 29}}}}},
      "selection": {"long": sel}}
md_macro = rpt.macro_env_md(ms)
check("宏观章节含五维表", "景气(PMI)" in md_macro and "利率环境" in md_macro)
check("宏观章节标注数据完整性", "数据齐全" in md_macro or "缺失维度" in md_macro)
md_picks = rpt.longterm_picks_md(lt, {"longterm": {"top_stocks": 10, "top_funds": 5}})
check("长期推荐章节含股票表与六维拆解",
      "长期价值候选（股票）" in md_picks and "六维拆解" in md_picks)
check("长期推荐章节含基金表", "长期基金候选" in md_picks)
check("长期推荐章节标注 ETF 不参与评分", "不参与价值评分" in md_picks)
check("金融类口径修正在报告中标注", "金融类取中性" in md_picks)
# 回归：非金融股没有 note，不能渲染成「（nan）」；口径修正只列被修正的标的
_corr_line = next((ln for ln in md_picks.splitlines() if ln.startswith("- 口径修正")), "")
check("口径修正不渲染 nan", "nan" not in _corr_line.lower(), _corr_line[:80])
check("口径修正只列金融类", "工商银行" in _corr_line and "贵州茅台" not in _corr_line,
      _corr_line[:80])

# 跟踪指数估值表：分位是窗口统计量，年限必须摆在表里，否则「新指数 4% 分位」
# 会被读成「便宜」——实测上游 PE=141 的指数就挂着 4% 分位。
check("指数估值表含成立年限列", "成立" in md_picks and "21.5y" in md_picks)
check("成立不足 5 年的指数被标记", "1.7y⚠" in md_picks)
check("短窗口分位有专门提示",
      "成立不足 5 年" in md_picks and "上证科创板综合指数" in md_picks)
check("PE/PB 分位背离被点出", "背离" in md_picks and "83.3" in md_picks)
check("行业相对位置表渲染", "行业相对位置" in md_picks and "12/44" in md_picks)
check("指数估值表不渲染 nan", "nan" not in md_picks.lower())

md_track = rpt.longterm_track_md(lt)
check("跟踪章节含超额列", "平均超额" in md_track)
check("跟踪章节含回放 IC 与显著性判断",
      "统计显著" in md_track and "无前视保证" in md_track)

# generate_report：mode=long 时不应出现短线章节
import tempfile  # noqa: E402
tmp = tempfile.mkdtemp(prefix="qt_long_")
rpt.REPORT_DIR = tmp
md_path, _ = rpt.generate_report({}, {}, {}, {"updated": False}, {},
                                 longterm=lt, mode="long")
body = open(md_path, encoding="utf-8").read()
heads = [ln for ln in body.splitlines() if ln.startswith("## ")]
check("mode=long 无短线章节",
      not any("离场模型" in h or "低买候选" in h for h in heads), str(heads))
check("mode=long 章节编号连续",
      [h.split("、")[0] for h in heads] == ["## 一", "## 二", "## 三", "## 四"],
      str(heads))
check("mode=long 标题正确", "长期价值投资报告" in body)
_short_params = {"weights": {"boll": 0.24, "rsi": 0.23, "volume": 0.14,
                            "trend": 0.12, "macd": 0.15, "drawdown": 0.12},
                 "version": "test"}
md_path2, _ = rpt.generate_report({}, {}, {}, {"updated": False}, _short_params,
                                  mode="short")
body2 = open(md_path2, encoding="utf-8").read()
heads2 = [ln for ln in body2.splitlines() if ln.startswith("## ")]
check("mode=short 无长期章节",
      not any("宏观环境" in h or "长期价值推荐" in h for h in heads2))
check("mode=short 含短线模型选型章节",
      any("短线模型选型" in h for h in heads2), str(heads2))

print("\n" + "=" * 60)
if fails:
    print(f"未通过 {len(fails)} 项：")
    for f in fails:
        print("  -", f)
    sys.exit(1)
print("长期轨自检全部通过")
