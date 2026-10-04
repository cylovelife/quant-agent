# -*- coding: utf-8 -*-
"""统一落盘层：SQLite（state/quant.db）。

为什么要有这一层
----------------
在此之前，中间数据散落在三类地方：当日 JSON 快照、按标的命名的 JSON、
以及 CSV。它们能跑，但有三处结构性缺陷：

1. **行情没有累积**。前复权 K 线从来不落盘，每次运行都要为 60~70 只标的
   重新发起网络请求。库建好之后，历史部分只写一次，之后只补增量。
2. **无法按维度查询**。「某标的过去两年的收盘序列」「某指数 PE 分位的历史」
   这类问题在文件布局下只能把整棵树读进内存再筛。
3. **快照按日期堆文件**。`state/cache/2026-09-22_cn_rank.json` 每天一份，
   永久累积且无法比较。

为什么是 SQLite 而不是 MySQL
----------------------------
这是**单机、单用户、单进程写入**的场景：一次运行抓一次、算一次、写一次。
MySQL 需要常驻服务、端口、账号、备份策略与连接池，换来的并发写能力本项目
一点都用不上；而 SQLite 是 Python 标准库自带、单文件、零运维，还能跟着
`state/` 一起备份与删除。表结构只用标准 SQL 类型，将来真要迁 MySQL，
改的是连接串而不是 schema。

写进这一层的三条纪律
--------------------
- **幂等**：所有写入都是 `INSERT ... ON CONFLICT DO UPDATE`。同一天重跑、
  同一份数据重复落库，都不会产生重复行或翻倍计数。
- **降级不报错**：数据层是优化不是正确性依赖。任何异常都吞掉并返回
  空结果，主流程退回原有的文件缓存路径。`QUANT_STORE=file` 可整体关闭。
- **不替代原有产物**：`state/picks_*.json`、`outcomes.csv`、`positions.json`
  等仍然照旧写。数据库是**新增**的一份结构化副本，不是替换。
"""

import json
import os
import sqlite3
import threading
from datetime import datetime, timedelta

import pandas as pd

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DEFAULT_DB = os.path.join(ROOT, "state", "quant.db")

# 结构版本：只作记录与排障用（DDL 全部 IF NOT EXISTS，加表加列都是前向兼容的）。
# 1 → 2：新增 macro_snapshot（判断类数据，不可从数据源重抓）。
# 2 → 3：index_valuation 增 window_days（估值序列的请求窗口，降级取短窗后必须可辨）。
# 3 → 4：新增 ml_eval（走前 ML 验证结论；面板会滚动，旧结论重算不出来）。
# 4 → 5：新增 fund_flow（个股资金流截面；上游只给当日快照，**逐日累积、无法回补**）。
# 5 → 6：新增事件类四表 lhb / lhb_inst / margin / restriction，加退市清单 delisted。
# 6 → 7：rank_snapshot 补 src（此前 upsert 收下 src 形参却没有这一列——见下）。
# 7 → 8：data_health 增 anomalies（归一异常统计，「字段缺失率」的落地形式）。
SCHEMA_VERSION = 8

# 回退开关：QUANT_STORE=file / off / 0 时本模块整体进入 no-op 模式，
# 所有读取返回 None、所有写入返回 0，主流程表现与引入本模块前完全一致。
_OFF_VALUES = ("file", "off", "0", "none", "false", "no")
_ENABLED = os.environ.get("QUANT_STORE", "sqlite").strip().lower() not in _OFF_VALUES

_lock = threading.RLock()
_conn = None
_conn_path = None
_last_error = None


# ---------------------------------------------------------------------------
# DDL
# ---------------------------------------------------------------------------
_KLINE_COLS = ["open", "high", "low", "close", "volume", "amount"]

DDL = [
    # 行情主表：market + code + adjust + date 唯一确定一根 K 线。
    # adjust 取值：qfq=前复权（短线/宏观口径）, raw=不复权（估值分位口径）,
    #              nav=场外基金净值（由净值序列伪装成 OHLCV）——当前**无写入方**，
    #              预留给后续把基金净值序列也收进库；写入前请确认下游用的是哪一档价格。
    # 三者**不可混用**：前复权在除权日会重算整条历史，拿它算估值分位是错的。
    """CREATE TABLE IF NOT EXISTS kline (
        market   TEXT NOT NULL,
        code     TEXT NOT NULL,
        adjust   TEXT NOT NULL,
        date     TEXT NOT NULL,
        open REAL, high REAL, low REAL, close REAL, volume REAL, amount REAL,
        src TEXT, fetched_at TEXT,
        PRIMARY KEY (market, code, adjust, date)
    )""",
    "CREATE INDEX IF NOT EXISTS ix_kline_date ON kline(date)",

    # 抓取元信息：判断「这只标的要不要再抓」的唯一依据。
    # 存 last_date 而不是只看抓取时间——行情没出新数据时，抓一百次也还是那根 K 线。
    """CREATE TABLE IF NOT EXISTS kline_meta (
        market TEXT NOT NULL, code TEXT NOT NULL, adjust TEXT NOT NULL,
        first_date TEXT, last_date TEXT, rows INTEGER,
        last_fetch TEXT, src TEXT,
        PRIMARY KEY (market, code, adjust)
    )""",

    # 榜单/快照：每天每市场一批，保留历史便于回看「当时候选池长什么样」。
    #
    # src 记录**这一行是哪一档降级供的数**（东财 / 腾讯 / 新浪）。
    # 这一列曾经漏掉：`upsert_rank_snapshot(..., src=...)` 一直收着这个形参、
    # 但表里没有这一列，于是标注被静默丢弃——schema v7 补上。
    # 后果不是「少了个字段」，而是**降级链的长期偏移无法被发现**：
    # 东财成功率只有 9%~15%、多数日子实际由腾讯的静态核心池供数，
    # 这件事在报告与库里都查不到。现在可以按 src 回溯（历史行是 NULL，补不回来）。
    """CREATE TABLE IF NOT EXISTS rank_snapshot (
        market TEXT NOT NULL, board TEXT NOT NULL, snap_date TEXT NOT NULL,
        code TEXT NOT NULL,
        name TEXT, price REAL, pct REAL, amount REAL,
        pe_ttm REAL, float_mcap REAL, turnover_ratio REAL, volume_ratio REAL,
        src TEXT, fetched_at TEXT,
        PRIMARY KEY (market, board, snap_date, code)
    )""",

    # 财务指标：既存抽取出的关键字段（便于 SQL 聚合），也存原始 payload
    # （口径变更或字段遗漏时不用重新抓）。
    #
    # notice_source 区分公告日的**可信级别**：'disclosure'=交易所披露的真实公告日；
    # 'statutory_deadline'=数据源不提供公告日时的法定披露截止日（保守上界）。
    # 两者不能混为一谈——真实公告日可能早于法定截止日，用后者做时点对齐会少用
    # 一段可用窗口，但绝不会前视。谁覆盖谁由下面的 upsert 规则决定。
    """CREATE TABLE IF NOT EXISTS fin_metrics (
        market TEXT NOT NULL, code TEXT NOT NULL, report_date TEXT NOT NULL,
        notice_date TEXT, notice_source TEXT,
        revenue REAL, net_profit REAL, net_profit_yoy REAL, revenue_yoy REAL,
        roe REAL, gross_margin REAL, net_margin REAL, debt_ratio REAL,
        eps REAL, bps REAL, ocf_ps REAL,
        src TEXT, fetched_at TEXT, payload TEXT,
        PRIMARY KEY (market, code, report_date)
    )""",

    """CREATE TABLE IF NOT EXISTS fund_profile (
        code TEXT PRIMARY KEY,
        name TEXT, scale REAL, manager TEXT,
        src TEXT, fetched_at TEXT, payload TEXT
    )""",

    # 指数估值快照（截面）与历史序列（时序）。
    """CREATE TABLE IF NOT EXISTS index_snapshot (
        index_code TEXT NOT NULL, market TEXT NOT NULL,
        name TEXT, index_type TEXT,
        pe REAL, pb REAL, pe_pct REAL, pb_pct REAL, roe REAL,
        div_yield REAL, chg_1y REAL, mdd_1y REAL,
        publish_date TEXT,
        upd_time TEXT, src TEXT, fetched_at TEXT,
        PRIMARY KEY (index_code, market)
    )""",
    # window_days = 这一行**来自多长的请求窗口**（5y=1825 / 3y=1095 / 1y=365）。
    # 上游对指数估值序列不支持「自动截断」：请求窗口长于该指数成立年限时直接
    # 返回 rc=102，所以取短窗是**降级**而不是等价替换。分位与序列长度强相关，
    # 不记窗口的话，一条 1 年的序列和一条 5 年的序列在库里长得一模一样。
    # 冲突时取**更宽**的窗口：短窗结果不覆盖长窗结果（长窗是短窗的超集）。
    """CREATE TABLE IF NOT EXISTS index_valuation (
        index_code TEXT NOT NULL, market TEXT NOT NULL,
        metric TEXT NOT NULL, date TEXT NOT NULL, value REAL,
        window_days INTEGER,
        src TEXT, fetched_at TEXT,
        PRIMARY KEY (index_code, market, metric, date)
    )""",

    # 行业内指标排名：rank/industry_avg 存原始字符串口径（如 "12/44"），
    # 便于报告原样展示，不做无依据的数值化。
    """CREATE TABLE IF NOT EXISTS industry_rank (
        code TEXT NOT NULL, metric TEXT NOT NULL, report_date TEXT NOT NULL,
        industry TEXT, rank TEXT, industry_avg TEXT, n_peers INTEGER,
        src TEXT, fetched_at TEXT, payload TEXT,
        PRIMARY KEY (code, metric, report_date)
    )""",

    # 宏观快照：宏观分是**判断**而不是可重抓的数据——原始指标（PMI/CPI/M2）随时能
    # 再抓一次，但「当时打了几分、建议多少仓位」抓不回来。以前它只活在当日
    # picks_*.json 里，想回答「宏观分高的时段长期超额是否更好」得逐个解析每日 JSON。
    # 一天一行，重跑覆盖。
    """CREATE TABLE IF NOT EXISTS macro_snapshot (
        run_date TEXT PRIMARY KEY,
        score REAL, label TEXT,
        stance_lo REAL, stance_hi REAL,
        style_bias TEXT, missing TEXT,
        src TEXT, fetched_at TEXT, payload TEXT
    )""",

    # 走前 ML 验证的结论。与宏观快照同一判据：面板本身是**滚动**的，今天的样本外
    # 结论明天无法从同一份数据重算出来，属于「丢了拿不回来」的判断类数据。
    # 一行 = 一次运行里的一个候选（`model='rule'` 是基准行）。
    """CREATE TABLE IF NOT EXISTS ml_eval (
        run_date TEXT NOT NULL, ret_col TEXT NOT NULL, model TEXT NOT NULL,
        n_periods INTEGER, n_oof INTEGER,
        ic_mean REAL, icir REAL, t REAL, positive_rate REAL, ic_mean_trimmed REAL,
        rule_ic_mean REAL, rule_icir REAL, rule_ic_mean_trimmed REAL,
        gate_passed INTEGER, verdict TEXT, features TEXT,
        src TEXT, fetched_at TEXT,
        PRIMARY KEY (run_date, ret_col, model)
    )""",

    # 数据源健康：每次抓取的耗时/成功与否。以前这些只在内存里，
    # 报告生成后就消失，无法回答「这个源这周稳定吗」。
    #
    # anomalies 存**归一异常的逐字段计数**（JSON），是设计文档要求的「字段缺失率」的
    # 落地形式：`{"bad_amount": {"net": 12}, "bad_code": 3}` 这样一行就能回答
    # 「这个源是整体挂了，还是某个字段开始解析不出来」——后者是上游改版的前兆，
    # 只看成功率看不出来（抓取成功、字段悄悄变 NULL，成功率仍然是 100%）。
    """CREATE TABLE IF NOT EXISTS data_health (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        run_date TEXT, source TEXT, market TEXT,
        ok INTEGER, elapsed REAL, n_items INTEGER, note TEXT,
        anomalies TEXT, ts TEXT
    )""",
    "CREATE INDEX IF NOT EXISTS ix_health_src ON data_health(source, run_date)",

    # 个股资金流截面（Phase 1 新数据源）。金额单位统一为**元**——上游给的是
    # 「12.34亿 / 5.6万」这类中文串，归一发生在 adapter 层（`datasources.akshare_source`），
    # 落盘层只认规范列，不猜上游格式。
    #
    # 为什么只有当日一档、没有 window 列：上游的「3/5/10/20 日排行」是**站内口径**，
    # 且只给净额一个字段（没有流入/流出拆分）。把两种口径塞进同一张表，等于把
    # 「这个数是谁算的」藏起来——而分位、环比这类下游计算全都依赖口径可比。
    # 多日窗口由我们自己在这条序列上求和得到，口径自持。
    #
    # **这张表只能逐日累积，不能回补**：上游不提供历史资金流，抓不到的那天就永远缺一天。
    # 因此 flow_date 用**行情交易日**（run.py::data_date）而不是墙钟日期，
    # 否则周末补跑会凭空多出一个没有行情的「交易日」。
    """CREATE TABLE IF NOT EXISTS fund_flow (
        market TEXT NOT NULL, code TEXT NOT NULL, flow_date TEXT NOT NULL,
        name TEXT, price REAL, pct REAL, turnover REAL,
        inflow REAL, outflow REAL, net REAL, amount REAL,
        src TEXT, fetched_at TEXT,
        PRIMARY KEY (market, code, flow_date)
    )""",
    "CREATE INDEX IF NOT EXISTS ix_fund_flow_date ON fund_flow(flow_date)",

    # ---------------------------------------------------------------------
    # 事件类数据（Phase 1 Step 3）。四张表共同的取舍：
    #
    # **刻意不存上游算好的「事件后 N 日涨跌幅」**（龙虎榜的 `上榜后1/2/5/10日`、
    # 解禁的 `解禁后20日涨跌幅`）。那些是**前视收益**，与我们「预测先落库、
    # 验证只用真实行情」的纪律直接冲突：它们一旦进表，任何按列名批量取特征的
    # 下游都可能把它当因子用——而这类泄漏在本项目已经付过一次很贵的学费
    # （重叠窗口让人工 IC 从 +0.039 虚报到 +0.4906）。事件后收益要由我们在
    # 自己的 K 线上算，口径自持、可审计。
    # ---------------------------------------------------------------------

    # 龙虎榜明细。单位：金额=元；pct / turnover / net_ratio / amount_ratio 都是
    # **百分数**（9.973 表示 9.973%），不是小数——上游在这几个字段上用百分数，
    # 不要按小数解读。
    #
    # 同一只股票同一天可能因**多个原因**上榜（涨幅偏离、换手率、连续三日…），
    # 所以主键含 reason；用 (trade_date, code) 会把第二条原因覆盖掉。
    #
    # 不发「流通市值」：同一厂商的两个龙虎榜接口单位不一致
    # （detail 给元、jgmmtj 给亿元），收进同一张表迟早被当成同一个量。
    """CREATE TABLE IF NOT EXISTS lhb (
        trade_date TEXT NOT NULL, code TEXT NOT NULL, reason TEXT NOT NULL,
        market TEXT, name TEXT, note TEXT,
        close REAL, pct REAL, turnover REAL,
        net_buy REAL, buy REAL, sell REAL, amount REAL, market_amount REAL,
        net_ratio REAL, amount_ratio REAL,
        src TEXT, fetched_at TEXT,
        PRIMARY KEY (trade_date, code, reason)
    )""",
    "CREATE INDEX IF NOT EXISTS ix_lhb_date ON lhb(trade_date)",
    "CREATE INDEX IF NOT EXISTS ix_lhb_code ON lhb(code)",

    # 龙虎榜机构买卖统计（机构专用席位的买卖额）。与明细分开存而不是并进一行：
    # 两个接口的覆盖面不同（明细按「上榜原因」拆行、机构统计按「股票×日」合并），
    # 硬拼成一行会出现「有的行有机构数、有的行没有」，读的人分不清是缺失还是零。
    """CREATE TABLE IF NOT EXISTS lhb_inst (
        trade_date TEXT NOT NULL, code TEXT NOT NULL,
        market TEXT, name TEXT, close REAL, pct REAL, turnover REAL,
        n_buy_inst INTEGER, n_sell_inst INTEGER,
        inst_buy REAL, inst_sell REAL, inst_net REAL,
        market_amount REAL, inst_net_ratio REAL, reason TEXT,
        src TEXT, fetched_at TEXT,
        PRIMARY KEY (trade_date, code)
    )""",
    "CREATE INDEX IF NOT EXISTS ix_lhb_inst_date ON lhb_inst(trade_date)",

    # 个股融资融券明细。金额=元，数量=股。
    #
    # **是 T+1 公布的**：跑当天要当天的数据会拿到空（实测 22:46 请求当日，
    # 沪市回空、深市回空表）。所以采集前先用沪市汇总接口解析「最近已公布交易日」，
    # 抓那一天——见 `datasources.akshare_events.resolve_margin_date`。
    # 明细本身**可回补**（真实交易日的历史能取到；先前判成「不可回补」是被中秋
    # 假期误导：请求非交易日才回空）。
    #
    # 三家的披露字段不同（沪市无「融券余额」、深市无「融资偿还额」），
    # 缺就写 NULL——不补 0：0 表示「当期没有发生」，NULL 表示「这家不披露」。
    """CREATE TABLE IF NOT EXISTS margin (
        trade_date TEXT NOT NULL, code TEXT NOT NULL,
        market TEXT, exchange TEXT, name TEXT,
        fin_balance REAL, fin_buy REAL, fin_repay REAL,
        short_volume REAL, short_sell REAL, short_repay REAL,
        short_balance REAL, total_balance REAL,
        src TEXT, fetched_at TEXT,
        PRIMARY KEY (trade_date, code)
    )""",
    "CREATE INDEX IF NOT EXISTS ix_margin_date ON margin(trade_date)",
    "CREATE INDEX IF NOT EXISTS ix_margin_code ON margin(code)",

    # 限售解禁。数量=股，市值=元，ratio_of_float=**小数**（0.000234 表示 0.0234%）——
    # 这个字段上游给的是小数而不是百分数，和龙虎榜那几个百分数**方向相反**，
    # 所以必须逐字段标注，不能按表推断。
    #
    # 与其他几张不同，这是**前瞻日历**：未来窗口随时能拉，所以这张表**可以回补**；
    # 但仍要逐日刷新，因为上游会修订解禁股数与市值。
    # 主键含 share_type：同一只股票同一天可能有多批不同类型的限售股解禁。
    """CREATE TABLE IF NOT EXISTS restriction (
        code TEXT NOT NULL, release_date TEXT NOT NULL, share_type TEXT NOT NULL,
        market TEXT, name TEXT,
        release_shares REAL, actual_shares REAL, actual_value REAL,
        ratio_of_float REAL, prev_close REAL,
        src TEXT, fetched_at TEXT,
        PRIMARY KEY (code, release_date, share_type)
    )""",
    "CREATE INDEX IF NOT EXISTS ix_restriction_date ON restriction(release_date)",
    "CREATE INDEX IF NOT EXISTS ix_restriction_code ON restriction(code)",

    # 退市标的清单——**幸存者偏差**的原料：因子评估面板若不纳入已退市标的，
    # 会系统性高估策略收益（跌到退市的那些股票从样本里消失了）。
    # 交易所清单是静态的，因此**可回补**。
    # 主键含 delist_date 而不是只用 code：A 股代码存在被回收再用的可能，
    # 单用 code 做主键会在重用时把前一段历史覆盖掉。
    """CREATE TABLE IF NOT EXISTS delisted (
        code TEXT NOT NULL, delist_date TEXT NOT NULL,
        market TEXT, name TEXT, list_date TEXT,
        src TEXT, fetched_at TEXT,
        PRIMARY KEY (code, delist_date)
    )""",
]


def enabled() -> bool:
    """数据层是否可用（可被环境变量整体关闭）。"""
    return _ENABLED


def configure(path=None, enabled=None):
    """由 run.py 用 config.json 覆盖默认值。

    环境变量优先级更高：命令行/容器里设的 `QUANT_STORE` / `QUANT_DB` 不该被
    一份仓库里的配置文件悄悄改掉。
    """
    global _ENABLED, _conn, _conn_path
    if enabled is not None and "QUANT_STORE" not in os.environ:
        new = bool(enabled)
        if new != _ENABLED:
            close()
            _ENABLED = new
    if path and "QUANT_DB" not in os.environ:
        if os.environ.get("QUANT_DB") != path and _conn_path and _conn_path != path:
            close()
        os.environ["QUANT_DB"] = path


def db_path() -> str:
    return os.environ.get("QUANT_DB", DEFAULT_DB)


def last_error():
    """最近一次被吞掉的异常，供自检脚本区分「没数据」与「库坏了」。"""
    return _last_error


def connect(force: bool = False):
    """返回共享连接；初始化失败或已关闭时返回 None。

    用 `check_same_thread=False` + 模块级锁，而不是每线程一个连接：
    本项目写库只发生在一处（主线程抓取后落盘），读可能来自心跳线程，
    单连接加锁足够且避免多连接下的写冲突。
    """
    global _conn, _conn_path, _last_error
    if not _ENABLED:
        return None
    with _lock:
        path = db_path()
        if _conn is not None and _conn_path == path and not force:
            return _conn
        try:
            os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
            c = sqlite3.connect(path, timeout=10, check_same_thread=False)
            c.row_factory = sqlite3.Row
            c.execute("PRAGMA journal_mode=WAL")      # 读写不互斥
            c.execute("PRAGMA synchronous=NORMAL")    # 单机场景够用，写入快很多
            c.execute("PRAGMA foreign_keys=ON")
            for ddl in DDL:
                c.execute(ddl)
            _migrate(c)
            c.execute("PRAGMA user_version=%d" % SCHEMA_VERSION)
            c.commit()
            _conn, _conn_path = c, path
            return _conn
        except Exception as e:          # 数据层不可用绝不能影响主流程
            _last_error = e
            _conn = None
            return None


def close():
    global _conn, _conn_path
    with _lock:
        if _conn is not None:
            try:
                _conn.close()
            except Exception:
                pass
        _conn, _conn_path = None, None


# 增量迁移：老库补新列。CREATE TABLE IF NOT EXISTS 不会改动已存在的表，
# 所以字段演进必须显式 ALTER，否则旧库会静默缺列、写入报错。
_MIGRATIONS = [
    ("fin_metrics", "notice_source", "TEXT"),
    # 指数成立/发布时间：没有它就无法判断「PE 分位 4%」是在 20 年窗口还是
    # 20 个月窗口上算出来的——后者几乎没有参考价值，而且还容易把「新指数上市时
    # 就贵」读成「现在便宜」。
    ("index_snapshot", "publish_date", "TEXT"),
    # 指数估值序列的**请求窗口**。上游对窗口长于指数成立年限的请求直接返回
    # rc=102，所以只能降级取短窗；不记窗口就分不出「5 年序列」和「1 年序列」。
    ("index_valuation", "window_days", "INTEGER"),
    # 事件四表统一补 market：第一次建表时漏了，而 sibling 表（restriction/delisted）
    # 都有——同一批表里有的带 market 有的不带，查询时就得靠记忆挑写法。
    ("lhb", "market", "TEXT"),
    ("lhb_inst", "market", "TEXT"),
    ("margin", "market", "TEXT"),
    # v7：榜单快照的来源档位。老库补列后历史行是 NULL（当时的来源无从恢复，
    # 只有 data_health 里留着每档的调用记录）。
    ("rank_snapshot", "src", "TEXT"),
    # v8：归一异常统计（字段缺失率）。老库补列后历史行是 NULL——当时的异常
    # 只在日志里出现过，已经查不回来了。
    ("data_health", "anomalies", "TEXT"),
]


def _migrate(c):
    for table, col, coltype in _MIGRATIONS:
        try:
            cols = {r[1] for r in c.execute(f"PRAGMA table_info({table})").fetchall()}
            if cols and col not in cols:
                c.execute(f"ALTER TABLE {table} ADD COLUMN {col} {coltype}")
        except Exception:
            pass


def _now() -> str:
    return datetime.now().strftime("%Y-%m-%d %H:%M:%S")


def _num(v):
    """把 NaN/NaT/空串归一成 None。

    sqlite3 会把 float('nan') 原样传给引擎、最终落成 NULL，但显式转换更安全：
    下游读到 None 会走「缺失」分支，读到 NaN 则可能污染均值计算。
    """
    if v is None:
        return None
    try:
        f = float(v)
    except (TypeError, ValueError):
        return None
    if f != f or f in (float("inf"), float("-inf")):
        return None
    return f


def _exec(sql: str, params=(), many=False):
    """执行写入。任何异常都被吞掉并记入 last_error，返回受影响行数。"""
    global _last_error
    c = connect()
    if c is None:
        return 0
    with _lock:
        try:
            cur = c.executemany(sql, params) if many else c.execute(sql, params)
            n = cur.rowcount
            c.commit()
            return n if n is not None and n >= 0 else 0
        except Exception as e:
            _last_error = e
            try:
                c.rollback()
            except Exception:
                pass
            return 0


def _query(sql: str, params=()) -> list:
    c = connect()
    if c is None:
        return []
    with _lock:
        try:
            return [dict(r) for r in c.execute(sql, params).fetchall()]
        except Exception as e:
            global _last_error
            _last_error = e
            return []


# ---------------------------------------------------------------------------
# 行情
# ---------------------------------------------------------------------------
_UPSERT_KLINE = """INSERT INTO kline
    (market, code, adjust, date, open, high, low, close, volume, amount, src, fetched_at)
    VALUES (?,?,?,?,?,?,?,?,?,?,?,?)
    ON CONFLICT(market, code, adjust, date) DO UPDATE SET
        open=excluded.open, high=excluded.high, low=excluded.low,
        close=excluded.close, volume=excluded.volume, amount=excluded.amount,
        src=excluded.src, fetched_at=excluded.fetched_at"""


def kline_frame(df, market: str, code: str, adjust: str, src: str = ""):
    """把 DataFrame 转成入库元组列表（不写库，供批量合并时复用）。"""
    if df is None or len(df) == 0 or "date" not in df.columns:
        return []
    d = df.copy()
    d["date"] = pd.to_datetime(d["date"], errors="coerce").dt.strftime("%Y-%m-%d")
    d = d[d["date"].notna()]
    if d.empty:
        return []
    cols = {c.lower(): c for c in d.columns}

    def col(name):
        return d[cols[name]] if name in cols else None

    series = {n: col(n) for n in _KLINE_COLS}
    now = _now()
    mk, cd = str(market), str(code)
    recs = []
    dates = d["date"].tolist()
    for i, dt in enumerate(dates):
        recs.append((mk, cd, adjust, dt,
                     _num(series["open"].iloc[i]) if series["open"] is not None else None,
                     _num(series["high"].iloc[i]) if series["high"] is not None else None,
                     _num(series["low"].iloc[i]) if series["low"] is not None else None,
                     _num(series["close"].iloc[i]) if series["close"] is not None else None,
                     _num(series["volume"].iloc[i]) if series["volume"] is not None else None,
                     _num(series["amount"].iloc[i]) if series["amount"] is not None else None,
                     src, now))
    return recs


def upsert_kline(market: str, code: str, adjust: str, df, src: str = "") -> int:
    """写入/更新 K 线（幂等）。返回**实际写入的行数**，失败返回 0。

    刻意不在 0 行时回退成「输入行数」：那样数据层不可用（或 QUANT_STORE=file）
    时会报告「写了 300 行」，把失败伪装成成功，调用方与自检都再也发现不了。
    """
    recs = kline_frame(df, market, code, adjust, src)
    if not recs:
        return 0
    n = _exec(_UPSERT_KLINE, recs, many=True)
    if n:
        _refresh_kline_meta(market, code, adjust, src)
    return n


def _refresh_kline_meta(market: str, code: str, adjust: str, src: str = ""):
    """按表内实际内容重算该序列的 first/last/rows 与抓取时间。

    刻意从表里反查而不是用刚写入的 DataFrame 计算：这样「库里的真实边界」
    与元信息永远一致，重复写入或部分失败都不会让 last_date 虚高。
    """
    rows = _query("""SELECT MIN(date) f, MAX(date) l, COUNT(*) n FROM kline
                     WHERE market=? AND code=? AND adjust=?""",
                  (str(market), str(code), adjust))
    if not rows or rows[0]["n"] in (0, None):
        return
    r = rows[0]
    _exec("""INSERT INTO kline_meta
             (market, code, adjust, first_date, last_date, rows, last_fetch, src)
             VALUES (?,?,?,?,?,?,?,?)
             ON CONFLICT(market, code, adjust) DO UPDATE SET
                 first_date=excluded.first_date, last_date=excluded.last_date,
                 rows=excluded.rows, last_fetch=excluded.last_fetch, src=excluded.src""",
          (str(market), str(code), adjust, r["f"], r["l"], int(r["n"]), _now(), src))


def load_kline(market: str, code: str, adjust: str, limit=None):
    """读回 K 线（按日期升序）。无数据返回 None，与「取到空表」区分开。

    列顺序刻意与 `fetcher.fetch_kline` 保持一致（date, open, close, high, low,
    volume, amount）——下游大量代码按下标或列名混用，两条来源列序不同迟早出事。
    """
    if not _ENABLED:
        return None
    sql = ("SELECT date, open, close, high, low, volume, amount FROM kline "
           "WHERE market=? AND code=? AND adjust=? ORDER BY date")
    rows = _query(sql, (str(market), str(code), adjust))
    if not rows:
        return None
    if limit and len(rows) > limit:
        rows = rows[-int(limit):]
    df = pd.DataFrame(rows)
    df["date"] = pd.to_datetime(df["date"])
    return df


def kline_meta(market: str, code: str, adjust: str):
    rows = _query("""SELECT * FROM kline_meta
                     WHERE market=? AND code=? AND adjust=?""",
                  (str(market), str(code), adjust))
    return rows[0] if rows else None


def market_latest_date(market: str, adjust: str):
    """该市场库内最新的 K 线日期（判断某标的是否落后于市场）。"""
    rows = _query("""SELECT MAX(last_date) d FROM kline_meta
                     WHERE market=? AND adjust=?""", (str(market), adjust))
    return rows[0]["d"] if rows and rows[0]["d"] else None


def kline_is_fresh(market: str, code: str, adjust: str, ttl_hours: float = 4.0,
                   min_rows: int = None, close_hour: int = 16):
    """库中数据是否可当次直接复用（替代一次网络抓取）。

    四个条件同时成立才复用，少一条都可能把「昨天的价」当成「今天的价」：

    1. **距上次抓取不到 ttl_hours**。跨日必须重抓，否则永远看不到新行情。
    2. **该标的 last_date 不低于同市场最新日期**。否则它是落后的，复用会得到
       一条比其他标的短一截的序列，面板截面宽度对不齐。
    3. **行数够用**（min_rows）。库里只有 300 根时不能拿来顶 900 根的请求。
    4. **没有跨越当日收盘时点**。这条最容易漏：盘中 14:00 抓到的最后一根是
       「未收盘的实时价」，16:00 之后行情已经定盘，若还按 TTL 命中，就等于
       把盘中价当收盘价灌进回测与复盘——数字会错得毫无声响。
    """
    if not _ENABLED:
        return False
    m = kline_meta(market, code, adjust)
    if not m or not m.get("last_fetch") or not m.get("last_date"):
        return False
    try:
        last_fetch = datetime.strptime(m["last_fetch"], "%Y-%m-%d %H:%M:%S")
    except Exception:
        return False
    now = datetime.now()
    if now - last_fetch > timedelta(hours=float(ttl_hours)):
        return False
    if min_rows and int(m.get("rows") or 0) < int(min_rows):
        return False
    if (last_fetch.date() == now.date()
            and last_fetch.hour < int(close_hour) <= now.hour):
        return False
    latest = market_latest_date(market, adjust)
    return bool(latest) and str(m["last_date"]) >= str(latest)


# ---------------------------------------------------------------------------
# 榜单快照
# ---------------------------------------------------------------------------
def upsert_rank_snapshot(market: str, board: str, snap_date: str, df, src: str = "") -> int:
    """落库每日榜单快照。board 区分榜单类型（跌幅榜/成交额榜/ETF池…）。"""
    if df is None or len(df) == 0 or "code" not in df.columns:
        return 0
    now = _now()
    mk, bd, sd = str(market), str(board), str(snap_date)[:10]

    def g(row, name):
        v = row.get(name)
        return _num(v) if name in ("price", "pct", "amount", "pe_ttm", "float_mcap",
                                   "turnover_ratio", "volume_ratio") else v

    recs = []
    for _, row in df.iterrows():
        recs.append((mk, bd, sd, str(row["code"]),
                     None if pd.isna(row.get("name")) else str(row.get("name")),
                     g(row, "price"), g(row, "pct"), g(row, "amount"),
                     g(row, "pe_ttm"), g(row, "float_mcap"),
                     g(row, "turnover_ratio"), g(row, "volume_ratio"),
                     src, now))
    return _exec("""INSERT INTO rank_snapshot
        (market, board, snap_date, code, name, price, pct, amount, pe_ttm,
         float_mcap, turnover_ratio, volume_ratio, src, fetched_at)
        VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)
        ON CONFLICT(market, board, snap_date, code) DO UPDATE SET
            name=excluded.name, price=excluded.price, pct=excluded.pct,
            amount=excluded.amount, pe_ttm=excluded.pe_ttm,
            float_mcap=excluded.float_mcap, turnover_ratio=excluded.turnover_ratio,
            volume_ratio=excluded.volume_ratio, src=excluded.src,
            fetched_at=excluded.fetched_at""",
                recs, many=True)


def load_rank_snapshot(market: str, board: str, snap_date=None):
    if snap_date:
        rows = _query("""SELECT * FROM rank_snapshot
                         WHERE market=? AND board=? AND snap_date=?
                         ORDER BY amount DESC""", (market, board, str(snap_date)[:10]))
    else:
        rows = _query("""SELECT * FROM rank_snapshot
                         WHERE market=? AND board=?
                           AND snap_date=(SELECT MAX(snap_date) FROM rank_snapshot
                                          WHERE market=? AND board=?)
                         ORDER BY amount DESC""", (market, board, market, board))
    return pd.DataFrame(rows) if rows else pd.DataFrame()


# ---------------------------------------------------------------------------
# 财务 / 基金 / 指数 / 行业
# ---------------------------------------------------------------------------
def _f10_get(row: dict, *keys):
    """从一份财务记录里按顺序取第一个存在的值（兼容东财 F10 与中信建投两套列名）。"""
    for k in keys:
        if k in row and row[k] not in (None, ""):
            return row[k]
    return None


def fin_record(row: dict, market: str, code: str, src: str) -> dict:
    """把不同来源的财务记录归一成 fin_metrics 的一行。

    东财 F10 与中信建投关键指标是两套列名，这里做统一映射；
    两边都没有的字段留 None，不做任何推算。

    公告日来源分级（notice_source）：
      - 源记录自带 `NOTICE_DATE` → 'disclosure'（交易所披露的真实日期）
      - 源记录带 `_notice_source` → 沿用其标记（如中信建投的 'statutory_deadline'）
      - 两者都没有 → None，此时下游必须按「无公告日」处理，不能拿 REPORT_DATE 顶上
    """
    rd = str(_f10_get(row, "REPORT_DATE", "reportDate") or "")[:10]
    nd = str(_f10_get(row, "NOTICE_DATE", "noticeDate") or "")[:10]
    if row.get("_notice_source"):
        nsrc = str(row["_notice_source"])
    elif nd:
        nsrc = "disclosure"
    else:
        nsrc = None
    return {
        "market": str(market), "code": str(code), "report_date": rd,
        "notice_date": nd or None, "notice_source": nsrc,
        "revenue": _num(_f10_get(row, "TOTALOPERATEREVE", "totalRevenue")),
        "net_profit": _num(_f10_get(row, "PARENTNETPROFIT", "netProfitAtsopc")),
        "net_profit_yoy": _num(_f10_get(row, "PARENTNETPROFITTZ", "netProfitAtsopcYoy",
                                        "netProfitAtsopcTb")),
        "revenue_yoy": _num(_f10_get(row, "TOTALOPERATEREVETZ", "revenueYoy", "totalRevenueTb")),
        "roe": _num(_f10_get(row, "ROEJQ", "wgtAvgRoe")),
        "gross_margin": _num(_f10_get(row, "XSMLL", "grossSellingRate")),
        "net_margin": _num(_f10_get(row, "XSJLL", "netSellingRate")),
        "debt_ratio": _num(_f10_get(row, "ZCFZL", "assetLiabRatio")),
        "eps": _num(_f10_get(row, "EPSJB", "basicEps")),
        "bps": _num(_f10_get(row, "BPS", "npPerShare")),
        "ocf_ps": _num(_f10_get(row, "MGJYXJJE", "operateCashFlowPs")),
        "src": src, "fetched_at": _now(),
        "payload": json.dumps(row, ensure_ascii=False, default=str),
    }


def upsert_financial(market: str, code: str, rows: list, src: str = "") -> int:
    """落库财务记录。rows 为原始记录列表（东财 F10 或中信建投返回体）。"""
    if not rows:
        return 0
    recs = []
    for r in rows:
        if not isinstance(r, dict):
            continue
        rec = fin_record(r, market, code, src)
        if not rec["report_date"]:
            continue
        recs.append(tuple(rec[k] for k in
                          ("market", "code", "report_date", "notice_date",
                           "notice_source", "revenue",
                           "net_profit", "net_profit_yoy", "revenue_yoy", "roe",
                           "gross_margin", "net_margin", "debt_ratio", "eps", "bps",
                           "ocf_ps", "src", "fetched_at", "payload")))
    if not recs:
        return 0
    # 公告日的覆盖规则：**真实公告日优先于法定截止日代理**。
    # 东财先写、中信建投后写时，若不做这层保护，代理日会把真实公告日冲掉，
    # 表现为「同一份报告期，公告日莫名晚了一个月」——回放时点跟着整体右移。
    return _exec("""INSERT INTO fin_metrics
        (market, code, report_date, notice_date, notice_source, revenue, net_profit,
         net_profit_yoy, revenue_yoy, roe, gross_margin, net_margin, debt_ratio, eps,
         bps, ocf_ps, src, fetched_at, payload)
        VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
        ON CONFLICT(market, code, report_date) DO UPDATE SET
            notice_date=CASE
                WHEN excluded.notice_source = 'disclosure' THEN excluded.notice_date
                WHEN fin_metrics.notice_source = 'disclosure' THEN fin_metrics.notice_date
                ELSE COALESCE(COALESCE(excluded.notice_date, fin_metrics.notice_date),
                              fin_metrics.notice_date)
            END,
            notice_source=CASE
                WHEN excluded.notice_source = 'disclosure' THEN excluded.notice_source
                WHEN fin_metrics.notice_source = 'disclosure' THEN fin_metrics.notice_source
                ELSE COALESCE(excluded.notice_source, fin_metrics.notice_source)
            END,
            revenue=COALESCE(excluded.revenue, fin_metrics.revenue),
            net_profit=COALESCE(excluded.net_profit, fin_metrics.net_profit),
            net_profit_yoy=COALESCE(excluded.net_profit_yoy, fin_metrics.net_profit_yoy),
            revenue_yoy=COALESCE(excluded.revenue_yoy, fin_metrics.revenue_yoy),
            roe=COALESCE(excluded.roe, fin_metrics.roe),
            gross_margin=COALESCE(excluded.gross_margin, fin_metrics.gross_margin),
            net_margin=COALESCE(excluded.net_margin, fin_metrics.net_margin),
            debt_ratio=COALESCE(excluded.debt_ratio, fin_metrics.debt_ratio),
            eps=COALESCE(excluded.eps, fin_metrics.eps),
            bps=COALESCE(excluded.bps, fin_metrics.bps),
            ocf_ps=COALESCE(excluded.ocf_ps, fin_metrics.ocf_ps),
            src=excluded.src, fetched_at=excluded.fetched_at,
            payload=excluded.payload""", recs, many=True)


def load_financial(market: str, code: str, limit=None):
    rows = _query("""SELECT * FROM fin_metrics WHERE market=? AND code=?
                     ORDER BY report_date DESC""", (str(market), str(code)))
    if limit:
        rows = rows[:int(limit)]
    return rows


def upsert_index_snapshot(rows: list, src: str = "") -> int:
    """落库指数估值截面。"""
    if not rows:
        return 0
    now = _now()
    recs = []
    for r in rows:
        code = str(r.get("securityCode") or r.get("indexCode") or "")
        mkt = str(r.get("marketCode") or r.get("indexMarket") or "")
        if not code:
            continue
        recs.append((code, mkt, r.get("securityName") or r.get("indexName"),
                     _num(r.get("indexType")), _num(r.get("pe")), _num(r.get("pb")),
                     _num(r.get("pePercentile")), _num(r.get("pbPercentile")),
                     _num(r.get("roe")), _num(r.get("dividendRatio")),
                     _num(r.get("change1Year")), _num(r.get("maxDrawdown1Year")),
                     str(r.get("publishDate") or "")[:10] or None,
                     str(r.get("updTime") or ""), src, now))
    if not recs:
        return 0
    return _exec("""INSERT INTO index_snapshot
        (index_code, market, name, index_type, pe, pb, pe_pct, pb_pct, roe,
         div_yield, chg_1y, mdd_1y, publish_date, upd_time, src, fetched_at)
        VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
        ON CONFLICT(index_code, market) DO UPDATE SET
            name=excluded.name, index_type=excluded.index_type, pe=excluded.pe,
            pb=excluded.pb, pe_pct=excluded.pe_pct, pb_pct=excluded.pb_pct,
            roe=excluded.roe, div_yield=excluded.div_yield, chg_1y=excluded.chg_1y,
            mdd_1y=excluded.mdd_1y, publish_date=COALESCE(excluded.publish_date,
                                                          index_snapshot.publish_date),
            upd_time=excluded.upd_time,
            src=excluded.src, fetched_at=excluded.fetched_at""", recs, many=True)


def upsert_index_valuation(index_code: str, market: str, metric: str,
                           history: list, src: str = "",
                           window_days: int = None) -> int:
    """落库指数估值历史序列（metric: pe / pb）。

    `window_days` 记录该行来自多长的请求窗口。冲突时取更宽的那一个——短窗是
    长窗的子集，用短窗结果覆盖长窗只会让「这条序列覆盖几年」的信息单向丢失。
    """
    if not history:
        return 0
    now = _now()
    wd = _num(window_days)
    recs = []
    for h in history:
        d = str(h.get("date") or "")[:10]
        v = _num(h.get("value"))
        if not d or v is None:
            continue
        recs.append((str(index_code), str(market), metric, d, v, wd, src, now))
    if not recs:
        return 0
    return _exec("""INSERT INTO index_valuation
        (index_code, market, metric, date, value, window_days, src, fetched_at)
        VALUES (?,?,?,?,?,?,?,?)
        ON CONFLICT(index_code, market, metric, date) DO UPDATE SET
            value=excluded.value, src=excluded.src, fetched_at=excluded.fetched_at,
            window_days=MAX(COALESCE(excluded.window_days, 0),
                            COALESCE(index_valuation.window_days, 0))""",
                recs, many=True)


def load_index_snapshot(limit=None):
    sql = "SELECT * FROM index_snapshot"
    if limit:
        sql += f" ORDER BY pe_pct ASC LIMIT {int(limit)}"
    return pd.DataFrame(_query(sql))


def load_index_valuation(index_code: str, market: str, metric: str):
    rows = _query("""SELECT date, value, window_days FROM index_valuation
                     WHERE index_code=? AND market=? AND metric=? ORDER BY date""",
                  (str(index_code), str(market), metric))
    return pd.DataFrame(rows) if rows else pd.DataFrame()


def index_valuation_coverage():
    """每条指数估值序列覆盖到的区间与实际请求窗口。

    单看行数无法判断序列质量：一条 1 年序列和一条 5 年序列都只是「一串日期」。
    这个视图把 `window_days` 与真实跨度一起摆出来，供展示与自检核对。
    """
    return _query("""SELECT index_code, market, metric, COUNT(*) n,
                            MIN(date) first_date, MAX(date) last_date,
                            MAX(COALESCE(window_days, 0)) window_days,
                            MAX(src) src
                     FROM index_valuation GROUP BY index_code, market, metric
                     ORDER BY window_days DESC, index_code""")


def upsert_industry_rank(code: str, metric: str, report_date: str, industry: str,
                         rank: str, industry_avg, peers: list, src: str = "") -> int:
    payload = json.dumps(peers or [], ensure_ascii=False, default=str)
    return _exec("""INSERT INTO industry_rank
        (code, metric, report_date, industry, rank, industry_avg, n_peers, src,
         fetched_at, payload)
        VALUES (?,?,?,?,?,?,?,?,?,?)
        ON CONFLICT(code, metric, report_date) DO UPDATE SET
            industry=excluded.industry, rank=excluded.rank,
            industry_avg=excluded.industry_avg, n_peers=excluded.n_peers,
            src=excluded.src, fetched_at=excluded.fetched_at,
            payload=excluded.payload""",
                (str(code), str(metric), str(report_date)[:10], industry, str(rank),
                 None if industry_avg is None else str(industry_avg),
                 len(peers or []), src, _now(), payload))


def load_industry_rank(code: str, metric: str = "pe"):
    rows = _query("""SELECT * FROM industry_rank WHERE code=? AND metric=?
                     ORDER BY report_date DESC LIMIT 1""", (str(code), metric))
    return rows[0] if rows else None


def upsert_fund_profile(code: str, name=None, scale=None, manager=None,
                        payload=None, src: str = "") -> int:
    blob = json.dumps(payload, ensure_ascii=False, default=str) if payload is not None else None
    return _exec("""INSERT INTO fund_profile
        (code, name, scale, manager, src, fetched_at, payload)
        VALUES (?,?,?,?,?,?,?)
        ON CONFLICT(code) DO UPDATE SET
            name=COALESCE(excluded.name, fund_profile.name),
            scale=COALESCE(excluded.scale, fund_profile.scale),
            manager=COALESCE(excluded.manager, fund_profile.manager),
            src=excluded.src, fetched_at=excluded.fetched_at,
            payload=COALESCE(excluded.payload, fund_profile.payload)""",
                (str(code), name, _num(scale), manager, src, _now(), blob))


# ---------------------------------------------------------------------------
# 宏观快照
# ---------------------------------------------------------------------------
def upsert_macro_snapshot(run_date: str, ms: dict, src: str = "eastmoney") -> int:
    """落一天的宏观打分（含建议仓位与风格倾向）。

    整行覆盖而不是 COALESCE 合并：宏观分是当日**当时的判断**，用新值覆盖旧值
    才是正确语义（同一天重跑取最后一次）；缺字段时写 NULL，好过留着昨天的分
    让人误以为今天也算过。
    """
    ms = ms or {}
    stance = ms.get("equity_stance") or []
    lo = stance[0] if len(stance) > 0 else None
    hi = stance[1] if len(stance) > 1 else None
    style = ms.get("style_bias")
    missing = ms.get("missing")
    blob = json.dumps(ms, ensure_ascii=False, default=str)
    return _exec("""INSERT INTO macro_snapshot
        (run_date, score, label, stance_lo, stance_hi, style_bias, missing,
         src, fetched_at, payload)
        VALUES (?,?,?,?,?,?,?,?,?,?)
        ON CONFLICT(run_date) DO UPDATE SET
            score=excluded.score, label=excluded.label,
            stance_lo=excluded.stance_lo, stance_hi=excluded.stance_hi,
            style_bias=excluded.style_bias, missing=excluded.missing,
            src=excluded.src, fetched_at=excluded.fetched_at,
            payload=excluded.payload""",
                (str(run_date), _num(ms.get("score")), ms.get("label"),
                 _num(lo), _num(hi),
                 json.dumps(style, ensure_ascii=False) if style is not None else None,
                 json.dumps(missing, ensure_ascii=False) if missing is not None else None,
                 src, _now(), blob))


# ---------------------------------------------------------------------------
# 走前 ML 验证结论
# ---------------------------------------------------------------------------
def save_ml_eval(run_date: str, res: dict, src: str = "ml_eval") -> int:
    """落一次 ML 验证的结论：基准行（model='rule'）+ 每个候选一行。

    为什么必须入库：面板是**滚动**的——今天的样本外截面集合明天就不再是同一个，
    同一个模型在同一个日期上的结论无法事后重算。这与「原始指标可重抓、判断不可
    重抓」是同一条判据。
    """
    res = res or {}
    rule = res.get("rule")
    if not rule:
        return 0
    ret_col = str(res.get("ret_col") or "")
    feats = json.dumps(res.get("features") or [], ensure_ascii=False)
    verdict = str(res.get("verdict") or "")
    gate = res.get("gate") or {}
    now = _now()
    rows = [(str(run_date), ret_col, "rule", int(rule.get("n_periods") or 0), None,
             _num(rule.get("ic_mean")), _num(rule.get("icir")), _num(rule.get("t")),
             _num(rule.get("positive_rate")), _num(rule.get("ic_mean_trimmed")),
             _num(rule.get("ic_mean")), _num(rule.get("icir")),
             _num(rule.get("ic_mean_trimmed")), None, verdict, feats, src, now)]
    for kind, c in sorted((res.get("candidates") or {}).items()):
        rows.append((str(run_date), ret_col, str(kind),
                     int(c.get("n_periods") or 0), int(c.get("n_oof") or 0),
                     _num(c.get("ic_mean")), _num(c.get("icir")), _num(c.get("t")),
                     _num(c.get("positive_rate")), _num(c.get("ic_mean_trimmed")),
                     _num(rule.get("ic_mean")), _num(rule.get("icir")),
                     _num(rule.get("ic_mean_trimmed")),
                     1 if gate.get("passed") else 0, verdict, feats, src, now))
    return _exec("""INSERT INTO ml_eval
        (run_date, ret_col, model, n_periods, n_oof, ic_mean, icir, t, positive_rate,
         ic_mean_trimmed, rule_ic_mean, rule_icir, rule_ic_mean_trimmed,
         gate_passed, verdict, features, src, fetched_at)
        VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
        ON CONFLICT(run_date, ret_col, model) DO UPDATE SET
            n_periods=excluded.n_periods, n_oof=excluded.n_oof,
            ic_mean=excluded.ic_mean, icir=excluded.icir, t=excluded.t,
            positive_rate=excluded.positive_rate,
            ic_mean_trimmed=excluded.ic_mean_trimmed,
            rule_ic_mean=excluded.rule_ic_mean, rule_icir=excluded.rule_icir,
            rule_ic_mean_trimmed=excluded.rule_ic_mean_trimmed,
            gate_passed=excluded.gate_passed, verdict=excluded.verdict,
            features=excluded.features, src=excluded.src,
            fetched_at=excluded.fetched_at""", rows, many=True)


def ml_eval_history(limit: int = 40):
    """ML 验证结论历史（新→旧），用于回看「门禁是不是偶尔才通过」。"""
    return _query("""SELECT run_date, ret_col, model, n_periods, n_oof, ic_mean, icir,
                            ic_mean_trimmed, rule_ic_mean, rule_icir,
                            rule_ic_mean_trimmed, gate_passed
                     FROM ml_eval ORDER BY run_date DESC, model LIMIT ?""",
                  (int(limit),))


def load_macro_snapshot(run_date: str = None):
    """取某天的宏观快照；不传日期则取最近一条。"""
    if run_date:
        rows = _query("SELECT * FROM macro_snapshot WHERE run_date=?", (str(run_date),))
    else:
        rows = _query("SELECT * FROM macro_snapshot ORDER BY run_date DESC LIMIT 1")
    return rows[0] if rows else None


def macro_history(limit: int = 60):
    """宏观分历史（新→旧），用于回看判断与后续超额的关系。"""
    return _query("""SELECT run_date, score, label, stance_lo, stance_hi, style_bias
                     FROM macro_snapshot ORDER BY run_date DESC LIMIT ?""",
                  (int(limit),))


# ---------------------------------------------------------------------------
# 个股资金流（Phase 1 新数据源）
# ---------------------------------------------------------------------------
# 上游列名（同花顺）的归一在 adapter 层完成，这里只接受规范列。
# 行数口径刻意返回**实际写入行数**：0 行既可能是「没取到」，也可能是「库不可用」，
# 调用方必须能看出来——返 0 而不是返输入行数，是既有代码付过学费的纪律。
_FUND_FLOW_COLS = ("code", "name", "price", "pct", "turnover",
                   "inflow", "outflow", "net", "amount")

_UPSERT_FUND_FLOW = """INSERT INTO fund_flow
    (market, code, flow_date, name, price, pct, turnover,
     inflow, outflow, net, amount, src, fetched_at)
    VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)
    ON CONFLICT(market, code, flow_date) DO UPDATE SET
        name=excluded.name, price=excluded.price, pct=excluded.pct,
        turnover=excluded.turnover, inflow=excluded.inflow,
        outflow=excluded.outflow, net=excluded.net, amount=excluded.amount,
        src=excluded.src, fetched_at=excluded.fetched_at"""


def fund_flow_frame(df, market: str, flow_date: str, src: str = ""):
    """DataFrame → 入库元组列表（不写库）。

    缺 `code` 列直接返回空：没有主键之一的记录写进去只会变成一行查不回来的脏数据。
    单个字段缺失写 NULL（「不知道」），不写 0——0 在因子里是「零净流入」这个**事实**。
    """
    if df is None or len(df) == 0 or "code" not in df.columns:
        return []
    d = df.copy()
    d["code"] = d["code"].astype(str).str.strip()
    d = d[d["code"] != ""]
    if d.empty:
        return []
    now = _now()
    mk, fd = str(market), str(flow_date)
    recs = []
    for _, r in d.iterrows():
        name = r.get("name") if "name" in d.columns else None
        recs.append((mk, r["code"], fd,
                     (str(name) if pd.notna(name) else None),
                     *[_num(r.get(c)) if c in d.columns else None
                       for c in ("price", "pct", "turnover",
                                 "inflow", "outflow", "net", "amount")],
                     src, now))
    return recs


def upsert_fund_flow(market: str, flow_date: str, df, src: str = "") -> int:
    """写入/更新某交易日的个股资金流截面（幂等）。返回实际写入行数，失败返回 0。

    同一 (market, code, flow_date) 重跑是**覆盖**而不是并存：上游对当日数据会盘中修订，
    留着两份会让下游不知道该用哪一份。
    """
    recs = fund_flow_frame(df, market, flow_date, src)
    if not recs:
        return 0
    return _exec(_UPSERT_FUND_FLOW, recs, many=True)


def load_fund_flow(flow_date: str = None, market: str = None, limit: int = None):
    """读回资金流截面（按日期升序、代码升序）。无数据返回 None。

    返回 None 而不是空表：调用方需要区分「这一天没数据」与「这一天全市净流出为零」。
    """
    if not _ENABLED:
        return None
    sql = ("SELECT market, code, flow_date, name, price, pct, turnover, "
           "inflow, outflow, net, amount, src FROM fund_flow WHERE 1=1")
    params = []
    if flow_date:
        sql += " AND flow_date=?"
        params.append(str(flow_date))
    if market:
        sql += " AND market=?"
        params.append(str(market))
    sql += " ORDER BY flow_date, code"
    rows = _query(sql, tuple(params))
    if not rows:
        return None
    if limit and len(rows) > limit:
        rows = rows[-int(limit):]
    return pd.DataFrame(rows)


def fund_flow_coverage(market: str = None):
    """按交易日统计覆盖：哪几天有数据、每天多少只、合计净额（亿元）。

    「净额合计」只是覆盖检查的顺手指标，不是策略信号——它随当日涨跌整体漂移。
    """
    sql = """SELECT flow_date, COUNT(*) n, COUNT(DISTINCT code) syms,
                    ROUND(SUM(COALESCE(net, 0))/1e8, 2) net_yi,
                    MAX(fetched_at) last_fetch
             FROM fund_flow"""
    params = []
    if market:
        sql += " WHERE market=?"
        params.append(str(market))
    sql += " GROUP BY flow_date ORDER BY flow_date"
    return _query(sql, tuple(params))


def fund_flow_is_fresh(flow_date: str, market: str = None,
                       close_hhmm: str = "15:00") -> bool:
    """当日资金流截面能否复用（顶替一次网络抓取）。

    唯一的判据是**上次抓取是否发生在该交易日收盘之后**：

    - 收盘后抓到的快照是**定盘的**，当天再跑多少次都该复用它（省 14 秒，也少打一次上游）；
    - 盘中抓到的快照会继续变，盘后**必须重抓一次覆盖**，否则盘中那一刻的资金流会被
      当成「当日最终值」落进表里，再流进因子检验。

    这和 K 线「跨过当日收盘时点必须失效」是同一个坑（`kline_is_fresh` 四条件之一）。
    盘中多次重跑不会被这里的复用挡住——那时 `fetched_at` 还没过收盘时点，判 False。
    """
    if not _ENABLED or not flow_date:
        return False
    sql = "SELECT COUNT(*) n, MAX(fetched_at) last_fetch FROM fund_flow WHERE flow_date=?"
    params = [str(flow_date)]
    if market:
        sql += " AND market=?"
        params.append(str(market))
    rows = _query(sql, tuple(params))
    if not rows or not rows[0]["n"]:
        return False
    last = rows[0]["last_fetch"]
    if not last:
        return False
    settle = f"{str(flow_date)[:10]} {close_hhmm}:00"
    return str(last)[:19] >= settle


# ---------------------------------------------------------------------------
# 事件类数据（Phase 1 Step 3）：龙虎榜 / 两融 / 解禁 / 退市
#
# 五张表的写入口径统一走 `_frame_rows`：缺列写 NULL、空值归一成 None、
# 主键字段为空的整行丢弃（缺主键的行写进去只会变成查不回来的脏数据）。
# 上层适配器（`datasources.akshare_source`）负责把上游单位/日期格式归一，
# 落盘层只认规范列——和 `fund_flow` 同一条分工。
# ---------------------------------------------------------------------------
_LHB_ALL = ("trade_date", "code", "reason", "market", "name", "note", "close", "pct",
            "turnover", "net_buy", "buy", "sell", "amount", "market_amount",
            "net_ratio", "amount_ratio")
_LHB_TEXT = ("trade_date", "code", "reason", "market", "name", "note")
_LHB_KEY = ("trade_date", "code", "reason")

_LHB_INST_ALL = ("trade_date", "code", "market", "name", "close", "pct", "turnover",
                 "n_buy_inst", "n_sell_inst", "inst_buy", "inst_sell", "inst_net",
                 "market_amount", "inst_net_ratio", "reason")
_LHB_INST_TEXT = ("trade_date", "code", "market", "name", "reason")
_LHB_INST_KEY = ("trade_date", "code")

_MARGIN_ALL = ("trade_date", "code", "market", "exchange", "name",
               "fin_balance", "fin_buy", "fin_repay",
               "short_volume", "short_sell", "short_repay",
               "short_balance", "total_balance")
_MARGIN_TEXT = ("trade_date", "code", "market", "exchange", "name")
_MARGIN_KEY = ("trade_date", "code")

_RESTRICTION_ALL = ("code", "release_date", "share_type", "market", "name",
                    "release_shares", "actual_shares", "actual_value",
                    "ratio_of_float", "prev_close")
_RESTRICTION_TEXT = ("code", "release_date", "share_type", "market", "name")
_RESTRICTION_KEY = ("code", "release_date", "share_type")

_DELISTED_ALL = ("code", "delist_date", "market", "name", "list_date")
_DELISTED_TEXT = ("code", "delist_date", "market", "name", "list_date")
_DELISTED_KEY = ("code", "delist_date")


def _frame_rows(df, cols, text_cols=(), key_cols=(), src=""):
    """DataFrame → 入库元组列表：按 `cols` 取列（缺列写 NULL）、空值归一成 None。

    主键字段为空的整行**丢弃**：主键是查询的唯一入口，缺了它这行就再也拿不出来，
    只会占着空间让覆盖率统计虚高。
    """
    if df is None or len(df) == 0:
        return []
    now = _now()
    recs = []
    for _, r in df.iterrows():
        vals = []
        for c in cols:
            v = r.get(c) if c in df.columns else None
            if c in text_cols:
                if v is None or (isinstance(v, float) and v != v):
                    vals.append(None)
                else:
                    s = str(v).strip()
                    vals.append(s or None)
            else:
                vals.append(_num(v))
        if key_cols and any(vals[cols.index(k)] is None for k in key_cols):
            continue
        recs.append(tuple(vals) + (src, now))
    return recs


def _bulk_upsert(sql: str, recs: list) -> int:
    """批量 upsert，返回**实际写入行数**（0 表示没写进去，不是「写了 0 行」）。"""
    if not recs:
        return 0
    return _exec(sql, recs, many=True)


_UPSERT_LHB = """INSERT INTO lhb
    (trade_date, code, reason, market, name, note, close, pct, turnover,
     net_buy, buy, sell, amount, market_amount, net_ratio, amount_ratio,
     src, fetched_at)
    VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
    ON CONFLICT(trade_date, code, reason) DO UPDATE SET
        market=excluded.market, name=excluded.name, note=excluded.note,
        close=excluded.close,
        pct=excluded.pct, turnover=excluded.turnover, net_buy=excluded.net_buy,
        buy=excluded.buy, sell=excluded.sell, amount=excluded.amount,
        market_amount=excluded.market_amount, net_ratio=excluded.net_ratio,
        amount_ratio=excluded.amount_ratio, src=excluded.src,
        fetched_at=excluded.fetched_at"""

_UPSERT_LHB_INST = """INSERT INTO lhb_inst
    (trade_date, code, market, name, close, pct, turnover, n_buy_inst,
     n_sell_inst, inst_buy, inst_sell, inst_net, market_amount,
     inst_net_ratio, reason, src, fetched_at)
    VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
    ON CONFLICT(trade_date, code) DO UPDATE SET
        market=excluded.market, name=excluded.name, close=excluded.close,
        pct=excluded.pct,
        turnover=excluded.turnover, n_buy_inst=excluded.n_buy_inst,
        n_sell_inst=excluded.n_sell_inst, inst_buy=excluded.inst_buy,
        inst_sell=excluded.inst_sell, inst_net=excluded.inst_net,
        market_amount=excluded.market_amount,
        inst_net_ratio=excluded.inst_net_ratio, reason=excluded.reason,
        src=excluded.src, fetched_at=excluded.fetched_at"""

_UPSERT_MARGIN = """INSERT INTO margin
    (trade_date, code, market, exchange, name, fin_balance, fin_buy,
     fin_repay, short_volume, short_sell, short_repay, short_balance,
     total_balance, src, fetched_at)
    VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
    ON CONFLICT(trade_date, code) DO UPDATE SET
        market=excluded.market, exchange=excluded.exchange, name=excluded.name,
        fin_balance=excluded.fin_balance, fin_buy=excluded.fin_buy,
        fin_repay=excluded.fin_repay, short_volume=excluded.short_volume,
        short_sell=excluded.short_sell, short_repay=excluded.short_repay,
        short_balance=excluded.short_balance,
        total_balance=excluded.total_balance, src=excluded.src,
        fetched_at=excluded.fetched_at"""

_UPSERT_RESTRICTION = """INSERT INTO restriction
    (code, release_date, share_type, market, name, release_shares,
     actual_shares, actual_value, ratio_of_float, prev_close, src, fetched_at)
    VALUES (?,?,?,?,?,?,?,?,?,?,?,?)
    ON CONFLICT(code, release_date, share_type) DO UPDATE SET
        market=excluded.market, name=excluded.name,
        release_shares=excluded.release_shares,
        actual_shares=excluded.actual_shares,
        actual_value=excluded.actual_value,
        ratio_of_float=excluded.ratio_of_float,
        prev_close=excluded.prev_close, src=excluded.src,
        fetched_at=excluded.fetched_at"""

_UPSERT_DELISTED = """INSERT INTO delisted
    (code, delist_date, market, name, list_date, src, fetched_at)
    VALUES (?,?,?,?,?,?,?)
    ON CONFLICT(code, delist_date) DO UPDATE SET
        market=excluded.market, name=excluded.name, list_date=excluded.list_date,
        src=excluded.src, fetched_at=excluded.fetched_at"""


def upsert_lhb(df, src: str = "") -> int:
    """龙虎榜明细（幂等，按 交易日+代码+上榜原因）。"""
    return _bulk_upsert(_UPSERT_LHB,
                        _frame_rows(df, _LHB_ALL, _LHB_TEXT, _LHB_KEY, src))


def upsert_lhb_inst(df, src: str = "") -> int:
    """龙虎榜机构买卖统计（幂等，按 交易日+代码）。"""
    return _bulk_upsert(_UPSERT_LHB_INST,
                        _frame_rows(df, _LHB_INST_ALL, _LHB_INST_TEXT,
                                    _LHB_INST_KEY, src))


def upsert_margin(df, src: str = "") -> int:
    """个股融资融券明细（幂等，按 交易日+代码）。"""
    return _bulk_upsert(_UPSERT_MARGIN,
                        _frame_rows(df, _MARGIN_ALL, _MARGIN_TEXT, _MARGIN_KEY, src))


def upsert_restriction(df, src: str = "") -> int:
    """限售解禁明细（幂等，按 代码+解禁日+限售股类型）。"""
    return _bulk_upsert(_UPSERT_RESTRICTION,
                        _frame_rows(df, _RESTRICTION_ALL, _RESTRICTION_TEXT,
                                    _RESTRICTION_KEY, src))


def upsert_delisted(df, src: str = "") -> int:
    """退市标的清单（幂等，按 代码+退市日）。"""
    return _bulk_upsert(_UPSERT_DELISTED,
                        _frame_rows(df, _DELISTED_ALL, _DELISTED_TEXT,
                                    _DELISTED_KEY, src))


# 事件表的日期列：覆盖率统计与读取都靠它，写死映射而不是猜列名。
_EVENT_DATE_COL = {"lhb": "trade_date", "lhb_inst": "trade_date",
                   "margin": "trade_date", "restriction": "release_date",
                   "delisted": "delist_date"}
_EVENT_TABLES = tuple(_EVENT_DATE_COL)


def event_coverage(table: str, by_date: bool = False, limit: int = 12):
    """事件表覆盖概览。

    `by_date=False` → 整体一行（行数 / 标的数 / 日期跨度 / 最近抓取）；
    `by_date=True`  → 按日期分组，取最近 `limit` 天（新→旧）。

    表名走白名单：这些函数会把表名拼进 SQL，不做白名单就是注入面。
    """
    if table not in _EVENT_TABLES:
        return []
    dcol = _EVENT_DATE_COL[table]
    if by_date:
        return _query(f"""SELECT {dcol} d, COUNT(*) n,
                                 COUNT(DISTINCT code) syms, MAX(fetched_at) last_fetch
                          FROM {table} GROUP BY {dcol}
                          ORDER BY {dcol} DESC LIMIT ?""", (int(limit),))
    return _query(f"""SELECT COUNT(*) n, COUNT(DISTINCT code) syms,
                             MIN({dcol}) first_date, MAX({dcol}) last_date,
                             MAX(fetched_at) last_fetch
                      FROM {table}""")


def margin_dates(market: str = "cn", exchanges=None) -> set:
    """库里**已经齐了**的两融交易日集合（供采集端判断「今天还缺什么」）。

    `exchanges` 给定时只返回**这些交易所全都有数据**的日期。这一点不是细节：
    沪、深两个接口各自独立失败（实测深市被 reset 而沪市正常），
    如果只按「日期存在」判断，那么沪市成功写下的那天会被当成「已齐」，
    深市的缺口就**永远不会被补**——而两融明细是逐日累积的，缺的那天就是永久缺。
    实测踩到过：10-01 那次自动化里沪市拿到 09-30、深市 blocked，
    按日期判断的话深市 09-30 当天就被判成「已在库」了。
    """
    sql = "SELECT trade_date, COUNT(DISTINCT exchange) n FROM margin WHERE market=?"
    params = [str(market)]
    if exchanges:
        marks = ",".join("?" * len(exchanges))
        sql += f" AND exchange IN ({marks})"
        params.extend([str(e) for e in exchanges])
    sql += " GROUP BY trade_date"
    rows = _query(sql, tuple(params))
    if not exchanges:
        return {str(r["trade_date"]) for r in rows}
    return {str(r["trade_date"]) for r in rows if int(r["n"] or 0) >= len(exchanges)}


def load_events(table: str, code: str = None, date_from: str = None,
                date_to: str = None, limit: int = None):
    """通用事件表读取（表名白名单 + 参数化条件）。无数据返回 None。"""
    if table not in _EVENT_TABLES:
        return None
    if not _ENABLED:
        return None
    dcol = _EVENT_DATE_COL[table]
    sql = f"SELECT * FROM {table} WHERE 1=1"
    params = []
    if code:
        sql += " AND code=?"
        params.append(str(code))
    if date_from:
        sql += f" AND {dcol}>=?"
        params.append(str(date_from))
    if date_to:
        sql += f" AND {dcol}<=?"
        params.append(str(date_to))
    sql += f" ORDER BY {dcol}, code"
    rows = _query(sql, tuple(params))
    if not rows:
        return None
    if limit and len(rows) > limit:
        rows = rows[-int(limit):]
    return pd.DataFrame(rows)


# ---------------------------------------------------------------------------
# 数据源健康
# ---------------------------------------------------------------------------
def record_health(source: str, market: str, ok: bool, elapsed: float = None,
                  n_items: int = None, note: str = "", run_date: str = None,
                  anomalies=None) -> int:
    """记一次抓取。`anomalies` 是归一异常的逐字段计数（dict，会序列化成 JSON）。

    写入返回**实际影响行数**：`record_health` 的返回值以前没人看，但自检要靠它
    确认「画像真的落库了」——返 0 时如果调用方以为成功了，就会重演
    「日志说记了、库里没有」那类事。
    """
    payload = None
    if anomalies:
        try:
            payload = json.dumps({k: v for k, v in anomalies.items() if v},
                                 ensure_ascii=False, sort_keys=True)
        except (TypeError, ValueError):
            payload = None
    return _exec("""INSERT INTO data_health
        (run_date, source, market, ok, elapsed, n_items, note, anomalies, ts)
        VALUES (?,?,?,?,?,?,?,?,?)""",
                (run_date or datetime.now().strftime("%Y-%m-%d"), str(source), str(market),
                 1 if ok else 0, _num(elapsed), n_items, note, payload, _now()))


def attach_health_anomalies(source: str, market: str, anomalies=None) -> int:
    """把归一异常附到**刚写下的那行**画像上。

    为什么要「附」而不是「另写一行」：一次抓取应该在画像里占一行——写两行会把
    成功率的分母翻倍（`health_summary` 按行数算），一个源看起来突然「调用次数翻番」，
    而真实调用次数没变。

    依赖「单线程串行抓取」这个前提（本项目成立）：抓取 → 归一 → 附加，中间不会有
    另一次同源抓取插入。若将来改成并发抓取，这里必须换成显式回传 health id。
    """
    if not anomalies:
        return 0
    payload = {k: v for k, v in anomalies.items() if v}
    if not payload:
        return 0
    return _exec("""UPDATE data_health SET anomalies=?
                    WHERE id=(SELECT MAX(id) FROM data_health
                              WHERE source=? AND market=?)""",
                 (json.dumps(payload, ensure_ascii=False, sort_keys=True),
                  str(source), str(market)))


def recent_anomalies(limit: int = 10):
    """最近带归一异常的画像行（新→旧）。供 `--db-stats` 回答「字段是不是开始变差了」。"""
    return _query("""SELECT run_date, source, market, n_items, note, anomalies, ts
                     FROM data_health
                     WHERE anomalies IS NOT NULL AND anomalies <> ''
                     ORDER BY id DESC LIMIT ?""", (int(limit),))


def health_summary(days: int = 14):
    """近 N 天各源的成功率与平均耗时（回答「这个源稳不稳」）。"""
    since = (datetime.now() - timedelta(days=int(days))).strftime("%Y-%m-%d")
    return _query("""SELECT source, market, COUNT(*) n,
                            SUM(ok) ok_n,
                            ROUND(AVG(elapsed), 2) avg_elapsed,
                            SUM(COALESCE(n_items,0)) items,
                            MAX(ts) last_ts
                     FROM data_health WHERE run_date >= ?
                     GROUP BY source, market ORDER BY n DESC""", (since,))


# ---------------------------------------------------------------------------
# 概览
# ---------------------------------------------------------------------------
def stats() -> dict:
    """各表行数 + 关键边界，供 `python run.py --db-stats` 与自检使用。"""
    out = {"enabled": _ENABLED, "path": db_path(), "schema": SCHEMA_VERSION}
    if not _ENABLED:
        return out
    tables = ["kline", "kline_meta", "rank_snapshot", "fin_metrics", "fund_profile",
              "index_snapshot", "index_valuation", "industry_rank", "fund_flow",
              "lhb", "lhb_inst", "margin", "restriction", "delisted",
              "macro_snapshot", "ml_eval", "data_health"]
    for t in tables:
        rows = _query(f"SELECT COUNT(*) n FROM {t}")
        out[t] = rows[0]["n"] if rows else 0
    sz = os.path.getsize(db_path()) if os.path.exists(db_path()) else 0
    out["size_mb"] = round(sz / 1048576, 2)
    # 行情覆盖：按口径分别统计标的数与日期跨度
    cov = _query("""SELECT adjust, COUNT(DISTINCT market || '/' || code) syms,
                           COUNT(*) rows, MIN(date) f, MAX(date) l
                    FROM kline GROUP BY adjust""")
    out["kline_coverage"] = cov
    return out


def clear_table(name: str) -> int:
    """清空某张表（自检与人工排障用；不做级联删除）。"""
    if name not in ("kline", "kline_meta", "rank_snapshot", "fin_metrics",
                    "fund_profile", "index_snapshot", "index_valuation",
                    "industry_rank", "fund_flow", "lhb", "lhb_inst", "margin",
                    "restriction", "delisted", "macro_snapshot", "ml_eval",
                    "data_health"):
        return 0
    return _exec(f"DELETE FROM {name}")


if __name__ == "__main__":
    import pprint
    pprint.pprint(stats())
