# 虚拟账户初始化与启动登记

任务 7a：在指定的独立本地 SQLite 账本中登记虚拟账户。这里提供入口和构造数据验证，并未启动用户的前瞻试验。

## 使用

在调用者自己选择的目录中准备请求文件，例如 `init.json`（金额和日期仅为示例）：

```json
{
  "ledger_path": "paper.db",
  "account_id": "my-paper-account",
  "opening_cash": "50000.00",
  "currency": "CNY",
  "start_date": "2026-10-27",
  "policy_path": "/absolute/path/to/execution_v1.json"
}
```

执行 `.venv/bin/python run.py --init-account /path/to/init.json`。相对账本与政策路径均相对请求文件解析；必须显式提供政策文件。金额必须是正数、精确到分的十进制字符串；当前只支持人民币 `CNY`。日期须是标准 `YYYY-MM-DD`。请求成功返回账户状态，初始现金等于本金、持仓为空，另有一条初始现金事件；账户与政策定义、政策指纹均留在账本中。初始现金不是后续入金，不计入 `net_external_flow`。

指定路径不存在时，先在同目录临时位置创建和验证完整账本，再以独占方式发布；指定路径已存在时只接受本项目的有效账本，不覆盖或重新初始化普通文件、行情库或旧版只读账本。相同账户 ID 与相同本金、日期和政策重复提交时，返回当前账户状态，不重新注资；内容不同则拒绝。重复初始化不会将已发生的资金变化重置。并发相同请求共享同一条初始现金事件。失败不留下半初始化的新账本。

`--ledger-status` 可以回读和对账；`--record-order`、`--record-action` 仍需显式调用。该步骤只冻结成交及费用政策，尚未冻结投资策略、风险预算、数据源或比较基准；这些属于后续 7c。每日模拟、持仓估值与净值曲线属于 7b。真实试验的本金、启动时点和其他假设须在启动前确定；不会用示例参数自动创建账户或补造过去的投资记录。

## 验证边界

测试使用临时文件和独立账本，覆盖命令入口、初始资金和空仓、重复请求与冲突、并发、资金变动后重试、无效输入、已有文件保护及注入失败回滚。它们证明构造输入下的初始化行为，不证明策略收益或真实行情数据覆盖。

2026-10-27 项目 `.venv` 实际验证：

```text
.venv/bin/python -m pytest -q tests/test_account_initialization.py tests/test_paper_ledger.py tests/test_corporate_actions.py tests/test_execution.py
# 93 passed
.venv/bin/python tests/offline_store.py
.venv/bin/python tests/offline_datasources.py
.venv/bin/python tests/offline_long.py
.venv/bin/python tests/offline_ml.py
# 四项均通过
.venv/bin/ruff check tests/test_corporate_actions.py tests/test_account_initialization.py src/quant_agent/application/paper/ledger.py src/quant_agent/entrypoints/execution.py
# 通过
```

组合回归首次发现公司行动测试的夹具注册依赖收集顺序，修复后上述组合通过；没有把该次失败记为通过。未进行真实账户初始化、真实联网采集或前瞻收益验证。

本步路由：`executing-plans`、`test-driven-development`、`idempotency-patterns`、`systematic-debugging`、`verification-before-completion`。读取规则：项目 `AGENTS.md`、`skills-index.md`、`engineering-workflow.md`、`python/code-style.md`、`python/data-runtime.md`、`data-consistency.md`、`shared-code-change-safety.md`。本步不涉及公司数据库或外部发布。
