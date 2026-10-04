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
"""
import json
import os
import re
from datetime import date, datetime

import numpy as np
import pandas as pd

import fundamentals as fd
import macro as mc

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

# 六维默认权重（可由 params["longterm"]["weights"] 覆盖，由迭代层更新）
DEFAULT_WEIGHTS = {
    "quality": 0.26,     # 质量/护城河：ROE 水平与稳定性、毛利率
    "growth": 0.14,      # 成长：营收/净利增速与 3 年 CAGR
    "cashflow": 0.16,    # 现金流质量：经营现金流/净利润（利润含金量）
    "balance": 0.08,     # 财务安全：资产负债率、流动比率
    "valuation": 0.24,   # 估值：PB 历史分位、PE 水平、股息率
    "macro": 0.12,       # 宏观契合：风格与当前宏观环境是否匹配
}
DIM_LABELS = {
    "quality": "质量", "growth": "成长", "cashflow": "现金流",
    "balance": "财务安全", "valuation": "估值", "macro": "宏观契合",
}

# 周期/防御风格的名称关键词（启发式，作用有限但可解释、可人工核对）
CYCLICAL_KW = ("钢", "煤", "有色", "化工", "石油", "石化", "水泥", "建材", "航空",
               "航运", "港口", "证券", "地产", "房地产", "工程", "重工", "机械",
               "稀土", "锂", "铜", "铝", "化纤", "造纸", "农药", "化肥", "能源")
DEFENSIVE_KW = ("药", "医药", "生物", "食品", "白酒", "乳", "电力", "水电", "水务",
                "燃气", "高速", "公路", "银行", "保险", "公用", "黄金", "中药",
                "酿", "酒", "机场", "铁路", "烟草")


def _band(x, pts):
    """分段线性映射：pts 为 [(输入值, 分数), ...]。越界取端点，缺失返回 None。

    这是全模块唯一的打分原语——所有因子的分数曲线都能用一行 pts 讲清楚。
    """
    if x is None:
        return None
    try:
        v = float(x)
    except (TypeError, ValueError):
        return None
    if not np.isfinite(v):
        return None
    p = sorted(pts, key=lambda t: t[0])
    if v <= p[0][0]:
        return float(p[0][1])
    if v >= p[-1][0]:
        return float(p[-1][1])
    for (x0, y0), (x1, y1) in zip(p, p[1:]):
        if x0 <= v <= x1:
            t = (v - x0) / (x1 - x0) if x1 > x0 else 1.0
            return float(y0 + (y1 - y0) * t)
    return None


def _mix(pairs):
    """按可得项归一化加权。pairs: [(分数或None, 权重), ...]"""
    avail = [(s, w) for s, w in pairs if s is not None]
    if not avail:
        return None
    tw = sum(w for _, w in avail)
    return sum(s * w for s, w in avail) / tw if tw else None


# ---------------------------------------------------------------------------
# L2 标的池：分层抽样（避免单一风格押注）
# ---------------------------------------------------------------------------
def build_stock_universe(spot: pd.DataFrame, cfg: dict) -> list:
    """从 A股成交额榜分层抽样：龙头 + 低估值 + 合理估值优质。

    为什么不全市场扫描：F10 财务是逐只请求（每只 1 次），
    600 只全扫要 600 次请求且大部分不值得做价值分析。
    价值投资真正需要的是「一篮子值得跟踪的优质标的」，
    所以按三类各抽一批，覆盖不同风格，而不是按单一指标排序取头部。
    """
    lt = cfg.get("longterm", {}) or {}
    min_mcap = float(lt.get("min_float_mcap", 120.0))       # 流通市值下限（亿元）
    df = spot.copy()
    if "float_mcap" not in df.columns:
        return []
    df = df[pd.to_numeric(df["float_mcap"], errors="coerce") >= min_mcap]
    df = df[~df["name"].astype(str).str.contains("ST|退", na=False)]
    if df.empty:
        return []
    n_leader = int(lt.get("universe_leaders", 24))
    n_cheap = int(lt.get("universe_cheap", 16))
    n_fair = int(lt.get("universe_fair", 16))
    df = df.assign(_pe=pd.to_numeric(df["pe_ttm"], errors="coerce"),
                   _mcap=pd.to_numeric(df["float_mcap"], errors="coerce"))
    # 亏损（PE<=0）与极端高估（PE>80）不进入深度分析
    healthy = df[(df["_pe"] > 0) & (df["_pe"] <= 80)]
    if healthy.empty:
        return []

    picks, seen = [], set()

    def _add(sub, tag, n):
        """从 sub 里取前 n 个未入选的标的（多取候选，避免与前面桶重复后不够数）。"""
        got = 0
        for _, r in sub.iterrows():
            if got >= n:
                break
            c = str(r["code"])
            if c in seen:
                continue
            seen.add(c)
            got += 1
            picks.append({"code": c, "name": str(r["name"]), "market": "cn",
                          "bucket": tag, "float_mcap": float(r["_mcap"]),
                          "pe_ttm": (float(r["_pe"]) if pd.notna(r["_pe"]) else None),
                          "amount": float(r["amount"]) if pd.notna(r["amount"]) else 0.0})

    # 每桶多取 3 倍候选行，保证去重后仍能凑满名额
    _add(healthy.sort_values("_mcap", ascending=False).head(n_leader * 3), "龙头", n_leader)
    _add(healthy[healthy["_mcap"] >= 300].sort_values("_pe").head(n_cheap * 3),
         "低估值", n_cheap)
    fair = healthy[(healthy["_pe"] >= 12) & (healthy["_pe"] <= 35)
                   & (healthy["_mcap"] >= 200)]
    _add(fair.sort_values("_mcap", ascending=False).head(n_fair * 3), "合理估值", n_fair)
    return picks


ETF_UNIVERSE_KW = {
    "宽基": ("沪深300", "中证500", "中证1000", "上证50", "创业板", "科创50", "深证100"),
    "红利": ("红利", "股息", "价值", "低波"),
    "消费医药": ("消费", "白酒", "食品", "医药", "医疗", "生物"),
    "金融地产": ("银行", "证券", "保险", "地产", "金融"),
    "科技制造": ("科技", "芯片", "半导体", "人工智能", "新能源", "光伏", "军工", "机器人"),
    "资源商品": ("黄金", "有色", "煤炭", "石油", "能源", "钢铁", "化工"),
    "海外债券": ("纳指", "标普", "恒生", "港股", "中概", "国债", "债券", "货币"),
}


def build_etf_universe(spot: pd.DataFrame, cfg: dict, per_group: int = 2) -> list:
    """从 ETF 成交额榜按主题分组抽取，作为长期配置的可交易载体。"""
    if spot is None or spot.empty:
        return []
    df = spot.copy()
    df = df[~df["name"].astype(str).str.contains("货币|现金|短融", na=False)]
    amt = pd.to_numeric(df["amount"], errors="coerce")
    df = df.assign(_amt=amt).sort_values("_amt", ascending=False)
    out, seen = [], set()
    for group, kws in ETF_UNIVERSE_KW.items():
        sub = df[df["name"].astype(str).str.contains("|".join(kws), na=False)]
        for _, r in sub.head(per_group).iterrows():
            c = str(r["code"])
            if c in seen:
                continue
            seen.add(c)
            out.append({"code": c, "name": str(r["name"]), "market": "etf",
                        "group": group, "price": float(r["price"]),
                        "amount": float(r["amount"]) if pd.notna(r["amount"]) else 0.0})
    return out


# ---------------------------------------------------------------------------
# L3 股票：六维评分
# ---------------------------------------------------------------------------
def tag_style(m: dict) -> dict:
    """风格标签（启发式）。用于宏观契合度，不参与质量判断。"""
    name = str(m.get("name") or "")
    div = m.get("div_yield")
    pb_pct = m.get("pb_pct")
    pe = m.get("pe_ttm")
    roe = m.get("roe_avg3") or m.get("roe")
    growth = m.get("rev_cagr3") or (m.get("rev_yoy") or 0) / 100.0
    tags = {
        "value": bool((div is not None and div >= 2.5)
                      or (pb_pct is not None and pb_pct < 0.35
                          and pe is not None and pe < 20)),
        "growth": bool(growth is not None and growth > 0.15
                       or (m.get("profit_yoy") or 0) > 25),
        "cyclical": any(k in name for k in CYCLICAL_KW),
        "defensive": any(k in name for k in DEFENSIVE_KW)
                     or bool(div is not None and div >= 3.5
                             and roe is not None and roe >= 10),
    }
    return tags


def score_longterm(m: dict, ms: dict, weights: dict = None) -> dict:
    """六维打分。m 为合并后的指标字典（含财务派生 + 估值 + 股息 + 名称）。"""
    w = dict(DEFAULT_WEIGHTS)
    if weights:
        w.update({k: float(v) for k, v in weights.items() if k in w})

    roe = m.get("roe_avg3") if m.get("roe_avg3") is not None else m.get("roe")
    # 1) 质量：ROE 水平（主）+ ROE 稳定性 + 毛利率水平
    q_core = _band(roe, [(2, 8), (5, 28), (8, 45), (12, 65), (15, 78),
                         (20, 90), (28, 100)])
    ra, rm = m.get("roe_avg3"), m.get("roe_min3")
    q_stab = None
    if ra and rm is not None and ra > 1:
        q_stab = _band(rm / ra, [(0.4, 25), (0.65, 55), (0.85, 80), (0.97, 93)])
    q_gm = _band(m.get("gross_margin_avg3"), [(8, 25), (15, 42), (25, 60),
                                              (40, 80), (60, 92), (80, 100)])
    v_quality = _mix([(q_core, 0.55), (q_stab, 0.20), (q_gm, 0.25)])

    # 2) 成长：营收同比 + 净利同比 + 3 年营收 CAGR
    g_rev = _band(m.get("rev_yoy"), [(-25, 5), (-10, 22), (0, 40), (8, 58),
                                     (15, 72), (25, 86), (40, 100)])
    g_prf = _band(m.get("profit_yoy"), [(-35, 5), (-15, 22), (0, 42), (10, 60),
                                        (20, 76), (35, 90), (60, 100)])
    g_cagr = _band((m.get("rev_cagr3") or 0) * 100 if m.get("rev_cagr3") is not None
                   else None, [(-8, 8), (0, 35), (8, 55), (15, 72), (25, 90), (35, 100)])
    v_growth = _mix([(g_rev, 0.40), (g_prf, 0.35), (g_cagr, 0.25)])

    # 金融特征识别：银行/保险没有毛利率概念（XSMLL 为空）、负债率天然 90%+。
    # 用通用制造/消费口径的评价规则去罚它们，会得到「所有银行都是垃圾」的错误结论。
    financial_like = (m.get("gross_margin") is None
                      and (m.get("debt_ratio") or 0) > 80)

    # 3) 现金流：经营现金流 / 净利润（利润含金量）
    #    金融股的「经营活动现金流」含存款/同业往来变动，口径与工商企业不可比，取中性。
    if financial_like:
        v_cashflow = 60.0
        flow_note = "金融类现金流口径特殊（含存款变动），本维取中性"
    else:
        v_cashflow = _band(m.get("ocf_to_profit"),
                           [(-0.5, 0), (0, 12), (0.4, 35), (0.75, 58),
                            (1.0, 78), (1.4, 93), (2.5, 100)])
        flow_note = None

    # 4) 财务安全：资产负债率 + 流动比率
    if financial_like:
        v_balance = 60.0
        bal_note = "金融类资产负债结构（负债率天然偏高），本维取中性"
    else:
        b_debt = _band(m.get("debt_ratio"), [(10, 96), (25, 92), (40, 82),
                                             (55, 66), (70, 42), (85, 15)])
        b_cur = _band(m.get("current_ratio"), [(0.8, 18), (1.2, 48), (1.8, 76),
                                               (3.0, 92), (5.0, 96)])
        v_balance = _mix([(b_debt, 0.60), (b_cur, 0.40)])
        bal_note = None

    # 5) 估值：PB 历史分位 + PE 水平 + 股息率
    e_pb = _band(m.get("pb_pct"), [(0.02, 98), (0.1, 92), (0.25, 82), (0.4, 70),
                                   (0.6, 52), (0.8, 30), (0.95, 12)])
    pe = m.get("pe_ttm")
    if pe is not None and pe <= 0:
        e_pe = 18.0                              # 亏损：可能是周期底部，不给 0 分
    else:
        e_pe = _band(pe, [(4, 94), (8, 90), (12, 84), (18, 74), (25, 60),
                          (35, 44), (50, 26), (70, 14), (100, 6)])
    e_div = _band(m.get("div_yield"), [(0, 18), (0.8, 38), (1.5, 52), (2.5, 68),
                                       (3.5, 82), (5.0, 93), (7.0, 98)])
    v_valuation = _mix([(e_pb, 0.35), (e_pe, 0.30), (e_div, 0.35)])

    # 6) 宏观契合
    tags = tag_style(m)
    bonus, fit_note = mc.macro_fit_bonus(tags, ms or {})
    v_macro = 50.0 + bonus * 8.33

    dims = {"quality": v_quality, "growth": v_growth, "cashflow": v_cashflow,
            "balance": v_balance, "valuation": v_valuation, "macro": v_macro}
    avail = {k: v for k, v in dims.items() if v is not None}
    tw = sum(w[k] for k in avail)
    total = sum(avail[k] * w[k] for k in avail) / tw if tw else None

    out = {"score": round(total, 1) if total is not None else None,
           **{f"v_{k}": (round(v, 1) if v is not None else None)
              for k, v in dims.items()},
           "style": "/".join([k for k, v in tags.items() if v]) or "neutral",
           "macro_fit": fit_note, "coverage": round(tw, 3)}
    if bal_note:
        out["balance_note"] = bal_note
    if flow_note:
        out["cashflow_note"] = flow_note
    out["financial_like"] = bool(financial_like)
    out["tags"] = tags
    return out


def analyze_stock(item: dict, ms: dict, weights: dict = None,
                  kline_raw_fn=None, ttl_days: int = 3) -> dict | None:
    """单只 A 股的深度分析：财务 → 派生指标 → 估值分位 → 股息 → 六维打分。"""
    code, market = item["code"], item.get("market", "cn")
    rows = fd.fetch_financial(code, market)
    if not rows:
        return None
    m = fd.derived_metrics(rows)
    if not m or (m.get("n_annual") or 0) < 2:
        return None                              # 上市不足 2 年，长期价值无意义
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
    for k2 in ("name", "code", "market", "report_date", "report_type", "price",
               "pe_ttm", "pb", "pb_pct", "px_pct", "div_yield", "total_mcap",
               "roe_avg3", "roe_min3", "roe", "gross_margin_avg3", "net_margin",
               "rev_yoy", "profit_yoy", "rev_cagr3", "profit_cagr3",
               "ocf_to_profit", "debt_ratio", "current_ratio", "n_annual",
               "score", "v_quality", "v_growth", "v_cashflow", "v_balance",
               "v_valuation", "v_macro", "style", "macro_fit", "coverage",
               "bucket", "valuation_method", "balance_note", "tags"):
        m.setdefault(k2, None)
    return m


# ---------------------------------------------------------------------------
# 基金：场外基金 + 场内 ETF
# ---------------------------------------------------------------------------
def score_fund(prof: dict, nav: pd.DataFrame, ms: dict, weights: dict = None) -> dict:
    """基金四维：风险调整后业绩 / 稳定性 / 规模健康度 / 经理。

    不用「近 1 年收益排名」直接选基——高收益常来自押注单一赛道，
    这里用「年化收益 ÷ 年化波动」的类 Sharpe 做主指标。
    刻意不设「宏观契合」维度：基金与宏观的匹配没有客观判定依据，
    与其编一个看似合理的分数，不如把这部分权重给可验证的业绩与稳定性。
    """
    w = {"perf": 0.34, "stability": 0.28, "scale": 0.24, "manager": 0.14}
    if weights:
        w.update({k: float(v) for k, v in weights.items() if k in w})

    sharpe = ann_ret = ann_vol = max_dd = None
    if nav is not None and len(nav) > 40:
        s = pd.to_numeric(nav["nav"], errors="coerce").dropna()
        if len(s) > 40:
            r = s.pct_change().dropna()
            ann_ret = float((s.iloc[-1] / s.iloc[0]) ** (250 / len(s)) - 1) * 100
            ann_vol = float(r.std() * (250 ** 0.5)) * 100
            if ann_vol > 1e-6:
                sharpe = ann_ret / ann_vol
            peak = s.cummax()
            max_dd = float((s / peak - 1).min()) * 100

    f_perf = _band(sharpe, [(-1.0, 8), (-0.3, 30), (0, 45), (0.4, 62),
                            (0.8, 80), (1.4, 93)])
    f_ret = _band(prof.get("ret_1y"), [(-35, 8), (-20, 28), (-8, 48), (0, 60),
                                       (15, 76), (35, 90), (60, 100)])
    perf = _mix([(f_perf, 0.6), (f_ret, 0.4)])
    # 稳定性：回撤越浅越好 + 近 1 年与近 6 月收益方向是否一致（不分裂）
    f_dd = _band(max_dd, [(-60, 10), (-40, 30), (-25, 55), (-15, 75), (-8, 90)])
    div_score = None
    if prof.get("ret_1y") is not None and prof.get("ret_6m") is not None:
        div_score = 80.0 if (prof["ret_1y"] > 0) == (prof["ret_6m"] > 0) else 45.0
    stability = _mix([(f_dd, 0.6), (div_score, 0.4)])
    # 规模健康度：太小有清盘风险；大幅缩水说明资金在用脚投票
    f_size = _band(prof.get("scale"), [(0.5, 25), (2, 50), (10, 75), (50, 88), (150, 92)])
    f_chg = _band(prof.get("scale_chg"), [(-0.4, 12), (-0.2, 38), (-0.05, 58),
                                          (0.05, 68), (0.25, 80)])
    scale = _mix([(f_size, 0.5), (f_chg, 0.5)])
    # 经理：星级 + 任职年限
    star = prof.get("manager_star")
    f_star = _band(star, [(1, 30), (3, 60), (4, 78), (5, 92)]) if star else None
    mnum = None
    mm = re.search(r"(\d+)\s*年", str(prof.get("manager_years") or ""))
    if mm:
        mnum = float(mm.group(1))
    f_yrs = _band(mnum, [(1, 35), (3, 55), (5, 72), (8, 86), (12, 94)])
    manager = _mix([(f_star, 0.55), (f_yrs, 0.45)])

    dims = {"perf": perf, "stability": stability, "scale": scale, "manager": manager}
    avail = {k: v for k, v in dims.items() if v is not None}
    tw = sum(w[k] for k in avail)
    total = sum(avail[k] * w[k] for k in avail) / tw if tw else None
    return {
        "score": round(total, 1) if total is not None else None,
        "f_perf": round(perf, 1) if perf is not None else None,
        "f_stability": round(stability, 1) if stability is not None else None,
        "f_scale": round(scale, 1) if scale is not None else None,
        "f_manager": round(manager, 1) if manager is not None else None,
        "sharpe": round(sharpe, 3) if sharpe is not None else None,
        "ann_ret": round(ann_ret, 2) if ann_ret is not None else None,
        "ann_vol": round(ann_vol, 2) if ann_vol is not None else None,
        "max_dd": round(max_dd, 2) if max_dd is not None else None,
        "coverage": round(tw, 3),
    }


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
        "code": code, "name": prof.get("name"), "market": "fund",
        "price": (float(nav["nav"].iloc[-1])
                  if nav is not None and len(nav) else None),
        **{k: prof.get(k) for k in ("ret_1y", "ret_6m", "ret_3m", "ret_1m",
                                    "scale", "scale_chg", "manager",
                                    "manager_star", "manager_years",
                                    "stock_position", "asset_stock_ratio",
                                    "rate_now", "holdings")},
        **s,
    }


# ---------------------------------------------------------------------------
# 主入口
# ---------------------------------------------------------------------------
def pick_longterm(cfg: dict, params: dict, ms: dict, market_data: dict,
                  kline_raw_fn=None, nav_fn=None, progress=None) -> dict:
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
        import fetcher
        fast = fetcher.fetch_cn_rank_by_amount(300)
        print("成交额榜:", len(fast))
    except Exception as e:
        print("成交额榜失败:", e)
    uni = build_stock_universe(fast, cfg)
    print("标的池:", len(uni), [f"{u['name']}({u['bucket']})" for u in uni[:8]])
