# -*- coding: utf-8 -*-
"""长期价值评分模型（长期价值轨）。

与短线低吸轨的根本区别：
- 短线看「价格位置与超跌反弹」，长期价值看「企业赚钱能力 + 买入价格 + 宏观环境」；
- 短线 5 日验证，长期按 1/3/6/12 月验证（见 value_track.py）。

四层决策：
  L1 宏观环境（macro.py）→ 决定总仓位基调与风格倾向
  L2 标的池（本模块 build_stock_universe）→ 分层抽样，不做风格押注
  L3 六维评分（本模块 score_longterm）→ 质量/成长/现金流/财务安全/估值/宏观契合
  L4 组合与跟踪（value_track.py）→ 前向收益 + 超额 + 参数迭代

评分方法全部是**分段线性映射 + 加权**，每个输入值到分数的换算关系可以逐条讲清楚，
不用黑箱模型——长期投资里，看得懂为什么得分高，比多 1 分准确率重要得多。

客观边界（必须知道）：
- 财务数据来自东财 F10，季报有披露滞后（最新一期通常是上一季度末）；
- 估值分位只用 PB（PB=BPS阶跃×不复权价），PE 分位因季报 EPS 为累计值不硬算；
- 风格标签（value/growth/cyclical/defensive）走名称关键词+财务特征的启发式，不是行业分类数据。

@author cylovelife
"""

import json
import os

import pandas as pd

import quant_agent.infrastructure.market_data.fundamentals as fd
import quant_agent.infrastructure.market_data.macro as mc
from quant_agent.domain.strategies.value import (
    CYCLICAL_KW as CYCLICAL_KW,
)
from quant_agent.domain.strategies.value import (
    DEFAULT_WEIGHTS as DEFAULT_WEIGHTS,
)
from quant_agent.domain.strategies.value import (
    DEFENSIVE_KW as DEFENSIVE_KW,
)
from quant_agent.domain.strategies.value import (
    ETF_UNIVERSE_KW as ETF_UNIVERSE_KW,
)
from quant_agent.domain.strategies.value import (
    _band as _band,
)
from quant_agent.domain.strategies.value import (
    _mix as _mix,
)
from quant_agent.domain.strategies.value import (
    build_etf_universe as build_etf_universe,
)
from quant_agent.domain.strategies.value import (
    build_stock_universe as build_stock_universe,
)
from quant_agent.domain.strategies.value import (
    score_fund as score_fund,
)
from quant_agent.domain.strategies.value import (
    score_longterm as score_longterm,
)
from quant_agent.domain.strategies.value import (
    tag_style as tag_style,
)
from quant_agent.runtime.paths import WORKSPACE_ROOT

ROOT = str(WORKSPACE_ROOT)

# 六维默认权重（可由 params["longterm"]["weights"] 覆盖，由迭代层更新）
DIM_LABELS = {
    "quality": "质量",
    "growth": "成长",
    "cashflow": "现金流",
    "balance": "财务安全",
    "valuation": "估值",
    "macro": "宏观契合",
}

# 周期/防御风格的名称关键词（启发式，作用有限但可解释、可人工核对）


# ---------------------------------------------------------------------------
# L2 标的池：分层抽样（避免单一风格押注）
# ---------------------------------------------------------------------------


# ---------------------------------------------------------------------------
# L3 股票：六维评分
# ---------------------------------------------------------------------------


def analyze_stock(
    item: dict, ms: dict, weights: dict = None, kline_raw_fn=None, ttl_days: int = 3
) -> dict | None:
    """单只 A 股的深度分析：财务 → 派生指标 → 估值分位 → 股息 → 六维打分。"""
    code, market = item["code"], item.get("market", "cn")
    rows = fd.fetch_financial(code, market)
    if not rows:
        return None
    m = fd.derived_metrics(rows)
    if not m or (m.get("n_annual") or 0) < 2:
        return None  # 上市不足 2 年，长期价值无意义
    m["code"], m["market"], m["name"] = code, market, item.get("name")

    try:
        k = kline_raw_fn(code, market) if kline_raw_fn else fd.fetch_kline_raw(code, market)
    except Exception:
        k = None
    vp = fd.valuation_percentile(k, rows)
    m["pb_pct"] = vp.get("pb_pct")
    m["px_pct"] = vp.get("px_pct")
    m["pb_median"] = vp.get("pb_median")

    val = item.get("valuation") or {}
    m["pe_ttm"] = val.get("pe_ttm") if val.get("pe_ttm") is not None else m.get("pe_ttm")
    m["pb"] = val.get("pb")
    m["price"] = val.get("price") or item.get("price")
    m["total_mcap"] = val.get("total_mcap") or item.get("float_mcap")

    div = fd.fetch_dividend(code, market)
    if div and div.get("dps_ttm") and m.get("price"):
        m["div_yield"] = round(div["dps_ttm"] / float(m["price"]) * 100, 3)
        m["dps_ttm"] = div["dps_ttm"]
    else:
        m["div_yield"] = None

    s = score_longterm(m, ms, weights)
    m.update({k2: v2 for k2, v2 in s.items() if k2 != "tags"})
    m["bucket"] = item.get("bucket")
    m["valuation_method"] = vp.get("method")
    for k2 in (
        "name",
        "code",
        "market",
        "report_date",
        "report_type",
        "price",
        "pe_ttm",
        "pb",
        "pb_pct",
        "px_pct",
        "div_yield",
        "total_mcap",
        "roe_avg3",
        "roe_min3",
        "roe",
        "gross_margin_avg3",
        "net_margin",
        "rev_yoy",
        "profit_yoy",
        "rev_cagr3",
        "profit_cagr3",
        "ocf_to_profit",
        "debt_ratio",
        "current_ratio",
        "n_annual",
        "score",
        "v_quality",
        "v_growth",
        "v_cashflow",
        "v_balance",
        "v_valuation",
        "v_macro",
        "style",
        "macro_fit",
        "coverage",
        "bucket",
        "valuation_method",
        "balance_note",
        "tags",
    ):
        m.setdefault(k2, None)
    return m


# ---------------------------------------------------------------------------
# 基金：场外基金 + 场内 ETF
# ---------------------------------------------------------------------------


def analyze_fund(code: str, ms: dict, nav_fn, weights: dict = None) -> dict | None:
    prof = fd.fetch_fund_profile(code)
    if not prof or not prof.get("name"):
        return None
    try:
        nav = nav_fn(code, "fund")
    except Exception:
        nav = None
    s = score_fund(prof, nav, ms, weights)
    return {
        "code": code,
        "name": prof.get("name"),
        "market": "fund",
        "price": (float(nav["nav"].iloc[-1]) if nav is not None and len(nav) else None),
        **{
            k: prof.get(k)
            for k in (
                "ret_1y",
                "ret_6m",
                "ret_3m",
                "ret_1m",
                "scale",
                "scale_chg",
                "manager",
                "manager_star",
                "manager_years",
                "stock_position",
                "asset_stock_ratio",
                "rate_now",
                "holdings",
            )
        },
        **s,
    }


# ---------------------------------------------------------------------------
# 主入口
# ---------------------------------------------------------------------------
def pick_longterm(
    cfg: dict,
    params: dict,
    ms: dict,
    market_data: dict,
    kline_raw_fn=None,
    nav_fn=None,
    progress=None,
) -> dict:
    """产出长期价值推荐：{stocks: [..], funds: [..], etfs: [..], stats: {...}}。

    market_data: {"cn_spot": DataFrame(成交额榜), "etf_spot": DataFrame}
    """
    lt = params.get("longterm", {}) or {}
    w_stock = lt.get("weights")
    w_fund = lt.get("fund_weights")
    max_analyze = int((cfg.get("longterm", {}) or {}).get("max_analyze", 60))
    max_fund = int((cfg.get("longterm", {}) or {}).get("max_fund_analyze", 12))

    stats = {"universe": 0, "analyzed": 0, "passed": 0, "failed": 0}

    # --- 股票 ---
    uni = build_stock_universe(market_data.get("cn_spot"), cfg)[:max_analyze]
    stats["universe"] = len(uni)
    if uni and progress:
        progress("长期轨：批量取估值", 0, 1)
    vals = fd.fetch_valuations([(u["code"], "cn") for u in uni]) if uni else {}
    stocks = []
    for i, u in enumerate(uni):
        u2 = dict(u)
        u2["valuation"] = vals.get(("cn", u["code"])) or {}
        try:
            r = analyze_stock(u2, ms, w_stock, kline_raw_fn)
        except Exception:
            r = None
        if r:
            stocks.append(r)
            stats["analyzed"] += 1
        else:
            stats["failed"] += 1
        if progress:
            progress(f"长期轨：分析 {u['name']}", i + 1, len(uni))
    df_s = pd.DataFrame(stocks)
    if not df_s.empty:
        thr = float((cfg.get("longterm", {}) or {}).get("min_score", 62))
        df_s = df_s.sort_values("score", ascending=False).reset_index(drop=True)
        stats["passed"] = int((df_s["score"] >= thr).sum())

    # --- 场外基金 ---
    funds = []
    wl = (cfg.get("fund_watchlist") or [])[:max_fund]
    for i, code in enumerate(wl):
        try:
            r = analyze_fund(code, ms, nav_fn, w_fund)
        except Exception:
            r = None
        if r:
            funds.append(r)
        if progress:
            progress(f"长期轨：基金 {code}", i + 1, len(wl))
    df_f = pd.DataFrame(funds)
    if not df_f.empty:
        df_f = df_f.sort_values("score", ascending=False).reset_index(drop=True)

    return {"stocks": df_s, "funds": df_f, "stats": stats}


if __name__ == "__main__":
    cfg = json.load(open(os.path.join(ROOT, "config.json"), encoding="utf-8"))
    params = json.load(open(os.path.join(ROOT, "params", "params.json"), encoding="utf-8"))
    snap = mc.build_snapshot()
    ms = mc.macro_score(snap)
    print("宏观:", ms["score"], ms["label"], ms["style_bias"])
    spot = fast = None
    try:
        import quant_agent.infrastructure.market_data.fetcher as fetcher

        fast = fetcher.fetch_cn_rank_by_amount(300)
        print("成交额榜:", len(fast))
    except Exception as e:
        print("成交额榜失败:", e)
    uni = build_stock_universe(fast, cfg)
    print("标的池:", len(uni), [f"{u['name']}({u['bucket']})" for u in uni[:8]])
