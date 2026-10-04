#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""新增数据源适配层（Phase 1）离线自检。

覆盖五类风险——每一类都对应一个「不测就会静默出错」的点：

1. **口径归一**：上游给的是 `'12.34亿'` 这种中文串，且股票代码被解析成了整数、
   前导零已丢（实测 5211 行里 6 位的只有 3720 个）。归一错一个量级，因子就整体失真；
   把「缺失」当成 0，缺失的股票会挤进一个虚假的名次。
2. **幂等与修订**：同一交易日重跑必须是覆盖而不是并存；不同日期、不同市场的
   数据必须互不干扰。
3. **失败分类**：`blocked`（出口被拒，确定性，重试白跑）与 `transient`
   （TLS 抖动，重试即过）判反了，要么浪费时间要么误杀源。
4. **熔断语义**：只认确定性失败跳闸、瞬时失败不跳闸、过 TTL 自动失效。
5. **降级不报错、留痕**：源挂了主流程必须照常走完，并且留下 `data_health` + 缺失记录。

自检必须用**独立库与独立缓存目录**，绝不能碰 `state/quant.db`，
也不能在真实 `state/cache/` 里留下熔断标记（那会真的掐掉生产里的源）。

用法：
  python tests/offline_datasources.py
  python tests/offline_datasources.py -v
"""
import argparse
import os
import subprocess
import sys
import tempfile

import pandas as pd

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, "src"))

_TMPDIR = tempfile.mkdtemp(prefix="quant_ds_test_")
os.environ["QUANT_DB"] = os.path.join(_TMPDIR, "test.db")

import requests  # noqa: E402
import fetcher  # noqa: E402
import store  # noqa: E402
import datasources  # noqa: E402
from datasources import akshare_source as aks  # noqa: E402
from datasources import akshare_events as ake  # noqa: E402
from datasources import sina_source as ss  # noqa: E402
from datasources import base as dsbase  # noqa: E402

# 熔断标记写的是 `base.CACHE_DIR`：指到临时目录，别污染生产缓存。
_REAL_CACHE = dsbase.CACHE_DIR
_TEST_CACHE = os.path.join(_TMPDIR, "cache")
os.makedirs(_TEST_CACHE, exist_ok=True)
dsbase.CACHE_DIR = _TEST_CACHE

VERBOSE = False
_fails = []


def check(name: str, cond: bool, detail: str = ""):
    if cond:
        if VERBOSE:
            print(f"  ✓ {name}")
    else:
        print(f"  ✗ {name}" + (f" —— {detail}" if detail else ""))
        _fails.append(name)


def _raw_frame(codes=("000001", "600000", "300750"), net="1.30亿"):
    return pd.DataFrame({
        "序号": list(range(1, len(codes) + 1)),
        "股票代码": [int(c) for c in codes],           # 上游口径：整数，前导零已丢
        "股票简称": [f"标的{c}" for c in codes],
        "最新价": ["11.50"] * len(codes),
        "涨跌幅": ["1.23%"] * len(codes),
        "换手率": ["0.90%"] * len(codes),
        "流入资金": ["2.50亿"] * len(codes),
        "流出资金": ["1.20亿"] * len(codes),
        "净额": [net] * len(codes),
        "成交额": ["9.90亿"] * len(codes),
    })


# ---------------------------------------------------------------------------
def test_parsers():
    print("· 解析：中文数量 / 百分比 / 代码")
    check("亿 → 元", abs(aks.parse_cn_amount("12.34亿") - 1.234e9) < 1e-3)
    check("万 → 元", abs(aks.parse_cn_amount("5.6万") - 5.6e4) < 1e-9)
    check("无单位", aks.parse_cn_amount("1234") == 1234.0)
    check("负数带单位", aks.parse_cn_amount("-1.5亿") == -1.5e8)
    check("千分位逗号", aks.parse_cn_amount("1,234.5万") == 1.2345e7)
    for bad in ("-", "", "  ", "--", None, "abc", "约1亿"):
        check(f"缺失/不可解析 {bad!r} → None", aks.parse_cn_amount(bad) is None)
    check("0 保留为 0（不是缺失）", aks.parse_cn_amount("0") == 0.0)

    check("百分比去 %", aks.parse_pct("653.16%") == 653.16)
    check("百分比缺失 → None", aks.parse_pct("-") is None)

    check("代码 1 → 000001", aks.normalize_code(1) == "000001")
    check("代码 '625' → 000625", aks.normalize_code("625") == "000625")
    check("6 位代码不变", aks.normalize_code("600000") == "600000")
    check("float 代码 301716.0", aks.normalize_code(301716.0) == "301716")
    check("超长代码 → None", aks.normalize_code(1234567) is None)
    check("非数字 → None", aks.normalize_code("abc") is None)


def test_normalize():
    print("· 归一：列映射 / 单位 / 前导零 / 缺失补齐")
    norm, anom = aks.normalize_individual(_raw_frame())
    check("行数不变", len(norm) == 3, str(len(norm)))
    check("列名规范", list(norm.columns) == aks.CANONICAL_COLS, str(list(norm.columns)))
    check("000001 前导零还原", norm["code"].iloc[0] == "000001", norm["code"].iloc[0])
    check("金额单位为元",
          abs(float(norm["net"].iloc[0]) - 1.3e8) < 1e-3, str(norm["net"].iloc[0]))
    check("百分比为数值", float(norm["pct"].iloc[0]) == 1.23)

    # 净额缺失但流入/流出都在 → 用减法补齐并计数（上游偶发只缺一个字段）
    raw = _raw_frame()
    raw.loc[1, "净额"] = "-"
    norm2, anom2 = aks.normalize_individual(raw)
    check("净额缺失由流入-流出补齐",
          abs(float(norm2["net"].iloc[1]) - 1.3e8) < 1e-3, str(norm2["net"].iloc[1]))
    check("补齐被计数", anom2["net_derived"] == 1, str(anom2))

    # 无法还原的代码不写库，但必须计数（静默丢弃=上游换口径时没人发现）
    raw3 = pd.concat([_raw_frame(("000001",)),
                      _raw_frame(("1234567",))], ignore_index=True)
    norm3, anom3 = aks.normalize_individual(raw3)
    check("坏代码被剔除", len(norm3) == 1 and anom3["bad_code"] == 1, str(anom3))

    # 重复代码只留一条（主键不容重复）
    raw4 = _raw_frame(("000001", "000001"))
    norm4, anom4 = aks.normalize_individual(raw4)
    check("重复代码去重", len(norm4) == 1 and anom4["dup_code"] == 1, str(anom4))

    # 缺失明确写成 None，不写 0
    raw5 = _raw_frame()
    raw5.loc[0, "流入资金"] = "-"
    norm5, _ = aks.normalize_individual(raw5)
    check("缺失落为 None 而非 0",
          pd.isna(norm5["inflow"].iloc[0]), str(norm5["inflow"].iloc[0]))


def test_normalize_schema_guard():
    print("· 归一：上游改列名必须显式报错")
    raw = _raw_frame().rename(columns={"净额": "主力净额"})
    try:
        aks.normalize_individual(raw)
        check("缺列抛 SchemaError", False, "没有抛异常——静默产出 None 字段")
    except dsbase.SchemaError as e:
        check("缺列抛 SchemaError", "净额" in str(e), str(e)[:120])
    except Exception as e:
        check("缺列抛 SchemaError", False, f"抛了 {type(e).__name__}: {e}")
    kind, retryable = dsbase.classify_error(dsbase.SchemaError("x"))
    check("SchemaError → schema/不可重试", (kind, retryable) == ("schema", False),
          f"{kind}/{retryable}")

    try:
        aks.normalize_individual([])
        check("空输入不抛", True)
    except Exception as e:
        check("空输入不抛", False, str(e))


# ---------------------------------------------------------------------------
def test_upsert_idempotent():
    print("· 落库：幂等 / 修订覆盖 / 口径隔离")
    store.clear_table("fund_flow")
    norm, _ = aks.normalize_individual(_raw_frame())
    n1 = store.upsert_fund_flow("cn", "2026-09-29", norm, src="unit")
    n2 = store.upsert_fund_flow("cn", "2026-09-29", norm, src="unit")
    n3 = store.upsert_fund_flow("cn", "2026-09-29", norm, src="unit")
    check("重复写入 3 次 → 3 行", n1 == 3 and n2 == 3 and n3 == 3, f"{n1}/{n2}/{n3}")
    rows = store._query("SELECT COUNT(*) n FROM fund_flow")[0]["n"]
    check("表内仍为 3 行", rows == 3, str(rows))

    # 同日重抓：上游对当日数据会盘中修订 → 覆盖而不是并存
    norm2 = norm.copy()
    norm2["net"] = 9.9e8
    store.upsert_fund_flow("cn", "2026-09-29", norm2, src="unit2")
    back = store.load_fund_flow("2026-09-29", "cn")
    check("同日重抓为覆盖（仍 3 行）", back is not None and len(back) == 3,
          str(None if back is None else len(back)))
    check("修订值生效", abs(float(back["net"].iloc[0]) - 9.9e8) < 1e-3,
          str(back["net"].iloc[0]))

    # 不同交易日 / 不同市场互不覆盖
    store.upsert_fund_flow("cn", "2026-09-28", norm, src="unit")
    store.upsert_fund_flow("hk", "2026-09-29", norm, src="unit")
    d28 = store.load_fund_flow("2026-09-28", "cn")
    cn = store.load_fund_flow("2026-09-29", "cn")
    hk = store.load_fund_flow("2026-09-29", "hk")
    check("不同日期并存",
          len(d28) == 3 and abs(float(d28["net"].iloc[0]) - 1.3e8) < 1e-3)
    check("不同市场隔离", len(cn) == 3 and len(hk) == 3)

    cov = store.fund_flow_coverage("cn")
    check("覆盖统计按日聚合", len(cov) == 2 and cov[0]["flow_date"] == "2026-09-28",
          str(cov))
    check("覆盖统计带净额合计", cov[-1]["net_yi"] is not None, str(cov[-1]))

    # 无数据返回 None（区别于「空表」）
    check("无数据返回 None", store.load_fund_flow("1999-01-01", "cn") is None)

    # 缺 code 列 → 不写脏数据
    check("缺 code 列不写库",
          store.upsert_fund_flow("cn", "2026-09-30", pd.DataFrame({"x": [1]})) == 0)


# ---------------------------------------------------------------------------
def test_failure_classification():
    print("· 失败分类：确定性失败不得重试")
    cases = [
        (requests.exceptions.ProxyError("Cannot connect to proxy"),
         "blocked", False),
        (requests.exceptions.ConnectionError(
            "('Connection aborted.', RemoteDisconnected('Remote end closed'))"),
         "blocked", False),
        (requests.exceptions.SSLError("bad handshake"), "transient", True),
        (requests.exceptions.ReadTimeout("read timed out"), "transient", True),
        (requests.exceptions.ConnectTimeout("connect timed out"), "transient", True),
        (dsbase.SchemaError("字段缺失"), "schema", False),
        (ValueError("Length mismatch: Expected axis has 0 elements"), "schema", False),
        (RuntimeError("something else"), "unknown", True),
    ]
    for exc, want_kind, want_retry in cases:
        kind, retryable = dsbase.classify_error(exc)
        check(f"{type(exc).__name__} → {want_kind}/{'可重试' if want_retry else '不重试'}",
              (kind, retryable) == (want_kind, want_retry),
              f"实际 {kind}/{retryable}")

    # SSLError 是 ConnectionError 的子类：判反了会把一次可成功的重试白白丢掉
    check("SSLError 不被误判为 ConnectionError",
          dsbase.classify_error(requests.exceptions.SSLError("x"))[0] == "transient")


def test_breaker_semantics():
    print("· 熔断：只认确定性失败，过 TTL 自动失效")
    b = dsbase.Breaker("unit_breaker", ttl_sec=0.5)
    b.reset()
    check("初始不熔断", not b.blocked())
    b.trip("blocked: reset by peer")
    check("跳闸后熔断", b.blocked())
    check("留下原因", "reset by peer" in b.reason(), b.reason())

    g = dsbase.SourceGuard("unit_guard", min_gap=0, budget_sec=60, retries=1)
    res = g.call("cn", lambda: (_ for _ in ()).throw(
        requests.exceptions.ReadTimeout("slow")))
    check("瞬时失败返回 ok=False", not res.ok and res.kind == "transient", repr(res))
    check("瞬时失败**不**跳闸（源还活着）", not g.breaker.blocked(),
          g.breaker.reason())

    import time
    time.sleep(0.6)
    check("过 TTL 自动清除熔断标记", not b.blocked())


def test_guard_short_circuit():
    print("· 护栏：熔断期内不发请求、有记录、留痕")
    g = dsbase.SourceGuard("unit_short", min_gap=0, budget_sec=60)
    g.breaker.reset()
    g.breaker.trip("blocked: push2his reset")
    dsbase.drain_missing()
    store.record_health("__sentinel__", "cn", True, 0.0, 0, "")
    called = {"n": 0}

    def _must_not_run():
        called["n"] += 1
        raise AssertionError("熔断期内仍然发起了请求")

    res = g.call("cn", _must_not_run)
    check("没有发起请求", called["n"] == 0)
    check("返回 skipped=breaker", res.skipped == "breaker" and not res.ok, repr(res))
    check("写了缺失留痕", len(dsbase.drain_missing()) == 1)
    hl = store._query("SELECT COUNT(*) n FROM data_health WHERE source=?",
                      ("unit_short",))[0]["n"]
    check("写了 data_health", hl >= 1, str(hl))

    # 预算熔断：累计耗时超预算后不再发起请求。
    # 注意预算判据是「累计耗时」——用一次真实耗时把预算顶破，而不是直接改状态字段
    # （直接改 spent 不会置 tripped，那样测的是测试自己而不是被测逻辑）。
    import time
    g2 = dsbase.SourceGuard("unit_budget", min_gap=0, budget_sec=0.001, retries=1,
                            log=lambda *_: None)
    g2.breaker.reset()
    g2.call("cn", lambda: time.sleep(0.02))
    check("超预算后 budget 进入熔断", g2.budget.guard())
    called2 = {"n": 0}
    res2 = g2.call("cn", lambda: called2.__setitem__("n", called2["n"] + 1))
    check("超预算后跳过且不发请求",
          res2.skipped == "budget" and called2["n"] == 0, repr(res2))


def test_direct_connection_env():
    print("· 直连：抓取期摘代理，退出后原样恢复")
    os.environ["HTTP_PROXY"] = "http://127.0.0.1:9"
    os.environ["https_proxy"] = "http://127.0.0.1:9"
    with dsbase.direct_connection():
        check("期内已摘掉 HTTP_PROXY", "HTTP_PROXY" not in os.environ)
        check("期内已摘掉 https_proxy", "https_proxy" not in os.environ)
    check("退出后 HTTP_PROXY 恢复",
          os.environ.get("HTTP_PROXY") == "http://127.0.0.1:9")
    check("退出后 https_proxy 恢复",
          os.environ.get("https_proxy") == "http://127.0.0.1:9")

    # 原本没有的变量不应被凭空造出来
    for k in ("HTTPS_PROXY", "ALL_PROXY", "all_proxy", "http_proxy"):
        os.environ.pop(k, None)
    with dsbase.direct_connection():
        pass
    check("未设置的变量不被凭空创建", "HTTPS_PROXY" not in os.environ)


# ---------------------------------------------------------------------------
def test_collect_end_to_end():
    print("· 端到端：抓取 → 归一 → 落库 → 画像（源用桩替换，不打网络）")
    store.clear_table("fund_flow")
    aks.configure(min_gap=0, retries=1)
    aks.guard().breaker.reset()
    dsbase.drain_missing()
    orig = aks.fetch_fund_flow
    aks.fetch_fund_flow = lambda symbol="即时": _raw_frame()
    try:
        res = aks.collect_fund_flow(flow_date="2026-09-29", log=lambda *_: None)
    finally:
        aks.fetch_fund_flow = orig

    check("ok=True", res["ok"], str(res))
    check("归一 3 行 / 写入 3 行", res["rows"] == 3 and res["written"] == 3, str(res))
    back = store.load_fund_flow("2026-09-29", "cn")
    check("库里读得回来", back is not None and len(back) == 3)
    check("src 标注来源", str(back["src"].iloc[0]) == aks.SOURCE, str(back["src"].iloc[0]))
    hl = store._query("""SELECT ok, n_items FROM data_health WHERE source=?
                         ORDER BY id DESC LIMIT 1""", (aks.SOURCE,))
    check("健康画像 ok=1 且带条目数",
          hl and hl[0]["ok"] == 1 and hl[0]["n_items"] == 3, str(hl))
    check("成功时不写缺失留痕", dsbase.drain_missing() == [])


def test_collect_degradation():
    print("· 降级：源挂了不抛异常、留痕、不写脏数据")
    store.clear_table("fund_flow")
    aks.configure(min_gap=0, retries=1)
    aks.guard().breaker.reset()
    dsbase.drain_missing()
    orig = aks.fetch_fund_flow

    def _boom(symbol="即时"):
        raise requests.exceptions.ConnectionError(
            "('Connection aborted.', RemoteDisconnected('Remote end closed'))")

    aks.fetch_fund_flow = _boom
    try:
        res = aks.collect_fund_flow(flow_date="2026-09-29", log=lambda *_: None)
    finally:
        aks.fetch_fund_flow = orig
        aks.guard().breaker.reset()

    check("返回 ok=False 而不抛", res["ok"] is False, str(res))
    check("归类为 blocked", res["kind"] == "blocked", str(res))
    check("未写入任何行", res["written"] == 0, str(res))
    check("表内仍为空",
          store._query("SELECT COUNT(*) n FROM fund_flow")[0]["n"] == 0)
    miss = dsbase.drain_missing()
    check("留下缺失记录", len(miss) == 1 and "blocked" in miss[0]["reason"], str(miss))
    hl = store._query("""SELECT ok FROM data_health WHERE source=?
                         ORDER BY id DESC LIMIT 1""", (aks.SOURCE,))
    check("健康画像 ok=0", hl and hl[0]["ok"] == 0, str(hl))

    # 归一失败（上游改列名）同样必须降级而不是崩
    aks.guard().breaker.reset()
    orig2 = aks.fetch_fund_flow
    aks.fetch_fund_flow = lambda symbol="即时": _raw_frame().rename(
        columns={"净额": "主力净额"})
    try:
        res2 = aks.collect_fund_flow(flow_date="2026-09-29", log=lambda *_: None)
    finally:
        aks.fetch_fund_flow = orig2
        aks.guard().breaker.reset()
    check("改列名 → schema 且不抛", res2["kind"] == "schema", str(res2))

    # 未安装 akshare：不能抛，也不能写脏数据
    orig_av = aks.available
    aks.available = lambda: False
    try:
        res3 = aks.collect_fund_flow(flow_date="2026-09-29", log=lambda *_: None)
    finally:
        aks.available = orig_av
        dsbase.drain_missing()
    check("akshare 未安装 → unavailable 不抛",
          res3["kind"] == "unavailable" and not res3["ok"], str(res3))


def test_same_day_reuse():
    print("· 同日复用：只认「收盘后抓到的」快照")
    store.clear_table("fund_flow")
    check("无数据不复用", not store.fund_flow_is_fresh("2026-09-29", "cn"))

    def _seed(date, fetched_at, market="cn", code="000001"):
        store._exec("""INSERT INTO fund_flow
            (market, code, flow_date, name, price, pct, turnover,
             inflow, outflow, net, amount, src, fetched_at)
            VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                    (market, code, date, "X", 1.0, 0.0, 0.0,
                     1e8, 0.5e8, 0.5e8, 2e8, "unit", fetched_at))

    _seed("2026-09-29", "2026-09-29 10:30:00")
    check("盘中快照不复用（定盘价还没出来）",
          not store.fund_flow_is_fresh("2026-09-29", "cn"))

    store.clear_table("fund_flow")
    _seed("2026-09-29", "2026-09-29 15:00:00")
    check("收盘整点即算定盘", store.fund_flow_is_fresh("2026-09-29", "cn"))

    store.clear_table("fund_flow")
    _seed("2026-09-29", "2026-09-29 23:28:20")
    check("收盘后抓的可复用", store.fund_flow_is_fresh("2026-09-29", "cn"))
    check("别的市场不受影响（hk 无数据）",
          not store.fund_flow_is_fresh("2026-09-29", "hk"))
    check("别的交易日本来就没有数据",
          not store.fund_flow_is_fresh("2026-09-28", "cn"))

    # 端到端：已经定盘时**不得再打上游**
    aks.configure(min_gap=0, retries=1)
    aks.guard().breaker.reset()
    dsbase.drain_missing()
    called = {"n": 0}

    def _must_not_run(symbol="即时"):
        called["n"] += 1
        raise AssertionError("已有定盘快照，却又打了一次上游")

    orig = aks.fetch_fund_flow
    aks.fetch_fund_flow = _must_not_run
    try:
        res = aks.collect_fund_flow(flow_date="2026-09-29", log=lambda *_: None)
    finally:
        aks.fetch_fund_flow = orig
    check("复用时不打上游", called["n"] == 0)
    check("复用返回 ok=True 且 reused=True",
          res["ok"] and res["reused"], str(res))

    # 盘中快照则必须重抓（否则那一刻的数会被当成当日最终值）
    store.clear_table("fund_flow")
    _seed("2026-09-29", "2026-09-29 10:30:00")
    called2 = {"n": 0}

    def _stub(symbol="即时"):
        called2["n"] += 1
        return _raw_frame()

    aks.fetch_fund_flow = _stub
    try:
        res2 = aks.collect_fund_flow(flow_date="2026-09-29", log=lambda *_: None)
    finally:
        aks.fetch_fund_flow = orig
    check("盘中快照会重抓", called2["n"] == 1 and not res2["reused"], str(res2))
    check("重抓后写入 3 行", res2["written"] == 3, str(res2))


# ---------------------------------------------------------------------------
# 事件类（Step 3）
# ---------------------------------------------------------------------------
def _lhb_raw():
    return pd.DataFrame([{
        "序号": 1, "代码": "000002", "名称": "万科A", "上榜日": "2026-09-29",
        "解读": "普通席位买入，成功率48.79%", "收盘价": 4.08, "涨跌幅": 9.973,
        "龙虎榜净买额": 159608251.75, "龙虎榜买入额": 466610675.4,
        "龙虎榜卖出额": 307002423.65, "龙虎榜成交额": 773613099.05,
        "市场总成交额": 3176619108, "净买额占总成交比": 5.024,
        "成交额占总成交比": 24.353, "换手率": 8.2563, "流通市值": 39637661668.32,
        "上榜原因": "日涨幅偏离值达到7%的前5只证券",
        "上榜后1日": 4.41176471, "上榜后2日": float("nan"),
        "上榜后5日": float("nan"), "上榜后10日": float("nan"),
    }, {
        "序号": 2, "代码": 11, "名称": "深物业A", "上榜日": "2026-09-29",
        "解读": "主力做T", "收盘价": 11.13, "涨跌幅": 9.98,
        "龙虎榜净买额": -103214298.66, "龙虎榜买入额": 141177943.0,
        "龙虎榜卖出额": 244392241.66, "龙虎榜成交额": 385570184.66,
        "市场总成交额": 653227762, "净买额占总成交比": -15.8,
        "成交额占总成交比": 59.025, "换手率": 8.0046, "流通市值": 6063164575.02,
        "上榜原因": "连续三个交易日内，涨幅偏离值累计达到20%的",
        "上榜后1日": 9.97, "上榜后2日": float("nan"),
        "上榜后5日": float("nan"), "上榜后10日": float("nan"),
    }])


def test_event_normalizers():
    print("· 事件类归一：日期/单位/代码，且**拒绝前视列**")
    check("日期 YYYYMMDD → YYYY-MM-DD", dsbase.norm_date("20260929") == "2026-09-29")
    check("日期 2026-09-29 不变", dsbase.norm_date("2026-09-29") == "2026-09-29")
    check("日期空值 → None", dsbase.norm_date("") is None)
    check("日期非法 → None", dsbase.norm_date("2026") is None)

    out, anom = ake.normalize_lhb(_lhb_raw())
    check("龙虎榜 2 行", len(out) == 2, str(len(out)))
    check("代码 11 → 000011", out["code"].iloc[1] == "000011", out["code"].iloc[1])
    check("日期归一", out["trade_date"].iloc[0] == "2026-09-29")

    # 前视列绝不能进库：它们会让下游按列名取特征时把「事件后收益」当因子
    forward = [c for c in out.columns if "上榜后" in c]
    check("不含「上榜后N日」前视列", not forward, str(forward))
    check("不含上游的流通市值（同厂商两接口单位不一致）",
          "market_cap" not in out.columns and "流通市值" not in out.columns)
    check("金额为元（不做任何缩放）",
          abs(float(out["net_buy"].iloc[0]) - 159608251.75) < 1e-6,
          str(out["net_buy"].iloc[0]))
    check("比例为百分数（不换算成小数）",
          abs(float(out["net_ratio"].iloc[0]) - 5.024) < 1e-9)

    # 缺必需列 → SchemaError（上游改列名必须显式炸）
    try:
        ake.normalize_lhb(_lhb_raw().rename(columns={"龙虎榜净买额": "主力净买额"}))
        check("缺必需列抛 SchemaError", False, "没有抛")
    except dsbase.SchemaError:
        check("缺必需列抛 SchemaError", True)
    except Exception as e:
        check("缺必需列抛 SchemaError", False, f"{type(e).__name__}: {e}")

    # 同股同日多原因 → 主键含 reason，不能被去重吃掉
    same = pd.concat([_lhb_raw(), _lhb_raw()], ignore_index=True)
    same.loc[2, "上榜原因"] = "日换手率达到20%的前5只证券"
    same.loc[2, "代码"] = "000002"
    same.loc[2, "上榜日"] = "2026-09-29"
    out2, _ = ake.normalize_lhb(same)
    check("同股同日两个原因各留一行", len(out2) == 3, str(len(out2)))


def _margin_sse_raw():
    return pd.DataFrame([{
        "信用交易日期": "20260929", "标的证券代码": "510050", "标的证券简称": "50ETF",
        "融资余额": 1508474717, "融资买入额": 84018014, "融资偿还额": 88311870,
        "融券余量": 34433640, "融券卖出量": 346300, "融券偿还量": 714900,
    }])


def _margin_szse_raw():
    return pd.DataFrame([{
        "证券代码": "000001", "证券简称": "平安银行", "融资买入额": 44067287,
        "融资余额": 4531894920, "融券卖出量": 18600, "融券余量": 15693237,
        "融券余额": 178118240, "融资融券余额": 4710013160,
    }])


def test_margin_semantics():
    print("· 两融：日期以**上游返回的**为准；深市无日期列必须由调用方给")
    sse, anom = ake.normalize_margin(_margin_sse_raw(), "sse", "2026-09-29")
    check("沪市用返回的信用交易日期", sse["trade_date"].iloc[0] == "2026-09-29")
    check("沪市带 exchange", sse["exchange"].iloc[0] == "sse")
    check("沪市无融券余额 → NULL（不是 0）",
          "short_balance" not in sse.columns or pd.isna(sse["short_balance"].iloc[0]))

    # 请求 T 但上游给 T-1：必须以返回的日期为准，否则库里会多出一天没有行情的数据
    old = _margin_sse_raw()
    old.loc[0, "信用交易日期"] = "20260928"
    s2, a2 = ake.normalize_margin(old, "sse", "2026-09-29")
    check("日期不一致时以上游为准", s2["trade_date"].iloc[0] == "2026-09-28",
          s2["trade_date"].iloc[0])
    check("日期不一致被标记", a2.get("date_mismatch") is True, str(a2))

    szse, _ = ake.normalize_margin(_margin_szse_raw(), "szse", "2026-09-29")
    check("深市用调用方给的日期", szse["trade_date"].iloc[0] == "2026-09-29")
    check("深市融资偿还额 → NULL（这家不披露）",
          "fin_repay" not in szse.columns or pd.isna(szse["fin_repay"].iloc[0]))
    try:
        ake.normalize_margin(_margin_szse_raw(), "szse", None)
        check("深市缺 date 抛 SchemaError", False, "没有抛")
    except dsbase.SchemaError:
        check("深市缺 date 抛 SchemaError", True)


def test_margin_no_data_on_old_date():
    print("· 两融：旧日期 = 无数据，不是源故障")
    def _raise_empty(**kw):
        raise ValueError("Length mismatch: Expected axis has 0 elements, "
                         "new values have 13 elements")
    out = ake._no_data_on_old_date(_raise_empty, date="20260925")
    check("空解析错 → 空表（不当故障）", isinstance(out, pd.DataFrame) and out.empty)

    def _raise_schema(**kw):
        raise KeyError("标的证券代码")
    try:
        ake._no_data_on_old_date(_raise_schema, date="20260925")
        check("其他异常照常抛出", False, "被吞掉了")
    except KeyError:
        check("其他异常照常抛出", True)


def test_margin_calendar_and_backfill():
    print("· 两融：T+1 公布 → 先解析「最近已公布日」；只补库里缺的日期")
    for t in ("margin",):
        store.clear_table(t)
    fake = _FakeAk()
    orig = ake._ak
    ake._ak = lambda: fake
    for name in ake._GUARD_DEFAULTS:
        ake.guard(name).configure(min_gap=0)
        ake.guard(name).breaker.reset()
    try:
        # 09-30 尚未公布 → 目标日应回落到最近的已公布日 09-29
        got = ake.resolve_margin_date("2026-09-30", log=lambda *_: None)
        check("未公布时回落到最近已公布日", got == "2026-09-29", str(got))
        check("已公布日原样返回",
              ake.resolve_margin_date("2026-09-28", log=lambda *_: None)
              == "2026-09-28")
        recent = ake.recent_margin_dates("2026-09-30", 3)
        check("最近 N 个已公布日（新→旧）",
              recent == ["2026-09-29", "2026-09-28", "2026-09-24"], str(recent))

        fake.calls.clear()
        r = ake.collect_margin("2026-09-30", backfill_days=0,
                               log=lambda *_: None)
        check("两融抓的是已公布日 09-29",
              r["trade_date"] == "2026-09-29", str(r.get("trade_date")))
        dates_asked = [c[1]["date"] for c in fake.calls
                       if c[0].startswith("stock_margin_detail_")]
        check("明细请求用的是 8 位紧凑日期",
              dates_asked and all(len(d) == 8 for d in dates_asked),
              str(dates_asked))
        check("沪/深各请求一次", dates_asked == ["20260929", "20260929"],
              str(dates_asked))
        check("入库 2 行（沪 1 + 深 1）", r["written"] == 2, str(r))
        check("margin_dates 反映已入库日期",
              store.margin_dates("cn") == {"2026-09-29"},
              str(store.margin_dates("cn")))

        # 补漏：库里已有 09-29 → 只补 09-28 与 09-24
        fake.calls.clear()
        r2 = ake.collect_margin("2026-09-30", backfill_days=3,
                                log=lambda *_: None)
        days_fetched = sorted({c[1]["date"] for c in fake.calls
                               if c[0].startswith("stock_margin_detail_")})
        check("补漏只抓库里缺的日期（09-29 不重抓）",
              days_fetched == ["20260924", "20260928"], str(days_fetched))
        check("补漏后 3 个交易日入库",
              store.margin_dates("cn") == {"2026-09-24", "2026-09-28",
                                           "2026-09-29"},
              str(store.margin_dates("cn")))

        # 全部已在库 → 不重抓（少两次请求、少一次被限流的风险）
        fake.calls.clear()
        r2b = ake.collect_margin("2026-09-30", backfill_days=3,
                                 log=lambda *_: None)
        check("全部已在库时跳过抓取",
              r2b.get("skipped") == "present" and r2b["written"] == 0
              and not [c for c in fake.calls
                       if c[0].startswith("stock_margin_detail_")], str(r2b)[:140])

        # 半齐不算齐：沪市成功、深市失败的那天必须重抓（否则深市缺口永不补齐）
        store.clear_table("margin")
        store.upsert_margin(ake.normalize_margin(
            _margin_sse_raw(), "sse", "2026-09-29")[0], src="unit")
        check("只齐了一个交易所 → 不算齐",
              store.margin_dates("cn", ["sse", "szse"]) == set(),
              str(store.margin_dates("cn", ["sse", "szse"])))
        check("按交易所单独看：沪市已齐",
              store.margin_dates("cn", ["sse"]) == {"2026-09-29"},
              str(store.margin_dates("cn", ["sse"])))
        fake.calls.clear()
        ake.collect_margin("2026-09-30", backfill_days=0, log=lambda *_: None)
        refetched = [c[1]["date"] for c in fake.calls
                     if c[0].startswith("stock_margin_detail_")]
        check("半齐的日期会被重抓", "20260929" in refetched, str(refetched))

        # 汇总接口挂掉 → 退回直接请求目标日（不抛）
        class _NoCal(_FakeAk):
            def stock_margin_sse(self, start_date=None, end_date=None):
                raise requests.exceptions.ConnectionError("boom")
        ake._ak = lambda: _NoCal()
        check("汇总不可用时退回目标日（不抛）",
              ake.resolve_margin_date("2026-09-30", log=lambda *_: None) is None)
        r3 = ake.collect_margin("2026-09-30", log=lambda *_: None)
        check("退回后仍能收尾返回", isinstance(r3, dict) and "ok" in r3, str(r3)[:120])
    finally:
        ake._ak = orig
        for name in ake._GUARD_DEFAULTS:
            ake.guard(name).configure(min_gap=0)
            ake.guard(name).breaker.reset()
        store.clear_table("margin")


def test_event_tables():
    print("· 事件表落库：幂等 / 主键口径 / 覆盖统计")
    for t in ("lhb", "lhb_inst", "margin", "restriction", "delisted"):
        store.clear_table(t)

    norm, _ = ake.normalize_lhb(_lhb_raw())
    n1 = store.upsert_lhb(norm, src="unit")
    n2 = store.upsert_lhb(norm, src="unit")
    check("龙虎榜重复写 2 次仍 2 行",
          n1 == 2 and n2 == 2
          and store._query("SELECT COUNT(*) n FROM lhb")[0]["n"] == 2)

    # 修订覆盖：同一 (日期,代码,原因) 重抓 → 覆盖
    rev = norm.copy()
    rev["net_buy"] = 1.0
    store.upsert_lhb(rev, src="unit2")
    back = store.load_events("lhb", date_from="2026-09-29", date_to="2026-09-29")
    check("同日重抓为覆盖", back is not None and len(back) == 2
          and abs(float(back["net_buy"].iloc[0]) - 1.0) < 1e-9)

    m, _ = ake.normalize_margin(_margin_szse_raw(), "szse", "2026-09-29")
    store.upsert_margin(m, src="unit")
    store.upsert_margin(m, src="unit")
    check("两融幂等 1 行",
          store._query("SELECT COUNT(*) n FROM margin")[0]["n"] == 1)

    r = pd.DataFrame([{
        "code": "002276", "release_date": "2026-09-29",
        "share_type": "股权激励限售股份", "market": "cn", "name": "万马股份",
        "release_shares": 235500.0, "actual_shares": 235500.0,
        "actual_value": 2293770.0, "ratio_of_float": 0.00023373,
        "prev_close": 9.74,
    }])
    store.upsert_restriction(r, src="unit")
    store.upsert_restriction(r, src="unit")
    check("解禁幂等 1 行",
          store._query("SELECT COUNT(*) n FROM restriction")[0]["n"] == 1)

    d, _ = ake.normalize_delisted(pd.DataFrame([{
        "公司代码": "600001", "公司简称": "邯郸钢铁",
        "上市日期": "1998-01-22", "暂停上市日期": "2009-12-29"}]), "sh")
    store.upsert_delisted(d, src="unit")
    check("退市清单 1 行，日期归一",
          store._query("SELECT delist_date FROM delisted")[0]["delist_date"]
          == "2009-12-29")
    check("退市清单无 code 行被丢弃",
          store.upsert_delisted(pd.DataFrame([{
              "公司代码": "abc", "公司简称": "X",
              "上市日期": "1998-01-22", "暂停上市日期": "2009-12-29"}]), "sh") == 0)

    # 覆盖统计：整体 + 按日
    cov = store.event_coverage("lhb")
    check("覆盖统计含日期跨度",
          cov and cov[0]["first_date"] == "2026-09-29"
          and cov[0]["last_date"] == "2026-09-29", str(cov))
    bydate = store.event_coverage("lhb", by_date=True, limit=5)
    check("按日覆盖统计", bydate and bydate[0]["d"] == "2026-09-29"
          and bydate[0]["syms"] == 2, str(bydate))
    check("非法表名返回空（白名单）", store.event_coverage("kline; DROP") == [])
    check("非法表名读取返回 None", store.load_events("kline") is None)

    # upsert 返回实际写入行数：库关掉时必须是 0（不能伪装成功）
    check("空表 upsert 返回 0", store.upsert_lhb(pd.DataFrame()) == 0)


def test_event_collect_end_to_end():
    print("· 事件类端到端：抓取 → 归一 → 落库（源用桩替换，不打网络）")
    for t in ("lhb", "lhb_inst", "margin", "restriction", "delisted"):
        store.clear_table(t)
    for name in ake._GUARD_DEFAULTS:
        ake.guard(name).configure(min_gap=0)
        ake.guard(name).breaker.reset()
    dsbase.drain_missing()

    orig = ake._ak
    fake = _FakeAk()
    ake._ak = lambda: fake
    try:
        rb = ake.collect_all("2026-09-29", lhb_lookback_days=0,
                             restriction_back_days=0, restriction_forward_days=0,
                             log=lambda *_: None)
    finally:
        ake._ak = orig
        for name in ake._GUARD_DEFAULTS:
            ake.guard(name).breaker.reset()

    check("collect_all 整体 ok", rb.get("ok") is True, str(rb)[:200])
    check("龙虎榜入库 2 行",
          store._query("SELECT COUNT(*) n FROM lhb")[0]["n"] == 2)
    check("龙虎榜机构统计入库 1 行",
          store._query("SELECT COUNT(*) n FROM lhb_inst")[0]["n"] == 1)
    check("两融入库 2 行（沪 1 + 深 1）",
          store._query("SELECT COUNT(*) n FROM margin")[0]["n"] == 2)
    check("解禁入库 1 行",
          store._query("SELECT COUNT(*) n FROM restriction")[0]["n"] == 1)
    check("退市清单入库 2 行（沪 1 + 深 1）",
          store._query("SELECT COUNT(*) n FROM delisted")[0]["n"] == 2)

    # 画像按源分开：五个源各有一条成功记录
    srcs = {r["source"] for r in store._query(
        "SELECT DISTINCT source FROM data_health WHERE ok=1 AND source LIKE 'akshare_%'")}
    for s in (ake.SOURCE_LHB, ake.SOURCE_LHB_INST, ake.SOURCE_MARGIN_SSE,
              ake.SOURCE_MARGIN_SZSE, ake.SOURCE_RESTRICTION,
              ake.SOURCE_DELISTED_SH, ake.SOURCE_DELISTED_SZ):
        check(f"画像含 {s}", s in srcs, str(sorted(srcs)))

    # 单个源挂掉不牵连其他源（各自独立护栏）
    class _BoomAk(_FakeAk):
        def stock_lhb_detail_em(self, **kw):
            raise requests.exceptions.ConnectionError("('Connection aborted.',)")

    ake._ak = lambda: _BoomAk()
    try:
        rb2 = ake.collect_all("2026-09-30", lhb_lookback_days=0,
                              restriction_back_days=0, restriction_forward_days=0,
                              log=lambda *_: None)
    finally:
        ake._ak = orig
        for name in ake._GUARD_DEFAULTS:
            ake.guard(name).breaker.reset()
    check("龙虎榜挂掉时整体仍 ok（两融/解禁/退市照跑）",
          rb2.get("ok") is True, str(rb2.get("lhb"))[:120])
    check("龙虎榜挂掉被记为 blocked",
          rb2["lhb"]["detail"]["kind"] == "blocked", str(rb2["lhb"]["detail"]))


class _FakeAk:
    """akshare 的桩：字段名刻意与真实接口一字不差，改列名时自检会先炸。

    同时记录每次调用的实参——「日期格式」这类**只在调用边界**才存在的约束，
    不记录参数就测不出来（实测踩过：传 YYYY-MM-DD 导致上游回错误信封）。
    """

    def __init__(self):
        self.calls = []

    def _rec(self, name, kw):
        self.calls.append((name, dict(kw)))
        return None

    def stock_lhb_detail_em(self, start_date=None, end_date=None):
        self._rec("stock_lhb_detail_em",
                  {"start_date": start_date, "end_date": end_date})
        return _lhb_raw()

    def stock_lhb_jgmmtj_em(self, start_date=None, end_date=None):
        self._rec("stock_lhb_jgmmtj_em",
                  {"start_date": start_date, "end_date": end_date})
        return pd.DataFrame([{
            "序号": 1, "代码": "000592", "名称": "平潭发展", "收盘价": 9.09,
            "涨跌幅": 3.53, "买方机构数": 4, "卖方机构数": 0,
            "机构买入总额": 308711117.03, "机构卖出总额": 49349019.98,
            "机构买入净额": 259362097.05, "市场总成交额": 5083871839,
            "机构净买额占总成交额比": 5.10, "换手率": 29.09,
            "流通市值": 174.08, "上榜原因": "日换手率达到20%的前5只证券",
            "上榜日期": "2026-09-29"}])

    def stock_margin_sse(self, start_date=None, end_date=None):
        self._rec("stock_margin_sse",
                  {"start_date": start_date, "end_date": end_date})
        # 已公布交易日：09-24 / 09-28 / 09-29（09-30 尚未公布 → 模拟 T+1）
        return pd.DataFrame({
            "信用交易日期": ["20260924", "20260928", "20260929"],
            "融资余额": [1.3e12, 1.31e12, 1.31e12]})

    def stock_margin_detail_sse(self, date=None):
        self._rec("stock_margin_detail_sse", {"date": date})
        # 真实接口返回的「信用交易日期」就是你请求的那一天，所以桩也必须**回显**
        # 请求日期。此前这里固定返回 09-29，掩盖了「按交易所判断齐备」的逻辑
        # （所有沪市行都被记到同一天，另外几天只有深市数据也不会被发现）。
        raw = _margin_sse_raw()
        if date:
            raw.loc[0, "信用交易日期"] = str(date)
        return raw

    def stock_margin_detail_szse(self, date=None):
        self._rec("stock_margin_detail_szse", {"date": date})
        return _margin_szse_raw()

    def stock_margin_detail_bse(self, date=None):
        self._rec("stock_margin_detail_bse", {"date": date})
        return _margin_szse_raw()

    def stock_restricted_release_detail_em(self, start_date=None, end_date=None):
        self._rec("stock_restricted_release_detail_em",
                  {"start_date": start_date, "end_date": end_date})
        return pd.DataFrame([{
            "序号": 1, "股票代码": "002276", "股票简称": "万马股份",
            "解禁时间": "2026-09-29", "限售股类型": "股权激励限售股份",
            "解禁数量": 235500.0, "实际解禁数量": 235500.0,
            "实际解禁市值": 2293770.0, "占解禁前流通市值比例": 0.00023373,
            "解禁前一交易日收盘价": 9.74,
            "解禁前20日涨跌幅": -5.99, "解禁后20日涨跌幅": 1.34}])

    def stock_info_sh_delist(self):
        return pd.DataFrame([{
            "公司代码": "600001", "公司简称": "邯郸钢铁",
            "上市日期": "1998-01-22", "暂停上市日期": "2009-12-29"}])

    def stock_info_sz_delist(self, symbol=None):
        return pd.DataFrame([{
            "证券代码": "000004", "证券简称": "国华退",
            "上市日期": "1990-12-01", "终止上市日期": "2026-07-14"}])


def test_event_date_and_nodata():
    print("· 事件类：日期必须以 YYYYMMDD 传给上游；上游回空 ≠ 源故障")
    check("compact_date 去横杠", dsbase.compact_date("2026-09-30") == "20260930")
    check("compact_date 幂等", dsbase.compact_date("20260930") == "20260930")
    check("compact_date 空值 → None", dsbase.compact_date("") is None)

    # 传错格式时上游回的是错误信封，akshare 对 None 取下标 —— 必须归为 nodata
    e = TypeError("'NoneType' object is not subscriptable")
    check("NoneType 下标错 → nodata", dsbase.classify_error(e) == ("nodata", True),
          str(dsbase.classify_error(e)))
    g = dsbase.SourceGuard("unit_nodata", min_gap=0, budget_sec=60, retries=1,
                            log=lambda *_: None)
    g.breaker.reset()
    res = g.call("cn", lambda: (_ for _ in ()).throw(e))
    check("nodata 失败后**不**跳闸（否则会把健康的源锁 30 分钟）",
          not g.breaker.blocked(), repr(res))
    check("nodata 归类正确", res.kind == "nodata", repr(res))
    g.breaker.reset()

    # 0 列的空壳 DataFrame = 上游没给数据，不是改版
    out, anom = ake.normalize_lhb(pd.DataFrame())
    check("0 列空壳 → nodata 而非 SchemaError", anom.get("nodata") is True, str(anom))
    check("0 列空壳归一结果为空表", out.empty)
    try:
        ake.normalize_lhb(pd.DataFrame(columns=["代码", "名称"]))   # 有列但缺关键列 = 真改版
        check("有列但缺关键列仍抛 SchemaError", False, "没有抛")
    except dsbase.SchemaError:
        check("有列但缺关键列仍抛 SchemaError", True)

    # 端到端：断言真的把 YYYYMMDD 传给了 akshare
    for t in ("lhb", "lhb_inst", "margin", "restriction", "delisted"):
        store.clear_table(t)
    for name in ake._GUARD_DEFAULTS:
        ake.guard(name).configure(min_gap=0)
        ake.guard(name).breaker.reset()
    dsbase.drain_missing()
    fake = _FakeAk()
    orig = ake._ak
    ake._ak = lambda: fake
    try:
        ake.collect_all("2026-09-30", lhb_lookback_days=3,
                        restriction_back_days=7, restriction_forward_days=120,
                        log=lambda *_: None)
    finally:
        ake._ak = orig
        for name in ake._GUARD_DEFAULTS:
            ake.guard(name).breaker.reset()

    calls = fake.calls
    lhb_args = [c for c in calls if c[0] == "stock_lhb_detail_em"]
    check("龙虎榜被调用", bool(lhb_args), str(calls[:3]))
    if lhb_args:
        kw = lhb_args[0][1]
        check("龙虎榜日期为 8 位紧凑格式（不是 2026-09-23）",
              kw["start_date"] == "20260927" and kw["end_date"] == "20260930",
              str(kw))
    m_args = [c for c in calls if c[0] == "stock_margin_detail_sse"]
    # 桩里 09-30 尚未公布 → 目标日应回落到已公布日 09-29（T+1 公布）
    check("两融请求的是已公布日、且为 8 位紧凑格式",
          m_args and m_args[0][1]["date"] == "20260929", str(m_args[:1]))
    r_args = [c for c in calls if c[0] == "stock_restricted_release_detail_em"]
    check("解禁窗口为 8 位紧凑格式",
          r_args and len(r_args[0][1]["start_date"]) == 8
          and len(r_args[0][1]["end_date"]) == 8, str(r_args[:1]))


def test_progress_bar_suppression():
    print("· 进度条压制：必须在 tqdm 被 import **之前**设好")
    code = (
        "import sys;"
        f"sys.path.insert(0,{os.path.join(ROOT, 'src')!r});"
        "import datasources;"          # 只导入我们的适配层，不碰 akshare
        "import os, json;"
        "print('SET', os.environ.get('TQDM_DISABLE'));"
        "print('TQDM_LOADED', 'tqdm' in sys.modules)"
    )
    out = subprocess.run([sys.executable, "-c", code], capture_output=True,
                         text=True, timeout=60).stdout
    check("导入 datasources 即设好 TQDM_DISABLE", "SET 1" in out, out)
    check("此时 tqdm 尚未被加载（否则设了也没用）",
          "TQDM_LOADED False" in out, out)


# ---------------------------------------------------------------------------
# 现货降级链（Step 4）
# ---------------------------------------------------------------------------
def _sina_etf_raw():
    return pd.DataFrame([
        {"代码": "sz159998", "名称": "计算机ETF天弘", "最新价": 0.901,
         "涨跌额": -0.009, "涨跌幅": -0.989, "昨收": 0.910,
         "成交量": 42030300, "成交额": 37950580},
        {"代码": "sh510050", "名称": "50ETF", "最新价": 0.0,
         "涨跌额": 0.0, "涨跌幅": 0.0, "昨收": 3.0,
         "成交量": 0, "成交额": 0},
        {"代码": "sz159997", "名称": "电子ETF天弘", "最新价": 1.943,
         "涨跌额": -0.049, "涨跌幅": -2.460, "昨收": 1.992,
         "成交量": 28811200, "成交额": 56719948},
    ])


def test_sina_normalize():
    print("· 新浪归一：剥交易所前缀 / 港股码不补零 / 停牌零价剔除")
    out, anom = ss.normalize_spot(_sina_etf_raw(), "etf")
    check("ETF 剥掉 sh/sz 前缀",
          list(out["code"]) == ["159998", "159997"], str(list(out["code"])))
    check("零价（停牌）行被剔除", anom["zero_price"] == 1, str(anom))
    check("涨跌幅按百分数保留",
          abs(float(out["pct"].iloc[0]) - (-0.989)) < 1e-9)
    check("成交额为元、不做缩放",
          abs(float(out["amount"].iloc[0]) - 37950580) < 1e-6)

    hk = pd.DataFrame([{"代码": "00001", "中文名称": "长和", "英文名称": "CKH",
                        "最新价": 68.65, "涨跌幅": 0.58608, "成交额": 519967585}])
    hk_out, _ = ss.normalize_spot(hk, "hk")
    check("港股 5 位代码保持不变（补零会变成另一个代码）",
          hk_out["code"].iloc[0] == "00001", hk_out["code"].iloc[0])
    check("港股名称取中文名", hk_out["name"].iloc[0] == "长和")

    # 无列空壳 = 本次没数据；有列但缺关键列 = 上游改版
    _, a1 = ss.normalize_spot(pd.DataFrame(), "etf")
    check("0 列空壳 → nodata", a1.get("nodata") is True, str(a1))
    try:
        ss.normalize_spot(pd.DataFrame(columns=["代码", "名称"]), "etf")
        check("有列但缺关键列抛 SchemaError", False, "没有抛")
    except dsbase.SchemaError:
        check("有列但缺关键列抛 SchemaError", True)

    # 不支持的市场的**不写画像**：写了会留下一条永远失败的假记录
    store._exec("DELETE FROM data_health WHERE source=?", (ss.SOURCE,))
    ss.spot_list("us", log=lambda *_: None)
    n = store._query("SELECT COUNT(*) n FROM data_health WHERE source=?",
                     (ss.SOURCE,))[0]["n"]
    check("不支持的市场不产生健康记录（避免污染画像）", n == 0, str(n))


def test_rank_snapshot_src_persisted():
    print("· 榜单快照的 src 必须真的落库（形参收下≠写进去）")
    store.clear_table("rank_snapshot")
    df = pd.DataFrame({"code": ["159998"], "name": ["x"], "price": [1.0],
                       "pct": [-1.0], "amount": [2e8]})
    n = store.upsert_rank_snapshot("etf", "amount", "2026-09-30", df,
                                   src="sina_spot_list")
    check("写入 1 行", n == 1, str(n))
    got = store._query("SELECT DISTINCT src FROM rank_snapshot")
    check("src 可查回（降级链的长期偏移要能被发现）",
          [r["src"] for r in got] == ["sina_spot_list"], str(got))
    cols = [r["name"] for r in store._query("PRAGMA table_info(rank_snapshot)")]
    check("rank_snapshot 有 src 列（老库靠迁移补上）", "src" in cols, str(cols))


def test_spot_tier_chain():
    print("· 降级链：档位顺序可控，且「前面拿到就不再打后面」")
    check("etf 档位含新浪", "sina" in fetcher._spot_tier_names("etf"),
          str(fetcher._spot_tier_names("etf")))
    check("us 档位**不含**新浪（无可用源，不摆一条永远失败的档）",
          "sina" not in fetcher._spot_tier_names("us"),
          str(fetcher._spot_tier_names("us")))

    # 档位顺序覆盖：非法档名必须被丢掉，而不是静默少一档
    old = fetcher._SPOT_TIERS["etf"]
    try:
        fetcher.configure_spot_tiers({"etf": ["eastmoney", "tpyo", "sina"]})
        check("非法档名被过滤",
              fetcher._spot_tier_names("etf") == ("eastmoney", "sina"),
              str(fetcher._spot_tier_names("etf")))
        fetcher.configure_spot_tiers({"etf": []})
        check("空序列不生效（不改坏配置）",
              fetcher._spot_tier_names("etf") == ("eastmoney", "sina"))
        fetcher.configure_spot_tiers({"不存在": ["sina"]})
        check("未知市场被忽略", "不存在" not in fetcher._SPOT_TIERS)
    finally:
        fetcher._SPOT_TIERS["etf"] = old

    # 链行为：用桩替换单档，观察调用序与最终来源
    calls = []
    frames = {"eastmoney": pd.DataFrame(), "tencent": pd.DataFrame(),
              "sina": _sina_etf_raw()[["代码"]].rename(
                  columns={"代码": "code"}).assign(
                  name="x", price=1.0, pct=-1.0, amount=2e8)}

    def _stub(market, tier, pages=2, record=True):
        calls.append(tier)
        return frames[tier]

    real = fetcher._spot_tier
    fetcher._spot_tier = _stub
    try:
        store.clear_table("rank_snapshot")
        df = fetcher.fetch_spot_list("etf")
        check("前两档为空时打到第三档",
              calls == ["eastmoney", "tencent", "sina"], str(calls))
        check("返回的是新浪那一档的数据", len(df) == 3, str(len(df)))
        rows = store._query("SELECT DISTINCT src FROM rank_snapshot")
        check("落库标注真实供数方（不是笼统的 fallback）",
              [r["src"] for r in rows] == ["sina_spot_list"], str(rows))

        calls.clear()
        frames["eastmoney"] = _sina_etf_raw()[["代码"]].rename(
            columns={"代码": "code"}).assign(name="x", price=1.0, pct=-1.0,
                                             amount=2e8)
        fetcher.fetch_spot_list("etf")
        check("第一档拿到就不再往下打（省时省请求）",
              calls == ["eastmoney"], str(calls))

        # 全档皆空：返回空表、不抛异常
        calls.clear()
        frames["eastmoney"] = pd.DataFrame()
        frames["sina"] = pd.DataFrame()
        df3 = fetcher.fetch_spot_list("hk")
        check("全档皆空时返回空表且不抛", df3.empty, str(len(df3)))
    finally:
        fetcher._spot_tier = real
        fetcher._SPOT_TIERS["etf"] = old


def test_no_import_cycle():
    print("· 导入顺序：fetcher 与 datasources 互不依赖成环")
    for first in ("fetcher", "datasources"):
        code = (
            "import sys;"
            f"sys.path.insert(0,{os.path.join(ROOT, 'src')!r});"
            f"import {first};"
            "import fetcher, datasources;"
            "from datasources import sina_source;"
            "print('OK', callable(fetcher.fetch_spot_list))"
        )
        r = subprocess.run([sys.executable, "-c", code], capture_output=True,
                           text=True, timeout=90)
        check(f"先 import {first} 也能起来",
              "OK True" in r.stdout, (r.stdout + r.stderr)[-160:])


def test_audit_fixes():
    print("· 审计修复：字段缺失率落库 / 源名唯一 / 探针无副作用 / 配置全接通")
    # ① 归一异常（「字段缺失率」的落地形式）要能写进画像并可查回
    store._exec("DELETE FROM data_health")
    store.record_health("unit_anom", "cn", True, 1.0, 10, "ok")
    n = store.attach_health_anomalies("unit_anom", "cn",
                                      {"bad_amount": {"net": 12}, "bad_code": 3})
    check("异常附加成功", n == 1, str(n))
    rows = store.recent_anomalies(3)
    check("异常可查回且是逐字段计数",
          rows and "bad_amount" in rows[0]["anomalies"]
          and "net" in rows[0]["anomalies"], str(rows[:1]))
    # 关键：附加**不新增行**——多写一行会把成功率的分母翻倍
    total = store._query("SELECT COUNT(*) n FROM data_health WHERE source=?",
                         ("unit_anom",))[0]["n"]
    check("附加不新增画像行（否则成功率分母翻倍）", total == 1, str(total))
    check("空异常不写", store.attach_health_anomalies("unit_anom", "cn", {}) == 0)

    # ② 同一个档只能有一个源名：rank_snapshot.src 与 data_health 源名必须一致
    health_names = {"eastmoney_clist", "tencent_quote_list", "sina_spot_list"}
    check("档位源名与画像源名一一对应",
          set(fetcher._TIER_SRC.values()) == health_names,
          str(fetcher._TIER_SRC))

    # ③ 探针不得改动生产状态：熔断标记必须原样留着。
    # 这里只把**最内层的网络调用**换成桩，`_spot_tier` 的真实逻辑（含熔断判定与
    # 标记写入）照跑——否则这个测试既会真的打网络（自检不该依赖网络），
    # 又可能因为把逻辑一起桩掉而变成空测试。
    marker = os.path.join(fetcher.CACHE_DIR, "em_blocked")
    existed = os.path.exists(marker)
    with open(marker, "w") as f:
        f.write("unit-test-marker")
    saved = (fetcher._fetch_spot_list_em, fetcher._fetch_spot_list_tencent,
             ss.spot_list)
    fetcher._fetch_spot_list_em = lambda m, p=2, use_cache=True: pd.DataFrame()
    fetcher._fetch_spot_list_tencent = lambda m: pd.DataFrame()
    ss.spot_list = lambda m, log=print: pd.DataFrame()
    try:
        fetcher.spot_probe(pages=1)
        check("探针跑完熔断标记仍在（不删生产状态）", os.path.exists(marker))
        with open(marker) as f:
            check("标记内容未被改写（否则反复跑探针会一直续期熔断）",
                  f.read() == "unit-test-marker")
    finally:
        (fetcher._fetch_spot_list_em, fetcher._fetch_spot_list_tencent,
         ss.spot_list) = saved
        if not existed and os.path.exists(marker):
            os.remove(marker)

    # ③b 适配器**真的**会写异常（≠「函数存在且测试通过」），且不会把「行数」当异常。
    # 这是本项目的老纪律：机制测试通过 ≠ 代码在用机制，必须查调用点。
    for t in ("lhb", "lhb_inst"):
        store.clear_table(t)
    store._exec("DELETE FROM data_health")
    for name in ake._GUARD_DEFAULTS:
        ake.guard(name).configure(min_gap=0)
        ake.guard(name).breaker.reset()

    class _CleanAk(_FakeAk):
        pass

    class _AnomAk(_FakeAk):
        def stock_lhb_detail_em(self, start_date=None, end_date=None):
            raw = _lhb_raw()
            raw.loc[0, "代码"] = 1234567      # 超 6 位 → 归一判为坏代码
            return raw

    orig_ak = ake._ak
    try:
        ake._ak = lambda: _CleanAk()
        ake.collect_lhb("2026-09-29", log=lambda *_: None)
        clean = store.recent_anomalies(20)
        check("干净数据不产生异常记录（否则每次成功都显示「有异常」）",
              not [r for r in clean if r["source"] == ake.SOURCE_LHB],
              str([r["anomalies"] for r in clean])[:120])

        store._exec("DELETE FROM data_health")
        ake._ak = lambda: _AnomAk()
        ake.collect_lhb("2026-09-29", log=lambda *_: None)
        rows = store.recent_anomalies(20)
        hit = [r for r in rows if r["source"] == ake.SOURCE_LHB]
        check("真异常会被适配器写进画像（调用点接线）", bool(hit), str(rows)[:160])
        if hit:
            check("异常记录含逐字段计数",
                  "bad_code" in hit[0]["anomalies"], hit[0]["anomalies"])
            check("「行数」不被当成异常",
                  "rows" not in hit[0]["anomalies"], hit[0]["anomalies"])
        check("一次抓取仍只占一行画像（异常是附上去的）",
              store._query("SELECT COUNT(*) n FROM data_health WHERE source=?",
                           (ake.SOURCE_LHB,))[0]["n"] == 1,
              str(store._query("SELECT COUNT(*) n FROM data_health WHERE source=?",
                               (ake.SOURCE_LHB,))[0]["n"]))
    finally:
        ake._ak = orig_ak
        for name in ake._GUARD_DEFAULTS:
            ake.guard(name).breaker.reset()
        store.clear_table("lhb")
        store.clear_table("lhb_inst")

    # ④ 三个源的 configure 必须能吃下 config.json 里的全套键（少一个参数就 TypeError）
    import json as _json
    ds = _json.load(open(os.path.join(ROOT, "config.json"), encoding="utf-8"))["datasource"]
    try:
        aks.configure(enabled=ds.get("akshare_enabled"),
                      min_gap=ds.get("akshare_min_gap_sec"),
                      budget_sec=ds.get("akshare_budget_sec"),
                      retries=ds.get("akshare_retries"),
                      breaker_ttl_sec=ds.get("akshare_breaker_ttl_sec"))
        ake.configure(enabled=ds.get("akshare_enabled"),
                      min_gap=ds.get("akshare_min_gap_sec"),
                      budget_sec=ds.get("akshare_event_budget_sec"),
                      retries=ds.get("akshare_retries"),
                      breaker_ttl_sec=ds.get("akshare_breaker_ttl_sec"),
                      bse_enabled=ds.get("akshare_margin_bse"))
        ss.configure(min_gap=ds.get("akshare_min_gap_sec"),
                     budget_sec=ds.get("akshare_sina_budget_sec"),
                     retries=ds.get("akshare_retries"),
                     breaker_ttl_sec=ds.get("akshare_breaker_ttl_sec"))
        check("三个源的 configure 都接受 config 全套键", True)
    except TypeError as e:
        check("三个源的 configure 都接受 config 全套键", False, f"TypeError: {e}")
    check("新浪档预算真的被设成配置值",
          abs(ss.guard().budget.budget - float(ds["akshare_sina_budget_sec"])) < 1e-9,
          str(ss.guard().budget.budget))
    check("事件类熔断 TTL 真的被设成配置值",
          all(abs(ake.guard(n).breaker.ttl_sec
                  - float(ds["akshare_breaker_ttl_sec"])) < 1e-9
              for n in ake._GUARD_DEFAULTS),
          str([ake.guard(n).breaker.ttl_sec for n in ake._GUARD_DEFAULTS][:2]))


def test_store_rollback_subprocess():
    print("· QUANT_STORE=file 回退（子进程验证）")
    code = (
        "import os,sys;"
        f"sys.path.insert(0,{os.path.join(ROOT, 'src')!r});"
        "os.environ['QUANT_STORE']='file';"
        "import pandas as pd;"
        "import store;"
        "from datasources import akshare_source as aks;"
        "n,_=aks.normalize_individual(pd.DataFrame({'股票代码':[1],'股票简称':['X'],"
        "'最新价':['1'],'涨跌幅':['1%'],'换手率':['1%'],'流入资金':['1亿'],"
        "'流出资金':['0.5亿'],'净额':['0.5亿'],'成交额':['2亿']}));"
        "print('ENABLED', store.enabled());"
        "print('WRITE', store.upsert_fund_flow('cn','2026-09-29',n,src='u'));"
        "print('READ', store.load_fund_flow('2026-09-29','cn'));"
        "print('COV', store.fund_flow_coverage('cn'))"
    )
    try:
        out = subprocess.run([sys.executable, "-c", code], capture_output=True,
                             text=True, timeout=90).stdout
    except Exception as e:
        check("回退模式子进程可运行", False, str(e))
        return
    check("enabled()=False", "ENABLED False" in out, out)
    check("写入为 no-op 且不抛", "WRITE 0" in out, out)
    check("读取返回 None", "READ None" in out, out)
    check("覆盖统计为空", "COV []" in out, out)


_REAL_BREAKERS_AT_START = (
    sorted(f for f in os.listdir(_REAL_CACHE) if f.startswith("breaker_"))
    if os.path.isdir(_REAL_CACHE) else [])


def test_no_production_cache_pollution():
    print("· 隔离：自检不动真实缓存目录里的熔断标记")
    # 断言「测试前后不变」而不是「一个都没有」：生产里合法的熔断标记
    # （比如某个源今天真的被拒了）不该让自检失败，而自检自己新增或删除才是问题。
    now = (sorted(f for f in os.listdir(_REAL_CACHE) if f.startswith("breaker_"))
           if os.path.isdir(_REAL_CACHE) else [])
    check("真实 state/cache 的 breaker_* 集合未变",
          now == _REAL_BREAKERS_AT_START,
          f"before={_REAL_BREAKERS_AT_START} after={now}")
    check("自检用的是临时缓存目录", dsbase.CACHE_DIR == _TEST_CACHE)


# ---------------------------------------------------------------------------
def main():
    global VERBOSE
    ap = argparse.ArgumentParser()
    ap.add_argument("-v", "--verbose", action="store_true")
    args = ap.parse_args()
    VERBOSE = args.verbose

    print(f"数据源适配层自检（库={os.environ['QUANT_DB']}）\n")
    test_parsers()
    test_normalize()
    test_normalize_schema_guard()
    test_upsert_idempotent()
    test_failure_classification()
    test_breaker_semantics()
    test_guard_short_circuit()
    test_direct_connection_env()
    test_collect_end_to_end()
    test_collect_degradation()
    test_same_day_reuse()
    test_event_normalizers()
    test_margin_semantics()
    test_margin_no_data_on_old_date()
    test_margin_calendar_and_backfill()
    test_event_tables()
    test_event_collect_end_to_end()
    test_event_date_and_nodata()
    test_progress_bar_suppression()
    test_sina_normalize()
    test_rank_snapshot_src_persisted()
    test_spot_tier_chain()
    test_no_import_cycle()
    test_audit_fixes()
    test_store_rollback_subprocess()
    test_no_production_cache_pollution()

    print()
    if _fails:
        print(f"❌ {len(_fails)} 项未通过:")
        for f in _fails:
            print(f"   - {f}")
        return 1
    print("✅ 全部通过")
    return 0


if __name__ == "__main__":
    sys.exit(main())
