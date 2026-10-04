# -*- coding: utf-8 -*-
"""数据抓取模块（多源容灾版）。

源分工：
- A股实时列表：腾讯财经排行接口（按涨跌幅排序，天然得到低买候选池）
- ETF/港股/美股列表：东方财富 clist（按成交额取头部，每日磁盘缓存）
- 全市场日K线：腾讯 ifzq（前复权）
- 场外基金净值：天天基金 pingzhongdata

网络策略：限速 + 直连优先、代理兜底、curl_cffi 浏览器指纹再兜底。
"""
import json
import os
import re
import time
from datetime import date, datetime

import pandas as pd
import requests

import store

HEADERS = {"User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
                         "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0 Safari/537.36",
           "Referer": "https://gu.qq.com/"}
NO_PROXY = {"http": None, "https": None}
ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
CACHE_DIR = os.path.join(ROOT, "state", "cache")
os.makedirs(CACHE_DIR, exist_ok=True)

# 行情复用窗口（小时）。同一交易日内重跑会命中数据库、不再发请求；
# 跨日或跨过收盘时点自动失效。详见 store.kline_is_fresh。
KLINE_TTL_HOURS = float(os.environ.get("QUANT_KLINE_TTL_H", 4))

# 本次进程内的「降级取数」记录：网络抓取失败后改用库中旧数据的那些标的。
# 存在的意义是**让它进报告**——静默回退旧数据会让报告看起来一切正常，
# 而里面的价格其实是几天前的。run.py 会 drain 走并写进 data_health。
_STALE_FALLBACKS = []

_last_req_ts = 0.0


def note_fallback(market: str, code: str, reason: str, last_date: str = ""):
    _STALE_FALLBACKS.append({"market": str(market), "code": str(code),
                             "reason": reason, "db_last_date": str(last_date or "")})


def configure(kline_ttl_hours=None):
    """由 run.py 用 config.json 覆盖行情复用窗口。环境变量优先。"""
    global KLINE_TTL_HOURS
    if kline_ttl_hours is not None and "QUANT_KLINE_TTL_H" not in os.environ:
        try:
            KLINE_TTL_HOURS = float(kline_ttl_hours)
        except (TypeError, ValueError):
            pass


def drain_fallbacks() -> list:
    """取走并清空降级记录（由报告层调用，避免跨运行重复披露）。"""
    out = list(_STALE_FALLBACKS)
    _STALE_FALLBACKS.clear()
    return out



class FetchBudget:
    """按「累计抓取耗时」熔断的数据源保护。

    为什么按时长而不是按失败次数
    ----------------------------
    限流是**瞬时**的：连续几只失败之后往往又能取到。按连续失败次数熔断会把限流误判成
    源故障，直接掐掉整个市场的行情——实测后果是某次运行里滚动面板从 63 只掉到 6 只，
    报告数字整体失真，且因为是「跳过」而非「报错」，不会有任何异常提示。

    真正要防的是「源不可用导致每只标的都耗尽重试预算」：单个标的要依次试 3 个源
    （各 15s 超时）再重试 3 次（含退避 ≈18s），最坏 ≈153s；12 只标的就能把一次运行
    拖成半小时的静默等待。这是**时间**问题，所以用累计耗时做判据：

    - 健康源：12~25 只标的通常 3~12s，远低于预算，永不触发；
    - 源不可用：几只标的后就超预算，立即停止并在日志里说明跳过原因。

    超预算只影响「还要不要继续抓」，不影响已经抓到的数据。
    """

    def __init__(self, budget_sec: float, name: str = "", log=None):
        self.budget = float(budget_sec)
        self.name = name
        self.log = log
        self.spent = 0.0
        self.tripped = False

    def guard(self) -> bool:
        """已熔断则返回 True，调用方应直接跳过、不再发起请求。"""
        return self.tripped

    def charge(self, seconds: float) -> bool:
        """记入一次抓取耗时；超预算则熔断并留痕。返回是否已熔断。"""
        self.spent += max(0.0, float(seconds))
        if not self.tripped and self.spent > self.budget:
            self.tripped = True
            msg = (f"  ⚠ {self.name} 抓取累计耗时 {self.spent:.0f}s 已超预算 "
                   f"{self.budget:.0f}s，判定数据源异常缓慢或不可用："
                   f"本市场剩余标的直接跳过（已取到的数据不受影响），"
                   f"避免长时间静默等待")
            (self.log or print)(msg)
        return self.tripped

    def reset(self):
        self.spent, self.tripped = 0.0, False

    def state(self) -> dict:
        return {"name": self.name, "spent": round(self.spent, 1),
                "budget": self.budget, "tripped": self.tripped}


def _raw_get(url, params=None, timeout=15):
    """单次请求：直连 -> 系统代理 -> curl_cffi 浏览器指纹。"""
    # 1) 直连
    try:
        s = requests.Session()
        s.trust_env = False
        r = s.get(url, params=params, headers=HEADERS, timeout=timeout)
        if r.status_code == 200 and r.text.strip():
            return r
    except Exception:
        pass
    # 2) 系统代理
    try:
        r = requests.get(url, params=params, headers=HEADERS, timeout=timeout)
        if r.status_code == 200 and r.text.strip():
            return r
    except Exception:
        pass
    # 3) curl_cffi（绕 TLS 指纹风控）
    try:
        from curl_cffi import requests as creq
        r = creq.get(url, params=params, headers=HEADERS, timeout=timeout,
                     impersonate="chrome")
        if r.status_code == 200 and r.text.strip():
            return r
    except Exception:
        pass
    return None


def _get(url, params=None, retries=3, timeout=15, min_gap=0.5):
    """带限速与重试的 GET。"""
    global _last_req_ts
    last_err = None
    for i in range(retries):
        gap = time.time() - _last_req_ts
        if gap < min_gap:
            time.sleep(min_gap - gap)
        _last_req_ts = time.time()
        r = _raw_get(url, params, timeout)
        if r is not None:
            return r
        last_err = f"retry{i+1}"
        time.sleep(min(30, 3 * (i + 1)))
    raise RuntimeError(f"请求失败({last_err}): {url}")


def _cache_path(name):
    return os.path.join(CACHE_DIR, f"{date.today().isoformat()}_{name}.json")


def _cached_json(name, fetch_fn, max_age_min=240):
    """当日缓存，默认 4 小时内复用（列表数据一天更新几次足够）。"""
    p = _cache_path(name)
    if os.path.exists(p):
        age = (time.time() - os.path.getmtime(p)) / 60
        if age < max_age_min:
            with open(p, encoding="utf-8") as f:
                return json.load(f)
    data = fetch_fn()
    with open(p, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False)
    return data


# ---------------------------------------------------------------------------
# A股列表：腾讯排行接口（direct=up -> 跌幅从大到小）
# ---------------------------------------------------------------------------
def fetch_cn_rank(top: int = 400) -> pd.DataFrame:
    """A股跌幅榜前 top 名（含代码/名称/最新价/涨跌幅/成交额/量比）。"""
    def _fetch():
        rows = []
        for off in range(0, top, 100):
            p = {"board_code": "aStock", "sort_type": "PriceRatio",
                 "direct": "up", "offset": off, "count": 100}
            j = _get("https://proxy.finance.qq.com/cgi/cgi-bin/rank/hs/getBoardRankList",
                     p).json()
            items = (j.get("data") or {}).get("rank_list") or []
            rows.extend(items)
            if len(items) < 100:
                break
        return rows

    rows = _cached_json("cn_rank", _fetch, max_age_min=30)
    df = pd.DataFrame(rows)
    if df.empty:
        return df
    out = pd.DataFrame({
        "code": df["code"].str.replace("sh", "").str.replace("sz", "").str.replace("bj", ""),
        "name": df["name"],
        "price": pd.to_numeric(df["zxj"], errors="coerce"),
        "pct": pd.to_numeric(df["zdf"], errors="coerce"),
        "amount": pd.to_numeric(df["turnover"], errors="coerce") * 1e4,  # 万->元
        "volume_ratio": pd.to_numeric(df["lb"], errors="coerce"),
    })
    out = out.dropna(subset=["price", "pct"])
    out["market"] = "cn"
    out = out.drop_duplicates(subset=["code"]).reset_index(drop=True)
    # 榜单快照落库：按 (market, board, snap_date, code) 幂等。
    # snap_date 是**抓取日**（墙钟）而非交易日——它记录的是「这一刻观测到的榜单」，
    # 周末补跑时写的就是周末，语义上不冒充交易日。
    store.upsert_rank_snapshot("cn", "pct_down", date.today().isoformat(), out,
                               src="tencent_rank_pct")
    return out


# ---------------------------------------------------------------------------
# ETF/港股/美股列表：东财 clist 按成交额取头部
# ---------------------------------------------------------------------------
_LIST_URL = "https://push2.eastmoney.com/api/qt/clist/get"
_MARKETS = {
    "etf": "b:MK0021,b:MK0022,b:MK0023,b:MK0024",
    "hk": "m:116",
    "us": "m:105,m:106,m:107",
}
_LIST_FIELDS = "f2,f3,f5,f6,f12,f13,f14"


def _em_blocked() -> bool:
    """东财限流封锁标记（30 分钟 TTL），封锁期内直接走降级源。"""
    p = os.path.join(CACHE_DIR, "em_blocked")
    if os.path.exists(p):
        if time.time() - os.path.getmtime(p) < 1800:
            return True
        os.remove(p)
    return False


def _em_mark_blocked():
    with open(os.path.join(CACHE_DIR, "em_blocked"), "w") as f:
        f.write(datetime.now().isoformat())


def fetch_spot_list(market: str, pages: int = 2) -> pd.DataFrame:
    """按成交额降序取某市场头部标的，逐档降级直到拿到数据。

    为什么把「档」显式写成有序列表而不是一串 if
    ------------------------------------------
    原来的实现是「东财 → 腾讯」两档写死的 if。问题是：**腾讯那一档不是等价替换**，
    它读的是 `universe_fallback.json` 里的静态核心池（ETF 64 只、港股 48 只），
    而全市场有 1693 只 ETF、2811 只港股。东财在本机成功率只有 9%~15%，
    也就是说**多数日子「跌幅居前的活跃标的」是在 64 只固定名单里选出来的**——
    报告看起来正常，机会集合却被动缩小了一个数量级。

    新浪那两条接口给的是全量，且实测可达，所以补进降级链（Step 4）。
    写成有序列表的另一个好处：调整优先级是改配置，不是改逻辑——
    `config.json` 的 `datasource.spot_tier_order` 可以把新浪提到腾讯前面
    （那会改变每日候选池，属于策略口径变更，需要单独确认后再动）。

    各档的成败与耗时都写 `data_health`：既能回答「今天这批 ETF 是谁给的」，
    也能回答「第三档到底有没有在用」——不写画像的话，一个永远不生效的降级
    和没接一样，而且看不出来。
    """
    df = pd.DataFrame()
    served_by = ""
    for tier in _spot_tier_names(market):
        df = _spot_tier(market, tier, pages)
        if not df.empty:
            served_by = _TIER_SRC[tier]
            break
    if not df.empty:
        store.upsert_rank_snapshot(market, "amount", date.today().isoformat(),
                                   df, src=served_by)
    return df


# 各档的展示名／`data_health` 源名。前两档沿用原名，避免历史画像断档。
# 档位名 → 落库/画像里的源名。**同一个档只能有一个名字**：此前 tencent 在
# `rank_snapshot.src` 里叫 `tencent_fallback`、在 `data_health` 里叫
# `tencent_quote_list`，于是「按源回溯」要记得两个名字，跨表 join 直接对不上。
_TIER_SRC = {"eastmoney": "eastmoney_clist", "tencent": "tencent_quote_list",
             "sina": "sina_spot_list"}
# 每个市场的档位顺序（默认值）。美股**不放新浪**：akshare 的新浪美股现货要逐页抓
# 911 页（实测预热 136/911 用了 69s），全量不可接受；不放进链里也就不会在画像里
# 留下一条永远失败的假记录。
_SPOT_TIERS = {
    "etf": ("eastmoney", "tencent", "sina"),
    "hk": ("eastmoney", "tencent", "sina"),
    "us": ("eastmoney", "tencent"),
}


def configure_spot_tiers(order: dict = None):
    """由 config.json 覆盖档位顺序。只接受已知档名，避免拼错后静默少一档。"""
    global _SPOT_TIERS
    if not order:
        return
    valid = set(_TIER_SRC)
    for mkt, seq in (order or {}).items():
        if mkt not in _SPOT_TIERS:
            continue
        seq = tuple(t for t in (seq or []) if t in valid)
        if seq:
            _SPOT_TIERS[mkt] = seq


def _spot_tier_names(market: str) -> tuple:
    return _SPOT_TIERS.get(market, ("eastmoney", "tencent"))


def _spot_tier(market: str, tier: str, pages: int = 2, record: bool = True,
               ignore_breaker: bool = False) -> pd.DataFrame:
    """单档抓取：返回该档的结果（可能为空），成败与耗时写 `data_health`。

    每档都**自己吞异常**：一档挂掉必须让下一档有机会，而不是把整条链带崩。

    `ignore_breaker=True` 只给探针用：探针要问「此刻通不通」，
    带着熔断状态看等于自问自答。**它是「忽略」而不是「清除」**——
    探针不该改生产状态（此前实现是删掉熔断标记文件，等于探完一次
    就让下一次真实运行多花 20 秒去撞同一个墙）。
    """
    t0 = time.time()
    if tier == "eastmoney":
        if not ignore_breaker and _em_blocked():
            if record:
                store.record_health("eastmoney_clist", market, False, 0.0, 0,
                                    "熔断标记生效中，直接走降级源")
            return pd.DataFrame()
        try:
            df = _fetch_spot_list_em(market, pages, use_cache=record)
        except Exception:
            df = pd.DataFrame()
        # 探针模式下**既不看熔断状态、也不改它**：`_em_mark_blocked` 会刷新标记的
        # mtime，反复跑探针等于把 30 分钟的熔断窗口一直续期——一个只读诊断不该
        # 有这种能力（实测就是这么发现的：断言「标记内容未被改写」红了）。
        if df.empty and not ignore_breaker:
            _em_mark_blocked()
        if record:
            store.record_health("eastmoney_clist", market, not df.empty,
                                time.time() - t0, len(df),
                                "" if not df.empty else "空返回，已置 30 分钟熔断标记")
        return df
    if tier == "tencent":
        try:
            df = _fetch_spot_list_tencent(market)
        except Exception:
            df = pd.DataFrame()
        if record:
            store.record_health("tencent_quote_list", market, not df.empty,
                                time.time() - t0, len(df))
        return df
    if tier == "sina":
        # 函数内导入：`fetcher` 与 `datasources` 会互相引用，模块级导入会成环。
        # 放在调用点既避开环，也不会让主流程为「用不到的一档」付导入成本。
        from datasources import sina_source as sina
        return sina.spot_list(market, log=(print if record else (lambda *_: None)))
    return pd.DataFrame()


def spot_probe(pages: int = 2) -> dict:
    """逐档实测各市场现货链（**只读，不落库**）。

    刻意**每档都跑一遍**、不提前 break：降级链最常见的失效方式是
    「最后一档从来没被执行过」——腾讯一直有数据，新浪那档就永远测不到，
    于是「加了」和「没加」在观测上完全一样。全跑一遍才能回答两件事：
    每一档各自通不通，以及**这条链最终会由谁供数**。
    """
    out = {}
    for market in ("etf", "hk", "us"):
        rows = []
        for tier in _spot_tier_names(market):
            t0 = time.time()
            df = _spot_tier(market, tier, pages, record=False,
                            ignore_breaker=True)
            rows.append({"tier": tier, "rows": int(len(df)),
                         "sec": round(time.time() - t0, 2),
                         "empty": bool(df.empty)})
        served = next((r["tier"] for r in rows if not r["empty"]), None)
        out[market] = {"tiers": rows, "served_by": served,
                       "tier_order": list(_spot_tier_names(market))}
    return out


def _fetch_spot_list_em(market: str, pages: int = 2,
                        use_cache: bool = True) -> pd.DataFrame:
    """东财 clist。`use_cache=False` 时绕开 30 分钟磁盘缓存，用于探针
    ——探针要测的是「此刻网络通不通」，命中缓存的话 rows>0 什么也证明不了。"""
    def _fetch():
        rows, pn = [], 1
        while pn <= pages:
            params = {"pn": pn, "pz": 400, "po": 1, "np": 1, "fltt": 2, "invt": 2,
                      "fid": "f6", "fs": _MARKETS[market], "fields": _LIST_FIELDS}
            r = _get(_LIST_URL, params).json()
            data = r.get("data") or {}
            diff = data.get("diff") or []
            rows.extend(diff)
            total = data.get("total", 0)
            if pn * 400 >= total or not diff:
                break
            pn += 1
        return rows

    rows = _cached_json(f"spot_{market}", _fetch, max_age_min=30) if use_cache \
        else _fetch()
    if not rows:
        return pd.DataFrame()
    df = pd.DataFrame(rows).rename(columns={
        "f2": "price", "f3": "pct", "f5": "volume", "f6": "amount",
        "f12": "code", "f13": "mkt_id", "f14": "name"})
    for c in ("price", "pct", "amount"):
        df[c] = pd.to_numeric(df[c], errors="coerce")
    df = df.dropna(subset=["price", "pct"])
    df = df[df["price"] > 0]
    return df.drop_duplicates(subset=["code"]).reset_index(drop=True)


def _fetch_spot_list_tencent(market: str) -> pd.DataFrame:
    """降级源：腾讯批量行情 + 核心标的池。"""
    uni_path = os.path.join(ROOT, "universe_fallback.json")
    with open(uni_path, encoding="utf-8") as f:
        codes = json.load(f).get(market, [])
    if not codes:
        return pd.DataFrame()
    rows = []
    for i in range(0, len(codes), 40):
        chunk = codes[i:i + 40]
        # 美股批量行情用 usCODE（不带交易所后缀）
        q = ",".join(f"us{c}" if market == "us" else _tx_code(c, market)
                     for c in chunk)
        txt = _get("https://qt.gtimg.cn/q=" + q, min_gap=0.4).text
        for m in re.finditer(r'v_([^=]+)="([^"]*)"', txt):
            fl = m.group(2).split("~")
            if len(fl) < 38:
                continue
            try:
                rows.append({"code": fl[2], "name": fl[1],
                             "price": float(fl[3]), "pct": float(fl[32]),
                             "amount": float(fl[37]) * 1e4, "mkt_id": None})
            except (ValueError, IndexError):
                continue
    df = pd.DataFrame(rows)
    if df.empty:
        return df
    return df.drop_duplicates(subset=["code"]).reset_index(drop=True)


# ---------------------------------------------------------------------------
# A股全市场按成交额排序（长期价值轨的选股池来源）
# ---------------------------------------------------------------------------
def fetch_cn_rank_by_amount(top: int = 600) -> pd.DataFrame:
    """A股按成交额降序取头部（含 PE(TTM)/流通市值/换手率/量比）。

    2026-09-21 实测：腾讯排行接口 sort_type="turnover" 生效，
    且返回体自带 pe_ttm 与 ltsz（流通市值，亿元），
    不必依赖被限流的东财 clist，也省掉逐只补估值的请求。
    """
    def _fetch():
        rows = []
        for off in range(0, top, 100):
            p = {"board_code": "aStock", "sort_type": "turnover",
                 "direct": "down", "offset": off, "count": 100}
            j = _get("https://proxy.finance.qq.com/cgi/cgi-bin/rank/hs/getBoardRankList",
                     p).json()
            items = (j.get("data") or {}).get("rank_list") or []
            rows.extend(items)
            if len(items) < 100:
                break
        return rows

    rows = _cached_json("cn_rank_amount", _fetch, max_age_min=60)
    df = pd.DataFrame(rows)
    if df.empty:
        return df
    out = pd.DataFrame({
        "code": df["code"].astype(str).str.replace("sh", "", regex=False)
                 .str.replace("sz", "", regex=False).str.replace("bj", "", regex=False),
        "name": df["name"],
        "price": pd.to_numeric(df.get("zxj"), errors="coerce"),
        "pct": pd.to_numeric(df.get("zdf"), errors="coerce"),
        "amount": pd.to_numeric(df.get("turnover"), errors="coerce") * 1e4,
        "pe_ttm": pd.to_numeric(df.get("pe_ttm"), errors="coerce"),
        "float_mcap": pd.to_numeric(df.get("ltsz"), errors="coerce"),
        "turnover_ratio": pd.to_numeric(df.get("hsl"), errors="coerce"),
        "volume_ratio": pd.to_numeric(df.get("lb"), errors="coerce"),
    })
    out = out.dropna(subset=["price", "pct"])
    out = out[out["price"] > 0]
    out["market"] = "cn"
    out = out.drop_duplicates(subset=["code"]).reset_index(drop=True)
    store.upsert_rank_snapshot("cn", "amount", date.today().isoformat(), out,
                               src="tencent_rank_turnover")
    return out


# ---------------------------------------------------------------------------
# 单标的实时价（腾讯批量行情，供迭代复盘）
# ---------------------------------------------------------------------------
def fetch_quote(code: str, market: str = "cn", mkt_id=None) -> float | None:
    """返回最新价，失败返回 None。"""
    try:
        # 美股批量行情用 usCODE（不带后缀）；K线才需要 .OQ/.N 后缀
        tx = f"us{code}" if market == "us" else _tx_code(code, market, mkt_id)
        r = _get("https://qt.gtimg.cn/q=" + tx, min_gap=0.3)
        txt = r.text
        m = re.search(r'"([^"]+)"', txt)
        if not m:
            return None
        fields = m.group(1).split("~")
        return float(fields[3])
    except Exception:
        return None


def fetch_quotes_batch(items, chunk: int = 40) -> dict:
    """批量取最新价，返回 {(market, code): {"price","pct","date"}}。

    items: [(code, market) 或 (code, market, mkt_id), ...]
    腾讯批量行情一次可带数十个代码，按 chunk 分批后请求成本几乎为零，
    因此持仓账本逐日结算不必为每个标的单独发请求。
    """
    out = {}
    todo = []
    for it in items or []:
        code, market = str(it[0]), it[1]
        mkt_id = it[2] if len(it) > 2 else None
        if market == "fund":
            continue
        tx = ("us" + re.sub(r"\.(OQ|N|A)$", "", code, flags=re.I)
              if market == "us" else _tx_code(code, market, mkt_id))
        todo.append((tx, market, code))
    for i in range(0, len(todo), chunk):
        batch = todo[i:i + chunk]
        q = ",".join(b[0] for b in batch)
        try:
            txt = _get("https://qt.gtimg.cn/q=" + q, min_gap=0.3).text
        except Exception:
            continue
        by_tx = {b[0].upper(): (b[1], b[2]) for b in batch}
        for m in re.finditer(r'v_([^=]+)="([^"]*)"', txt):
            key, payload = m.group(1).upper(), m.group(2)
            fl = payload.split("~")
            if len(fl) < 5:
                continue
            mkt, code = by_tx.get(key, (None, None))
            if mkt is None:
                for b in batch:  # 兜底：用返回的纯代码匹配
                    if len(fl) > 2 and b[2].upper() == fl[2].upper():
                        mkt, code = b[1], b[2]
                        break
            if mkt is None:
                continue
            try:
                price = float(fl[3])
            except (ValueError, IndexError):
                continue
            if price <= 0:
                continue
            pct = 0.0
            if len(fl) > 32:
                try:
                    pct = float(fl[32])
                except ValueError:
                    pct = 0.0
            stamp = fl[30] if len(fl) > 30 else ""
            date_s = (f"{stamp[0:4]}-{stamp[4:6]}-{stamp[6:8]}"
                      if len(stamp) >= 8 and stamp[:8].isdigit() else "")
            out[(mkt, str(code))] = {"price": price, "pct": pct, "date": date_s}
    return out


# ---------------------------------------------------------------------------
# 日 K 线：腾讯 ifzq（前复权）
# ---------------------------------------------------------------------------
_KLINE_URLS = {
    "cn": "https://web.ifzq.gtimg.cn/appstock/app/fqkline/get",
    "etf": "https://web.ifzq.gtimg.cn/appstock/app/fqkline/get",
    "hk": "https://web.ifzq.gtimg.cn/appstock/app/fqkline/get",
    "us": "https://web.ifzq.gtimg.cn/appstock/app/usfqkline/get",
}


def _tx_code(code: str, market: str, mkt_id=None) -> str:
    code = str(code).strip()
    if market == "hk":
        return f"hk{code.zfill(5)}"
    if market == "us":
        code = re.sub(r"\.(OQ|N|A)$", "", code, flags=re.I)  # 剥离已带的后缀
        # 东财 mkt_id: 105=纳斯达克 106=纽交所 107=美交所
        suffix = {105: ".OQ", 106: ".N", 107: ".A"}.get(mkt_id, ".OQ")
        return f"us{code}{suffix}"
    # cn/etf
    if market == "etf":
        return ("sh" if code.startswith(("5", "6")) else "sz") + code
    if code.startswith(("6", "9", "5")):
        return "sh" + code
    if code.startswith(("4", "8")):
        return "bj" + code
    return "sz" + code


def fetch_kline(code: str, market: str = "cn", limit: int = 300, mkt_id=None,
                use_db: bool = True) -> pd.DataFrame:
    """日 K 线（前复权）。返回列: date,open,close,high,low,volume,amount

    数据库复用
    ----------
    抓到的序列会写入 state/quant.db 的 kline 表（adjust='qfq'）。下次调用时，
    若库中数据「新鲜、行数够、且不落后于同市场最新交易日」，直接复用、不再发
    请求——这是本模块最省时间的一处：单次运行要取 60~70 只标的，历史部分
    其实只写一次。同日重跑（调参、重出报告）几乎零网络成本。

    复用判据的四个条件见 `store.kline_is_fresh`，其中最容易被忽略的是
    「跨过当日收盘时点一律失效」：盘中 14:00 抓到的末根是未定盘的实时价，
    盘后若还按时间窗口命中，就等于把盘中价当收盘价写进回测与次日复盘。

    读库时多取一根（`limit + 1`）
    -----------------------------
    上游对 `,,,N,` 实际返回 N+1 根，冷启路径因此会拿到 301 行。若热路径严格
    取 300 行，同一份输入「命中缓存」与「完整重算」就会差一根，面板宽度对不齐。
    多取一根让两条路径形状一致。

    为什么行情**不**像财务那样回退陈旧数据
    ------------------------------------
    抓取失败时这里返回空表，而不是「库里的旧序列」——因为前复权 K 线直接驱动
    当日因子评分与买卖建议，拿几天前的价格算 RSI/BOLL，会输出一个**看上去正常
    但实际基于过期价格**的推荐。宁可让该标的本次缺席（run.py 的 data_health 会
    统计并披露 missing 数量），也不给出基于陈旧价格的建议。
    （不复权序列 `fundamentals.fetch_kline_raw` 反之：它只喂估值分位这类长窗口
    统计，陈旧几天对分位几乎无影响，所以那里允许回退。）
    """
    if use_db and store.enabled() and store.kline_is_fresh(
            market, code, "qfq", ttl_hours=KLINE_TTL_HOURS, min_rows=limit):
        cached = store.load_kline(market, code, "qfq", limit=int(limit) + 1)
        if cached is not None and len(cached):
            return cached

    tx = _tx_code(code, market, mkt_id)
    params = {"param": f"{tx},day,,,{limit},qfq"}
    # retries=1：这是**批量**抓取，外层循环本身就是重试机制（下一个标的往往是好的），
    # 而 _get 默认 retries=3 会让单个失败标的耗尽 3 源×15s + 3 次退避 ≈153s。
    # 把预算花在「多取几个标的」上，比反复重试同一个更有价值；
    # 单只失败退化为该标的本次无数据（外部还有 FetchBudget 兜总时长）。
    try:
        r = _get(_KLINE_URLS[market], params, retries=1).json()
        d = (r.get("data") or {}).get(tx, {})
        klines = d.get("qfqday") or d.get("day") or []
    except Exception as e:
        note_fallback(market, code, f"行情抓取失败，本次无数据: {e}")
        return pd.DataFrame()

    if not klines:
        note_fallback(market, code, "行情源返回空，本次无数据")
        return pd.DataFrame()

    recs = []
    for item in klines:
        # item: [date, open, close, high, low, volume, (amount|info)]
        try:
            recs.append({"date": item[0], "open": float(item[1]), "close": float(item[2]),
                         "high": float(item[3]), "low": float(item[4]),
                         "volume": float(item[5]),
                         "amount": float(item[6]) if len(item) > 6 and
                                   isinstance(item[6], (str, float, int)) and
                                   not item[6].startswith("{") else 0.0})
        except (ValueError, AttributeError):
            continue
    df = pd.DataFrame(recs)
    if df.empty:
        return df
    df["date"] = pd.to_datetime(df["date"])
    if use_db:
        store.upsert_kline(market, code, "qfq", df, src="tencent_ifzq_qfq")
    return df


# ---------------------------------------------------------------------------
# 场外基金净值：天天基金
# ---------------------------------------------------------------------------
def fetch_fund_nav(code: str, limit: int = 120) -> pd.DataFrame:
    txt = _get(f"https://fund.eastmoney.com/pingzhongdata/{code}.js",
               min_gap=0.5).text
    m = re.search(r"Data_netWorthTrend\s*=\s*(\[.*?\]);", txt)
    if not m:
        return pd.DataFrame()
    data = json.loads(m.group(1))
    recs = []
    for item in data[-limit:]:
        recs.append({
            "date": datetime.fromtimestamp(item["x"] / 1000).strftime("%Y-%m-%d"),
            "nav": item["y"],
            "growth": item.get("equityReturn", 0.0),
        })
    df = pd.DataFrame(recs)
    df["date"] = pd.to_datetime(df["date"])
    return df


def fetch_fund_name(code: str) -> str:
    try:
        txt = _get(f"https://fund.eastmoney.com/pingzhongdata/{code}.js").text
        m = re.search(r'fS_name\s*=\s*"([^"]+)"', txt)
        return m.group(1) if m else code
    except Exception:
        return code


if __name__ == "__main__":
    df = fetch_cn_rank(120)
    print(f"A股跌幅榜: {len(df)} 条, 示例:")
    print(df.head(3).to_string())
    for mkt in ("etf", "hk", "us"):
        d = fetch_spot_list(mkt)
        print(f"{mkt}: {len(d)} 条, 示例: {d.iloc[0].to_dict() if len(d) else '无'}")
    print(fetch_kline("600519", "cn").tail(2))
    print(fetch_kline("00700", "hk").tail(1))
    print(fetch_kline("AAPL", "us").tail(1))
