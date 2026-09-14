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

HEADERS = {"User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
                         "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0 Safari/537.36",
           "Referer": "https://gu.qq.com/"}
NO_PROXY = {"http": None, "https": None}
ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
CACHE_DIR = os.path.join(ROOT, "state", "cache")
os.makedirs(CACHE_DIR, exist_ok=True)

_last_req_ts = 0.0


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
    return out.drop_duplicates(subset=["code"]).reset_index(drop=True)


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
    """按成交额降序取某市场头部标的；东财被限流时降级到腾讯核心池。"""
    if not _em_blocked():
        try:
            df = _fetch_spot_list_em(market, pages)
            if not df.empty:
                return df
        except Exception:
            pass
        _em_mark_blocked()
    return _fetch_spot_list_tencent(market)


def _fetch_spot_list_em(market: str, pages: int = 2) -> pd.DataFrame:
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

    rows = _cached_json(f"spot_{market}", _fetch, max_age_min=30)
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


def fetch_kline(code: str, market: str = "cn", limit: int = 300, mkt_id=None) -> pd.DataFrame:
    """日 K 线（前复权）。返回列: date,open,close,high,low,volume,amount"""
    tx = _tx_code(code, market, mkt_id)
    params = {"param": f"{tx},day,,,{limit},qfq"}
    r = _get(_KLINE_URLS[market], params).json()
    d = (r.get("data") or {}).get(tx, {})
    klines = d.get("qfqday") or d.get("day") or []
    if not klines:
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
    df["date"] = pd.to_datetime(df["date"])
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
