# -*- coding: utf-8 -*-
"""akshare 数据源适配器（Phase 1）。

选型依据是实测，不是文档（见 `docs/research/phase1_data_channel_probe.md`）：

| 数据 | 接口 | 上游 | 实测 |
|---|---|---|---|
| 个股资金流（截面） | `stock_fund_flow_individual` | **同花顺** data.10jqka.com.cn | 5211 行 / 14s ✅ |
| 龙虎榜 | `stock_lhb_*_em` | datacenter-web | ✅（Step 3） |
| 融资融券 | `stock_margin_detail_{sse,szse,bse}` | 交易所官网 | ✅（Step 3） |
| 限售解禁 | `stock_restricted_release_*_em` | datacenter-web | ✅（Step 3） |

**刻意不用东财的个股资金流接口**：`stock_individual_fund_flow`（push2his）与
`stock_individual_fund_flow_rank`（push2 clist）在本机对 curl / requests 直连 /
curl_cffi 三种客户端**一律被 reset 或回空**。同花顺这条是**异构**源，
既避开了坏掉的集群，也避免了「第二源其实是同一条链路」的假冗余。

为什么只做「即时」不做 3/5/10/20 日排行
---------------------------------------
上游的多日排行是**站内口径**，且只返回净额一个字段（没有流入/流出拆分）。
把两种口径放进同一张表，等于把「这个数是谁算的」藏起来；而分位、环比、IC 这类
下游计算全都依赖口径可比。多日窗口我们在这条日序列上自己求和，口径自持、可审计。
"""

import re

import pandas as pd

import store

from . import base as _base
from .base import SchemaError, SourceGuard, anomaly_payload, note_missing
from .base import normalize_code as _normalize_code

# 归一后的规范列。落盘层（`store.upsert_fund_flow`）只认这一组。
CANONICAL_COLS = ["code", "name", "price", "pct", "turnover",
                  "inflow", "outflow", "net", "amount"]

# 上游中文列名 → 规范列名。这张表是**契约**：上游改列名时这里会炸 SchemaError，
# 而不是静默产出一堆 None 字段（那种错要等三个月后才有人发现因子全是空的）。
_COL_MAP = {
    "股票代码": "code", "股票简称": "name", "最新价": "price",
    "涨跌幅": "pct", "换手率": "turnover",
    "流入资金": "inflow", "流出资金": "outflow",
    "净额": "net", "成交额": "amount",
}
# 「此字段缺失」的表示法。空串/横杠是**不知道**，不是 0。
_MISSING_TOKENS = ("", "-", "--", "—", "nan", "None", "null", "/")
_AMOUNT_COLS = ("inflow", "outflow", "net", "amount", "price")
_PCT_COLS = ("pct", "turnover")

_UNIT = {"亿": 1e8, "万": 1e4, "千": 1e3, "百": 1e2}
_NUM_RE = re.compile(r"^(-?\d+(?:\.\d+)?)\s*(亿|万|千|百)?$")

SOURCE = "akshare_ths_fund_flow"
MARKET = "cn"

_GUARD = None


def available() -> bool:
    """akshare 是否可用（探测实现与总开关都在 `base`，这里只做转发）。"""
    return _base.akshare_installed()


def enabled() -> bool:
    """本数据源是否启用（总开关开着 **且** akshare 真的装上了）。"""
    return _base.sources_enabled()


def guard() -> SourceGuard:
    """本源的护栏（进程内单例：熔断与预算状态必须跨调用保持）。"""
    global _GUARD
    if _GUARD is None:
        _GUARD = SourceGuard(SOURCE, min_gap=1.0, budget_sec=90.0, retries=2)
    return _GUARD


def configure(enabled=None, **kw):
    """由 run.py 用 config.json 覆盖。开关走 `base`（全局一份），护栏参数走本模块。"""
    if enabled is not None:
        _base.configure_sources(enabled)
    guard().configure(**kw)


def _ak():
    import akshare as ak
    return ak


# ---------------------------------------------------------------------------
# 归一（纯函数：不碰网络也不碰库，所以能直接用构造的 DataFrame 测）
# ---------------------------------------------------------------------------
def parse_cn_amount(v):
    """中文数量串 → **元**（float）。「12.34亿」→ 1234000000.0。

    解析不出来一律返回 `None`（「不知道」），**绝不返回 0**：0 是「资金净流入为零」
    这个事实，None 是「没这个数」。在因子里两者含义相反——0 会作为一个真实观测值
    参与排序，把缺失的股票挤到一个虚假的名次上。
    """
    if v is None:
        return None
    if isinstance(v, (int, float)):
        f = float(v)
        return None if f != f else f
    s = str(v).strip()
    if s in _MISSING_TOKENS:
        return None
    s = s.replace(",", "").replace("元", "")
    m = _NUM_RE.match(s)
    if not m:
        return None
    return float(m.group(1)) * _UNIT.get(m.group(2) or "", 1.0)


def parse_pct(v):
    """百分数字符串 → float（百分数本身，不是小数）。「653.16%」→ 653.16。"""
    if v is None:
        return None
    if isinstance(v, (int, float)):
        f = float(v)
        return None if f != f else f
    s = str(v).strip()
    if s in _MISSING_TOKENS:
        return None
    s = s.replace("%", "").replace(",", "")
    m = _NUM_RE.match(s)
    return float(m.group(1)) if m else None


def normalize_code(v):
    """股票代码 → 6 位字符串。

    **上游返回的代码已经被解析成整数，前导零丢了**：`000001` → `1`（实测：
    5211 行里 6 位的有 3720 个，其余是 1~4 位）。A 股代码恒为 6 位，左侧补零即可还原。

    长度 > 6 或含非数字的返回 `None`（口径变了）——宁可少一行、记一条异常，
    也不要猜一个「看起来像代码」的串写进库，那会污染主键。

    实现放在 `base`（龙虎榜/两融/解禁同样要补零），这里保留同名入口，
    以免调用方要同时记住两个模块。
    """
    return _normalize_code(v)


def normalize_individual(df) -> tuple:
    """同花顺个股资金流 → `(规范 DataFrame, 异常统计)`。

    异常统计里记的是「有值但解析失败」的行数——静默丢弃是最坏的处理方式：
    上游悄悄换了单位（比如把「亿」改成「万元」）时，只有把它显式报出来才看得见。
    """
    if df is None or len(df) == 0:
        return pd.DataFrame(columns=CANONICAL_COLS), {}
    if not isinstance(df, pd.DataFrame):
        raise SchemaError(f"上游返回的不是 DataFrame，而是 {type(df).__name__}")
    missing = [c for c in _COL_MAP if c not in df.columns]
    if missing:
        raise SchemaError(f"上游字段缺失 {missing}；实际列={list(df.columns)}")

    d = df.rename(columns=_COL_MAP)
    out = pd.DataFrame(index=d.index)
    out["name"] = d["name"].astype(str).str.strip()

    raw_codes = d["code"]
    out["code"] = raw_codes.map(normalize_code)
    bad_code = int(out["code"].isna().sum())

    for c in _AMOUNT_COLS:
        out[c] = d[c].map(parse_cn_amount)
    for c in _PCT_COLS:
        out[c] = d[c].map(parse_pct)

    # 净额缺失但流入/流出都在 → 用减法补齐，并标注（上游偶发只缺一个字段）。
    derived = 0
    need = out["net"].isna() & out["inflow"].notna() & out["outflow"].notna()
    if need.any():
        out.loc[need, "net"] = out.loc[need, "inflow"] - out.loc[need, "outflow"]
        derived = int(need.sum())

    bad_amount = {c: int((d[c].notna() & out[c].isna()).sum()) for c in _AMOUNT_COLS}
    dropped = out[out["code"].isna()]
    out = out[out["code"].notna()].copy()
    out["code"] = out["code"].astype(str)
    # 同一代码重复出现时保留最后一条（上游分页拼接偶发重复），主键不容重复。
    dup = int(out["code"].duplicated().sum())
    out = out.drop_duplicates(subset=["code"], keep="last").reset_index(drop=True)
    out = out[CANONICAL_COLS]
    return out, {"bad_code": bad_code, "dup_code": dup, "net_derived": derived,
                 "bad_amount": {k: v for k, v in bad_amount.items() if v},
                 "dropped_sample": ([] if dropped.empty
                                    else dropped["name"].head(3).tolist())}


def fetch_fund_flow(symbol: str = "即时"):
    """抓一次同花顺个股资金流（原始 DataFrame，未归一）。

    进度条的压制机制放在 `base` 模块**导入时**完成——tqdm 只在被 import 的那一刻
    读环境变量，所以这件事必须早于 akshare 被导入；写在调用现场是无效的（实测过）。
    """
    return _ak().stock_fund_flow_individual(symbol=symbol)


# ---------------------------------------------------------------------------
# 抓取 + 落库
# ---------------------------------------------------------------------------
def collect_fund_flow(flow_date: str = None, market: str = MARKET,
                      log=print, persist: bool = True,
                      reuse_settled: bool = True) -> dict:
    """抓取 → 归一 → 落库 → 记画像。**不抛异常**，失败返回 `ok=False`。

    `reuse_settled=True`（默认）时，若该交易日已经有**收盘后抓到的**快照，直接复用、
    不再打上游（判据见 `store.fund_flow_is_fresh`）。盘中抓的不会被复用，
    因为定盘价还没出来——这和 K 线「跨过收盘必须失效」是同一个坑。

    返回::

        {"ok": bool, "kind": str, "elapsed": s, "rows": 归一后行数,
         "written": 实际写入行数, "flow_date": ..., "anomalies": {...},
         "reused": bool, "skipped": "breaker"/"budget"/"disabled"/""}
    """
    base = {"ok": False, "kind": "", "elapsed": 0.0, "rows": 0, "written": 0,
            "flow_date": flow_date, "anomalies": {}, "reused": False, "skipped": ""}
    if not _base.switch_on():
        base.update(kind="disabled", skipped="disabled")
        return base
    if not available():
        note_missing(SOURCE, market, "akshare 未安装")
        log("  资金流跳过：akshare 未安装（pip install akshare）")
        base.update(kind="unavailable", skipped="unavailable")
        return base
    if persist and reuse_settled and flow_date and \
            store.fund_flow_is_fresh(flow_date, market):
        log(f"  资金流复用当日定盘快照（{flow_date} 收盘后已抓过一次），跳过网络")
        base.update(ok=True, kind="", reused=True)
        return base

    g = guard()
    res = g.call(market, lambda: fetch_fund_flow("即时"))
    if not res.ok:
        log(f"  资金流未取到（{res.kind}{'，已跳过' if res.skipped else ''}）："
            f"{res.error[:100]}")
        base.update(kind=res.kind, elapsed=res.elapsed, skipped=res.skipped)
        return base

    try:
        norm, anomalies = normalize_individual(res.df)
    except Exception as e:
        # 归一失败也是「源头变了」：记 schema 并跳闸，别让它每天白跑一次。
        kind = "schema" if isinstance(e, SchemaError) else "unknown"
        if kind == "schema":
            g.breaker.trip(f"schema: {e}"[:160])
        note_missing(SOURCE, market, f"归一失败({kind}): {e}"[:150])
        store.record_health(SOURCE, market, False, res.elapsed, 0,
                            f"归一失败({kind}): {e}"[:150])
        log(f"  资金流归一失败（{kind}）：{e}")
        base.update(kind=kind, elapsed=res.elapsed)
        return base

    if norm.empty:
        note_missing(SOURCE, market, "归一后为空")
        log("  资金流归一后为空（上游可能改版或本次返回空表）")
        base.update(kind="empty", elapsed=res.elapsed, anomalies=anomalies)
        return base

    written = 0
    if persist:
        written = store.upsert_fund_flow(market, flow_date, norm, src=SOURCE)
        # 「已落库」必须来自写入回执，不能来自「我调了写库函数」——适配器自写库、
        # 调用方只看返回值时，写入失败照样会打日志说成功（老库缺列时实测复现过）。
        if written != len(norm):
            log(f"  ⚠ 资金流落库行数不符：归一 {len(norm)} 行，实际写入 {written} 行"
                f"（库不可用或表结构异常）")
    anom = anomaly_payload(anomalies)
    # 归一异常（字段解析失败/代码异常/去重计数）附到刚写下的那行画像上：
    # 「抓取成功但字段悄悄变 NULL」在成功率上看不出来，只能靠这一列。
    if anom:
        store.attach_health_anomalies(SOURCE, market, anom)
    log(f"  资金流 {len(norm)} 只 / 写入 {written} 行（账期 {flow_date}，"
        f"{res.elapsed:.1f}s）" + (f"；异常 {anom}" if anom else ""))
    base.update(ok=written > 0 if persist else len(norm) > 0,
                kind=res.kind, elapsed=res.elapsed, rows=len(norm),
                written=written, anomalies=anom)
    return base


def status() -> dict:
    """当前开关与护栏状态（供 `--fund-flow` 与排障打印）。"""
    g = guard()
    return {"source": SOURCE, "enabled": enabled(), "installed": available(),
            "min_gap_sec": g.min_gap, "budget_sec": g.budget.budget,
            "retries": g.retries, "spent_sec": round(g.budget.spent, 1),
            "breaker": g.breaker.blocked(), "breaker_reason": g.breaker.reason()}


def probe() -> dict:
    """自检：只测「能不能连上、拿多少行、字段对不对」，不写库。

    与 `csc_source.probe()` 同一用途——源不通时先回答「是网的问题还是代码的问题」。
    """
    out = {"source": SOURCE, "akshare": None, "reachable": False, "rows": 0,
           "cols_ok": False, "missing_cols": [], "anomalies": {}, "error": ""}
    if not _base.switch_on():
        out["error"] = ("数据源已关闭（QUANT_AKSHARE=off 或 "
                        "config.datasource.akshare_enabled=false）")
        return out
    try:
        import akshare as ak
        out["akshare"] = getattr(ak, "__version__", "?")
    except Exception as e:
        out["error"] = f"akshare 不可用: {e}"
        return out
    res = guard().call(MARKET, lambda: fetch_fund_flow("即时"))
    out["reachable"] = bool(res.ok)
    if not res.ok:
        out["error"] = f"{res.kind}: {res.error[:200]}"
        return out
    try:
        norm, anomalies = normalize_individual(res.df)
    except Exception as e:
        out["error"] = f"归一失败: {e}"
        return out
    out["rows"] = len(norm)
    out["raw_rows"] = len(res.df)
    out["missing_cols"] = [c for c in _COL_MAP if c not in res.df.columns]
    out["cols_ok"] = not out["missing_cols"]
    out["anomalies"] = {k: v for k, v in anomalies.items() if v}
    out["elapsed"] = round(res.elapsed, 2)
    if len(norm):
        r = norm.iloc[0]
        out["sample"] = {c: (None if pd.isna(r[c]) else r[c]) for c in CANONICAL_COLS}
    return out
