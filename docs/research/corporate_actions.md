# 虚拟账本：公司行动与外部资金流

任务 6，2026-10-27。这里记录的是独立虚拟账本的显式事件，不是已启动的投资账户，也不代表已完成每日估值。

## 核算口径

- 入金 `deposit` 和提款 `withdrawal` 分别增减现金，并单独累计 `net_external_flow`；不得透支。初始本金不计入外部资金流。
- 分红分三步：登记日收盘 `dividend_entitlement` 固定持有数量和每股税后金额；除息日前开盘 `dividend_accrual` 确认应收；到账日 `dividend_payment` 把应收转成现金。登记后卖出不取消已登记权益；重复到账被拒绝。登记日冻结的金额不是当日已到账或已确认的应收。
- 拆股／并股 `split` 以明确的整数比例在开盘前调整持仓批次，保留取得日期与可卖约束；不能整除的碎股拒绝处理，不臆造碎股现金。原始成交价格标注为原始价格，并非拆分后单位成本。
- 事件提供 `source`、`known_at` 和生效日期／阶段（`preopen`、`open`、`close`）；同账户按时间顺序登记，未完成订单阻止插入公司行动。相同事件 ID、相同内容重放不重复记账；相同 ID 不同内容拒绝；事务失败不保留半条事件。
- 股息税后每股金额是输入的明确假设，不自动推断税额。现有财务摘要不足以证明完整的登记日、除息日和付款日事件，当前没有自动采集或完整覆盖校验。账本始终标记 `corporate_action_coverage=unverified`，`total_return=null`；不能把缺失事件当零。
- 现有长期跟踪报告显示价格变化，历史回放使用 raw 价格变化，并明确不把它当含分红、费用的账户总回报；qfq 评分与 raw 成交／估值仍分开。

## 显式记账示例

先有独立版本 2 测试账本与已存在的账户；以下为构造数据，不代表现实事件。将请求放在测试目录的 `action.json`：

```json
{
  "ledger_path": "paper.db",
  "account_id": "fixture",
  "action": {
    "kind": "deposit", "action_id": "deposit-1",
    "date": "2026-10-27", "phase": "preopen",
    "source": "fixture", "known_at": "2026-10-27@preopen",
    "amount": "100.00"
  }
}
```

运行 `.venv/bin/python run.py --record-action /path/to/action.json`，相对路径以请求文件所在目录解析。该命令不会创建账户。

其他事件在同一 `action` 对象中使用相同公共字段，并分别提供：

| kind | 额外字段 | 阶段约束 |
|---|---|---|
| withdrawal | `amount`，正的金额字符串 | 按时间顺序 |
| split | `market`、`code`、`numerator`、`denominator` | `preopen` |
| dividend_entitlement | `market`、`code`、`net_per_share`（金额字符串） | 登记日 `close` |
| dividend_accrual | `entitlement_id` | 登记日之后的除息日 `preopen` |
| dividend_payment | `accrual_id` | 应收确认之后 |

市场目前仅 `cn`、`etf`，代码为六位数字。可用原有 `--ledger-status` 查看现金、应收、权益登记和事件序列。

## 旧账本升级

版本 1 账本可只读查看，不直接写入新事件。明确准备一个新目标路径，在请求文件中提供 `{"source":"old.db","target":"new.db"}`，执行 `.venv/bin/python run.py --migrate-ledger-copy /path/to/migration.json`。路径相对请求文件解析；目标已存在则拒绝。先验证源账本，再以 SQLite 备份创建副本、升级并复核；失败清理候选副本，源文件保持原样。测试仅使用构造的临时版本 1 数据库，未迁移真实账本。

## 本步验证及边界

在项目虚拟环境使用临时目录和独立 SQLite：公司行动测试覆盖登记、除息、登记后卖出、到账、拆股、碎股拒绝、资金流、重复事件、时间顺序、数据库故障回滚、损坏记录与副本升级；相关跨模块回归、四项基础离线检查及构造行情的端到端检查另见计划中的本轮记录。构造测试不证明真实公司行动来源完整，亦不是长期样本外投资结果。

项目虚拟环境本轮结果（2026-10-27）：

```text
.venv/bin/python -m pytest -q tests/test_corporate_actions.py tests/test_paper_ledger.py tests/test_execution.py tests/test_data_contract.py tests/test_trade_semantics.py tests/test_indicator_backtest_boundaries.py tests/test_local_research.py tests/test_research_tracking.py
# 218 passed
.venv/bin/python tests/offline_store.py
.venv/bin/python tests/offline_datasources.py
.venv/bin/python tests/offline_long.py
.venv/bin/python tests/offline_ml.py
# 四项全部通过
.venv/bin/python tests/offline_e2e.py --synthetic
# 构造行情端到端通过，内含 150 项回归
```

系统 `python3` 缺少 `akshare`，运行数据源离线自检曾失败；切换到项目 `.venv` 后通过。上述结果均为本地构造数据，未执行真实联网采集或前瞻账户验证。

本步路由与规则：`executing-plans`、`test-driven-development`（含测试反模式）、`idempotency-patterns`、`verification-before-completion`；已读取项目 `AGENTS.md`、`skills-index.md`、`engineering-workflow.md`、`python/code-style.md`、`python/data-runtime.md`、`shared-code-change-safety.md`、`data-consistency.md`、`anti-hallucination.md`。本步无 Java、TypeScript、Rust、生产数据库或外部发布操作。
