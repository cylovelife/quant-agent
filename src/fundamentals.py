# -*- coding: utf-8 -*-
"""基本面与基金档案数据层（长期价值轨专用）。

数据源（均为 2026-09-21 实测可用，字段索引已逐条核对）：

1. 估值 / 市值 —— 腾讯批量行情 qt.gtimg.cn（A股/ETF 为 88 字段布局）
     [3]  最新价        [32] 涨跌幅%       [38] 换手率%
     [39] PE(TTM)      [46] PB            [49] 量比
     [44] 流通市值(亿)  [45] 总市值(亿)
     [52] PE(动)       [53] PE(静)
   ⚠ 港股(78字段)/美股(73字段)布局不同，本模块只保证 A股/ETF 口径。
   ⚠ ETF 的 PE/PB 字段为空，估值因子对 ETF 自动降级为「无」。

2. 财务指标 —— 东财 F10 RPT_F10_FINANCE_MAINFINADATA
     返回 35 期历史（季报粒度，季度更新），字段含：
     EPSJB 每股收益 / BPS 每股净资产 / MGJYXJJE 每股经营现金流
     TOTALOPERATEREVE 营收 / PARENTNETPROFIT 归母净利
     TOTALOPERATEREVETZ 营收同比 / PARENTNETPROFITTZ 净利同比
     ROEJQ 加权ROE / XSMLL 毛利率 / XSJLL 净利率 / ZCFZL 资产负债率
     LD 流动比率 / SD 速动比率 / JYXJLYYSR 经营现金流占营收比

3. 历史价格（不复权）—— 腾讯 ifzq。用于估值分位：
   历史 PE/PB 必须用**不复权价 + 当时财务口径**才成立；
   现有 fetcher.fetch_kline 取前复权，除权会重算整条历史，会污染分位计算。

4. 分红 —— 东财 RPT_SHAREBONUS_DET（失败返回 None，因子层做中性处理）。

5. 基金档案 —— 天天基金 pingzhongdata：
     fS_name 名称 / syl_1n|6y|3y|1y 近1年|6月|3月|1月收益
     Data_fundSharesPositions 股票仓位测算 / Data_fluctuationScale 规模变化
     Data_currentFundManager 现任经理 / Data_assetAllocation 资产配置
     stockCodes 重仓股（code+市场后缀拼接，如 "6005191" = 600519 沪市）

缓存：财务与基金档案落 state/cache/fin、state/cache/fund，
按「数据周期 TTL + 请求失败回退旧缓存」复用，避免每次运行重复请求。
"""
import json
import os
import re
import time
from datetime import date, datetime

import pandas as pd

import csc_source
import fetcher
import store

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
FIN_DIR = os.path.join(ROOT, "state", "cache", "fin")
FUND_DIR = os.path.join(ROOT, "state", "cache", "fund")
KLINE_DIR = os.path.join(ROOT, "state", "cache", "kline_raw")
for _d in (FIN_DIR, FUND_DIR, KLINE_DIR):
    os.makedirs(_d, exist_ok=True)

F10_URL = "https://datacenter-web.eastmoney.com/api/data/v1/get"
TX_QUOTE_URL = "https://qt.gtimg.cn/q="


# ---------------------------------------------------------------------------
# 代码规范
# ---------------------------------------------------------------------------
def secucode(code: str, market: str = "cn") -> str | None:
    """腾讯/东财通用证券代码 → 东财 SECUCODE（600519.SH）。仅支持 A股。"""
    c = str(code).strip()
    if market != "cn":
        return None
    if c.startswith(("6", "9")):
        return f"{c}.SH"
    if c.startswith(("0", "3")):
        return f"{c}.SZ"
    if c.startswith(("4", "8")):
        return f"{c}.BJ"
    return None


def is_stock(code: str, market: str) -> bool:
    """只有 A 股个股有 F10 财务数据；ETF/基金/港美不适用。"""
    c = str(code).strip()
    if market == "cn":
        return c.startswith(("60", "68", "00", "30", "83", "87", "43", "92"))
    return False


# ---------------------------------------------------------------------------
# 1) 估值批量：腾讯行情
# ---------------------------------------------------------------------------
def _f(fl, i, cast=float):
    """防御性取字段：越界/空串/非数 一律返回 None。"""
    if i >= len(fl):
        return None
    v = fl[i]
    if v is None or v == "":
        return None
    try:
        return cast(v)
    except (TypeError, ValueError):
        return None


def _parse_valuation(payload: str, market: str, code: str) -> dict:
    fl = payload.split("~")
    if len(fl) < 6:
        return {}
    price = _f(fl, 3)
    if not price or price <= 0:
        return {}
    pe_ttm, pb = _f(fl, 39), _f(fl, 46)
    mcap, fcap = _f(fl, 45), _f(fl, 44)
    return {
        "code": str(code), "market": market,
        "name": fl[1] if len(fl) > 1 else str(code),
        "price": price,
        "pct": _f(fl, 32),
        "pe_ttm": pe_ttm if (pe_ttm and pe_ttm > 0) else None,
        "pb": pb if (pb and pb > 0) else None,
        "pe_dyn": _f(fl, 52), "pe_static": _f(fl, 53),
        "total_mcap": mcap, "float_mcap": fcap,   # 单位：亿元
        "turnover": _f(fl, 38),                   # 换手率%
        "amount": _f(fl, 37),                     # 成交额（万元）
        "ts": (fl[30] if len(fl) > 30 else ""),
    }


def fetch_valuations(items, chunk: int = 40) -> dict:
    """批量取估值。items: [(code, market[, mkt_id])]，返回 {(market, code): dict}。

    腾讯批量行情单次可带数十代码，60 只股票只需 2 个请求。
    """
    out, todo = {}, []
    for it in items or []:
        code, market = str(it[0]), it[1]
        if market not in ("cn", "etf"):
            continue
        todo.append((fetcher._tx_code(code, market, it[2] if len(it) > 2 else None),
                     market, code))
    for i in range(0, len(todo), chunk):
        batch = todo[i:i + chunk]
        try:
            txt = fetcher._get(TX_QUOTE_URL + ",".join(b[0] for b in batch),
                               min_gap=0.3).text
        except Exception:
            continue
        for m in re.finditer(r'v_([^=]+)="([^"]*)"', txt):
            key, payload = m.group(1).upper(), m.group(2)
            hit = next((b for b in batch if b[0].upper() == key), None)
            if hit is None:
                continue
            rec = _parse_valuation(payload, hit[1], hit[2])
            if rec:
                out[(hit[1], str(hit[2]))] = rec
    return out


# ---------------------------------------------------------------------------
# 2) 财务指标：东财 F10
# ---------------------------------------------------------------------------
def _fin_cache_path(code: str, market: str) -> str:
    return os.path.join(FIN_DIR, f"{market}_{code}.json")


# --- 数据源保护 -------------------------------------------------------------
# 东财 F10 是长期轨唯一的财报源。若它整段不可用，每只标的都要耗尽 fetcher._get 的
# 重试预算（3 个源 × 15s 超时 + 3 次退避 ≈153s）；标的池 50+ 只就是 2 小时以上的
# 静默等待，日志里只有稀疏的进度行，与卡死无法区分。
# 判据用**累计耗时**而非失败次数：限流是瞬时的，按次数熔断会把限流误判成源故障。
F10_BUDGET_SEC = float(os.environ.get("QUANT_F10_BUDGET_SEC", 180))
_F10_BUDGET = fetcher.FetchBudget(F10_BUDGET_SEC, name="东财 F10")


def _read_cached(path: str) -> list:
    """读旧缓存；不存在或损坏返回空列表（降级路径，绝不抛错）。"""
    if os.path.exists(path):
        try:
            with open(path, encoding="utf-8") as f:
                return json.load(f)
        except Exception:
            return []
    return []


def fetch_financial(code: str, market: str = "cn", periods: int = 16,
                    ttl_days: int = 15) -> list:
    """取多期主要财务指标（按报告期降序）。

    取数顺序（逐级降级，每一级都留痕）：

    1. 文件缓存（TTL 内）——命中即回，顺手把数据补写进数据库；
    2. 东方财富 F10——主源，带**真实公告日** `NOTICE_DATE`；
    3. **中信建投关键指标**——东财失败或被熔断时的兜底。上游不提供公告日，
       这里用法定披露截止日作保守上界代理，来源标记 `statutory_deadline`；
    4. 数据库里该标的的历史记录（比文件缓存更耐久）；
    5. 文件旧缓存（无 TTL 限制，最后一道）。

    为什么要有 3 和 4：东财是**唯一**的财报源，一旦限流整条长期轨就只能吃旧缓存
    （`_F10_BUDGET` 熔断后每题都要等重试预算耗尽）。中信建投这条链补上了这个
    单点，代价是公告日精度下降——方向取保守，所以不会前视。

    降级到 3/4/5 时会调用 `fetcher.note_fallback`，让「哪些标的的财务是降级取的、
    公告日是不是代理值」出现在报告的 data_health 里，而不是只躺在日志。
    """
    if not is_stock(code, market):
        return []
    path = _fin_cache_path(code, market)
    if os.path.exists(path):
        if (time.time() - os.path.getmtime(path)) / 86400 < ttl_days:
            try:
                with open(path, encoding="utf-8") as f:
                    rows = json.load(f)
                store.upsert_financial(market, code, rows, src="eastmoney_F10")
                return rows
            except Exception:
                pass

    sc = secucode(code, market)
    rows = []
    if sc and not _F10_BUDGET.guard():
        _t = time.time()
        try:
            r = fetcher._get(F10_URL, {
                "reportName": "RPT_F10_FINANCE_MAINFINADATA", "columns": "ALL",
                "filter": f'(SECUCODE="{sc}")', "pageNumber": 1, "pageSize": periods,
                "sortColumns": "REPORT_DATE", "sortTypes": "-1",
                "source": "HSF10", "client": "PC"}, min_gap=0.35).json()
            rows = ((r.get("result") or {}).get("data")) or []
        except Exception:
            rows = []
        _el = time.time() - _t
        _F10_BUDGET.charge(_el)
        store.record_health("eastmoney_F10", market, bool(rows), _el, len(rows),
                            "" if rows else "空返回或限流")
    if rows:
        with open(path, "w", encoding="utf-8") as f:
            json.dump(rows, f, ensure_ascii=False)
        store.upsert_financial(market, code, rows, src="eastmoney_F10")
        return rows

    # ---- 兜底 A：中信建投关键指标 ------------------------------------------
    if csc_source.available():
        _t = time.time()
        csc_rows = csc_source.fin_key_indicators(code, periods, market)
        store.record_health("csc_gjzb", market, bool(csc_rows), time.time() - _t,
                            len(csc_rows), "" if csc_rows else "无数据")
        if csc_rows:
            fetcher.note_fallback(
                market, code,
                "财务改由中信建投提供（公告日=法定披露截止日，保守上界）")
            return csc_rows

    # ---- 兜底 B：数据库历史记录 --------------------------------------------
    db_rows = store.load_financial(market, code, limit=periods)
    if db_rows:
        recs = []
        for d in db_rows:
            try:
                recs.append(json.loads(d["payload"]))
            except Exception:
                continue
        if recs:
            fetcher.note_fallback(market, code,
                                  f"财务源均不可用，改用数据库历史记录"
                                  f"（最新报告期 {db_rows[0]['report_date']}）")
            return recs

    # ---- 兜底 C：文件旧缓存 ------------------------------------------------
    cached = _read_cached(path)
    if cached:
        fetcher.note_fallback(market, code, "财务源均不可用，改用历史旧缓存（可能过期）")
    return cached


# ---------------------------------------------------------------------------
# 3) 不复权历史价（估值分位用）
# ---------------------------------------------------------------------------
def fetch_kline_raw(code: str, market: str = "cn", limit: int = 900,
                    ttl_days: int = 3, mkt_id=None) -> pd.DataFrame:
    """不复权日线。估值分位必须用原始价格口径（前复权会篡改历史）。

    落库口径 adjust='raw'，与短线轨的 'qfq' 严格分开——两者在除权日会给出
    不同的历史序列，混用会让估值分位静默算错。
    """
    if store.enabled() and store.kline_is_fresh(
            market, code, "raw", ttl_hours=float(ttl_days) * 24, min_rows=limit):
        cached = store.load_kline(market, code, "raw", limit=limit)
        if cached is not None and len(cached):
            return cached

    path = os.path.join(KLINE_DIR, f"{market}_{code}_{limit}.json")
    if os.path.exists(path):
        if (time.time() - os.path.getmtime(path)) / 86400 < ttl_days:
            try:
                with open(path, encoding="utf-8") as f:
                    recs = json.load(f)
                if recs:
                    df = pd.DataFrame(recs)
                    df["date"] = pd.to_datetime(df["date"])
                    store.upsert_kline(market, code, "raw", df, src="tencent_ifzq_raw")
                    return df
            except Exception:
                pass
    if market == "fund":
        return pd.DataFrame()
    tx = fetcher._tx_code(code, market, mkt_id)
    try:
        # 与 fetcher.fetch_kline 同源，但 param 不写 qfq -> 不复权
        r = fetcher._get(fetcher._KLINE_URLS[market],
                         {"param": f"{tx},day,,,{limit},"}, min_gap=0.35).json()
        d = (r.get("data") or {}).get(tx, {})
        klines = d.get("day") or d.get("qfqday") or []
    except Exception:
        klines = []
    recs = []
    for item in klines:
        try:
            recs.append({"date": item[0], "open": float(item[1]),
                         "close": float(item[2]), "high": float(item[3]),
                         "low": float(item[4]), "volume": float(item[5])})
        except (ValueError, IndexError, TypeError):
            continue
    if not recs:
        # 源不可用时退回库中旧序列，并登记降级（报告会披露哪些标的是陈旧的）
        stale = store.load_kline(market, code, "raw", limit=limit)
        if stale is not None and len(stale):
            m = store.kline_meta(market, code, "raw") or {}
            fetcher.note_fallback(market, code, "不复权K线源失败，改用库内旧序列",
                                  m.get("last_date"))
            return stale
        return pd.DataFrame()
    with open(path, "w", encoding="utf-8") as f:
        json.dump(recs, f, ensure_ascii=False)
    df = pd.DataFrame(recs)
    df["date"] = pd.to_datetime(df["date"])
    store.upsert_kline(market, code, "raw", df, src="tencent_ifzq_raw")
    return df


# ---------------------------------------------------------------------------
# 4) 分红（股息率）
# ---------------------------------------------------------------------------
def fetch_dividend(code: str, market: str = "cn", ttl_days: int = 30) -> dict | None:
    """近 12 个月每股派现（税前，元）。失败返回 None。"""
    if not is_stock(code, market):
        return None
    path = os.path.join(FIN_DIR, f"{market}_{code}_div.json")
    if os.path.exists(path):
        if (time.time() - os.path.getmtime(path)) / 86400 < ttl_days:
            try:
                with open(path, encoding="utf-8") as f:
                    return json.load(f)
            except Exception:
                pass
    sc = secucode(code, market)
    if not sc:
        return None
    try:
        r = fetcher._get(F10_URL, {
            "reportName": "RPT_SHAREBONUS_DET", "columns": "ALL",
            "filter": f'(SECURITY_CODE="{code}")', "pageNumber": 1, "pageSize": 20,
            "sortColumns": "EX_DIVIDEND_DATE", "sortTypes": "-1",
            "source": "HSF10", "client": "PC"}, min_gap=0.35).json()
        rows = ((r.get("result") or {}).get("data")) or []
    except Exception:
        rows = []
    if not rows:
        return None
    # 口径核对（2026-09-21 实测）：PRETAX_BONUS_RMB 是「每 10 股派现（元，含税）」，
    # 原文 IMPL_PLAN_PROFILE 写作「10派280.2423元(含税)」——不能直接当每股派现累加。
    cutoff = pd.Timestamp.now() - pd.Timedelta(days=400)
    per10, last = 0.0, None
    for row in rows:
        d = row.get("EX_DIVIDEND_DATE") or ""
        v = row.get("PRETAX_BONUS_RMB")
        prog = str(row.get("ASSIGN_PROGRESS") or "")
        if not d or v is None:
            continue
        if prog and "实施" not in prog:      # 只认已实施方案，排除预案/取消
            continue
        ts = pd.to_datetime(str(d)[:10], errors="coerce")
        if pd.isna(ts):
            continue
        last = last or str(d)[:10]
        if ts >= cutoff:
            try:
                per10 += float(v)
            except (TypeError, ValueError):
                pass
    out = {"dps_ttm": round(per10 / 10.0, 4),     # 每股派现（元）
           "per10_sum": round(per10, 4),
           "last_ex_date": last}
    with open(path, "w", encoding="utf-8") as f:
        json.dump(out, f, ensure_ascii=False)
    return out


# ---------------------------------------------------------------------------
# 5) 基金档案：天天基金
# ---------------------------------------------------------------------------
def fetch_fund_profile(code: str, ttl_days: int = 2) -> dict:
    """场外基金档案：规模/经理/收益/仓位/重仓股。失败返回 {}。"""
    path = os.path.join(FUND_DIR, f"{code}.json")
    if os.path.exists(path):
        if (time.time() - os.path.getmtime(path)) / 86400 < ttl_days:
            try:
                with open(path, encoding="utf-8") as f:
                    return json.load(f)
            except Exception:
                pass
    try:
        txt = fetcher._get(f"https://fund.eastmoney.com/pingzhongdata/{code}.js",
                           min_gap=0.5).text
        if not txt or "Data_netWorthTrend" not in txt:
            raise RuntimeError("空响应")
    except Exception:
        if os.path.exists(path):
            try:
                with open(path, encoding="utf-8") as f:
                    return json.load(f)
            except Exception:
                pass
        return {}

    def _num(pat):
        m = re.search(pat, txt)
        if not m:
            return None
        try:
            return float(m.group(1))
        except (TypeError, ValueError):
            return None

    def _json_var(name):
        # 注意：不能用非贪婪的 \{.*?\} —— 对象内含嵌套 {} 会在第一个 } 处截断，
        # 导致 json.loads 失败（Data_fluctuationScale 就是这么解析出空的）。
        # 这些变量值内部不含分号，取到第一个 ";" 为止最稳。
        m = re.search(rf"var\s+{name}\s*=\s*(.*?);", txt, re.S)
        if not m:
            return None
        try:
            return json.loads(m.group(1).strip())
        except Exception:
            return None

    name_m = re.search(r'var\s+fS_name\s*=\s*"([^"]+)"', txt)
    prof = {
        "code": code,
        "name": name_m.group(1) if name_m else code,
        "ret_1y": _num(r'var\s+syl_1n\s*=\s*"(-?[\d.]+)"'),      # 近1年 %
        "ret_6m": _num(r'var\s+syl_6y\s*=\s*"(-?[\d.]+)"'),      # 近6月 %
        "ret_3m": _num(r'var\s+syl_3y\s*=\s*"(-?[\d.]+)"'),      # 近3月 %
        "ret_1m": _num(r'var\s+syl_1y\s*=\s*"(-?[\d.]+)"'),      # 近1月 %
        "rate_now": _num(r'var\s+fund_Rate\s*=\s*"(-?[\d.]+)"'),   # 现费率 %
        "rate_src": _num(r'var\s+fund_sourceRate\s*=\s*"(-?[\d.]+)"'),
        "fetch_date": date.today().isoformat(),
    }
    # 规模变化：series 是对象列表 [{"y": 规模(亿), "mom": "环比%"}, ...]
    scale = _json_var("Data_fluctuationScale")
    if isinstance(scale, dict):
        cats = scale.get("categories") or []
        ser = scale.get("series") or []
        ys = [s.get("y") for s in ser if isinstance(s, dict)
              and isinstance(s.get("y"), (int, float))]
        if ys:
            prof["scale"] = float(ys[-1])
            prof["scale_prev"] = float(ys[-2]) if len(ys) > 1 else None
            prof["scale_trend"] = ys[-6:]
            prof["scale_cats"] = cats[-6:]
            if len(ys) >= 2 and ys[-2]:
                prof["scale_chg"] = round(ys[-1] / ys[-2] - 1, 4)
            if len(ys) >= 3 and ys[-3]:
                prof["scale_chg_2q"] = round(ys[-1] / ys[-3] - 1, 4)
    # 股票仓位测算（最近一期 %）
    pos = _json_var("Data_fundSharesPositions")
    if pos:
        try:
            prof["stock_position"] = float(pos[-1][1])
            prof["position_trend"] = [float(x[1]) for x in pos[-6:]
                                      if isinstance(x[1], (int, float))]
        except (IndexError, TypeError, ValueError):
            pass
    # 资产配置（最近一期股票占净比）
    alloc = _json_var("Data_assetAllocation")
    if isinstance(alloc, dict):
        for s in alloc.get("series") or []:
            if s.get("name") == "股票占净比" and s.get("data"):
                prof["asset_stock_ratio"] = s["data"][-1]
                prof["stock_ratio_trend"] = s["data"]
            if s.get("name") == "债券占净比" and s.get("data"):
                prof["asset_bond_ratio"] = s["data"][-1]
            if s.get("name") == "现金占净比" and s.get("data"):
                prof["asset_cash_ratio"] = s["data"][-1]
    # 现任经理
    mgr = _json_var("Data_currentFundManager")
    if isinstance(mgr, list) and mgr:
        m0 = mgr[0] or {}
        prof["manager"] = m0.get("name")
        prof["manager_star"] = (m0.get("star") or [0])[0] if isinstance(
            m0.get("star"), list) else m0.get("star")
        prof["manager_years"] = m0.get("workTime")
        prof["manager_size"] = m0.get("fundSize")
    # 同类评价（110011 实测为空，保留容错）
    ev = _json_var("Data_performanceEvaluation")
    if isinstance(ev, dict) and ev.get("data"):
        prof["evaluation"] = ev.get("avr")
        prof["evaluation_items"] = [
            {"label": d.get("name"), "value": d.get("value")}
            for d in ev["data"] if isinstance(d, dict)]
    # 持仓股票：格式为「代码+市场后缀」，如 "6005191"(沪) "0008580"(深) "00700116"(港)
    codes_m = re.search(r'var\s+stockCodes\s*=\s*(\[.*?\]);', txt, re.S)
    if codes_m:
        try:
            raw = json.loads(codes_m.group(1))
            parsed, seen = [], set()
            for s in raw:
                s = str(s)
                if len(s) < 7:
                    continue
                mk, code = None, None
                for suf, m in (("116", "hk"), ("105", "us"), ("106", "us"),
                               ("107", "us"), ("1", "cn"), ("0", "cn")):
                    if s.endswith(suf) and len(s) - len(suf) in (5, 6):
                        mk = m
                        code = s[: len(s) - len(suf)]
                        if mk == "cn" and len(code) != 6:
                            mk = None
                        break
                if not mk:
                    continue
                key = (mk, code)
                if key in seen:
                    continue
                seen.add(key)
                parsed.append({"code": code, "market": mk})
            prof["holdings"] = parsed[:10]
        except Exception:
            pass
    try:
        with open(path, "w", encoding="utf-8") as f:
            json.dump(prof, f, ensure_ascii=False)
    except Exception:
        pass
    return prof


# ---------------------------------------------------------------------------
# 派生指标：把原始财务行编成「质量 / 成长 / 现金流 / 安全」四组
# ---------------------------------------------------------------------------
def _annual_rows(rows: list) -> list:
    """只保留年报行，按报告期升序。"""
    out = []
    for r in rows:
        if str(r.get("REPORT_TYPE") or "").find("年报") >= 0:
            out.append(r)
    out.sort(key=lambda r: str(r.get("REPORT_DATE") or ""))
    return out


def _ttm_eps(rows: list) -> float | None:
    """近似 TTM EPS：用最近一期累计 EPS 与其同比增速外推。

    季报 EPSJB 是累计值，直接当 TTM 会低估；这里做保守近似，
    仅用于「PE 水平判断」，不参与分位计算（分位只用年报口径）。
    """
    for r in sorted(rows, key=lambda x: str(x.get("REPORT_DATE") or ""), reverse=True):
        eps = r.get("EPSJB")
        if eps is None:
            continue
        rt = str(r.get("REPORT_TYPE") or "")
        if "年报" in rt:
            return float(eps)
        # 非年报：按营收同比把累计 EPS 折成 TTM 近似
        tz = r.get("PARENTNETPROFITTZ")
        factor = 1.0
        if tz is not None:
            try:
                factor = max(0.5, min(2.0, 1 + float(tz) / 100.0))
            except (TypeError, ValueError):
                factor = 1.0
        return float(eps) * factor
    return None


def derived_metrics(rows: list) -> dict:
    """从 F10 原始行派生估值/质量/成长/现金流/安全指标。全部容错，缺项为 None。"""
    if not rows:
        return {}
    rows = sorted(rows, key=lambda r: str(r.get("REPORT_DATE") or ""), reverse=True)
    last = rows[0]
    ann = _annual_rows(rows)

    def g(r, k):
        v = r.get(k) if r else None
        try:
            return float(v) if v is not None else None
        except (TypeError, ValueError):
            return None

    def avg(vals):
        v = [x for x in vals if x is not None]
        return sum(v) / len(v) if v else None

    def std(vals):
        v = [x for x in vals if x is not None]
        if len(v) < 2:
            return None
        m = sum(v) / len(v)
        return (sum((x - m) ** 2 for x in v) / (len(v) - 1)) ** 0.5

    # 质量：ROE 近 3 年（不足则取现有）
    roes = [g(r, "ROEJQ") for r in ann[-3:]] or [g(r, "ROEJQ")]
    gm = [g(r, "XSMLL") for r in ann[-3:]] or [g(r, "XSMLL")]
    m = {
        "report_date": str(last.get("REPORT_DATE") or "")[:10],
        "report_type": last.get("REPORT_TYPE"),
        "eps": g(last, "EPSJB"),
        "eps_ttm": _ttm_eps(rows),
        "bps": g(last, "BPS"),
        "roe": g(last, "ROEJQ"),
        "roe_avg3": avg(roes),
        "roe_min3": min([x for x in roes if x is not None], default=None),
        "roe_std3": std(roes),
        "gross_margin": g(last, "XSMLL"),
        "gross_margin_avg3": avg(gm),
        "net_margin": g(last, "XSJLL"),
        "rev": g(last, "TOTALOPERATEREVE"),
        "profit": g(last, "PARENTNETPROFIT"),
        "rev_yoy": g(last, "TOTALOPERATEREVETZ"),
        "profit_yoy": g(last, "PARENTNETPROFITTZ"),
        "ocf_ps": g(last, "MGJYXJJE"),
        "ocf_to_rev": g(last, "JYXJLYYSR"),
        "debt_ratio": g(last, "ZCFZL"),
        "current_ratio": g(last, "LD"),
        "quick_ratio": g(last, "SD"),
        "n_periods": len(rows),
        "n_annual": len(ann),
    }
    # 利润含金量 = 每股经营现金流 / 每股收益
    if m["ocf_ps"] is not None and m["eps"] and abs(m["eps"]) > 1e-9:
        m["ocf_to_profit"] = round(m["ocf_ps"] / m["eps"], 3)
    else:
        m["ocf_to_profit"] = None
    # 3 年营收/净利 CAGR（用年报口径）
    if len(ann) >= 4:
        for key, src in (("rev_cagr3", "TOTALOPERATEREVE"),
                         ("profit_cagr3", "PARENTNETPROFIT")):
            a, b = g(ann[-1], src), g(ann[-4], src)
            if a and b and a > 0 and b > 0:
                m[key] = round((a / b) ** (1 / 3) - 1, 4)
    return m


def valuation_percentile(kline_raw: pd.DataFrame, rows: list,
                         years: int = 4) -> dict:
    """用「不复权价 + 各期每股净资产」重建历史 PB 序列，算当前分位。

    方法透明说明：PB_t = 收盘价_t / BPS(该时点之前最近一期报告)，
    BPS 用东财 F10 历史 BPS 阶跃拼接。PE 分位因季报 EPS 为累计值、
    TTM 重建误差大，故只给 PB 分位 + 当前 PE 绝对值，不硬算 PE 分位。
    """
    out = {"pb_pct": None, "pb_median": None, "px_pct": None,
           "years": years, "method": "PB=BPS阶跃×不复权价"}
    if kline_raw is None or kline_raw.empty:
        return out
    k = kline_raw.copy()
    k["date"] = pd.to_datetime(k["date"])
    k = k.sort_values("date")
    cutoff = k["date"].max() - pd.Timedelta(days=int(365.25 * years))
    k = k[k["date"] >= cutoff]
    if len(k) < 60:
        return out
    # 价格分位（相对历史区间的位置），作为「估值分位」的粗代理
    lo, hi = float(k["close"].min()), float(k["close"].max())
    cur = float(k["close"].iloc[-1])
    if hi > lo:
        out["px_pct"] = round((cur - lo) / (hi - lo), 3)
    bps_rows = [(str(r.get("REPORT_DATE") or "")[:10], r.get("BPS"))
                for r in (rows or []) if r.get("BPS")]
    bps_rows = sorted([(d, float(v)) for d, v in bps_rows if d], key=lambda x: x[0])
    if len(bps_rows) >= 3:
        series = []
        for _, row in k.iterrows():
            d = row["date"].strftime("%Y-%m-%d")
            cand = [v for dd, v in bps_rows if dd <= d]
            if not cand or not cand[-1] or cand[-1] <= 0:
                continue
            series.append(float(row["close"]) / cand[-1])
        if len(series) >= 60:
            s = pd.Series(series)
            out["pb_median"] = round(float(s.median()), 3)
            out["pb_pct"] = round(float((s <= series[-1]).mean()), 3)
            out["pb_min"] = round(float(s.min()), 3)
            out["pb_max"] = round(float(s.max()), 3)
    return out


if __name__ == "__main__":
    v = fetch_valuations([("600519", "cn"), ("000001", "cn"), ("510300", "etf")])
    for k, r in v.items():
        print(k, {kk: r[kk] for kk in ("name", "price", "pe_ttm", "pb",
                                       "total_mcap", "turnover")})
    rows = fetch_financial("600519", "cn")
    print("财务期数:", len(rows))
    print(json.dumps(derived_metrics(rows), ensure_ascii=False, indent=2))
    k = fetch_kline_raw("600519", "cn", 900)
    print("不复权K线:", len(k))
    print(json.dumps(valuation_percentile(k, rows), ensure_ascii=False))
    print(json.dumps(fetch_dividend("600519", "cn"), ensure_ascii=False))
    print(json.dumps(fetch_fund_profile("110011"), ensure_ascii=False)[:800])
