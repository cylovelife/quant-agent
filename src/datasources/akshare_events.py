# -*- coding: utf-8 -*-
"""事件类数据适配器：龙虎榜 / 融资融券 / 限售解禁 / 退市清单（Phase 1 Step 3）。

选型与口径全部来自实测（`docs/research/phase1_data_channel_probe.md` 与 Step 3 现场复测）：

| 数据 | 接口 | 上游 | 可回补 | 实测 |
|---|---|---|---|---|
| 龙虎榜明细 | `stock_lhb_detail_em(start,end)` | datacenter-web | ✅ 2024 年数据可取 | 1554 行/月，14s |
| 龙虎榜机构统计 | `stock_lhb_jgmmtj_em(start,end)` | datacenter-web | ✅ | 912 行/月，9s |
| 个股两融 | `stock_margin_detail_{sse,szse,bse}(date)` | 交易所官网 | ❌ **只服务最新交易日** | 沪 2000 / 深 2107 行 |
| 限售解禁 | `stock_restricted_release_detail_em(start,end)` | datacenter-web | ✅ 未来窗口随时可拉 | 445 行/季，4s |
| 退市清单 | `stock_info_{sh,sz}_delist()` | 交易所官网 | ✅ 静态清单 | 159 + 208 行 |

两条贯穿全模块的纪律
--------------------
1. **不收上游算好的事件后收益**。龙虎榜的 `上榜后1/2/5/10日`、解禁的
   `解禁后20日涨跌幅` 都是**前视量**：它们一旦进库，任何按列名批量取特征的
   下游都可能把它当因子用。本项目已经为这类泄漏付过一次很贵的学费
   （重叠窗口让 GBR 的样本外 IC 从 +0.039 虚报到 +0.4906）。
   事件后收益由我们在自己的 K 线上算，口径自持。
   —— 实现上不是「过滤掉」，而是**列映射表里干脆没有它们**，并留一条断言测试。

2. **单位与日期逐字段标注，不按表推断**。同一家厂商内部就不统一：
   龙虎榜的 `净买额占总成交比` 是**百分数**（5.02 表示 5.02%），
   解禁的 `占解禁前流通市值比例` 是**小数**（0.000234 表示 0.0234%）；
   日期更是 `20260929` 与 `2026-09-29` 两种写法并存。
   日期统一走 `base.norm_date`——不归一的话，「20260929」和「2026-09-29」
   会在同一个主键列里被当成两天，逐日累积的数据一天裂成两份。
"""

import pandas as pd

import store

from . import base as _base
from .base import (SchemaError, SourceGuard, anomaly_payload,
                   compact_date, note_missing, norm_date,
                   normalize_code, to_float)

# 每个源一个独立的护栏：龙虎榜坏了不该把两融也掐掉（它们上游主机都不同）。
SOURCE_LHB = "akshare_lhb"
SOURCE_LHB_INST = "akshare_lhb_inst"
# 交易所按**主机**分开挂护栏，不合成一个「两融」源：
# 沪/深/北是三个不同的上游（sse.com.cn / szse.cn / bse.cn），实测过深市被 reset
# 而沪市正常。共用一个源名就等于让深市的抖动把沪市一起掐掉 30 分钟——
# 而画像里还会显示成「两融这个源不稳定」，连问题出在谁身上都看不出来。
SOURCE_MARGIN_SSE = "akshare_margin_sse"
SOURCE_MARGIN_SZSE = "akshare_margin_szse"
SOURCE_MARGIN_BSE = "akshare_margin_bse"
SOURCE_RESTRICTION = "akshare_restriction"
SOURCE_DELISTED_SH = "akshare_delisted_sh"
SOURCE_DELISTED_SZ = "akshare_delisted_sz"

# 护栏参数：min_gap / budget_sec / retries
_GUARD_DEFAULTS = {
    SOURCE_LHB:          (1.0, 120.0, 2),
    SOURCE_LHB_INST:     (1.0, 120.0, 2),
    SOURCE_MARGIN_SSE:   (1.0, 60.0, 2),
    SOURCE_MARGIN_SZSE:  (1.0, 60.0, 2),
    SOURCE_MARGIN_BSE:   (1.0, 90.0, 2),   # 北交所单次就 11~35s
    SOURCE_RESTRICTION:  (1.0, 90.0, 2),
    SOURCE_DELISTED_SH:  (1.0, 60.0, 2),
    SOURCE_DELISTED_SZ:  (1.0, 60.0, 2),
}
_GUARDS = {}

# 北交所两融：实测单次 11~35s（还会偶发 ProxyError），只换回 344 行。
# 默认关闭——拿一次日循环里最长的一段耗时去换边际价值最低的一段数据不划算。
# 需要时用 config.json 的 `akshare_margin_bse: true` 打开。
_BSE_ENABLED = False


def guard(name: str) -> SourceGuard:
    """按源取护栏（进程内单例：熔断与预算状态必须跨调用保持）。"""
    if name not in _GUARDS:
        gap, budget, retries = _GUARD_DEFAULTS[name]
        _GUARDS[name] = SourceGuard(name, min_gap=gap, budget_sec=budget,
                                    retries=retries)
    return _GUARDS[name]


def configure(enabled=None, min_gap=None, budget_sec=None, retries=None,
              breaker_ttl_sec=None, bse_enabled=None):
    """由 run.py 用 config.json 覆盖。开关走 `base`（全局一份）。

    参数必须覆盖 `SourceGuard.configure` 的**全部**可调项：少一个就会
    静默退回内置默认值，而「配置写了不生效」比「没有这个配置」更难发现。
    """
    global _BSE_ENABLED
    if enabled is not None:
        _base.configure_sources(enabled)
    if bse_enabled is not None:
        _BSE_ENABLED = bool(bse_enabled)
    for name in _GUARD_DEFAULTS:
        guard(name).configure(min_gap=min_gap, budget_sec=budget_sec,
                              retries=retries, breaker_ttl_sec=breaker_ttl_sec)


def _ak():
    import akshare as ak
    return ak


def bse_enabled() -> bool:
    return bool(_BSE_ENABLED)


# ---------------------------------------------------------------------------
# 归一（纯函数：列映射表里没有的列，永远不会进库）
# ---------------------------------------------------------------------------
def _normalize(df, colmap, text_cols, key_cols, required,
               market: str = "cn", extra: dict = None):
    """上游 DataFrame → 规范 DataFrame + 异常统计。

    `required` 里的中文列缺任何一个就抛 `SchemaError`——上游改列名时必须**显式炸**，
    而不是静默产出一堆 None（那样要等三个月后有人发现因子全是空的）。
    """
    if df is None:
        return pd.DataFrame(), {"nodata": True}
    if not isinstance(df, pd.DataFrame):
        raise SchemaError(f"上游返回的不是 DataFrame，而是 {type(df).__name__}")
    if len(df.columns) == 0:
        # **一个列都没有** ≠ 列名变了：上游返回空结果时 akshare 会给出一个无列的空壳。
        # 这必须当「本次没有数据」，不能当 schema 变更——否则会熔断一个健康的源，
        # 把「今天这个查询没结果」升级成「今天之后半小时都没有数据」。
        # 判据是「有没有列」：有列但列名不对才是真的改版。
        return pd.DataFrame(), {"nodata": True}
    missing = [c for c in required if c not in df.columns]
    if missing:
        raise SchemaError(f"上游字段缺失 {missing}；实际列={list(df.columns)}")

    out = pd.DataFrame(index=df.index)
    for cn, en in colmap.items():
        if cn not in df.columns:
            out[en] = None
            continue
        s = df[cn]
        if en in text_cols:
            out[en] = s.map(lambda v: (None if v is None or
                                       (isinstance(v, float) and v != v)
                                       else (str(v).strip() or None)))
        elif en.endswith("_date"):
            out[en] = s.map(norm_date)
        elif en == "code":
            out[en] = s.map(normalize_code)
        else:
            out[en] = s.map(to_float)
    # market 统一在这里注入：各调用方自己传容易出现「有的表填了有的忘了」，
    # 而这一列是查询口径的一部分（见 store 里事件表的注释）。
    out["market"] = market
    for k, v in (extra or {}).items():
        out[k] = v

    dropped = int(out[out["code"].isna()].shape[0]) if "code" in out.columns else 0
    out = out[out["code"].notna()] if "code" in out.columns else out
    keys = [k for k in key_cols if k in out.columns]
    if keys:
        bad_key = int(out[keys].isna().any(axis=1).sum())
        out = out[~out[keys].isna().any(axis=1)]
    else:
        bad_key = 0
    dup = int(out.duplicated(subset=keys).sum()) if keys else 0
    if keys:
        # 上游分页拼接偶发重复行；主键不容重复，保留最后一条。
        out = out.drop_duplicates(subset=keys, keep="last")
    out = out.reset_index(drop=True)
    return out, {"bad_code": dropped, "bad_key": bad_key, "dup": dup,
                 "rows": len(out)}


_LHB_MAP = {
    "上榜日": "trade_date", "代码": "code", "上榜原因": "reason",
    "名称": "name", "解读": "note",
    "收盘价": "close", "涨跌幅": "pct", "换手率": "turnover",
    "龙虎榜净买额": "net_buy", "龙虎榜买入额": "buy", "龙虎榜卖出额": "sell",
    "龙虎榜成交额": "amount", "市场总成交额": "market_amount",
    "净买额占总成交比": "net_ratio", "成交额占总成交比": "amount_ratio",
}
_LHB_TEXT = ("reason", "name", "note")
_LHB_KEY = ("trade_date", "code", "reason")
_LHB_REQUIRED = ("上榜日", "代码", "上榜原因", "龙虎榜净买额")


def normalize_lhb(df):
    """龙虎榜明细 → 规范表。

    注意**没有映射**的列：`上榜后1日/2日/5日/10日`（前视收益）、`序号`、
    `流通市值`（同厂商另一个接口用亿元，混进同一列会读错量级）。
    """
    return _normalize(df, _LHB_MAP, _LHB_TEXT, _LHB_KEY, _LHB_REQUIRED)


_LHB_INST_MAP = {
    "上榜日期": "trade_date", "代码": "code", "名称": "name", "上榜原因": "reason",
    "收盘价": "close", "涨跌幅": "pct", "换手率": "turnover",
    "买方机构数": "n_buy_inst", "卖方机构数": "n_sell_inst",
    "机构买入总额": "inst_buy", "机构卖出总额": "inst_sell",
    "机构买入净额": "inst_net", "市场总成交额": "market_amount",
    "机构净买额占总成交额比": "inst_net_ratio",
}
_LHB_INST_TEXT = ("name", "reason")
_LHB_INST_KEY = ("trade_date", "code")
_LHB_INST_REQUIRED = ("上榜日期", "代码", "机构买入净额")


def normalize_lhb_inst(df):
    """龙虎榜机构买卖统计 → 规范表（同样不含 `流通市值`，单位与明细不一致）。"""
    return _normalize(df, _LHB_INST_MAP, _LHB_INST_TEXT,
                      _LHB_INST_KEY, _LHB_INST_REQUIRED)


_MARGIN_MAP_SSE = {
    "信用交易日期": "trade_date", "标的证券代码": "code", "标的证券简称": "name",
    "融资余额": "fin_balance", "融资买入额": "fin_buy", "融资偿还额": "fin_repay",
    "融券余量": "short_volume", "融券卖出量": "short_sell",
    "融券偿还量": "short_repay",
}
# 深市/北交所没有「信用交易日期」列（也不给融资偿还额），日期由**调用方**给定；
# 沪市虽然有日期列，但如果实际返回的日期与请求的不一致，以**返回的**为准——
# 数据是哪天就该记哪天，不能记成我们想要的那天。
_MARGIN_MAP_NO_DATE = {
    "证券代码": "code", "证券简称": "name",
    "融资买入额": "fin_buy", "融资余额": "fin_balance",
    "融券卖出量": "short_sell", "融券余量": "short_volume",
    "融券余额": "short_balance", "融资融券余额": "total_balance",
}
_MARGIN_TEXT = ("name",)
_MARGIN_KEY = ("trade_date", "code")


def normalize_margin(df, exchange: str, trade_date: str):
    """两融明细 → 规范表。`exchange` ∈ sse/szse/bse。

    沪市用「信用交易日期」列（权威），深市/北交所没有日期列，用调用方给的
    `trade_date`。返回 `(规范表, 异常统计)`，异常里带 `date_from_source`
    以便调用方发现「要的是 T、给的是 T-1」。
    """
    m = _MARGIN_MAP_SSE if exchange == "sse" else _MARGIN_MAP_NO_DATE
    if exchange != "sse" and not trade_date:
        raise SchemaError("深市/北交所两融没有日期列，调用方必须给定 trade_date")
    out, anom = _normalize(df, m, _MARGIN_TEXT, _MARGIN_KEY,
                           ("证券代码",) if exchange != "sse" else
                           ("标的证券代码",), extra={"exchange": exchange})
    anom["date_from_source"] = None
    if exchange == "sse" and len(out):
        src_dates = sorted(set(out["trade_date"].dropna()))
        anom["date_from_source"] = src_dates[-1] if src_dates else None
        anom["date_mismatch"] = bool(src_dates and trade_date not in src_dates)
        if anom["date_mismatch"]:
            # 以上游给的日期为准：把 trade_date 换成实际日期，否则库里会多出
            # 一天「我们以为存在、其实没有任何行情」的数据。
            out = out.copy()
            out["trade_date"] = anom["date_from_source"]
    elif exchange != "sse" and len(out):
        out = out.copy()
        out["trade_date"] = trade_date
    return out, anom


_RESTRICTION_MAP = {
    "解禁时间": "release_date", "股票代码": "code", "股票简称": "name",
    "限售股类型": "share_type",
    "解禁数量": "release_shares", "实际解禁数量": "actual_shares",
    "实际解禁市值": "actual_value",
    "占解禁前流通市值比例": "ratio_of_float",
    "解禁前一交易日收盘价": "prev_close",
}
_RESTRICTION_TEXT = ("share_type", "name")
_RESTRICTION_KEY = ("code", "release_date", "share_type")
_RESTRICTION_REQUIRED = ("解禁时间", "股票代码", "限售股类型")


def normalize_restriction(df, market: str = "cn"):
    """限售解禁 → 规范表。

    没有映射 `解禁前20日涨跌幅` / `解禁后20日涨跌幅`：前者是过去、后者是未来，
    两个方向相反的字段并排放在同一行里，用的时候极容易读反。
    """
    out, anom = _normalize(df, _RESTRICTION_MAP, _RESTRICTION_TEXT,
                           _RESTRICTION_KEY, _RESTRICTION_REQUIRED,
                           market=market)
    return out, anom


_SH_DELIST_MAP = {"公司代码": "code", "公司简称": "name",
                  "上市日期": "list_date", "暂停上市日期": "delist_date"}
_SZ_DELIST_MAP = {"证券代码": "code", "证券简称": "name",
                  "上市日期": "list_date", "终止上市日期": "delist_date"}


def normalize_delisted(df, exchange: str, market: str = "cn"):
    """退市清单 → 规范表。`exchange` ∈ sh/sz。"""
    m = _SH_DELIST_MAP if exchange == "sh" else _SZ_DELIST_MAP
    out, anom = _normalize(df, m, ("name",), ("code", "delist_date"),
                           ("公司代码",) if exchange == "sh" else ("证券代码",),
                           market=market)
    return out, anom


# ---------------------------------------------------------------------------
# 抓取 + 落库
# ---------------------------------------------------------------------------
def _no_data_on_old_date(fn, **kw):
    """调两融明细接口；把「该日期没有数据」与「源坏了」分开。

    实测：沪市两融接口对**非最新交易日**不返回空表，而是让 akshare 在解析空响应时
    抛 `ValueError: Length mismatch`；深市直接回空表。两融明细**只服务最新一个
    交易日**，所以这属于正常的「无此日数据」。
    当成源故障处理会去重试、去熔断一个健康的源——正是既有代码反复警告的那种错。
    """
    try:
        return fn(**kw)
    except ValueError as e:
        if "Length mismatch" in str(e):
            return pd.DataFrame()
        raise


def _collect(name, market, fn, normalize, upsert, log, extra_note=""):
    """一次抓取 → 归一 → 落库的公共流程（护栏/画像/缺失留痕全在 guard 里）。"""
    g = guard(name)
    res = g.call(market, fn)
    base = {"ok": False, "kind": res.kind, "elapsed": res.elapsed,
            "rows": 0, "written": 0, "skipped": res.skipped, "note": ""}
    if not res.ok:
        log(f"    {name} 未取到（{res.kind}{'，已跳过' if res.skipped else ''}）："
            f"{res.error[:90]}")
        return base
    try:
        norm, anom = normalize(res.df)
    except Exception as e:
        kind = "schema" if isinstance(e, SchemaError) else "unknown"
        if kind == "schema":
            g.breaker.trip(f"schema: {e}"[:160])
        note_missing(name, market, f"归一失败({kind}): {e}"[:150])
        store.record_health(name, market, False, res.elapsed, 0,
                            f"归一失败({kind}): {e}"[:150])
        log(f"    {name} 归一失败（{kind}）：{e}")
        base.update(kind=kind, note="归一失败")
        return base
    raw_anom = dict(anom or {})
    anom = anomaly_payload(raw_anom)
    if anom:
        store.attach_health_anomalies(name, market, anom)
    if norm is None or norm.empty:
        nodata = bool(raw_anom.get("nodata"))
        note_missing(name, market, "上游无数据（nodata）" if nodata else "归一后为空")
        log(f"    {name} 无数据（{'上游明确回空' if nodata else '归一后为空'}）"
            f"{'，' + extra_note if extra_note else ''}")
        base.update(kind="nodata" if nodata else "empty", note="无数据")
        return base
    written = upsert(norm, src=name) if upsert else 0
    if written != len(norm):
        log(f"    ⚠ {name} 落库行数不符：归一 {len(norm)} 行，实际写入 {written} 行")
    log(f"    {name}: {len(norm)} 行 / 写入 {written} 行（{res.elapsed:.1f}s）"
        + (f"；异常 {anom}" if anom else ""))
    base.update(ok=written > 0, rows=len(norm), written=written, note=extra_note)
    return base


def collect_lhb(start_date: str, end_date: str = None, log=print) -> dict:
    """龙虎榜：明细 + 机构统计。可回补（实测 2024 年数据可取）。

    入参用 `YYYY-MM-DD`（与库内口径一致），**在调用上游那一刻**转成接口要求的
    `YYYYMMDD`——传错格式时上游回的是错误信封，而不是报「参数格式错」。
    """
    end_date = end_date or start_date
    out = {"start": start_date, "end": end_date}
    _s, _e = compact_date(start_date), compact_date(end_date)
    out["detail"] = _collect(
        SOURCE_LHB, "cn",
        lambda: _ak().stock_lhb_detail_em(start_date=_s, end_date=_e),
        normalize_lhb, store.upsert_lhb, log)
    out["inst"] = _collect(
        SOURCE_LHB_INST, "cn",
        lambda: _ak().stock_lhb_jgmmtj_em(start_date=_s, end_date=_e),
        normalize_lhb_inst, store.upsert_lhb_inst, log)
    out["ok"] = bool(out["detail"]["ok"] or out["inst"]["ok"])
    return out


def resolve_margin_date(trade_date: str, lookback_days: int = 45,
                        log=print):
    """用沪市**汇总**接口确定「≤ trade_date 的最近已公布交易日」。

    为什么必须多这一步（实测踩到）：交易所的两融明细是 **T+1 公布**，
    跑当天要当天的数据会拿到空——而这张表是逐日累积的，
    少一天就是永久缺一天。原来的实现直接请求交易日，实测在 22:46 请求 09-30
    时沪市回空、深市回空表，等于当天的两融数据直接丢了。
    汇总接口一次 0.4s 就能给出「到底公布了哪几天」，拿它当日历用。

    返回 `None` 表示连汇总也拿不到（调用方退回直接请求 trade_date）。
    """
    from datetime import datetime, timedelta
    try:
        d0 = (datetime.strptime(norm_date(trade_date), "%Y-%m-%d")
              - timedelta(days=int(lookback_days))).strftime("%Y%m%d")
        df = _ak().stock_margin_sse(start_date=d0,
                                    end_date=compact_date(trade_date))
    except Exception as e:
        log(f"    两融日历解析失败（退回直接请求 {trade_date}）: {str(e)[:80]}")
        return None
    if df is None or len(df) == 0 or "信用交易日期" not in getattr(df, "columns", []):
        return None
    dates = sorted({norm_date(v) for v in df["信用交易日期"]} - {None})
    cands = [d for d in dates if d <= trade_date]
    return cands[-1] if cands else None


def recent_margin_dates(trade_date: str, days: int, lookback_days: int = 45):
    """≤ trade_date 的最近 `days` 个已公布交易日（新→旧）。"""
    from datetime import datetime, timedelta
    try:
        d0 = (datetime.strptime(norm_date(trade_date), "%Y-%m-%d")
              - timedelta(days=int(lookback_days))).strftime("%Y%m%d")
        df = _ak().stock_margin_sse(start_date=d0,
                                    end_date=compact_date(trade_date))
    except Exception:
        return []
    if df is None or len(df) == 0 or "信用交易日期" not in getattr(df, "columns", []):
        return []
    dates = sorted({norm_date(v) for v in df["信用交易日期"]} - {None})
    return [d for d in reversed(dates) if d <= trade_date][:int(days)]


def _margin_one_day(day: str, log=print) -> dict:
    """抓某一个已公布交易日的沪/深（可选北交所）明细并落库。"""
    out = {"trade_date": day, "exchanges": {}}

    def _one(exchange, name_suffix, src):
        return _collect(
            src, "cn",
            lambda: _no_data_on_old_date(getattr(_ak(), name_suffix),
                                         date=compact_date(day)),
            lambda df: normalize_margin(df, exchange, day),
            store.upsert_margin, log, extra_note=f"exchange={exchange}")

    out["exchanges"]["sse"] = _one("sse", "stock_margin_detail_sse",
                                   SOURCE_MARGIN_SSE)
    out["exchanges"]["szse"] = _one("szse", "stock_margin_detail_szse",
                                    SOURCE_MARGIN_SZSE)
    if bse_enabled():
        out["exchanges"]["bse"] = _one("bse", "stock_margin_detail_bse",
                                       SOURCE_MARGIN_BSE)
    out["rows"] = sum(v["rows"] for v in out["exchanges"].values())
    out["written"] = sum(v["written"] for v in out["exchanges"].values())
    out["ok"] = out["written"] > 0
    return out


def collect_margin(trade_date: str, backfill_days: int = 0,
                   log=print) -> dict:
    """个股两融明细（沪/深，北交所按开关）。

    两个实测结论决定了这段逻辑：

    1. **T+1 公布**：跑当天要当天的数据会拿到空。所以先用汇总接口解析出
       「最近已公布交易日」，再抓那一天——否则当天的两融永久缺失。
    2. **可回补**（先前判成「不可回补」是被中秋假期误导：09-25~09-27 休市，
       请求非交易日才回空）。既然是真实交易日就能取，这里就顺手做**自愈补漏**：
       `backfill_days>0` 时把最近几个已公布交易日里**库里还没有的**补齐。
       只补缺的日期，不盲目重抓（幂等但没必要）。
    """
    out = {"requested": trade_date, "days": {}}
    target = resolve_margin_date(trade_date, log=log)
    if target is None:
        target = trade_date
    days = [target]
    if backfill_days > 0:
        for d in recent_margin_dates(trade_date, int(backfill_days) + 1):
            if d not in days:
                days.append(d)
    # 「已在库」必须按**交易所**判断，不能按日期：沪/深各自会独立失败，
    # 按日期判断会让「沪市成功、深市失败」的那天被判为已齐，深市缺口永不补齐。
    need_ex = ["sse", "szse"] + (["bse"] if bse_enabled() else [])
    have = store.margin_dates("cn", need_ex)
    todo = [d for d in days if d not in have]
    if not todo:
        # 已经在库就不打无谓的请求：两融是逐日累积的，重抓同一批数据没有收益，
        # 只是多两次请求、多一份被上游限流的风险。
        log(f"    两融目标日 {target}：候选 {days} 均已齐（{'+'.join(need_ex)}），跳过抓取")
        out.update(trade_date=target, rows=0, written=0, ok=True,
                   skipped="present")
        return out
    log(f"    两融目标日 {target}（请求日 {trade_date}）；待抓 {todo}")
    for d in todo:
        out["days"][d] = _margin_one_day(d, log=log)
    out["trade_date"] = target
    out["rows"] = sum(v["rows"] for v in out["days"].values())
    out["written"] = sum(v["written"] for v in out["days"].values())
    out["ok"] = out["written"] > 0
    if not out["ok"]:
        # 全都没抓到时，把明细当天的情况带出去，便于判断是「没公布」还是「源坏了」
        log(f"    两融无数据：目标日 {target} 可能尚未公布"
            f"（交易所 T+1 公布）")
    return out


def collect_restriction(start_date: str, end_date: str, log=print) -> dict:
    """限售解禁（前瞻日历，可回补）。"""
    out = _collect(
        SOURCE_RESTRICTION, "cn",
        lambda: _ak().stock_restricted_release_detail_em(
            start_date=compact_date(start_date), end_date=compact_date(end_date)),
        normalize_restriction, store.upsert_restriction, log,
        extra_note=f"{start_date}~{end_date}")
    out.update(start=start_date, end=end_date)
    return out


def collect_delisted(log=print) -> dict:
    """退市清单（幸存者偏差原料，可回补）。"""
    sh = _collect(SOURCE_DELISTED_SH, "cn",
                  lambda: _ak().stock_info_sh_delist(),
                  lambda df: normalize_delisted(df, "sh"),
                  store.upsert_delisted, log, extra_note="sh")
    sz = _collect(SOURCE_DELISTED_SZ, "cn",
                  lambda: _ak().stock_info_sz_delist(symbol="终止上市公司"),
                  lambda df: normalize_delisted(df, "sz"),
                  store.upsert_delisted, log, extra_note="sz")
    return {"ok": bool(sh["ok"] or sz["ok"]), "sh": sh, "sz": sz,
            "rows": sh["rows"] + sz["rows"],
            "written": sh["written"] + sz["written"]}


def collect_all(trade_date: str = None, lhb_lookback_days: int = 0,
                restriction_back_days: int = 7,
                restriction_forward_days: int = 120,
                margin_backfill_days: int = 0, log=print) -> dict:
    """日循环入口：一趟跑完事件类全部来源。

    `trade_date` 是**行情交易日**（不是墙钟日期），与资金流同口径。
    - 龙虎榜：截至 trade_date 的 `lhb_lookback_days+1` 天窗口（0 = 只取当天）；
      留出回看天数的用途是「某天没跑，第二天能把缺的补回来」——
      龙虎榜可回补，所以这里多花几秒换「不断档」是划算的。
    - 解禁：`[trade_date-back, trade_date+forward]`，前瞻部分是**过期不可得**的信息
      （日历会被用掉），所以要留足窗口；解禁明细本身可回补，冗余无害（幂等 upsert）。
    """
    from datetime import datetime, timedelta

    if not _base.sources_enabled():
        log(f"  事件类数据源未启用"
            f"（enabled={_base.switch_on()}，已安装={_base.akshare_installed()}），跳过")
        return {"ok": False, "skipped": "disabled"}

    def _shift(d, n):
        return (datetime.strptime(d, "%Y-%m-%d") + timedelta(days=n)).strftime("%Y-%m-%d")

    out = {"trade_date": trade_date}
    log("  事件类数据源：龙虎榜 / 两融 / 解禁 / 退市清单")
    if lhb_lookback_days:
        out["lhb"] = collect_lhb(_shift(trade_date, -int(lhb_lookback_days)),
                                 trade_date, log=log)
    else:
        out["lhb"] = collect_lhb(trade_date, log=log)
    out["margin"] = collect_margin(trade_date,
                                   backfill_days=int(margin_backfill_days or 0),
                                   log=log)
    out["restriction"] = collect_restriction(
        _shift(trade_date, -int(restriction_back_days)),
        _shift(trade_date, int(restriction_forward_days)), log=log)
    out["delisted"] = collect_delisted(log=log)
    out["ok"] = any(v.get("ok") for v in
                    (out["lhb"], out["margin"], out["restriction"], out["delisted"]))
    return out
