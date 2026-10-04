# -*- coding: utf-8 -*-
"""宏观环境评估（长期价值轨的择时层）。

设计原则：**每个维度都必须有可核验的一手数据，且给出降级路径**。
拿不到的指标不假装有，直接标 null 并在报告里说明缺口。

维度与数据源（2026-09-21 实测）：

| 维度 | 指标 | 数据源 | 实测状态 |
|---|---|---|---|
| 景气 | 制造业 PMI + 方向 | 东财 RPT_ECONOMY_PMI | ✅ 2026-08 制造业 49.8 |
| 通胀 | CPI 同比 | 东财 RPT_ECONOMY_CPI | ✅ 2026-08 同比 0.8% |
| 货币 | M2 / M1 同比、M1-M2 剪刀差 | 东财 RPT_ECONOMY_CURRENCY_SUPPLY | ✅ M2 +7.5%、M1 +4.1% |
| 利率 | 10 年国债 ETF(511260) 与短端(511010) 走势 | 腾讯日线 | ✅ |
| 风险偏好 | 沪深300ETF(510300) 趋势 / 纳指ETF(513100) / 黄金ETF(518880) | 腾讯日线 | ✅ |

利率不用 LPR 接口（实测 RPT_ECONOMY_LPR 返回 0 条），改用国债 ETF 价格：
价格上行 = 收益率下行 = 货币宽松，这是市场交易出来的结果，比公布值更及时。

评分不是「预测涨跌」，而是回答「当前环境对长期持有权益资产友好到什么程度」，
产出宏观分 0~100 与建议权益仓位区间；风格匹配（macro_fit）用于给标的加分。
"""
import json
import os
import time
from datetime import date

import numpy as np
import pandas as pd

import fetcher

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
MACRO_DIR = os.path.join(ROOT, "state", "cache", "macro")
os.makedirs(MACRO_DIR, exist_ok=True)

EM_URL = "https://datacenter-web.eastmoney.com/api/data/v1/get"

# 每个宏观指标给出多个候选 reportName，逐个尝试（接口改名时不至于整体失效）
MACRO_SPECS = {
    "pmi": {"names": ["RPT_ECONOMY_PMI"], "sort": "REPORT_DATE"},
    "cpi": {"names": ["RPT_ECONOMY_CPI"], "sort": "REPORT_DATE"},
    "money": {"names": ["RPT_ECONOMY_CURRENCY_SUPPLY"], "sort": "REPORT_DATE"},
    "gdp": {"names": ["RPT_ECONOMY_GDP"], "sort": "REPORT_DATE"},
    "ppi": {"names": ["RPT_ECONOMY_PPI"], "sort": "REPORT_DATE"},
}

# 代理资产（都走腾讯日线，可交易、可核验）
PROXY = {
    "bond_short": ("511010", "etf"),   # 短端国债
    "bond_10y": ("511260", "etf"),     # 10 年国债
    "equity_cn": ("510300", "etf"),    # 沪深300
    "equity_growth": ("159915", "etf"),  # 创业板
    "equity_us": ("513100", "etf"),    # 纳指
    "gold": ("518880", "etf"),         # 黄金
}


# ---------------------------------------------------------------------------
# 抓取
# ---------------------------------------------------------------------------
def _em_query(report_name: str, sort_col: str, size: int = 13) -> list:
    try:
        r = fetcher._get(EM_URL, {
            "reportName": report_name, "columns": "ALL",
            "pageNumber": 1, "pageSize": size,
            "sortColumns": sort_col, "sortTypes": "-1",
            "source": "WEB", "client": "WEB"}, min_gap=0.4).json()
        return ((r.get("result") or {}).get("data")) or []
    except Exception:
        return []


def fetch_macro_raw(ttl_days: int = 2) -> dict:
    """抓宏观原始序列（月度数据，2 天缓存足够）。"""
    path = os.path.join(MACRO_DIR, "macro_raw.json")
    if os.path.exists(path):
        if (time.time() - os.path.getmtime(path)) / 86400 < ttl_days:
            try:
                with open(path, encoding="utf-8") as f:
                    return json.load(f)
            except Exception:
                pass
    out = {}
    for key, spec in MACRO_SPECS.items():
        rows = []
        for name in spec["names"]:
            rows = _em_query(name, spec["sort"])
            if rows:
                out[f"{key}_source"] = name
                break
        out[key] = rows[:12]
    with open(path, "w", encoding="utf-8") as f:
        json.dump(out, f, ensure_ascii=False)
    return out


def _mom(closes, n: int):
    """n 个交易日动量（%）。"""
    if closes is None or len(closes) < n + 1:
        return None
    a, b = float(closes[-1]), float(closes[-1 - n])
    if b <= 0:
        return None
    return (a / b - 1) * 100


def build_snapshot(kline_fn=None, ttl_days: int = 2) -> dict:
    """汇总宏观快照：公布值 + 市场代理走势。

    kline_fn(code, market) 可选，传入则复用主流程的 K 线缓存（省请求）。
    """
    raw = fetch_macro_raw(ttl_days)
    snap = {"date": date.today().isoformat(),
            "sources": {k: v for k, v in raw.items() if k.endswith("_source")}}

    # --- PMI ---
    pmi = raw.get("pmi") or []
    if pmi:
        cur = pmi[0]
        snap["pmi"] = {
            "period": cur.get("TIME"),
            "mfg": cur.get("MAKE_INDEX"),
            "non_mfg": cur.get("NMAKE_INDEX"),
            "mfg_prev": pmi[1].get("MAKE_INDEX") if len(pmi) > 1 else None,
        }
    # --- CPI ---
    cpi = raw.get("cpi") or []
    if cpi:
        snap["cpi"] = {"period": cpi[0].get("TIME"),
                       "yoy": cpi[0].get("NATIONAL_SAME"),
                       "mom": cpi[0].get("NATIONAL_SEQUENTIAL")}
    # --- 货币 ---
    m = raw.get("money") or []
    if m:
        snap["money"] = {"period": m[0].get("TIME"),
                         "m2_yoy": m[0].get("BASIC_CURRENCY_SAME"),
                         "m1_yoy": m[0].get("CURRENCY_SAME"),
                         "m2_prev": m[1].get("BASIC_CURRENCY_SAME") if len(m) > 1 else None}
        if snap["money"]["m2_yoy"] is not None and snap["money"]["m1_yoy"] is not None:
            snap["money"]["m1_minus_m2"] = round(
                snap["money"]["m1_yoy"] - snap["money"]["m2_yoy"], 2)
    # --- PPI / GDP（可选）---
    ppi = raw.get("ppi") or []
    if ppi:
        snap["ppi"] = {"period": ppi[0].get("TIME"),
                       "yoy": ppi[0].get("NATIONAL_SAME")}
    gdp = raw.get("gdp") or []
    if gdp:
        snap["gdp"] = {"period": gdp[0].get("TIME"),
                       "yoy": gdp[0].get("SUM_SAME") or gdp[0].get("DOMESTICL_PRODUCT_BASE")}

    # --- 市场代理走势 ---
    mkt = {}
    for key, (code, mkt_name) in PROXY.items():
        try:
            k = kline_fn(code, mkt_name) if kline_fn else fetcher.fetch_kline(code, mkt_name)
        except Exception:
            k = None
        if k is None or len(k) < 25:
            mkt[key] = None
            continue
        c = k["close"].to_numpy(dtype=float)
        mkt[key] = {
            "code": code,
            "last": round(float(c[-1]), 4),
            "mom_5": _mom(c, 5), "mom_20": _mom(c, 20), "mom_60": _mom(c, 60),
            "vol_60": round(float(np.std(np.diff(c[-61:]) / c[-61:-1]) * 100), 3)
            if len(c) > 61 else None,
            "date": str(k.iloc[-1]["date"])[:10],
        }
    snap["market"] = mkt
    return snap


# ---------------------------------------------------------------------------
# 评分
# ---------------------------------------------------------------------------
def _ramp(x, lo, hi):
    """把 x 线性映射到 0~100（lo→0, hi→100），越界截断。"""
    if x is None:
        return None
    return float(max(0.0, min(100.0, (x - lo) / (hi - lo) * 100)))


def _pmi_score(snap):
    d = snap.get("pmi") or {}
    v, prev = d.get("mfg"), d.get("mfg_prev")
    if v is None:
        return None, "制造业 PMI 数据缺失"
    if v >= 52:
        s = 92.0
    elif v >= 50:
        s = 60 + (v - 50) * 16            # 50→60, 52→92
    elif v >= 48:
        s = 30 + (v - 48) * 15            # 48→30, 50→60
    else:
        s = max(8.0, 30 - (48 - v) * 8)
    note = f"制造业 PMI {v}（{d.get('period')}）"
    if prev is not None:
        dv = v - prev
        s = max(0.0, min(100.0, s + (8 if dv > 0 else -8 if dv < 0 else 0)))
        note += f"，环比 {'+' if dv >= 0 else ''}{dv:.1f}"
    note += "。" + ("扩张区间" if v >= 50 else "收缩区间")
    return s, note


def _cpi_score(snap):
    d = snap.get("cpi") or {}
    v = d.get("yoy")
    if v is None:
        return None, "CPI 数据缺失"
    # 温和通胀（1~3%）对企业定价与盈利最友好；通缩压制名义增长，高通胀压制估值
    if 1.0 <= v <= 3.0:
        s = 85 - abs(v - 2.0) * 5
    elif 0 <= v < 1.0:
        s = 55 + v * 30                   # 低通胀：偏松但需求弱
    elif 3.0 < v <= 5.0:
        s = 85 - (v - 3.0) * 15
    else:
        s = max(10.0, 55 + v * 20) if v < 0 else max(20.0, 55 - (v - 5) * 10)
    note = f"CPI 同比 {v:+.1f}%（{d.get('period')}）"
    if v < 0:
        note += "，通缩压力，压制企业名义营收"
    elif v < 1.0:
        note += "，低通胀，需求偏弱但货币空间充足"
    elif v <= 3.0:
        note += "，温和通胀，对企业盈利与估值均友好"
    else:
        note += "，通胀偏高，压制估值"
    return s, note


def _money_score(snap):
    d = snap.get("money") or {}
    m2, m1 = d.get("m2_yoy"), d.get("m1_yoy")
    if m2 is None:
        return None, "货币供应数据缺失"
    base = _ramp(m2, 5.0, 12.0) or 50.0
    note = f"M2 同比 {m2:+.1f}%、M1 同比 " + (f"{m1:+.1f}%" if m1 is not None else "缺失")
    spread = d.get("m1_minus_m2")
    if spread is not None:
        # M1-M2 剪刀差收窄/转正 = 资金活化，企业盈利改善的领先信号
        base = max(0.0, min(100.0, base + spread * 4))
        note += f"，M1-M2 剪刀差 {spread:+.1f}pct"
        note += "（资金活化，利好风险资产）" if spread > -2 else "（资金淤积，观望）"
    note += f"（{d.get('period')}）"
    return base, note


def _z_from_mom(mom_pct, vol_daily_pct, days):
    """把 n 日动量换算成「相对自身波动尺度」的 z 值。

    债券 ETF 的 20 日动量常在 ±0.5% 内、沪深300 常在 ±10% 内，
    用同一组固定阈值映射会让利率维度永远贴着 50 分（无区分度）。
    除以该资产自身的日波动率×√n 做自适应归一化才可比。
    """
    if mom_pct is None or vol_daily_pct is None or vol_daily_pct <= 1e-6:
        return None
    scale = vol_daily_pct * (days ** 0.5)
    if scale <= 1e-9:
        return None
    return mom_pct / scale


def _rate_score(snap):
    mk = snap.get("market") or {}
    b10, bs = mk.get("bond_10y"), mk.get("bond_short")
    if not b10 or b10.get("mom_20") is None:
        return None, "国债 ETF 走势不可用，利率维度跳过"
    m10 = b10["mom_20"]
    z = _z_from_mom(m10, b10.get("vol_60"), 20)
    # z 校准区间 ±1.5（约对应 20 日动量 1.5 个自身标准差）
    s = _ramp(z, -1.5, 1.5) if z is not None else _ramp(m10, -1.5, 1.5)
    note = f"10 年国债 ETF 20 日 {m10:+.2f}%（收益率" + ("下行" if m10 > 0 else "上行") + "）"
    if z is not None:
        note += f"，按自身波动归一 z={z:+.2f}"
    if bs and bs.get("mom_20") is not None:
        slope = m10 - bs["mom_20"]
        note += f"，长短端价差 {slope:+.2f}pct"
        if slope > 0.3:
            note += "，曲线走陡→市场预期宽松/增长改善"
        elif slope < -0.3:
            note += "，曲线走平→避险或紧缩预期"
    return s, note


def _risk_score(snap):
    mk = snap.get("market") or {}
    eq, us, gold = mk.get("equity_cn"), mk.get("equity_us"), mk.get("gold")
    if not eq or eq.get("mom_60") is None:
        return None, "宽基指数走势不可用"
    s = _ramp(eq["mom_60"], -15.0, 15.0)
    note = f"沪深300ETF 60 日 {eq['mom_60']:+.2f}%"
    if us and us.get("mom_60") is not None:
        note += f"，纳指ETF 60 日 {us['mom_60']:+.2f}%"
    if gold and gold.get("mom_60") is not None:
        note += f"，黄金 60 日 {gold['mom_60']:+.2f}%"
        if gold["mom_60"] > 8 and eq["mom_60"] < 0:
            s = max(0.0, s - 10)
            note += "。避险资产强势+权益走弱，风险偏好受压"
    return s, note


DIM_WEIGHTS = {"景气(PMI)": 0.24, "通胀(CPI)": 0.14, "货币(M1/M2)": 0.22,
               "利率环境": 0.20, "风险偏好": 0.20}


def macro_score(snap: dict) -> dict:
    """宏观分 0~100 + 建议权益仓位区间 + 每维评分与依据。"""
    items = []
    for name, fn in (("景气(PMI)", _pmi_score), ("通胀(CPI)", _cpi_score),
                     ("货币(M1/M2)", _money_score), ("利率环境", _rate_score),
                     ("风险偏好", _risk_score)):
        try:
            s, note = fn(snap)
        except Exception as e:
            s, note = None, f"计算异常：{e}"
        items.append({"name": name, "score": round(s, 1) if s is not None else None,
                      "weight": DIM_WEIGHTS[name], "note": note})
    # 只对有数据的维度归一化（缺失维度不拉低总分，只在报告里标注缺口）
    avail = [it for it in items if it["score"] is not None]
    wsum = sum(it["weight"] for it in avail)
    score = (sum(it["score"] * it["weight"] for it in avail) / wsum) if wsum else None
    missing = [it["name"] for it in items if it["score"] is None]

    if score is None:
        label, stance = "未知", None
    elif score >= 75:
        label, stance = "友好", (0.7, 0.9)
    elif score >= 60:
        label, stance = "偏友好", (0.5, 0.75)
    elif score >= 45:
        label, stance = "中性", (0.35, 0.6)
    elif score >= 30:
        label, stance = "偏谨慎", (0.2, 0.45)
    else:
        label, stance = "谨慎", (0.1, 0.3)

    # 风格倾向：由利率与景气共同决定（都是长期价值最关心的两个变量）
    rate = next((it["score"] for it in items if it["name"] == "利率环境"), None)
    pmi = next((it["score"] for it in items if it["name"] == "景气(PMI)"), None)
    style = []
    if rate is not None and rate > 60:
        style.append("高股息/低估值")     # 利率下行，股息溢价扩大
        style.append("长久期成长")        # 贴现率下行
    if pmi is not None and pmi > 60:
        style.append("顺周期")
    elif pmi is not None and pmi < 40:
        style.append("防御/必需消费")
    if not style:
        style.append("均衡")

    return {
        "score": round(score, 1) if score is not None else None,
        "label": label,
        "equity_stance": list(stance) if stance else None,
        "style_bias": style,
        "items": items,
        "missing": missing,
        "date": snap.get("date"),
        "pmi": snap.get("pmi"), "cpi": snap.get("cpi"), "money": snap.get("money"),
        "market": snap.get("market"),
    }


def macro_fit_bonus(tags: dict, ms: dict) -> tuple:
    """标的风格标签 与 宏观风格倾向 的匹配分（-6 ~ +6）与说明。

    tags: {"value": bool, "growth": bool, "cyclical": bool, "defensive": bool}
    """
    bias = (ms or {}).get("style_bias") or []
    if not bias or "均衡" in bias:
        return 0.0, "宏观风格中性，不加不减"
    bonus, hits = 0.0, []
    if "高股息/低估值" in bias and tags.get("value"):
        bonus += 3.0
        hits.append("低估值高股息")
    if "长久期成长" in bias and tags.get("growth"):
        bonus += 2.0
        hits.append("成长")
    if "顺周期" in bias and tags.get("cyclical"):
        bonus += 2.0
        hits.append("顺周期")
    if "防御/必需消费" in bias and tags.get("defensive"):
        bonus += 2.0
        hits.append("防御")
    if not hits:
        return 0.0, f"当前宏观偏向「{'/'.join(bias)}」，本标的风格不占优"
    return min(6.0, bonus), f"契合当前宏观偏向「{'/'.join(bias)}」：{'/'.join(hits)}"


if __name__ == "__main__":
    snap = build_snapshot()
    print(json.dumps({k: v for k, v in snap.items() if k != "market"},
                     ensure_ascii=False, indent=2)[:1200])
    print("市场代理:", json.dumps(snap.get("market"), ensure_ascii=False)[:600])
    ms = macro_score(snap)
    print(json.dumps({k: ms[k] for k in ("score", "label", "equity_stance",
                                         "style_bias", "missing")},
                     ensure_ascii=False))
    for it in ms["items"]:
        print(f"  {it['name']:<14} {it['score']}  {it['note']}")
