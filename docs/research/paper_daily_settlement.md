# 虚拟账户逐日结算

任务 7b，2026-10-27。逐日处理显式给定的模拟订单和公司行动，使用本地行情库中当日 `raw` 收盘价记录账户资金曲线。本文示例均为构造数据；本步没有启动前瞻账户。

## 口径

- 每日净资产 = 现金 + 持仓市值 + 已确认未到账分红应收。所有持仓须有本交易日的 raw 收盘价及来源；缺价时整日回滚，不把持仓当作零。股票／ETF 的现金与数量仍按任务 4–6 的既有模拟成交和事件账本计算。
- 报告持仓数量、市值、FIFO 成本、未实现交易损益、已实现交易损益、累计费用、外部净入金和扣除外部资金流后的累计损益。买入成本包含买入费用，卖出损益包含卖出费用；分红作为账户应收／现金处理，不混入交易损益。拆并股保留原持仓批次总成本，成本按数量分配时保留精度并在展示时四舍五入到分。
- `unit_value` 是在所输入事件和价格完整这一条件下的资金流调整单位净值：开盘前入提款按上一日单位净值增减份额，收盘入提款按本日流入流出前单位净值增减份额。只支持这两个明确时点；全部赎回或净资产非正目前拒绝结算。公司行动覆盖尚未核实，`total_return` 仍为 `null`，不能把该单位净值视为已验证的完整总回报。
- 每日以日期顺序处理；缺少日历中声明的交易日会阻止跨日结算。日历由调用方提供来源和有序日期，系统不证明该来源的完整性；日历、行情来源和实际可见性仍需在真实试验前核验。行情抓取时间不证明历史时点已经可见。
- 同一天的事件、费用和收盘记录在一个 SQLite 事务中完成；日终记录不可修改。相同输入重跑返回已记录结果，内容变更会拒绝。多日请求按日顺序分别提交，某日失败时以前成功的日子仍保留；按原输入重试即可从该日继续。开户日首先记录作为初始曲线起点，后续由调用方提供每个应结算日。
- 进入逐日管理后，新订单和公司行动须走逐日入口，原单笔入口只允许重读已完成的同一事件，避免账本与已封存日终记录分叉。开户后已有未纳入日终结算的事件不会被静默吸收，需要先处理账本与日终记录的一致性；本步不迁移或重写现有账户历史。

## 命令与请求

已初始化的账本使用 `.venv/bin/python run.py --settle-paper-days /path/to/days.json`，账本与行情路径相对请求文件解析：

```json
{
  "ledger_path": "paper.db",
  "db_path": "market.db",
  "account_id": "fixture",
  "calendar": {"source": "fixture calendar", "sessions": ["2026-10-27"]},
  "days": [
    {"day": "2026-10-26", "previous_day": null, "events": [], "close_symbols": []},
    {"day": "2026-10-27", "previous_day": "2026-10-26", "events": [], "close_symbols": []}
  ]
}
```

`calendar.sessions` 是本次输入覆盖范围内的完整声明日期，不保证来源真实完整；开户日起点可以不是交易日。每一天的 `close_symbols` 必须恰好列出日终尚持有的证券，格式为 `{"market":"cn","code":"600000"}`。程序按各标的读取当日 raw 行情，保留来源和价格版本。

`events` 按 `preopen → open → close` 排序。公司行动或资金流使用 `{"action": {...}}`，内部字段沿用[公司行动记录](corporate_actions.md)；订单使用 `{"order": {...}, "instrument": {...}, "session": {...}}`，字段与[单笔模拟订单](order_execution.md)一致，开盘价格由本地 raw 行情读取。成功结果给出每日资金状态；`--paper-curve` 使用与 `--ledger-status` 相同的 `ledger_path`、`account_id` 请求，只读完整已封存曲线。

## 局限

目前没有自动生成策略订单或自动搜集公司行动；需要调用方提供按日期冻结的事件。日历完整性、停牌等缺价情形、公司行动的完整覆盖尚未自动证明。缺失时停止该日而非使用旧价。基准账户与策略／风险／代码冻结属于任务 7c；本步不是历史收益或前瞻策略验证。

## 本轮验证

2026-10-27，项目 `.venv`、临时 SQLite 与构造行情：

```text
.venv/bin/python -m pytest -q tests/test_paper_daily.py tests/test_account_initialization.py tests/test_paper_ledger.py tests/test_corporate_actions.py tests/test_execution.py
# 102 passed；之后补充分红到账／提款用例并单独运行逐日测试：10 passed
.venv/bin/python tests/offline_store.py
.venv/bin/python tests/offline_datasources.py
.venv/bin/python tests/offline_long.py
.venv/bin/python tests/offline_ml.py
# 四项均通过
.venv/bin/python tests/offline_e2e.py --synthetic
# 构造行情端到端通过；内含 150 项既有回归
.venv/bin/ruff check src/quant_agent/application/paper/daily.py src/quant_agent/application/paper/ledger.py src/quant_agent/infrastructure/persistence/execution_data.py src/quant_agent/entrypoints/execution.py tests/test_paper_daily.py
# 通过
```

逐日测试覆盖命令从临时行情库读取 raw 开收盘价、空仓、买卖费用及浮盈亏、分红应收和到账、拆股、入提款、资金流调整单位净值、重复／并发、缺价事务回滚、日历漏日及单笔入口约束。未验证真实联网采集、实际公司行动与日历覆盖或前瞻策略表现。

本步路由：`executing-plans`、`test-driven-development`、`idempotency-patterns`、`verification-before-completion`；工程约束读取项目 `AGENTS.md`、`skills-index.md`、`engineering-workflow.md`、`python/code-style.md`、`python/data-runtime.md`、`data-consistency.md`、`shared-code-change-safety.md`。未涉及公司业务数据库、提交或发布。
