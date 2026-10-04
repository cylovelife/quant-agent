# -*- coding: utf-8 -*-
"""新增数据源的适配层（Phase 1）。

为什么单独一层，而不是往 `fetcher.py` 里塞分支
----------------------------------------------
`fetcher.py` 里那套东西（三档网络栈、按累计耗时熔断、`data_health` 画像、
降级留痕）**是对的**，但它是围绕「行情」写的：函数签名绑死 market/code、
熔断标记名硬编码东财、假设「一次请求 = 一只标的」。

Phase 1 要加的是另一类数据——资金流 / 龙虎榜 / 两融 / 解禁。它们的请求形态完全不同：
**一次请求拿全市场截面**，逐标的循环在这里没有意义。所以把可复用的部分抽出来，
行情那条链保持不动。

三条纪律（沿用既有实现，不另立一套）
------------------------------------
1. **不裸调第三方库。** `akshare` 内部自建 `requests` session：它会继承环境变量里的
   代理，并且完全绕过本项目的限速、熔断与画像。所有调用一律走 `SourceGuard.call()`。
2. **失败要分类。** 本机对国内数据源呈现海外出口 IP，实测东财 push 集群
   （`push2his` / `*.push2` / `push2delay`）对 curl、`requests` 直连、`curl_cffi`
   **三种客户端一律 reset / 502**——这是**确定性**失败，重试只是白跑（见
   `docs/research/phase1_data_channel_probe.md`）。而 `SSLError` 实测重试即过
   （szse 两融首轮失败、重试成功），是**瞬时**失败。把两者混为一谈，要么把时间
   浪费在重试上，要么把抖动误判成坏源。
3. **降级必须留痕且不报错。** 拿不到就返回空 + 记 `data_health` + 记缺失，
   **绝不回落旧值假装今天的数**：资金流是「当日快照」型数据，昨天的资金流
   顶替不了今天的，回退等于凭空造出一个不存在的观测值。
"""

import contextlib
import os
import time
from datetime import datetime

import pandas as pd

# 第三方库自己的进度条（akshare 内部用 tqdm 逐页打印）必须压掉：它写 stderr、
# 没有本项目的时间戳，而「日志时间戳是否前进」是排查长任务卡死（被休眠冻住
# 还是在算）的**唯一**判据，噪声混进去会让这个判据变钝。
#
# tqdm 读的是 `TQDM_*` 前缀的环境变量（4.66+ 的 envwrap，把 TQDM_DISABLE 映射成
# `disable=True`），而且**只在 tqdm 被 import 的那一刻读**。所以这件事唯一可行的
# 位置就是这里：本模块在任何 akshare 调用之前被导入，也就必然早于 tqdm。
# ——放在调用现场（比如某个 with 块里）是没有用的：那时 tqdm 早就 import 完了。
# 实测过这一点：调用期设 TQDM_DISABLE 无效，import 前设才有效。
os.environ.setdefault("TQDM_DISABLE", "1")

ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
CACHE_DIR = os.path.join(ROOT, "state", "cache")
os.makedirs(CACHE_DIR, exist_ok=True)

import store  # noqa: E402  （与 fetcher 一致：src/ 在主流程的 sys.path 上）
from fetcher import FetchBudget  # noqa: E402  复用既有的「按时长熔断」实现

# 代理环境变量。抓取期间临时摘掉，强制直连——与 `fetcher._raw_get` 的第一档一致。
_PROXY_VARS = ("HTTP_PROXY", "HTTPS_PROXY", "http_proxy", "https_proxy",
               "ALL_PROXY", "all_proxy")

# 本次进程内的「缺失」记录：源没取到、需要进报告的那些。与 `fetcher` 的
# fallbacks 分开，因为语义不同——那边是「用了旧数据」，这里是「这次没有数据」。
_MISSING = []


def note_missing(source: str, market: str, reason: str):
    """记一条「本次没取到」。调用方（报告层）负责把它写进数据完整性提示。"""
    _MISSING.append({"source": str(source), "market": str(market),
                     "reason": str(reason), "ts": datetime.now().isoformat(timespec="seconds")})


def drain_missing() -> list:
    """取走并清空缺失记录（避免跨运行重复披露）。"""
    out = list(_MISSING)
    _MISSING.clear()
    return out


@contextlib.contextmanager
def direct_connection():
    """抓取期间摘掉代理环境变量，强制直连；退出时**原样恢复**。

    为什么不设 `NO_PROXY=*`：不同 requests / urllib3 版本对通配符的处理并不一致，
    直接摘掉变量在任何版本上都等价于「不走代理」。

    代价要说清楚：这段时间内**进程内其他线程**发起的请求也会直连。本项目抓取是
    单线程串行的（`run.py` 的抓取段没有并发 worker），所以可以接受；若将来引入
    并发抓取，这里必须改成「通过会话参数传 proxies」而不是改全局环境变量。
    """
    saved = {k: os.environ.pop(k) for k in _PROXY_VARS if k in os.environ}
    try:
        yield
    finally:
        os.environ.update(saved)


# ---------------------------------------------------------------------------
# 总开关与依赖探测（所有 akshare 源共用一套）
#   开关只应有一处：分模块各写一个 `_ENABLED`，迟早出现「资金流开着、事件类关着」
#   这种没人知道的状态。环境变量 `QUANT_AKSHARE=off` 优先级高于 config.json。
# ---------------------------------------------------------------------------
_OFF_VALUES = ("off", "0", "false", "no", "none", "disabled")
_ENABLED = os.environ.get("QUANT_AKSHARE", "on").strip().lower() not in _OFF_VALUES


def akshare_installed() -> bool:
    """akshare 是否装上了。不 import 就判断——主流程不该为「有没有装」付 3 秒。"""
    try:
        import importlib.util
        return importlib.util.find_spec("akshare") is not None
    except Exception:
        return False


def configure_sources(enabled=None):
    """总开关（环境变量优先）。"""
    global _ENABLED
    if enabled is not None and "QUANT_AKSHARE" not in os.environ:
        _ENABLED = bool(enabled)


def sources_enabled() -> bool:
    """开关开着 **且** akshare 真的装上了。"""
    return _ENABLED and akshare_installed()


def switch_on() -> bool:
    """只看总开关（不问装没装）——给那些要把「关掉了」和「没装」分开报的调用方用。"""
    return bool(_ENABLED)


def sources_status() -> dict:
    return {"enabled": bool(_ENABLED), "installed": akshare_installed(),
            "env_override": "QUANT_AKSHARE" in os.environ}


# ---------------------------------------------------------------------------
# 归一：跨适配器共用的小工具
#   放在 base 而不是某个 adapter 里，是因为「代码补零」「日期归一」这两件事
#   每个新源都要做一遍。任何一处写歪了，主键就对不上——而主键对不上的表现是
#   「同一条记录写了两遍、查询只返回一半」，不会报错。
# ---------------------------------------------------------------------------
def normalize_code(v):
    """股票代码 → 6 位字符串。

    上游经常把代码解析成**整数**，前导零已经丢了：`'000001'` → `1`
    （实测同花顺个股资金流 5211 行里，只有 3720 个还是 6 位）。
    A 股代码恒为 6 位，左侧补零即可还原。

    长度 > 6 或含非数字的返回 `None`（口径变了）——宁可少一行、记一条异常，
    也不要猜一个「看起来像代码」的串写进库，那会污染主键。
    """
    if v is None:
        return None
    s = str(v).strip()
    if s.endswith(".0"):
        s = s[:-2]
    if not s.isdigit() or len(s) > 6:
        return None
    return s.zfill(6)


def norm_date(v):
    """日期 → `YYYY-MM-DD`。

    上游同一家厂商的日期格式都不统一：沪深交易所给 `20260929`，东财给
    `2026-09-29`，pandas 有时给 `Timestamp`。不归一就会在同一个主键列里
    出现两种写法，于是「2026-09-29」和「20260929」被当成两天，
    逐日累积的数据一天裂成两份。
    """
    if v is None:
        return None
    try:
        if hasattr(v, "strftime"):
            return v.strftime("%Y-%m-%d")
    except Exception:
        pass
    s = str(v).strip()
    if not s or s.lower() in ("nan", "nat", "none"):
        return None
    digits = "".join(ch for ch in s if ch.isdigit())
    if len(digits) >= 8:
        return f"{digits[:4]}-{digits[4:6]}-{digits[6:8]}"
    return None


def to_float(v):
    """数值 → float；NaN/空串/不可解析 → None（`None` 是「不知道」，不是 0）。"""
    if v is None:
        return None
    if isinstance(v, str):
        s = v.strip().replace(",", "").replace("%", "")
        if s in ("", "-", "--", "—", "None", "nan", "null", "/"):
            return None
        v = s
    try:
        f = float(v)
    except (TypeError, ValueError):
        return None
    if f != f or f in (float("inf"), float("-inf")):
        return None
    return f


def compact_date(v):
    """日期 → `YYYYMMDD`（东财/交易所接口要求的格式）。

    **这不是格式偏好，是接口契约**：把 `2026-09-30` 传给东财的 datacenter 接口，
    它不回空数组，而是回一个错误信封（`{"result": null}`）；akshare 随后对
    `None` 取下标，抛出一个跟真实原因毫无关系的 `TypeError: 'NoneType' object
    is not subscriptable`。实测踩过：`--new-sources` 里龙虎榜/解禁全军覆没，
    而裸调同一接口（传 `20260930`）一切正常。
    库内与日志一律用 `YYYY-MM-DD`，只在**调用上游的那一刻**转成紧凑格式。
    """
    d = norm_date(v)
    return d.replace("-", "") if d else None


# 判据（写死在这里，避免以后各适配器各挑一套）：
# **「有东西被丢了、或被改成了 NULL」才算异常。**
# 上游的正常特征、以及已经被我们正确处理且不损失数据的现象，都不算——
# 否则那一列会常亮，而常亮的告警等于没有告警（实测踩过：`rows` 让每一次
# 成功抓取都显示「有异常」，`dup` 则会让龙虎榜每天亮一次）。
_NON_ANOMALY_KEYS = (
    "rows",       # 归一后行数：计数，不是异常
    "nodata",     # 本次没数据：见 classify/nodata 的说明，另有专门通路
    "dup",        # 上游分页重叠导致的重复行——我们按主键去重，**不丢信息**
    "dup_code",   # 同上（资金流的口径）
)


def anomaly_payload(anom) -> dict:
    """从归一统计里挑出**真正的异常项**。

    留下的都是「丢了数据或写成了 NULL」的信号：
    坏代码/坏主键（整行被丢）、金额解析失败（值变 NULL）、零价（行被丢）。
    """
    return {k: v for k, v in (anom or {}).items()
            if v and k not in _NON_ANOMALY_KEYS}


# ---------------------------------------------------------------------------
# 失败分类
# ---------------------------------------------------------------------------
class SchemaError(RuntimeError):
    """上游返回的字段结构与预期不符。

    显式类型而不是靠字符串匹配 `KeyError`：解析代码里一处 `d["净额"]` 打错字，
    和上游真的改了列名，对我们来说都是「读不懂」，但前者我们该修、后者该换源，
    日志里必须能一眼分开。抛这个类型的只有一个地方——归一函数。
    """


def classify_error(e) -> tuple:
    """把异常归成 `(kind, retryable)`。

    kind 取值：
      - `blocked`   出口被拒（连接 reset / 代理不可达）——确定性，重试无用
      - `transient` 超时 / TLS 抖动——可重试
      - `empty`     响应体为空（解析不出 JSON）——给一次机会，再空就当被拒
      - `nodata`    上游明确表示「这个查询没有结果」——**不跳闸**，见下
      - `schema`    字段结构与预期不符——确定性，是上游改了或我们读错了，要人看
      - `unknown`   未归类，按可重试处理（宁可多试一次，也不要静默丢掉一个源）

    `nodata` 与 `empty` 必须分开：前者是「查了、上游说没有」，后者是「拿回来是空的」。
    合约查询（某日龙虎榜、某窗口解禁）本来就可能没有记录，若把它当故障去熔断，
    会在一个健康的源上挂 30 分钟禁写——把「今天没数据」升级成「今天和之后半小时
    都没数据」。所以 `nodata` 可重试一次、但永不跳闸。
    识别方式：akshare 在收到错误信封时对 `data_json["result"]`（None）取下标，
    抛出与真实原因无关的 `TypeError: 'NoneType' object is not subscriptable`。

    判断顺序还有一处讲究：`SSLError` 与 `ProxyError` 都是 `ConnectionError` 的子类，
    所以先按更具体的类别落位、再退回父类——否则 TLS 抖动会被误判成「出口被拒」，
    白白放弃一次本可以成功的重试（实测 szse 首轮 SSLError、重试即过）。
    """
    if isinstance(e, SchemaError):
        return "schema", False
    text = f"{type(e).__name__}: {e}"
    if isinstance(e, TypeError) and "not subscriptable" in text:
        return "nodata", True
    name = type(e).__name__
    text = f"{name}: {e}"
    try:
        import requests as _rq
        if isinstance(e, _rq.exceptions.Timeout):
            return "transient", True
        if isinstance(e, _rq.exceptions.SSLError):
            return "transient", True
        if isinstance(e, (_rq.exceptions.ProxyError, _rq.exceptions.ConnectionError)):
            return "blocked", False
    except Exception:
        pass
    if "JSONDecodeError" in text or "Expecting value" in text:
        return "empty", True
    if any(k in text for k in ("KeyError", "IndexError", "Length mismatch")):
        return "schema", False
    if "Timeout" in text:
        return "transient", True
    if any(k in text for k in ("Connection aborted", "Connection reset",
                               "RemoteDisconnected", "Max retries exceeded",
                               "Connection refused")):
        return "blocked", False
    return "unknown", True


class Breaker:
    """按源熔断：命中确定性失败后，TTL 内不再发起请求。

    与 `fetcher._em_blocked` 的区别：那个是东财专用（标记文件名硬编码），
    这里是按源名打标记，各源互不牵连——不能因为东财坏了就把交易所官网也掐掉。

    **只认确定性失败**（`blocked` / `schema`）才跳闸，瞬时失败不跳：
    用抖动去熔断，正是既有代码注释里警告过的错误（把限流误判成源故障）。
    """

    def __init__(self, name: str, ttl_sec: float = 1800.0):
        self.name = str(name)
        self.ttl_sec = float(ttl_sec)

    def _marker(self) -> str:
        return os.path.join(CACHE_DIR, f"breaker_{self.name}")

    def blocked(self) -> bool:
        p = self._marker()
        if not os.path.exists(p):
            return False
        try:
            age = time.time() - os.path.getmtime(p)
        except OSError:
            return False
        if age < self.ttl_sec:
            return True
        try:
            os.remove(p)      # 过 TTL 自动失效，不需要人工清
        except OSError:
            pass
        return False

    def reason(self) -> str:
        try:
            with open(self._marker(), encoding="utf-8") as f:
                return f.read().strip()
        except OSError:
            return ""

    def trip(self, reason: str):
        try:
            with open(self._marker(), "w", encoding="utf-8") as f:
                f.write(f"{datetime.now().isoformat(timespec='seconds')} {reason}")
        except OSError:
            pass

    def reset(self):
        try:
            os.remove(self._marker())
        except OSError:
            pass


class Result:
    """一次抓取的结果。`df` 可能是空表，但 `ok` 明确说明这次是成是败。"""

    __slots__ = ("ok", "df", "kind", "attempts", "elapsed", "error", "skipped")

    def __init__(self, ok=False, df=None, kind="", attempts=0, elapsed=0.0,
                 error="", skipped=""):
        self.ok = ok
        self.df = df if df is not None else pd.DataFrame()
        self.kind = kind
        self.attempts = attempts
        self.elapsed = elapsed
        self.error = error
        self.skipped = skipped

    @property
    def rows(self) -> int:
        return 0 if self.df is None or not hasattr(self.df, "__len__") else len(self.df)

    def __repr__(self):
        return (f"Result(ok={self.ok}, rows={self.rows}, kind={self.kind or '-'}, "
                f"attempts={self.attempts}, {self.elapsed:.1f}s"
                + (f", skipped={self.skipped}" if self.skipped else "")
                + (f", err={self.error[:60]}" if self.error else "") + ")")


class SourceGuard:
    """一个数据源的护栏：限速 + 预算 + 熔断 + 健康画像 + 缺失留痕。

    用法::

        g = SourceGuard("akshare_ths_fund_flow", min_gap=1.5, budget_sec=90)
        res = g.call("cn", lambda: ak.stock_fund_flow_individual(symbol="即时"))
        if res.ok:
            ...

    失败**不抛异常**：调用方拿到 `res.ok=False` 继续往下走。数据源是优化不是
    正确性依赖——这条纪律在 `store.py` 与 `fetcher.py` 里已经写过，这里照办。
    """

    def __init__(self, name: str, min_gap: float = 1.0, budget_sec: float = 90.0,
                 breaker_ttl_sec: float = 1800.0, retries: int = 1, log=None):
        self.name = str(name)
        self.min_gap = float(min_gap)
        self.budget = FetchBudget(budget_sec, name=self.name, log=log)
        self.breaker = Breaker(self.name, ttl_sec=breaker_ttl_sec)
        self.retries = max(1, int(retries))
        self.log = log or (lambda *_a, **_k: None)
        self._last_ts = 0.0

    def configure(self, min_gap=None, budget_sec=None, retries=None,
                  breaker_ttl_sec=None):
        """由 run.py 用 config.json 覆盖参数（不重建对象，保住进程内的熔断状态）。"""
        if min_gap is not None:
            self.min_gap = float(min_gap)
        if budget_sec is not None:
            self.budget.budget = float(budget_sec)
        if retries is not None:
            self.retries = max(1, int(retries))
        if breaker_ttl_sec is not None:
            self.breaker.ttl_sec = float(breaker_ttl_sec)

    def _pace(self):
        """两次请求之间的最小间隔。源被打了限速就不能再「能打多快打多快」。"""
        gap = time.time() - self._last_ts
        if gap < self.min_gap:
            time.sleep(self.min_gap - gap)
        self._last_ts = time.time()

    def call(self, market: str, fn, n_items=None) -> Result:
        """带护栏地调用一次抓取。

        `fn` 是**无参**可调用对象（用 lambda 绑参数），返回 DataFrame。
        """
        if self.breaker.blocked():
            why = self.breaker.reason()
            self.log(f"    {self.name} 熔断中（{why[:60]}），本次跳过 {market}")
            store.record_health(self.name, market, False, 0.0, 0,
                                f"熔断中: {why[:120]}")
            note_missing(self.name, market, f"熔断中（{why[:80]}）")
            return Result(kind="blocked", skipped="breaker")
        if self.budget.guard():
            store.record_health(self.name, market, False, 0.0, 0, "累计耗时超预算，跳过")
            note_missing(self.name, market, "累计耗时超预算")
            return Result(kind="budget", skipped="budget")

        last_err, last_kind = "", "unknown"
        for attempt in range(1, self.retries + 1):
            self._pace()
            t0 = time.time()
            try:
                with direct_connection():
                    df = fn()
                dt = time.time() - t0
                self.budget.charge(dt)
                n = n_items if n_items is not None else (
                    len(df) if hasattr(df, "__len__") else 0)
                store.record_health(self.name, market, True, dt, n,
                                    f"attempt={attempt}")
                self.breaker.reset()
                return Result(ok=True, df=df, kind="", attempts=attempt, elapsed=dt)
            except Exception as e:
                dt = time.time() - t0
                self.budget.charge(dt)
                last_kind, retryable = classify_error(e)
                last_err = f"{type(e).__name__}: {e}"
                self.log(f"    {self.name} 第 {attempt} 次失败"
                         f"（{last_kind}{'/可重试' if retryable else '/确定性'}）: "
                         f"{last_err[:120]}")
                if not retryable or attempt >= self.retries:
                    break

        store.record_health(self.name, market, False, self.budget.spent, 0,
                            f"{last_kind}: {last_err[:150]}")
        note_missing(self.name, market, f"{last_kind}: {last_err[:120]}")
        # 只有确定性失败才跳闸：瞬时失败说明源还活着，掐掉它反而是更坏的错；
        # `nodata` 更不能跳闸——「这个查询没有结果」不是源病了的证据。
        if last_kind in ("blocked", "schema", "empty"):
            self.breaker.trip(f"{last_kind}: {last_err[:120]}")
        return Result(kind=last_kind, attempts=self.retries, error=last_err)
