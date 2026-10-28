"""Research signal vocabulary shared by rules and rendering.

@author cylovelife
"""

SIGNALS = {
    "price": "价格突破",
    "macd": "MACD转正",
    "trend": "动态MA60",
    "volume": "回踩量能",
}

STATUS_LABELS = {
    "pending": "待确认",
    "met": "已满足",
    "invalidated": "已失效",
    "unavailable": "暂不可判定",
}
