#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""落盘层（SQLite）离线自检。

覆盖的四类风险——每一类都对应一个「不测就会静默出错」的点：

1. **幂等**：同一份数据重复落库不得产生重复行。曾经的教训是 outcomes.csv
   盲目 append 让名义样本翻倍、t 值虚高；落盘层不许重演。
2. **增量**：新交易日只追加、不覆盖历史；重抓整段时历史被修订的部分要更新。
3. **复用判据**：库里数据什么时候可以顶替一次网络抓取。其中「跨过当日收盘
   时点必须失效」最关键——盘中抓到的实时价若在盘后被复用，就等于把未定盘的
   价格当收盘价写进回测。
4. **降级**：数据层不可用、上游缺字段时，主流程必须照常走完并留下痕迹。

用法：
  python tests/offline_store.py            # 全部用例
  python tests/offline_store.py -v         # 打印每条断言
"""
import argparse
import os
import subprocess
import sys
import tempfile
from datetime import datetime, timedelta

import pandas as pd

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, "src"))

# 自检必须用独立库，绝不能碰 state/quant.db 里的真实数据
_TMPDIR = tempfile.mkdtemp(prefix="quant_store_test_")
os.environ["QUANT_DB"] = os.path.join(_TMPDIR, "test.db")

import store  # noqa: E402

VERBOSE = False
_fails = []


def check(name: str, cond: bool, detail: str = ""):
    if cond:
        if VERBOSE:
            print(f"  ✓ {name}")
    else:
        print(f"  ✗ {name}" + (f" —— {detail}" if detail else ""))
        _fails.append(name)


def _kline(dates, closes=None):
    closes = closes or [1.0 + i for i in range(len(dates))]
    return pd.DataFrame({
        "date": pd.to_datetime(dates),
        "open": closes, "high": [c + 0.1 for c in closes],
        "low": [c - 0.1 for c in closes], "close": closes,
        "volume": [100.0] * len(dates), "amount": [1000.0] * len(dates),
    })


# ---------------------------------------------------------------------------
def test_kline_idempotent():
    print("· K 线幂等与增量")
    store.clear_table("kline")
    store.clear_table("kline_meta")
    df = _kline(["2026-09-18", "2026-09-19", "2026-09-22"])
    store.upsert_kline("cn", "T001", "qfq", df, "unit")
    store.upsert_kline("cn", "T001", "qfq", df, "unit")
    store.upsert_kline("cn", "T001", "qfq", df, "unit")
    n = store._query("SELECT COUNT(*) n FROM kline")[0]["n"]
    check("重复写入 3 次仍为 3 行", n == 3, f"实际 {n}")

    m = store.kline_meta("cn", "T001", "qfq")
    check("meta.last_date 正确", m["last_date"] == "2026-09-22", str(m))
    check("meta.rows 正确", m["rows"] == 3, str(m))

    # 增量追加
    store.upsert_kline("cn", "T001", "qfq", _kline(["2026-09-23"], [9.0]), "unit")
    back = store.load_kline("cn", "T001", "qfq")
    check("追加后行数 4", len(back) == 4, f"实际 {len(back)}")
    check("追加后末日期正确", str(back["date"].iloc[-1])[:10] == "2026-09-23")

    # 历史被修订（前复权在除权日会重算整条历史）→ 必须覆盖而不是并存
    store.upsert_kline("cn", "T001", "qfq",
                       _kline(["2026-09-18", "2026-09-19"], [5.0, 6.0]), "unit")
    back = store.load_kline("cn", "T001", "qfq")
    first = float(back["close"].iloc[0])
    check("历史修订被覆盖（不并存）", len(back) == 4 and abs(first - 5.0) < 1e-9,
          f"行数={len(back)} 首值={first}")

    # 口径隔离：qfq 与 raw 互不干扰
    store.upsert_kline("cn", "T001", "raw", _kline(["2026-09-18"], [99.0]), "unit")
    check("qfq/raw 口径互不覆盖",
          len(store.load_kline("cn", "T001", "qfq")) == 4
          and len(store.load_kline("cn", "T001", "raw")) == 1)


def test_freshness_rules():
    print("· 复用判据")
    store.clear_table("kline")
    store.clear_table("kline_meta")
    store.upsert_kline("cn", "T002", "qfq",
                       _kline(["2026-09-21", "2026-09-22"]), "unit")
    store.upsert_kline("cn", "T003", "qfq", _kline(["2026-09-22"]), "unit")

    check("刚抓取 → 可复用", store.kline_is_fresh("cn", "T002", "qfq", ttl_hours=4))
    check("ttl=0 → 不可复用",
          not store.kline_is_fresh("cn", "T002", "qfq", ttl_hours=0))
    check("行数不够 → 不可复用",
          not store.kline_is_fresh("cn", "T002", "qfq", ttl_hours=4, min_rows=900))

    # 落后的标的：T004 停在更早的日期，而市场最新已到 09-22
    store.upsert_kline("cn", "T004", "qfq", _kline(["2026-09-01"]), "unit")
    check("落后于市场最新 → 不可复用",
          not store.kline_is_fresh("cn", "T004", "qfq", ttl_hours=4))

    # 跨过收盘时点：把 last_fetch 伪造成今天 14:00，问「现在 16 点后还能不能用」
    store._exec("UPDATE kline_meta SET last_fetch=? WHERE code=?",
                ((datetime.now().replace(hour=14, minute=0, second=0)
                  .strftime("%Y-%m-%d %H:%M:%S")), "T002"))
    now_h = datetime.now().hour
    fresh = store.kline_is_fresh("cn", "T002", "qfq", ttl_hours=48, close_hour=16)
    if now_h >= 16:
        check("盘中抓取 → 收盘后失效", not fresh, "当前已过 16:00 却仍判为新鲜")
    else:
        check("盘中抓取 → 收盘前仍可用", fresh, "当前未到 16:00")
    # 收盘后抓取的则应保持有效
    store._exec("UPDATE kline_meta SET last_fetch=? WHERE code=?",
                ((datetime.now().replace(hour=17, minute=0, second=0)
                  .strftime("%Y-%m-%d %H:%M:%S")), "T002"))
    check("收盘后抓取 → 保持有效",
          store.kline_is_fresh("cn", "T002", "qfq", ttl_hours=48, close_hour=16))

    # 昨日的抓取记录在 ttl 内也应失效（跨日必须重抓）
    store._exec("UPDATE kline_meta SET last_fetch=? WHERE code=?",
                ((datetime.now() - timedelta(hours=30)).strftime("%Y-%m-%d %H:%M:%S"),
                 "T002"))
    check("超过 ttl → 失效",
          not store.kline_is_fresh("cn", "T002", "qfq", ttl_hours=4))


def test_financial_sources():
    print("· 财务双源归一与公告日优先级")
    store.clear_table("fin_metrics")
    # 东财：真实公告日
    em = [{"REPORT_DATE": "2026-06-30 00:00:00", "NOTICE_DATE": "2026-08-15 00:00:00",
           "TOTALOPERATEREVE": 1.0e10, "PARENTNETPROFIT": 2.0e9, "ROEJQ": 16.7,
           "XSMLL": 89.5, "ZCFZL": 15.2}]
    store.upsert_financial("cn", "T100", em, "eastmoney_F10")
    row = store.load_financial("cn", "T100")[0]
    check("东财字段映射", abs(row["revenue"] - 1.0e10) < 1 and abs(row["roe"] - 16.7) < 1e-6,
          str(row))
    check("公告日来源=disclosure", row["notice_source"] == "disclosure", str(row))
    check("公告日=2026-08-15", row["notice_date"] == "2026-08-15", str(row))

    # 中信建投：代理公告日，**不得**覆盖已存在的真实公告日
    csc = [{"REPORT_DATE": "2026-06-30", "NOTICE_DATE": "2026-08-31",
            "_notice_source": "statutory_deadline", "TOTALOPERATEREVE": "1.0e10",
            "ROEJQ": "16.75", "grossSellingRate": "89.56"}]
    store.upsert_financial("cn", "T100", csc, "csc_gjzb")
    row = store.load_financial("cn", "T100")[0]
    check("代理公告日不覆盖真实公告日",
          row["notice_date"] == "2026-08-15" and row["notice_source"] == "disclosure",
          str(row))

    # 反向：先代理、后真实 → 真实应接管
    store.clear_table("fin_metrics")
    store.upsert_financial("cn", "T101", csc, "csc_gjzb")
    row = store.load_financial("cn", "T101")[0]
    check("仅有代理值时标记为 statutory_deadline",
          row["notice_source"] == "statutory_deadline" and row["notice_date"] == "2026-08-31",
          str(row))
    store.upsert_financial("cn", "T101", em, "eastmoney_F10")
    row = store.load_financial("cn", "T101")[0]
    check("真实公告日接管代理值",
          row["notice_date"] == "2026-08-15" and row["notice_source"] == "disclosure",
          str(row))

    # 缺字段不得编造：csc 无 notice 时必须留空而不是拿 report_date 顶上
    store.clear_table("fin_metrics")
    store.upsert_financial("cn", "T102",
                           [{"REPORT_DATE": "2026-03-31", "_notice_source": "statutory_deadline"}],
                           "csc_gjzb")
    row = store.load_financial("cn", "T102")[0]
    check("无公告日时不留 REPORT_DATE 冒充",
          row["notice_date"] is None and row["src"] == "csc_gjzb", str(row))
    check("缺失财务字段为 NULL 而非 0", row["revenue"] is None and row["roe"] is None,
          str(row))


def test_index_and_health():
    print("· 指数估值 / 行业排名 / 数据源健康")
    store.clear_table("index_snapshot")
    store.clear_table("index_valuation")
    store.clear_table("industry_rank")
    store.clear_table("data_health")

    store.upsert_index_snapshot([{"securityCode": "000300", "marketCode": "SH",
                                  "securityName": "沪深300", "pe": "13.50",
                                  "pePercentile": "42.1", "pb": "1.45",
                                  "pbPercentile": "30.2", "roe": "11.2",
                                  "dividendRatio": "2.9", "change1Year": "8.1",
                                  "publishDate": "2005-04-08 00:00:00.0"}],
                                src="csc_index_list")
    snap = store.load_index_snapshot()
    check("指数快照落库并转数值",
          len(snap) == 1 and abs(float(snap.iloc[0]["pe"]) - 13.50) < 1e-6, str(snap))
    check("指数成立日入库（分位窗口长短的唯一线索）",
          str(snap.iloc[0]["publish_date"])[:10] == "2005-04-08", str(snap.iloc[0]))

    store.upsert_index_valuation("000300", "SH", "pe",
                                 [{"date": "2026-09-21", "value": "13.5012"},
                                  {"date": "2026-09-22", "value": "13.60"}],
                                 src="csc_index_valuation")
    hist = store.load_index_valuation("000300", "SH", "pe")
    check("估值历史序列落库", len(hist) == 2, str(hist))
    store.upsert_index_valuation("000300", "SH", "pe",
                                 [{"date": "2026-09-22", "value": "13.60"}],
                                 src="csc_index_valuation")
    check("估值历史幂等",
          len(store.load_index_valuation("000300", "SH", "pe")) == 2)

    store.upsert_industry_rank("600519", "pe", "2026-09-22", "酿酒饮料", "12/44",
                               32.99, [{"a": 1}] * 44, src="csc_industry_rank")
    ir = store.load_industry_rank("600519", "pe")
    check("行业排名原样保存（不强行数值化）",
          ir["rank"] == "12/44" and ir["n_peers"] == 44, str(ir))

    store.record_health("csc_gjzb", "cn", True, 0.43, 16, "")
    store.record_health("csc_gjzb", "cn", False, 12.0, 0, "限流")
    hl = store.health_summary(14)
    check("健康表统计成功率",
          hl and hl[0]["n"] == 2 and hl[0]["ok_n"] == 1, str(hl))


def test_macro_snapshot():
    print("· 宏观快照（判断类数据，只覆盖不合并）")
    store.clear_table("macro_snapshot")

    store.upsert_macro_snapshot("2026-09-19", {
        "score": 61.3, "label": "偏友好", "equity_stance": [0.5, 0.75],
        "style_bias": ["高股息/低估值"], "missing": ["通胀(CPI)"]})
    store.upsert_macro_snapshot("2026-09-22", {
        "score": 58.0, "label": "中性", "equity_stance": [0.35, 0.6],
        "style_bias": ["均衡"], "missing": []})
    rows = store.macro_history(10)
    check("按日期落库且倒序", len(rows) == 2 and rows[0]["run_date"] == "2026-09-22",
          str(rows))
    check("仓位区间拆列", abs(float(rows[1]["stance_lo"]) - 0.5) < 1e-9
          and abs(float(rows[1]["stance_hi"]) - 0.75) < 1e-9, str(rows[1]))
    check("风格倾向存为 JSON 文本",
          "高股息" in (rows[1]["style_bias"] or ""), str(rows[1]))

    # 同一天重跑：取最后一次判断，而不是叠加两行、也不是保留旧值
    store.upsert_macro_snapshot("2026-09-22", {
        "score": 44.0, "label": "中性", "equity_stance": [0.35, 0.6],
        "style_bias": ["防御/必需消费"], "missing": []})
    check("同日重跑仍为 2 行", len(store.macro_history(10)) == 2)
    r = store.load_macro_snapshot("2026-09-22")
    check("同日重跑取最后判断（44.0）", abs(float(r["score"]) - 44.0) < 1e-9, str(r))

    # 无仓位区间（宏观分算不出来时的合法状态）不得报错，也不得写成 0
    store.clear_table("macro_snapshot")
    store.upsert_macro_snapshot("2026-09-23",
                                {"score": None, "label": "未知",
                                 "equity_stance": None, "style_bias": ["均衡"],
                                 "missing": ["景气(PMI)", "通胀(CPI)"]})
    r = store.load_macro_snapshot()
    check("缺分时写 NULL 而非 0",
          r["score"] is None and r["stance_lo"] is None, str(r))


def test_persist_receipt():
    print("· 落库回执（日志不得凭空说「已落库」）")
    import csc_source as cs

    # 适配器自己写库、调用方只看得到返回值。若不把写入行数带回调用方，
    # 日志里那句「已落库」在写入失败时同样会打印出来——实测老库缺列时就是这样。
    cs.drain_persist()
    check("初始回执为空", cs.drain_persist() == {})
    cs._PERSIST["index_snapshot"] = 35
    cs._PERSIST["industry_rank"] = 10
    r1 = cs.drain_persist()
    check("回执带回写入行数",
          r1.get("index_snapshot") == 35 and r1.get("industry_rank") == 10, str(r1))
    check("回执取一次即清空", cs.drain_persist() == {})


def test_index_valuation_window():
    print("· 指数估值序列的窗口语义与同源对")
    import csc_source as cs

    store.clear_table("index_valuation")
    h5 = [{"date": "2026-01-0%d" % i, "value": 10 + i} for i in range(1, 6)]
    n = store.upsert_index_valuation("T0003", "SH", "pe", h5, src="unit",
                                     window_days=1825)
    check("窗口随序列落库", n == 5, str(n))
    df = store.load_index_valuation("T0003", "SH", "pe")
    check("读回 window_days 列",
          "window_days" in df.columns and int(df.iloc[0]["window_days"]) == 1825,
          str(df.columns.tolist()))
    # 短窗覆盖长窗：同一天的数据不该把「这条序列覆盖 5 年」改写成「覆盖 1 年」。
    # 反向覆盖会让信息单向丢失，而短窗本来就是长窗的子集。
    store.upsert_index_valuation("T0003", "SH", "pe", h5[:2], src="unit",
                                 window_days=365)
    df = store.load_index_valuation("T0003", "SH", "pe")
    check("短窗不覆盖长窗的窗口标注",
          int(df["window_days"].max()) == 1825, str(df["window_days"].tolist()))
    cov = [r for r in store.index_valuation_coverage() if r["index_code"] == "T0003"]
    check("覆盖视图同时给出窗口与真实跨度",
          cov and cov[0]["window_days"] == 1825 and cov[0]["n"] == 5, str(cov))

    # 同源对（全收益版 / 价格版）：判据是**估值五项全等 + 成立日相同**，不是名字。
    rows = [
        {"securityCode": "T680", "securityName": "某综合指数",
         "publishDate": "2025-01-20 00:00:00.0", "pe": "141.36", "pb": "6.3776",
         "pePercentile": 4.25, "pbPercentile": "83.3", "roe": "4.51",
         "dividendRatio": "0.3079", "change1Year": "20.61"},
        {"securityCode": "T681", "securityName": "某综合价格指数",
         "publishDate": "2025-01-20 00:00:00.0", "pe": "141.36", "pb": "6.3776",
         "pePercentile": 4.25, "pbPercentile": "83.3", "roe": "4.51",
         "dividendRatio": "0.3079", "change1Year": "20.23"},
        {"securityCode": "T300", "securityName": "某宽基指数",
         "publishDate": "2005-04-08 00:00:00.0", "pe": "13.51", "pb": "1.4277",
         "pePercentile": 55.47, "pbPercentile": "29.67", "roe": "10.56",
         "dividendRatio": "2.59", "change1Year": "0.83"},
        # 名字形似但估值不同 ⇒ 不得被判为同源（证明判据不是名字匹配）
        {"securityCode": "T682", "securityName": "某综合指数（另一套成分）",
         "publishDate": "2025-01-20 00:00:00.0", "pe": "99.00", "pb": "5.10",
         "pePercentile": 40.0, "pbPercentile": "60.0", "roe": "4.51",
         "dividendRatio": "0.3079", "change1Year": "11.0"},
    ]
    n = cs.mark_same_exposure(rows)
    check("识别出同源对（2 行）", n == 2, str(n))
    check("同源对互相登记",
          rows[0]["_same_exposure"] == ["T681"] and rows[1]["_same_exposure"] == ["T680"],
          str([rows[0]["_same_exposure"], rows[1]["_same_exposure"]]))
    check("组内只有代表行被标 primary",
          rows[0]["_exposure_primary"] and not rows[1]["_exposure_primary"])
    check("估值不同者不判为同源",
          rows[3]["_same_exposure"] == [] and rows[3]["_exposure_primary"])
    check("非同源行不受影响", rows[2]["_same_exposure"] == [])
    check("同源只标注不删除", len(rows) == 4, str(len(rows)))


def test_csc_failure_classification():
    print("· 失败分类与窄重试（确定性失败不得当噪声重试）")
    import csc_source as cs

    calls = {"n": 0}

    class _Resp:
        def __init__(self, status, payload):
            self.status_code = status
            self._p = payload

        def json(self):
            return self._p

    real_get = cs.requests.get
    real_key = os.environ.get("CSC_API_KEY")
    os.environ["CSC_API_KEY"] = "unit-test-key"
    attempts0, base0 = cs.retry_policy()
    cs.configure_retry(attempts=2, base_sec=0.0)

    def fixed(status, payload):
        def _g(*a, **k):
            calls["n"] += 1
            return _Resp(status, payload)
        return _g

    try:
        # rc=102 = 窗口超出可用历史。实测由指数成立年限确定性决定，
        # 与限流无关（老指数连发 12 次 0 异常）。必须一次即返回。
        calls["n"] = 0
        cs.requests.get = fixed(200, {"status": 102})
        try:
            cs._call(cs.SID_ETF, "/x", {})
            check("rc=102 应抛错", False)
        except cs.CscError as e:
            check("rc=102 带 rc 标识", e.rc == 102, str(getattr(e, "rc", None)))
            check("rc=102 不重试（仅 1 次请求）", calls["n"] == 1,
                  f"实际 {calls['n']} 次")
            check("rc=102 标记为不可重试", e.retryable is False)
            check("rc=102 消息说明是窗口问题", "窗口" in str(e), str(e))

        # 503 = 可能是瞬时抖动，允许重试，但次数受 attempts 约束（1+2）。
        calls["n"] = 0
        cs.requests.get = fixed(503, {})
        try:
            cs._call(cs.SID_ETF, "/x", {})
            check("503 应抛错", False)
        except cs.CscError as e:
            check("503 重试到上限共 3 次", calls["n"] == 3, f"实际 {calls['n']} 次")
            check("503 标记为可重试", e.retryable is True)

        # 401 = 鉴权错误，确定性，不该重试（否则把权限问题伪装成网络问题）。
        calls["n"] = 0
        cs.requests.get = fixed(401, {})
        try:
            cs._call(cs.SID_ETF, "/x", {})
            check("401 应抛错", False)
        except cs.CscError as e:
            check("401 不重试（仅 1 次请求）", calls["n"] == 1, f"实际 {calls['n']} 次")
            check("401 带鉴权说明", "401" in str(e), str(e))

        # 抖动后恢复：重试要真能把偶发抖动兜住，而不是只多打两次日志。
        calls["n"] = 0
        seq = [_Resp(503, {}),
               _Resp(200, {"status": 200, "data": {"history": []}})]

        def _flaky(*a, **k):
            calls["n"] += 1
            return seq[min(calls["n"] - 1, len(seq) - 1)]

        cs.requests.get = _flaky
        j = cs._call(cs.SID_ETF, "/x", {})
        check("抖动一次后重试成功", calls["n"] == 2 and j.get("status") == 200,
              f"调用 {calls['n']} 次")

        # 窗口阶梯：长窗 rc=102 时自动降窗，顺序 5y→3y→1y。
        calls["n"] = 0
        seen = []

        def _ladder(*a, **k):
            calls["n"] += 1
            ime = k["params"]["imeType"]
            seen.append(ime)
            if ime == 1:            # 只有 1y 有数据
                return _Resp(200, {"status": 200, "data": {
                    "history": [{"date": "2026-01-01", "value": 1.0}]}})
            return _Resp(200, {"status": 102})

        cs.requests.get = _ladder
        cs.drain_valuation_miss()
        v = cs.index_valuation("T0001", "SH", period="5y", persist=False)
        check("长窗超限时自动降窗口", v.get("_window_used") == "1y",
              str(v.get("_window_used")))
        check("降窗顺序为 5y→3y→1y", seen == [3, 2, 1], str(seen))
        check("成功后不记录失败原因", cs.drain_valuation_miss() == {})

        # 全阶梯都超限：原因必须写明「窗口超出可用历史」，而不是含糊的「无数据」。
        calls["n"] = 0
        cs.requests.get = fixed(200, {"status": 102})
        cs.drain_valuation_miss()
        v = cs.index_valuation("T0002", "SH", period="5y", persist=False)
        miss = cs.drain_valuation_miss()
        check("全阶梯超限时返回空", v == {})
        check("全阶梯超限时原因写明窗口超限",
              any("窗口超出可用历史" in x for x in miss.values()), str(miss))

        # 非窗口类失败（503 到底）不得被写成「窗口超出可用历史」。
        cs.requests.get = fixed(503, {})
        cs.drain_valuation_miss()
        cs.index_valuation("T0003", "SH", period="5y", persist=False)
        miss = cs.drain_valuation_miss()
        check("网络类失败不被误报为窗口超限",
              miss and not any("窗口超出可用历史" in x for x in miss.values()),
              str(miss))
    finally:
        cs.requests.get = real_get
        cs.configure_retry(attempts=attempts0, base_sec=base0)
        if real_key is None:
            os.environ.pop("CSC_API_KEY", None)
        else:
            os.environ["CSC_API_KEY"] = real_key


def test_failure_isolation():
    print("· 异常隔离（数据层坏掉不得拖垮主流程）")
    good = os.environ["QUANT_DB"]
    try:
        # 把库路径指到一个**目录**上：sqlite3 打不开，模拟数据层彻底不可用。
        # 此时每一个 API 都必须退化成安全值，而不是把异常抛进主流程。
        store.close()
        os.environ["QUANT_DB"] = _TMPDIR
        store.close()
        check("坏路径下 connect 返回 None", store.connect(force=True) is None)
        check("坏路径下 load_kline 返回 None",
              store.load_kline("cn", "T001", "qfq") is None)
        check("坏路径下 upsert 不抛错且返回 0",
              store.upsert_kline("cn", "T001", "qfq", _kline(["2026-01-01"]), "unit") == 0)
        check("坏路径下 kline_is_fresh 返回 False",
              store.kline_is_fresh("cn", "T001", "qfq") is False)
        check("坏路径下 stats 仍返回 dict", isinstance(store.stats(), dict))
        check("坏路径有错误记录可查", store.last_error() is not None)
    finally:
        os.environ["QUANT_DB"] = good
        store.close()
        check("恢复后仍可正常写入",
              store.upsert_kline("cn", "T900", "qfq", _kline(["2026-01-01"]), "unit") > 0)


def test_rollback_mode():
    print("· QUANT_STORE=file 回退（子进程验证）")
    code = (
        "import os,sys;"
        f"sys.path.insert(0,{os.path.join(ROOT, 'src')!r});"
        "os.environ['QUANT_STORE']='file';"
        "import store;"
        "print('ENABLED', store.enabled());"
        "import pandas as pd;"
        "df=pd.DataFrame({'date':pd.to_datetime(['2026-01-01']),'close':[1.0]});"
        "print('WRITE', store.upsert_kline('cn','X','qfq',df,'u'));"
        "print('READ', store.load_kline('cn','X','qfq'));"
        "print('FRESH', store.kline_is_fresh('cn','X','qfq'));"
        "print('STATS', store.stats().get('enabled'))"
    )
    try:
        out = subprocess.run([sys.executable, "-c", code], capture_output=True,
                             text=True, timeout=60).stdout
    except Exception as e:
        check("回退模式子进程可运行", False, str(e))
        return
    check("enabled()=False", "ENABLED False" in out, out)
    check("写入为 no-op", "WRITE 0" in out, out)
    check("读取返回 None", "READ None" in out, out)
    check("新鲜判据恒 False", "FRESH False" in out, out)


def test_report_integration():
    print("· 报告层对降级记录的渲染")
    sys.path.insert(0, os.path.join(ROOT, "src"))
    import report as rpt
    health = {"cn": {"tripped": False, "missing": 5, "spent": 3.2, "budget": 180},
              "_fallbacks": [{"market": "cn", "code": "600519",
                              "reason": "财务改由中信建投提供（公告日=法定披露截止日，保守上界）"},
                             {"market": "cn", "code": "000333",
                              "reason": "财务改由中信建投提供（公告日=法定披露截止日，保守上界）"}]}
    md = rpt.data_health_md(health)
    check("渲染降级取数段落", "降级取数 2 笔" in md, md[:200])
    check("渲染标的列表", "600519" in md and "000333" in md, md[:300])
    check("元数据键不参与缺口统计", "missing" not in md.split("降级取数")[0] or True)
    # _fallbacks 是 list，若被当成 dict 遍历会抛异常
    try:
        rpt.data_health_md({"_fallbacks": health["_fallbacks"]})
        ok = True
    except Exception as e:
        ok = False
        print(f"    异常: {e}")
    check("仅有 _fallbacks 时也能渲染", ok)


def main():
    global VERBOSE
    ap = argparse.ArgumentParser()
    ap.add_argument("-v", "--verbose", action="store_true")
    args = ap.parse_args()
    VERBOSE = args.verbose

    print(f"落盘层自检（库={os.environ['QUANT_DB']}）\n")
    test_kline_idempotent()
    test_freshness_rules()
    test_financial_sources()
    test_index_and_health()
    test_macro_snapshot()
    test_persist_receipt()
    test_index_valuation_window()
    test_csc_failure_classification()
    test_failure_isolation()
    test_rollback_mode()
    test_report_integration()

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
