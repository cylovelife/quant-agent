# -*- coding: utf-8 -*-
"""中信建投（CSC）SkillHub 数据源适配器。

它填补现有哪些缺口
------------------
现有链路里，A 股财报只有东方财富 F10 一个源，且经常限流；ETF 与指数**完全没有
估值数据**（`_parse_valuation` 对 ETF 明确降级为「无」）；个股也没有任何行业
相对位置信息。这三块正好是中信建投这边有的。

| 本模块函数 | 上游接口 | 用来做什么 |
|---|---|---|
| `fin_key_indicators` | `/info/f10/finance/more?ids=gjzb` | 东财 F10 失败时的财务兜底 |
| `index_list` | `/info/etf/index/list` | 指数估值截面（PE/PB 及分位、ROE、股息率） |
| `index_valuation` | `/info/etf/index/valuation` | 指数估值历史序列（日频） |
| `index_components` | `/info/etf/index/components` | 指数成分股权重 |
| `industry_rank` | `/info/f10/finance/industryRank` | 个股在所属行业的指标排名与行业均值 |
| `company_basic_info` | `/info/f10/menu/basicInfo` | 公司主营业务与简介 |
| `fund_basic_info` | `/strategy/fundEvaluation/fundProduct/fundBasicInfo` | 场外基金产品信息 |

鉴权是两级，缺一不可
--------------------
1. `X-API-Key`：密钥本身有效，否则 401。
2. `X-Calling-Skill-Id`：必须是在广场**已注册**的标识。本地文件夹名可能不等于
   注册值（实测过 `csc-etf-selection` 的文档处处写自己，但广场注册值是
   `csc-etf-index-analysis`，按文档传必然 400）。所以这里把 skill_id 写成常量，
   不做任何从目录名推断的「聪明」逻辑。

密钥来源（按优先级）
--------------------
1. 环境变量 `CSC_API_KEY`；
2. `$CSC_SKILLS_DIR/<skill_id>/.env`（默认 `~/.workbuddy/skills`）里的
   `CSC_API_KEY=`。
密钥只在进程内使用，**不写日志、不进产物、不回显**。

诚实边界（必须原样传递给上层，不许补齐）
--------------------------------------
- **仅 A 股**。港股 / 美股 / ETF 的财务数据该接口不提供。
- **财务关键指标不返回公告日 `NOTICE_DATE`**。这是最要命的一条：本项目历史回放
  的「无前视」保证依赖公告日对齐（`value_track.replay_score` 取
  `NOTICE_DATE or REPORT_DATE`），而这里的记录只有报告期。若放任它退化到
  REPORT_DATE，就等于「报告期末就知道财报」，是**前视偏差**。
  本模块的处理是：用**法定披露截止日**作为公告日的保守上界（见
  `statutory_notice_date`），并把来源标成 `_notice_source='statutory_deadline'`，
  与东财的真实公告日（`'disclosure'`）区分开、在产物中可披露。
  方向永远取「宁可晚算、不可早算」——代理日只会晚于真实公告日，因此不会前视；
  代价是回放时点会少掉一小段真实可用的窗口，这一点必须在报告里说清楚。
- 更新频率：财务 2~3 小时、ETF 列表约 45 分钟、指数估值约 4 小时。都不是实时。
"""

import json
import os
import random
import re
import threading
import time

import requests

import store

BASE = "https://skillhub.csc108.com/api/skillhub/v1"
SKILLS_DIR = os.environ.get("CSC_SKILLS_DIR",
                            os.path.expanduser("~/.workbuddy/skills"))

# 广场注册值（不是本地目录名）。改这里之前先去广场核对。
SID_FIN = "csc-stock-financial-query"
SID_ETF = "csc-etf-index-analysis"
SID_PROFILE = "csc-listed-company-profile-industry-compare"
SID_FUND = "csc-fund-product-info-query"

_ENABLED = os.environ.get("QUANT_CSC", "on").strip().lower() not in \
    ("off", "0", "none", "false", "no")

_key_cache = {}
_lock = threading.Lock()
_last_error = ""


class CscError(RuntimeError):
    """调用失败。带上游 message，便于区分「没权限」和「没数据」。

    `retryable` 是本模块的**核心分类**：它把「上游这次没打通」与「我要的东西
    本来就不存在」分开。不分类的后果是实测过的——把所有失败都当噪声重试，
    既浪费配额、又让「窗口超限」这种确定性结果永远带着「可能是网络问题」的
    错觉写进日志。
    """

    def __init__(self, msg, rc=None, retryable: bool = False):
        super().__init__(msg)
        self.rc = rc
        self.retryable = bool(retryable)


# 上游 rc=102（实测语义，不是猜测）：
#   同一天对新老指数反复调用 —— 老指数 12/12 成功；新指数稳定 102。
#   000680（成立 1.7 年）只有 imeType=1（1y）能取到 242 点，≥3y 全部 102；
#   000698（3.0 年）可取 1y/3y，5y 起 102；000300（21.5 年）全窗口可取。
#   ⇒ 「请求窗口超出该指数的可用历史」。确定性结果，重试只会白跑。
# 因此 102 **不**归入可重试类；上层改用「降窗口」处理（见 index_valuation）。
RC_WINDOW_EXCEEDED = 102

# 真正可能瞬时恢复的：网络异常与这几个 HTTP 状态。窄重试，不扩大。
_RETRY_STATUS = {408, 425, 429, 500, 502, 503, 504}

# 重试参数：首次之外再试几次、退避基数。实测老指数连发 12 次 0 异常，
# 说明正常节奏下不会触发限流，所以这里只需覆盖偶发抖动，不宜加大。
_RETRY_ATTEMPTS = 2
_RETRY_BASE = 0.8
_retry_lock = threading.Lock()


def configure_retry(attempts=None, base_sec=None):
    """由 run.py 用 config.json 覆盖重试参数（`datasource.csc_retry_*`）。"""
    global _RETRY_ATTEMPTS, _RETRY_BASE
    with _retry_lock:
        if attempts is not None:
            _RETRY_ATTEMPTS = max(0, int(attempts))
        if base_sec is not None:
            _RETRY_BASE = max(0.0, float(base_sec))


def retry_policy() -> "tuple[int, float]":
    with _retry_lock:
        return _RETRY_ATTEMPTS, _RETRY_BASE


def enabled() -> bool:
    return _ENABLED


def configure(enabled=None):
    """由 run.py 用 config.json 覆盖开关。环境变量 `QUANT_CSC` 优先。"""
    global _ENABLED
    if enabled is not None and "QUANT_CSC" not in os.environ:
        _ENABLED = bool(enabled)


def last_error() -> str:
    return _last_error


def api_key(skill_id: str):
    """按优先级取密钥；取不到返回 None（调用方降级，不抛错）。"""
    env = os.environ.get("CSC_API_KEY")
    if env and env.strip():
        return env.strip()
    with _lock:
        if skill_id in _key_cache:
            return _key_cache[skill_id]
    key = None
    path = os.path.join(SKILLS_DIR, skill_id, ".env")
    try:
        with open(path, encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if line.startswith("CSC_API_KEY="):
                    v = line.split("=", 1)[1].strip().strip('"').strip("'")
                    if v:
                        key = v
                        break
    except Exception:
        key = None
    with _lock:
        _key_cache[skill_id] = key
    return key


def available(skill_id: str = SID_FIN) -> bool:
    return bool(_ENABLED and api_key(skill_id))


def _call(skill_id: str, path: str, params: dict, timeout: float = 25):
    """统一调用：返回解析后的 JSON；失败抛 CscError（带脱敏后的原因）。

    刻意**不**复用 fetcher 的三重容灾（直连→代理→浏览器指纹）：那条链路是为
    行情站的 WAF 设计的，而对 SkillHub 用 curl_cffi 伪装浏览器没有意义，
    反而会让 401/403 这类**权限问题**被当成网络问题重试三次。

    重试范围很窄，只覆盖 `CscError.retryable` 为真的情形（网络异常、429/5xx、
    未识别的 rc）；401/400/403 与 rc=102（窗口超限）一次即返回。退避是
    指数 + 抖动，避免多个标的在同一秒同步重试。
    """
    global _last_error
    if not _ENABLED:
        raise CscError("QUANT_CSC=off，中信建投数据源已关闭")
    key = api_key(skill_id)
    if not key:
        msg = (f"未找到 {skill_id} 的 CSC_API_KEY（环境变量或 "
               f"{os.path.join(SKILLS_DIR, skill_id, '.env')}）")
        _last_error = msg
        raise CscError(msg)
    attempts, base = retry_policy()
    last = None
    for i in range(attempts + 1):
        try:
            return _call_once(skill_id, path, params, timeout, key)
        except CscError as e:
            last = e
            if not e.retryable or i >= attempts:
                raise
            delay = base * (2 ** i) + random.uniform(0, 0.2)
            _last_error = f"{e}（第 {i + 1} 次失败，{delay:.1f}s 后重试）"
            time.sleep(delay)
    raise last


def _call_once(skill_id: str, path: str, params: dict, timeout: float, key: str):
    """单次请求。只做**分类**，不做重试；重试由 `_call` 统一负责。"""
    global _last_error
    try:
        r = requests.get(BASE + path, params=params, timeout=timeout,
                         headers={"X-API-Key": key, "X-Calling-Skill-Id": skill_id,
                                  "Content-Type": "application/json"})
    except Exception as e:
        # 网络层异常：连接被重置、超时、DNS——这些是唯一「重试通常有意义」的一类。
        _last_error = f"{path} 请求异常: {e}"
        raise CscError(_last_error, retryable=True)
    # 鉴权与语义错误是确定性的：重试只是把同一个错误再犯三次。
    if r.status_code == 401:
        _last_error = f"{path} 鉴权失败(401)：API Key 无效"
        raise CscError(_last_error)
    if r.status_code == 400:
        _last_error = (f"{path} 400：X-Calling-Skill-Id「{skill_id}」未被广场注册")
        raise CscError(_last_error)
    if r.status_code == 403:
        _last_error = f"{path} 403：Skill「{skill_id}」未绑定该开放 API"
        raise CscError(_last_error)
    if r.status_code != 200:
        retryable = r.status_code in _RETRY_STATUS
        _last_error = (f"{path} HTTP {r.status_code}"
                       + ("（可重试类）" if retryable else ""))
        raise CscError(_last_error, retryable=retryable)
    try:
        j = r.json()
    except Exception as e:
        _last_error = f"{path} 返回非 JSON: {e}"
        raise CscError(_last_error, retryable=True)
    # 财务类接口用 responseCode，ETF 类用 status，两者都是 0/200 表示成功
    rc = j.get("responseCode", j.get("status"))
    if rc not in (0, 200, "0", "200", None):
        try:
            rcv = int(rc)
        except (TypeError, ValueError):
            rcv = None
        desc = j.get("responseDesc") or j.get("errmsg") or ""
        if rcv == RC_WINDOW_EXCEEDED:
            _last_error = (f"{path} 窗口超出可用历史 rc={rc}"
                           f"（请求窗口长于该标的成立年限）")
            raise CscError(_last_error, rc=rcv, retryable=False)
        # 未识别的 rc：按「可能瞬时」处理，但受 attempts 上限约束，
        # 且消息里带上 rc 原文，便于事后归类而不是一眼当成没有数据。
        _last_error = (f"{path} 上游返回失败 rc={rc} desc={desc}"
                       if desc else f"{path} 上游返回失败 rc={rc}")
        raise CscError(_last_error, rc=rcv, retryable=True)
    return j


def _num(v):
    if v is None or v == "":
        return None
    try:
        f = float(str(v).replace("%", "").strip())
    except (TypeError, ValueError):
        return None
    return None if f != f else f


# ---------------------------------------------------------------------------
# 财务（A 股）
# ---------------------------------------------------------------------------
# 中信建投关键指标 → 东财 F10 列名。下游 value_strategy / value_track 认的是
# 东财那套列名（ROEJQ / XSMLL / PARENTNETPROFIT …），所以在这里做一次映射，
# 而不是去改每一个消费方。
_FIN_COLMAP = {
    "totalRevenue": "TOTALOPERATEREVE",
    "netProfitAtsopc": "PARENTNETPROFIT",
    "netProfitAfterNrgalAtsolc": "KCFJCXSYJLR",
    "revenueYoy": "TOTALOPERATEREVETZ",
    "netProfitAtsopcYoy": "PARENTNETPROFITTZ",
    "wgtAvgRoe": "ROEJQ",
    "grossSellingRate": "XSMLL",
    "netSellingRate": "XSJLL",
    "assetLiabRatio": "ZCFZL",
    "basicEps": "EPSJB",
    "npPerShare": "BPS",
    "operateCashFlowPs": "MGJYXJJE",
    "op": "OPERATE_PROFIT",
    "netCfPs": "MGJYXJLLJE",
}


def statutory_notice_date(report_date: str):
    """把报告期映射成「法定披露截止日」，作为公告日的**保守上界代理**。

    A 股定期报告的披露上限（证监会规则）：
      一季报（3-31）→ 4-30；中报（6-30）→ 8-31；三季报（9-30）→ 10-31；
      年报（12-31）  → 次年 4-30。

    用它当公告日只会**晚于或等于**真实公告日，因此不会制造前视；反过来，
    真实公告日可能早于它，于是回放时会少掉一段本来可用的窗口。这是刻意选的
    方向：宁可少样本，也不要把「还没公告的数据」提前用上。
    """
    try:
        y, m = int(str(report_date)[:4]), int(str(report_date)[5:7])
    except (ValueError, IndexError):
        return ""
    if m == 12:
        return f"{y + 1}-04-30"
    if m == 3:
        return f"{y}-04-30"
    if m == 6:
        return f"{y}-08-31"
    if m == 9:
        return f"{y}-10-31"
    return ""


def fin_key_indicators(code: str, periods: int = 16, market: str = "cn",
                       persist: bool = True) -> list:
    """A 股关键财务指标（按报告期降序），已映射到东财 F10 列名。

    返回空列表表示取不到（非 A 股、无权限或上游无数据），调用方自行降级。

    `NOTICE_DATE` 填的是**法定披露截止日**（保守上界），不是真实公告日，
    并带 `_notice_source='statutory_deadline'` 以便上层如实披露。详见模块
    docstring 中的说明。
    """
    rows = []
    if market != "cn":
        return rows
    c = str(code).strip()
    if not c.isdigit() or len(c) != 6:
        return rows
    try:
        j = _call(SID_FIN, "/info/f10/finance/more",
                  {"stockCode": c, "ids": "gjzb", "type": "combine",
                   "pageNum": 1, "pageSize": max(1, min(int(periods), 30))})
    except CscError:
        return rows
    for it in (j.get("keyIndicatorList") or []):
        rd = str(it.get("reportDate") or "")[:10]
        if not rd:
            continue
        rec = {"REPORT_DATE": rd,
               "NOTICE_DATE": statutory_notice_date(rd),
               "_notice_source": "statutory_deadline",
               "_src": "csc_gjzb",
               "REPORT_TYPE": it.get("type")}
        for src_k, dst_k in _FIN_COLMAP.items():
            rec[dst_k] = it.get(src_k)
        rows.append(rec)
    if rows and persist:
        store.upsert_financial("cn", c, rows, src="csc_gjzb")
    return rows


def fin_report_dates(code: str, market: str = "cn") -> dict:
    """财报公告接口的「报告期 → 公告标题」映射。

    用途：给关键指标补一个**保守的公告日代理**。上游 keyIndicator 不给公告日，
    但公告接口给出披露过的报告期集合；据此可以判断某个报告期在时点 t 是否
    *尚未披露*（不在集合里 → 一定还没公告），从而把明显的前视样本剔掉。
    这不是精确的公告日，但它把「未来函数」问题从「静默引入」降级为「可检测」。
    """
    out = {}
    if market != "cn":
        return out
    try:
        j = _call(SID_FIN, "/info/f10/finance/report", {"stockCode": str(code).strip()})
    except CscError:
        return out
    for it in (j.get("announceList") or []):
        rd = str(it.get("reportDate") or "")[:10]
        if rd:
            out.setdefault(rd, str(it.get("title") or ""))
    return out


# ---------------------------------------------------------------------------
# 指数 / ETF
# ---------------------------------------------------------------------------
_INDEX_TYPE = {"all": 1, "broad": 2, "industry": 3, "theme": 4, "strategy": 5}

# 落库结果回执。适配器自己写库，调用方只看得到返回值——若不把写入结果带回去，
# 日志就只能写「已落库」，而这句话在写入失败时同样会打印出来。
# 与 fetcher.drain_fallbacks() 同一套路：调用方 drain 一次，拿到真实结果。
_PERSIST = {}


def drain_persist() -> dict:
    """取出并清空上次落库结果（{表名: 实际写入行数}）。"""
    out = dict(_PERSIST)
    _PERSIST.clear()
    return out


def index_list(index_type="broad", page_size: int = 200, page_num: int = 1,
               persist: bool = True) -> list:
    """指数估值截面列表。index_type: all/broad/industry/theme/strategy 或 1..5。"""
    t = _INDEX_TYPE.get(str(index_type), None) or \
        (int(index_type) if str(index_type).isdigit() else 2)
    try:
        j = _call(SID_ETF, "/info/etf/index/list",
                  {"type": t, "pageSize": min(int(page_size), 200),
                   "pageNum": max(1, int(page_num))})
    except CscError:
        return []
    rows = j.get("data") or []
    if rows and persist:
        _PERSIST["index_snapshot"] = store.upsert_index_snapshot(
            rows, src="csc_index_list")
    return rows


def mark_same_exposure(rows: list) -> int:
    """标注「估值同源」的指数对（全收益版 / 价格版），返回发现的同源行数。

    判据不是名字相似，而是**估值五项逐一相同且成立日相同**：
    PE / PB / PE 分位 / PB 分位 / ROE / 股息率 / publishDate 全等。
    两个成分股不同的指数不可能在这七项上全等。

    实测 000680「上证科创板综合指数」/ 000681「上证科创板综合价格指数」：
      · 上述七项完全相同，成立日同为 2025-01-20；
      · change1Year 20.6139% vs 20.2300%、maxDrawdown1Year 40.35 vs 40.23
        —— 差的这一点正是分红贡献，恰好是「全收益 vs 价格」的特征差。

    处置是**标注而非删除**：两个代码各自有 ETF 跟踪，删掉任何一行都会让
    「我的 ETF 跟的是哪一个」失去答案。标注后由展示层合并成一行、并列出
    同源代码，避免同一份「便宜」在排序里被数两遍。

    给每行加：
      `_same_exposure` : [同组其他代码, ...]（无同源为空列表）
      `_exposure_primary` : 组内代表行 True，其余 False（代表行 = 原始顺序首行）
    """
    def keyfn(x):
        return (x.get("pe"), x.get("pb"), str(x.get("pePercentile")),
                str(x.get("pbPercentile")), x.get("roe"),
                x.get("dividendRatio"), str(x.get("publishDate") or "")[:10])

    groups = {}
    for x in rows:
        x["_same_exposure"] = []
        x["_exposure_primary"] = True
        if x.get("pe") in (None, ""):
            continue
        groups.setdefault(keyfn(x), []).append(x)
    n = 0
    for members in groups.values():
        if len(members) < 2:
            continue
        codes = [str(m.get("securityCode")) for m in members]
        for i, m in enumerate(members):
            m["_same_exposure"] = [c for j, c in enumerate(codes) if j != i]
            m["_exposure_primary"] = (i == 0)
            n += 1
    return n


_IME = {"1y": 1, "3y": 2, "5y": 3, "10y": 4, "all": 5}
_METRIC = {"pe": 1, "pb": 2}

# 窗口阶梯（由长到短）与对应天数。降窗口是**语义降级**：分位是窗口统计量，
# 1 年窗口的 4% 分位和 5 年窗口的 4% 分位不是一回事，所以实际窗口必须随结果
# 一起往外传，落库时也要记下来。
_WINDOW_LADDER = ["5y", "3y", "1y"]
_WINDOW_DAYS = {"1y": 365, "3y": 1095, "5y": 1825, "10y": 3650}

# 逐条取序列的失败原因（"code.market" → 原因）。上层要能把「上游没有这么长的
# 历史」与「这次没打通」分开打印——两者都表现为「返回空」，但处置完全不同。
_VALUATION_MISS = {}


def drain_valuation_miss() -> dict:
    """取出并清空逐条失败原因（与 drain_persist 同套路）。"""
    out = dict(_VALUATION_MISS)
    _VALUATION_MISS.clear()
    return out


def index_valuation(index_code: str, market: str = "SH", period: str = "5y",
                    metric: str = "pe", persist: bool = True,
                    auto_window: bool = True) -> dict:
    """指数估值历史序列。成功返回上游 `data`（附 `_window_used`），失败返回 {}。

    **窗口自适应**：上游对「请求窗口长于该指数成立年限」的请求不回空数组，
    而是直接返回 rc=102。所以长窗取不到时按 5y→3y→1y 阶梯降级，而不是当成
    失败跳过——新指数因此也能拿到一段自己的曲线，代价是窗口更短。实际所用
    窗口以 `_window_used` 返回，落库写进 `index_valuation.window_days`。

    这不是重试：参数变了，拿到的也不可能是同一份数据。区别在于日志怎么读——
    重试到底说明「没打通」，降窗口到底说明「该指数只有这么长的历史」。
    """
    etric = _METRIC.get(str(metric), metric if str(metric).isdigit() else 1)
    metric_key = "pe" if str(etric) == "1" else "pb"
    want = str(period)
    if not auto_window or want not in _WINDOW_LADDER:
        ladder = [want]
    else:
        ladder = _WINDOW_LADDER[_WINDOW_LADDER.index(want):]
    last_rc, last_err = None, ""
    for p in ladder:
        ime = _IME.get(p, p if p.isdigit() else 3)
        try:
            j = _call(SID_ETF, "/info/etf/index/valuation",
                      {"indexCode": str(index_code), "marketCode": str(market),
                       "imeType": ime, "etricType": etric}, timeout=35)
        except CscError as e:
            if e.rc == RC_WINDOW_EXCEEDED:
                last_rc = e.rc
                continue      # 窗口超限 → 换更短的窗口；不重试、不计网络失败
            last_err = str(e)
            break
        d = j.get("data") or {}
        if isinstance(d, dict) and d.get("history"):
            d["_window_used"] = p
            d["_window_requested"] = want
            if persist:
                n = store.upsert_index_valuation(
                    index_code, market, metric_key, d["history"],
                    src="csc_index_valuation", window_days=_WINDOW_DAYS.get(p))
                _PERSIST["index_valuation"] = \
                    _PERSIST.get("index_valuation", 0) + n
            return d
        last_err = f"上游返回空序列（{p}）"
    if last_rc == RC_WINDOW_EXCEEDED and not last_err:
        reason = (f"窗口超出可用历史 rc={RC_WINDOW_EXCEEDED}"
                  f"（已试 {'/'.join(ladder)}，该指数成立时间短于请求窗口）")
    else:
        reason = last_err or "上游无数据"
    _VALUATION_MISS[f"{index_code}.{market}"] = reason
    return {}


def index_components(index_code: str, market: str = "SH", pages: int = 1,
                     page_size: int = 50) -> list:
    """指数成分股与权重（分页拉取）。"""
    out, page = [], 1
    while page <= max(1, int(pages)):
        try:
            j = _call(SID_ETF, "/info/etf/index/components",
                      {"indexCode": str(index_code), "marketCode": str(market),
                       "pageNum": page, "pageSize": min(int(page_size), 50)})
        except CscError:
            break
        d = j.get("data") or {}
        items = d.get("list") or []
        if not items:
            break
        out.extend(items)
        if len(items) < min(int(page_size), 50):
            break
        page += 1
    return out


def etf_list(etf_type=1, filter_dup: int = 1) -> list:
    """ETF 列表（含规模 nav 与跟踪指数）。etf_type 1..7。"""
    try:
        j = _call(SID_ETF, "/info/etf/list",
                  {"type": int(etf_type), "filterDupIndex": int(filter_dup)},
                  timeout=35)
    except CscError:
        return []
    return j.get("data") or []


# ---------------------------------------------------------------------------
# 个股资料 / 行业排名
# ---------------------------------------------------------------------------
def company_basic_info(code: str, market: str = "cn") -> dict:
    """公司主营业务、简介、上市日期等。非 A 股返回 {}。"""
    if market != "cn":
        return {}
    try:
        j = _call(SID_PROFILE, "/info/f10/menu/basicInfo",
                  {"stockCode": str(code).strip()})
    except CscError:
        return {}
    return j.get("basicInfo") or {}


def industry_rank(code: str, metric: str = "pe", report_date: str = None,
                  persist: bool = True) -> dict:
    """个股在所属行业的该指标排名、行业均值与同业列表。

    metric 支持 pe / pb / ps / pcf 及财务指标代码（如 kfjlr、zsy 等，
    以上游为准，不做猜测性枚举）。
    """
    p = {"stockCode": str(code).strip(), "metric": str(metric)}
    if report_date:
        p["reportDate"] = str(report_date)[:10]
    try:
        j = _call(SID_PROFILE, "/info/f10/finance/industryRank", p)
    except CscError:
        return {}
    out = {"industry": j.get("industryName"), "rank": j.get("industryRank"),
           "industry_avg": j.get("industryAvg"), "report_date": j.get("reportDate"),
           "metric": metric, "peers": j.get("industryList") or []}
    if persist and (out["rank"] or out["industry"]):
        n = store.upsert_industry_rank(code, metric, out["report_date"] or "",
                                       out["industry"], out["rank"], out["industry_avg"],
                                       out["peers"], src="csc_industry_rank")
        _PERSIST["industry_rank"] = _PERSIST.get("industry_rank", 0) + n
    return out


# ---------------------------------------------------------------------------
# 场外基金
# ---------------------------------------------------------------------------
def fund_basic_info(fund_code: str) -> dict:
    """场外基金产品信息。上游要求 `000001.OF` 这种带后缀的代码。"""
    c = str(fund_code).strip()
    if not re.match(r"^\d{6}$", c):
        return {}
    try:
        j = _call(SID_FUND, "/strategy/fundEvaluation/fundProduct/fundBasicInfo",
                  {"fund_code": f"{c}.OF"})
    except CscError:
        return {}
    d = j.get("data") if isinstance(j.get("data"), dict) else j
    return d if isinstance(d, dict) else {}


def probe() -> dict:
    """连通性自检：逐个账户试一次最小请求，返回各接口可用性。

    仅用于人工排障与自检脚本；不参与业务链路。
    """
    out = {}
    t0 = time.time()
    try:
        rows = _call(SID_FIN, "/info/f10/finance/more",
                     {"stockCode": "600519", "ids": "gjzb", "type": "combine",
                      "pageNum": 1, "pageSize": 1})
        out["finance"] = {"ok": True, "rows": len(rows.get("keyIndicatorList") or [])}
    except CscError as e:
        out["finance"] = {"ok": False, "err": str(e)}
    try:
        rows = _call(SID_ETF, "/info/etf/index/list",
                     {"type": 2, "pageSize": 1, "pageNum": 1})
        out["index"] = {"ok": True, "rows": len(rows.get("data") or [])}
    except CscError as e:
        out["index"] = {"ok": False, "err": str(e)}
    try:
        rows = _call(SID_PROFILE, "/info/f10/menu/basicInfo", {"stockCode": "600519"})
        out["profile"] = {"ok": bool(rows.get("basicInfo"))}
    except CscError as e:
        out["profile"] = {"ok": False, "err": str(e)}
    out["elapsed"] = round(time.time() - t0, 2)
    out["enabled"] = _ENABLED
    return out


if __name__ == "__main__":
    print(json.dumps(probe(), ensure_ascii=False, indent=2))
