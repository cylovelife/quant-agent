# 前瞻虚拟试验与现金基准

任务 7c 提供显式登记、同步推进与比较入口。2026-10-27 已按用户授权启动 50,000 元虚拟试验，见 [冷启动记录](cold_start_2026-10-27.md)；以下示例仍为构造输入，启动不代表收益已得到验证。

## 冻结与运行口径

- 一次登记原子创建两个空仓虚拟账户：策略账户和纯现金基准，同本金、币种、启动日期及模拟费用政策。策略账户选择一只已明确的场内 ETF、固定数量；在登记的首个可交易日按该数量提交一次开盘买入单，之后持有，直到本版本结束。未成交或拒单不自动调整数量或追单；规则固定，不依据后续结果改参数。
- 首个交易日必须是启动日期之后、所声明日历中的下一个交易日；启动日期本身须是交易日。登记时启动日期不得早于当日；当日作为启动日期时必须在北京时间 15:00 前登记。每日数据必须等到北京时间 16:00 后才能结算；尚未发生的日期不能结算。这些限制避免将注册前或未来价格当作前瞻结果。
- 基准无证券交易、无模拟持仓收益；入提款以同金额、同日、同阶段镜像到两账户。每个交易日的两边结算在同一 SQLite 事务中，任一侧失败则两侧当日一起回滚。多日请求依次提交，已成功的日期保留；重复请求不重复下单或入金。
- 固定单笔购买支出上限和最低现金储备；如果成交或后续提款导致超出限制，暂停该日并回滚，不能事后静默放宽。首期仅支持 CNY、场内 ETF 和现金基准，不执行实际交易。支持的前瞻版本不包含策略切换、追加买入或自动卖出；以后要切换策略需先设计带生效时点的连续账户机制，不能重置本金。
- 记录完整成交政策、策略标的和数量、风险参数、数据来源、注册时刻、Python 版本及相关源码 SHA-256。结算前复核源码指纹；变更时停止新的结算。外部参数文件变化不会静默改变账本中的冻结政策。日历、行情与公司行动来源是用户声明值，尚不证明来源完整或历史时点可见。
- 比较输出逐日两账户净资产、单位净值和净资产差；先检查起点、资金流及结算日期一致。公司行动覆盖未核实时标记总回报不可计算，不把净资产差宣称为已验证的净超额收益。

## 准备登记请求

开始日期、首个可交易日、ETF、固定数量、风险限额及来源须在实际登记前冻结；用户可授权由研究者根据公开证据选择。以下 JSON 仅展示结构；`policy_path` 是明确选择的模拟成交政策文件，示例日期与代码必须按实际决定替换：

```json
{
  "ledger_path": "experiment.db",
  "policy_path": "/absolute/path/to/execution_v1.json",
  "experiment": {
    "experiment_id": "trial-1",
    "account_id": "strategy",
    "benchmark_id": "cash-benchmark",
    "opening_cash": "50000.00",
    "currency": "CNY",
    "start_date": "2026-10-27",
    "first_trade_date": "2026-10-28",
    "strategy": {"kind": "buy_and_hold", "market": "etf", "code": "510300", "exchange": "SSE", "profile": "cn_equity_etf", "quantity": 100},
    "risk": {"max_order_cash": "2000.00", "min_cash_reserve": "1000.00"},
    "data_sources": {"market": "fixture", "calendar": "fixture-calendar", "corporate_actions": "fixture-action"},
    "benchmark": {"kind": "cash", "cashflow_policy": "mirror"}
  }
}
```

准备好实际参数后，执行 `.venv/bin/python run.py --start-paper-experiment /path/to/start.json`。相对路径以请求文件目录为准。新文件先完整创建再独占发布；已有账本中两账户 ID 必须都是新的。同 ID 同内容重试返回原冻结记录；内容不同拒绝。此命令是实际的本地虚拟账户启动动作，不会因测试或阅读文档自动运行。

## 手动推进及查询

`.venv/bin/python run.py --advance-paper-experiment /path/to/advance.json` 读取本地 raw 行情和明确提供的交易日历／事件。请求顶层为 `ledger_path`、`db_path`、`experiment_id`、`calendar`、`days`。`calendar` 的格式与 [逐日结算](paper_daily_settlement.md)一致，必须包含启动交易日和首个可交易日；每个 `days` 项有 `day`、`previous_day`、`actions`、`session`、`close_symbols`。开户日 `previous_day` 为 `null`、空事件、空持仓，`session` 为 `null`。首个交易日提供 `session` 事实（字段见[订单模拟](order_execution.md)）、ETF 收盘标识，程序按冻结数量构造买单；以后 `session` 为 `null`，持有时继续提供对应收盘标识。`actions` 使用[公司行动](corporate_actions.md)字段；入提款自动镜像到基准账户。

`.venv/bin/python run.py --compare-paper-experiment /path/to/compare.json` 使用 `{"ledger_path":"experiment.db","experiment_id":"trial-1"}` 只读比较。行情缺失、数据源不一致、日历缺口、风险约束触发、代码变更或公司行动未输入时，应核实后继续；不能用旧价、零分红或历史重建冒充完整前瞻记录。

本步交付手动执行的入口，不创建定时任务。真实行情、日历、公司行动来源及费用适用性需要在实际试验前核实；登记和构造测试不等于策略晋级、长期样本外验证或已实现收益。

## 本步验证

2026-10-27 使用项目 `.venv`、临时账本和构造行情：

```text
.venv/bin/python -m pytest -q tests/test_paper_experiment.py tests/test_paper_daily.py tests/test_account_initialization.py tests/test_paper_ledger.py tests/test_corporate_actions.py tests/test_execution.py
# 114 passed（含 11 项试验测试）
.venv/bin/python tests/offline_store.py
.venv/bin/python tests/offline_datasources.py
.venv/bin/python tests/offline_long.py
.venv/bin/python tests/offline_ml.py
# 四项均通过
.venv/bin/python tests/offline_e2e.py --synthetic
# 构造行情端到端通过，内含 150 项既有回归
.venv/bin/ruff check src/quant_agent/application/paper/experiment.py src/quant_agent/application/paper/daily.py src/quant_agent/application/paper/ledger.py src/quant_agent/entrypoints/execution.py tests/test_paper_experiment.py
# 通过
```

构造试验验证了登记幂等和禁止倒填、首次按冻结规则生成买单、现金基准、资金流镜像、超额预算及盘中现金储备回滚、基准侧故障的双账户回滚、代码／来源变更停止、实际入口读取临时 raw 行情及只读比较。未验证真实联网数据、真实交易日历或公司行动覆盖，这些测试当时未开始前瞻试验；实际启动记录见上方冷启动文档。

本步路由：`executing-plans`、`test-driven-development`、`idempotency-patterns`、`verification-before-completion`；读取项目 `AGENTS.md`、`skills-index.md`、`engineering-workflow.md`、`python/code-style.md`、`python/data-runtime.md`、`data-consistency.md`、`shared-code-change-safety.md`。未涉及公司业务数据库、提交或发布。
