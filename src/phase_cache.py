"""重计算阶段的「内容寻址」缓存。

为什么需要
----------
单次 `run.py --mode all` 约 6 分钟，其中三段纯 CPU 计算占约 70%：

    离场模型标定   fit_exit_model     ≈155s   （5470 笔路径的面板标定 + 样本外验证）
    离场规则体检   parameter_scan     ≈ 60s   （5315 笔信号的参数扫描）
    短线模型选型   select_short       ≈ 48s   （36 组规则 × 3 折 walk-forward）

三者都是 ``(K 线缓存, 参数, 阶段配置)`` 的确定性纯函数——输入不变时重跑会得到
完全相同的结果。这里的缓存按**输入内容签名**寻址：K 线内容、权重、参数版本、
阶段专属配置只要有一项变化，签名就变，缓存自然失效。所以不存在「把旧结果喂给
新数据」的风险，最坏情况只是签名抖动导致不命中（退化成原来的一次完整计算）。

刻意不做的事
------------
- **不按墙钟日期做键**。同一天里数据可能被修订（前复权序列在除权日会重算整条
  历史），按日期做键会把旧结果喂给新数据。因此键里放的是 K 线内容的指纹。
- **不静默命中**。命中与未命中都返回状态，由调用方写日志，避免「报告里的数字
  其实是昨天算的」这类无人察觉的漂移。

numpy 类型必须显式转换
----------------------
``json.dump`` 无法序列化 numpy 标量。若用 ``default=str`` 兜底，数字会变成字符串，
下游 ``it['stop'] == cur_val`` 这类**数值比较会静默变成字符串比较**从而失效。
因此这里用 ``_sanitize`` 把 numpy 标量显式降级为 Python 原生 int/float/bool；
遇到无法安全转换的对象宁可跳过缓存（返回 None），也不写入半损坏的数据。
"""

import hashlib
import json
import os

import numpy as np
import pandas as pd

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
CACHE_DIR = os.path.join(ROOT, "state", "cache", "phases")

# 缓存格式版本：缓存结构或语义有变时递增，让旧缓存自动失效。
# 1 → 2：model_select 的 avg_per_day 取消四舍五入（选型决策量精度修正），
#         旧缓存里的值是舍入后的，与新语义不一致，必须作废。
FORMAT = 2


# ---------------------------------------------------------------------------
# 1) 输入指纹
# ---------------------------------------------------------------------------
def _frame_fingerprint(k) -> str:
    """单个标的 K 线的内容指纹（长度 + 末日期 + 全表内容哈希）。

    包含内容哈希而非仅长度/日期，是为了捕捉「同一天的数据被修订」：
    例如前复权序列在除权日会重算整条历史，长度可能不变但数值全变。
    """
    if k is None:
        return "none"
    try:
        n = len(k)
    except Exception:
        return "bad"
    if n == 0:
        return "empty"
    try:
        h = int(pd.util.hash_pandas_object(k, index=False).sum())
    except Exception:
        h = 0
    last = ""
    try:
        if "date" in k.columns:
            last = str(k["date"].iloc[-1])[:10]
    except Exception:
        last = ""
    return f"n{n}:{last}:{h & 0xFFFFFFFFFFFFFFFF:x}"


def kline_signature(kline_map: dict) -> str:
    """整份 K 线缓存的内容签名（与字典插入顺序无关）。"""
    parts = []
    for key in sorted(kline_map or {}, key=lambda x: str(x)):
        parts.append(f"{key}#{_frame_fingerprint((kline_map or {})[key])}")
    blob = "|".join(parts)
    return hashlib.sha1(blob.encode("utf-8")).hexdigest()[:16]


def params_signature(params: dict, extra: dict) -> str:
    """参数与阶段专属配置的签名。

    ``extra`` 只放**真正影响该阶段输出**的配置（如离场模型的 min_bucket_n /
    oos_frac / shrink_k，选型的网格），而不是整份 config——否则改一处无关配置
    就会把所有缓存打掉。
    """
    payload = {
        "format": FORMAT,
        "weights": params.get("weights"),
        "score_threshold": params.get("score_threshold"),
        "backtest": params.get("backtest"),
        "extra": extra,
    }
    blob = json.dumps(_sanitize(payload), sort_keys=True, ensure_ascii=False)
    return hashlib.sha1(blob.encode("utf-8")).hexdigest()[:16]


def cache_key(phase: str, kline_sig: str, param_sig: str) -> str:
    return hashlib.sha1(f"{phase}|{kline_sig}|{param_sig}".encode("utf-8")).hexdigest()[:24]


# ---------------------------------------------------------------------------
# 2) numpy → 原生类型（安全降级）
# ---------------------------------------------------------------------------
def _sanitize(o):
    """把 numpy 标量降级为 Python 原生类型；无法安全转换时抛 TypeError。

    刻意**不**使用 ``default=str`` 兜底：那会把数字变成字符串，让下游的数值
    比较静默失效。宁可抛错、跳过本次缓存写入。
    """
    if isinstance(o, dict):
        return {str(k): _sanitize(v) for k, v in o.items()}
    if isinstance(o, (list, tuple)):
        return [_sanitize(v) for v in o]
    if isinstance(o, (np.integer,)):
        return int(o)
    if isinstance(o, (np.floating,)):
        return float(o)
    if isinstance(o, np.bool_):
        return bool(o)
    if isinstance(o, np.ndarray):
        return [_sanitize(v) for v in o.tolist()]
    if isinstance(o, float):
        # NaN/Inf 不是合法 JSON；让它们落成 None 而不是写出非法字面量
        return o if np.isfinite(o) else None
    return o


# ---------------------------------------------------------------------------
# 3) 读写
# ---------------------------------------------------------------------------
def path_for(phase: str, key: str) -> str:
    return os.path.join(CACHE_DIR, f"{phase}_{key}.json")


def load(phase: str, key: str):
    """命中则返回缓存对象，未命中/损坏返回 None。"""
    p = path_for(phase, key)
    if not os.path.exists(p):
        return None
    try:
        with open(p, encoding="utf-8") as f:
            rec = json.load(f)
    except Exception:
        return None
    if not isinstance(rec, dict) or rec.get("format") != FORMAT:
        return None
    return rec.get("data")


def save(phase: str, key: str, obj) -> bool:
    """写入缓存。任何异常都返回 False（缓存是优化，不允许影响主流程）。"""
    try:
        payload = {"format": FORMAT, "phase": phase, "key": key,
                   "data": _sanitize(obj)}
        json.dumps(payload)          # 先校验可序列化，再落盘
        os.makedirs(CACHE_DIR, exist_ok=True)
        p = path_for(phase, key)
        tmp = p + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(payload, f, ensure_ascii=False)
        os.replace(tmp, p)
        return True
    except Exception:
        return False


def clear(phase: "str | None" = None) -> int:
    """清缓存（phase=None 清全部）。返回删除的文件数。"""
    if not os.path.isdir(CACHE_DIR):
        return 0
    n = 0
    for fn in os.listdir(CACHE_DIR):
        if phase and not fn.startswith(f"{phase}_"):
            continue
        if fn.endswith(".json") or fn.endswith(".json.tmp"):
            try:
                os.remove(os.path.join(CACHE_DIR, fn))
                n += 1
            except OSError:
                pass
    return n
