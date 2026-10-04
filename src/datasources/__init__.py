# -*- coding: utf-8 -*-
"""新增数据源适配层（Phase 1）。

    base.py            护栏（限速 / 预算 / 熔断 / 失败分类 / 健康画像 / 缺失留痕）
                        + 跨源共用归一工具（代码补零 / 日期归一 / 数值归一）
                        + akshare 总开关（环境变量优先于 config.json）
    akshare_source.py  akshare 适配器（Step 2：个股资金流截面）
    akshare_events.py  akshare 适配器（Step 3：龙虎榜 / 两融 / 解禁 / 退市清单）
    sina_source.py     新浪系现货列表（Step 4：ETF / 港股），作东财与腾讯之外的第三档

约定：**任何新源都在这里实现**，统一走 `SourceGuard`，不要直接在主流程里
`import akshare` 就调——那会绕过限速、熔断与 `data_health` 画像，并且把
第三方的网络细节（代理继承、TLS 指纹）泄露到业务代码里。
"""

from . import akshare_events  # noqa: F401
from . import akshare_source  # noqa: F401
from . import base  # noqa: F401
from . import sina_source  # noqa: F401

__all__ = ["base", "akshare_source", "akshare_events", "sina_source"]
