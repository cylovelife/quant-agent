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

@author cylovelife
"""

import json
import os
import time
from datetime import date

import numpy as np

import quant_agent.infrastructure.market_data.fetcher as fetcher
from quant_agent.domain.strategies.macro import (
    DIM_WEIGHTS as DIM_WEIGHTS,
)
from quant_agent.domain.strategies.macro import (
    _cpi_score as _cpi_score,
)
from quant_agent.domain.strategies.macro import (
    _mom as _mom,
)
from quant_agent.domain.strategies.macro import (
    _money_score as _money_score,
)
from quant_agent.domain.strategies.macro import (
    _pmi_score as _pmi_score,
)
from quant_agent.domain.strategies.macro import (
    _ramp as _ramp,
)
from quant_agent.domain.strategies.macro import (
    _rate_score as _rate_score,
)
from quant_agent.domain.strategies.macro import (
    _risk_score as _risk_score,
)
from quant_agent.domain.strategies.macro import (
    _z_from_mom as _z_from_mom,
)
from quant_agent.domain.strategies.macro import (
    macro_fit_bonus as macro_fit_bonus,
)
from quant_agent.domain.strategies.macro import (
    macro_score as macro_score,
)
from quant_agent.runtime.paths import WORKSPACE_ROOT

ROOT = str(WORKSPACE_ROOT)
MACRO_DIR = os.path.join(ROOT, "state", "cache", "macro")

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
    "bond_short": ("511010", "etf"),  # 短端国债
    "bond_10y": ("511260", "etf"),  # 10 年国债
    "equity_cn": ("510300", "etf"),  # 沪深300
    "equity_growth": ("159915", "etf"),  # 创业板
    "equity_us": ("513100", "etf"),  # 纳指
    "gold": ("518880", "etf"),  # 黄金
}


# ---------------------------------------------------------------------------
# 抓取
# ---------------------------------------------------------------------------
def _em_query(report_name: str, sort_col: str, size: int = 13) -> list:
    try:
        r = fetcher._get(
            EM_URL,
            {
                "reportName": report_name,
                "columns": "ALL",
                "pageNumber": 1,
                "pageSize": size,
                "sortColumns": sort_col,
                "sortTypes": "-1",
                "source": "WEB",
                "client": "WEB",
            },
            min_gap=0.4,
        ).json()
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
    os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
    with open(path, "w", encoding="utf-8") as f:
        json.dump(out, f, ensure_ascii=False)
    return out


def build_snapshot(kline_fn=None, ttl_days: int = 2) -> dict:
    """汇总宏观快照：公布值 + 市场代理走势。

    kline_fn(code, market) 可选，传入则复用主流程的 K 线缓存（省请求）。
    """
    raw = fetch_macro_raw(ttl_days)
    snap = {
        "date": date.today().isoformat(),
        "sources": {k: v for k, v in raw.items() if k.endswith("_source")},
    }

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
        snap["cpi"] = {
            "period": cpi[0].get("TIME"),
            "yoy": cpi[0].get("NATIONAL_SAME"),
            "mom": cpi[0].get("NATIONAL_SEQUENTIAL"),
        }
    # --- 货币 ---
    m = raw.get("money") or []
    if m:
        snap["money"] = {
            "period": m[0].get("TIME"),
            "m2_yoy": m[0].get("BASIC_CURRENCY_SAME"),
            "m1_yoy": m[0].get("CURRENCY_SAME"),
            "m2_prev": m[1].get("BASIC_CURRENCY_SAME") if len(m) > 1 else None,
        }
        if snap["money"]["m2_yoy"] is not None and snap["money"]["m1_yoy"] is not None:
            snap["money"]["m1_minus_m2"] = round(
                snap["money"]["m1_yoy"] - snap["money"]["m2_yoy"], 2
            )
    # --- PPI / GDP（可选）---
    ppi = raw.get("ppi") or []
    if ppi:
        snap["ppi"] = {"period": ppi[0].get("TIME"), "yoy": ppi[0].get("NATIONAL_SAME")}
    gdp = raw.get("gdp") or []
    if gdp:
        snap["gdp"] = {
            "period": gdp[0].get("TIME"),
            "yoy": gdp[0].get("SUM_SAME") or gdp[0].get("DOMESTICL_PRODUCT_BASE"),
        }

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
            "mom_5": _mom(c, 5),
            "mom_20": _mom(c, 20),
            "mom_60": _mom(c, 60),
            "vol_60": round(float(np.std(np.diff(c[-61:]) / c[-61:-1]) * 100), 3)
            if len(c) > 61
            else None,
            "date": str(k.iloc[-1]["date"])[:10],
        }
    snap["market"] = mkt
    return snap


# ---------------------------------------------------------------------------
# 评分
# ---------------------------------------------------------------------------


if __name__ == "__main__":
    snap = build_snapshot()
    print(
        json.dumps({k: v for k, v in snap.items() if k != "market"}, ensure_ascii=False, indent=2)[
            :1200
        ]
    )
    print("市场代理:", json.dumps(snap.get("market"), ensure_ascii=False)[:600])
    ms = macro_score(snap)
    print(
        json.dumps(
            {k: ms[k] for k in ("score", "label", "equity_stance", "style_bias", "missing")},
            ensure_ascii=False,
        )
    )
    for it in ms["items"]:
        print(f"  {it['name']:<14} {it['score']}  {it['note']}")
