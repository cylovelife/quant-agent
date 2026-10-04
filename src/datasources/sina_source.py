# -*- coding: utf-8 -*-
"""新浪系现货列表适配器（Phase 1 Step 4）：ETF / 港股。

存在的理由不是「多一个源」，而是**消除一个会导致结论失真的降级**：

现状是 ETF/港股列表走东财 `push2` clist，而本机东财成功率只有 9%~15%
（`data_health` 长期画像），于是一年中的大部分运行都落在第二档——
`fetcher._fetch_spot_list_tencent` 读的是 `universe_fallback.json` 里的
**静态核心池：ETF 64 只、港股 48 只**。

也就是说：**「跌幅居前的活跃 ETF」这个候选池，多数日子是在 64 只的固定名单里选出来的**，
而全市场有 1693 只 ETF、2811 只港股。静态池不漏报错误，它只是悄悄把机会集合缩小了——
这比报错更难发现，因为报告看起来一切正常。

新浪这两条给的是**全量**，且经过实测可用：

| 接口 | 行数 | 耗时 | 覆盖 |
|---|---|---|---|
| `fund_etf_category_sina(symbol="ETF基金")` | 1693 | 3.1s | 全部 ETF |
| `stock_hk_spot()` | 2811 | 22.6s | 全部港股 |

单位与代码口径（实测确认）：`成交额` 是**元**；`涨跌幅` 是**百分数**；
ETF 代码带 `sh`/`sz` 前缀需剥掉，港股是 5 位数字码（与 `universe_fallback.json` 一致）。
两张表都**没有** PE / 流通市值 / 换手率 / 量比——这几列写 NULL，
下游 `prefilter` 只需要 price/pct/amount/name，不依赖它们。

美股**不做**：akshare 的新浪美股现货要逐页抓 911 页（实测预热 136/911 用了 69s，
全量不可接受），所以美股的 tier 列表里干脆不放新浪——不放就不会在画像里
留下一条永远失败的假记录。
"""

import re

import pandas as pd

import store

from .base import (SchemaError, SourceGuard, akshare_installed,
                   anomaly_payload, sources_enabled, to_float)

SOURCE = "sina_spot_list"

# 只支持这两个市场；美股没有可用的新浪源（见模块说明），拿不到就老老实实返回空。
SUPPORTED_MARKETS = ("etf", "hk")

_SPOT_ALL = ["code", "name", "price", "pct", "amount"]
_GUARD = None


def available() -> bool:
    return akshare_installed()


def enabled() -> bool:
    return sources_enabled()


def guard() -> SourceGuard:
    """本源的护栏。预算给得比别的源宽：港股全量一次就要 20s 上下。"""
    global _GUARD
    if _GUARD is None:
        _GUARD = SourceGuard(SOURCE, min_gap=1.0, budget_sec=120.0, retries=2)
    return _GUARD


def configure(**kw):
    guard().configure(**kw)


def _ak():
    import akshare as ak
    return ak


# ---------------------------------------------------------------------------
# 归一（纯函数）
# ---------------------------------------------------------------------------
_CODE_PREFIX = re.compile(r"^(sh|sz|bj)", re.I)


def normalize_code(v, market: str):
    """新浪代码 → 本项目的代码口径。

    - ETF：新浪给 `sz159998`（带交易所前缀），剥掉前缀得 `159998`；
    - 港股：新浪给 `00001`（5 位），保持原样——与 `universe_fallback.json`
      和东财口径一致，补零会把它变成另一个代码。
    """
    if v is None:
        return None
    s = str(v).strip()
    if not s:
        return None
    if market == "etf":
        s = _CODE_PREFIX.sub("", s)
        return s.zfill(6) if s.isdigit() else None
    return s


def normalize_spot(df, market: str):
    """新浪现货表 → `(规范表, 异常统计)`。

    规范列与 `fetcher` 的现货口径对齐（code/name/price/pct/amount）；
    PE、流通市值、换手率、量比这几列新浪不提供，调用方写 NULL——
    **不补 0**：0 在估值/流动性判断里是「极度便宜/毫无成交」这个事实，
    而这里是「上游不提供」。
    """
    if df is None:
        return pd.DataFrame(columns=_SPOT_ALL), {"nodata": True}
    if not isinstance(df, pd.DataFrame):
        raise SchemaError(f"上游返回的不是 DataFrame，而是 {type(df).__name__}")
    if len(df.columns) == 0:
        # 无列的空壳 = 本次没数据（与事件类适配器同一条判据）
        return pd.DataFrame(columns=_SPOT_ALL), {"nodata": True}
    need = ["代码", "最新价", "涨跌幅", "成交额"]
    missing = [c for c in need if c not in df.columns]
    if missing:
        raise SchemaError(f"上游字段缺失 {missing}；实际列={list(df.columns)}")

    name_col = "中文名称" if "中文名称" in df.columns else "名称"
    out = pd.DataFrame({
        "code": df["代码"].map(lambda v: normalize_code(v, market)),
        "name": (df[name_col].astype(str).str.strip()
                 if name_col in df.columns else ""),
        "price": df["最新价"].map(to_float),
        "pct": df["涨跌幅"].map(to_float),
        "amount": df["成交额"].map(to_float),
    })
    bad_code = int(out["code"].isna().sum())
    out = out[out["code"].notna()]
    # 停牌/无成交的行会带 0 价；留着会让 prefilter 的 price 条件把它当成便宜货
    zero_price = int((out["price"].fillna(0) <= 0).sum())
    out = out[out["price"].fillna(0) > 0]
    out = out.dropna(subset=["pct"])
    dup = int(out["code"].duplicated().sum())
    out = out.drop_duplicates(subset=["code"], keep="first").reset_index(drop=True)
    out = out[_SPOT_ALL]
    return out, {"bad_code": bad_code, "zero_price": zero_price, "dup": dup,
                 "rows": len(out)}


def fetch_spot(market: str):
    """抓一次新浪现货（原始 DataFrame，未归一）。"""
    ak = _ak()
    if market == "etf":
        return ak.fund_etf_category_sina(symbol="ETF基金")
    if market == "hk":
        return ak.stock_hk_spot()
    raise ValueError(f"新浪源不支持的市场: {market}（支持 {SUPPORTED_MARKETS}）")


def spot_list(market: str, log=print) -> pd.DataFrame:
    """取某市场的现货列表（规范化）。**不抛异常**，失败返回空表。

    与 `fetcher` 的现货口径一致：返回 DataFrame（可能为空），
    成败与耗时由护栏写进 `data_health`，调用方按「空表就走下一档」处理。
    """
    if market not in SUPPORTED_MARKETS:
        # 不支持的市场的**不写画像**：写了会留下一条永远失败的记录，
        # 把健康画像污染成噪声（美股就是这种情况）。
        log(f"    新浪源不支持 {market}，跳过")
        return pd.DataFrame(columns=_SPOT_ALL)
    if not enabled():
        return pd.DataFrame(columns=_SPOT_ALL)
    res = guard().call(market, lambda: fetch_spot(market))
    if not res.ok:
        log(f"    新浪 {market} 未取到（{res.kind}）：{res.error[:90]}")
        return pd.DataFrame(columns=_SPOT_ALL)
    try:
        norm, anom = normalize_spot(res.df, market)
    except Exception as e:
        kind = "schema" if isinstance(e, SchemaError) else "unknown"
        guard().breaker.trip(f"{kind}: {e}"[:160])
        log(f"    新浪 {market} 归一失败（{kind}）：{e}")
        return pd.DataFrame(columns=_SPOT_ALL)
    anom = anomaly_payload(anom)
    if anom:
        store.attach_health_anomalies(SOURCE, market, anom)
    log(f"    新浪 {market}: {len(norm)} 只（{res.elapsed:.1f}s）"
        + (f"；异常 {anom}" if anom else ""))
    return norm


def probe() -> dict:
    """自检：两个市场各测一次，只读不写。"""
    out = {"source": SOURCE, "enabled": enabled(), "markets": {}}
    if not enabled():
        out["error"] = "数据源未启用（QUANT_AKSHARE=off 或未装 akshare）"
        return out
    for m in SUPPORTED_MARKETS:
        df = spot_list(m, log=lambda *_: None)
        out["markets"][m] = {"rows": int(len(df))}
        if len(df):
            r = df.iloc[0]
            out["markets"][m]["sample"] = {c: (None if pd.isna(r[c]) else r[c])
                                           for c in _SPOT_ALL}
    return out
