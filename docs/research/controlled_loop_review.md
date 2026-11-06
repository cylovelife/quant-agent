# 受控研究闭环复核与交付

2026-11-06，范围为 `codex/quant-research-loop` 相对基础提交 `1b87ccd4c0a86685219e4944d39c33a25aba7d4c` 的完整改造，包括未提交新增文件。主审与一个只读独立评审均仅使用构造数据和临时实验库。

## 有效发现及修复

| 级别 | 触发条件与影响 | 修复与验证 |
|---|---|---|
| P1 | 通用登记自填嵌套 protocol，将 validation 指向测试文件并修改嵌套日期；外层仍登记原测试窗口。旧 `_context` 可接受，验证阶段实际读取测试数据。仅比较日期也不足以阻止通用登记替换 runtime snapshot。 | 通用登记在读快照前拒绝保留协议；专用登记同事务写 append-only 来源标记；执行先验证来源，再比较完整内外协议。旧无来源记录拒绝自动获得来源。用通用/旧记录、测试伪装验证以及日期/预算/权重/成本/特征/数量错配回归覆盖。 |
| P2 | 阶段文件读取和解析在父进程完成后才启动限时 worker。测试文件变为 FIFO 时，尝试可一直悬置。 | 读取、摘要检查、解析、契约校验和账户计算放入同一个限时 worker。真实 FIFO 回归证实超时后记录失败，唯一测试机会已消费，重试只回读。父进程负责清理 worker 内全部临时账户。 |
| P2 | 最后一个验证尝试计算成功但完整耗时超预算时，`validate` 首调和回读仍返回成功；后续锁定虽拒绝，验证回执本身不准确。 | 每个验证返回前核验完整 managed attempt 耗时，覆盖单候选最后一次验证超预算的首次调用及回读。 |

定向复核确认上述两条独立评审发现已堵住，未发现新增有效问题；独立执行门禁测试 **10 passed**。相同 attempt 并发、失败回读、未完成尝试、手工 finish 与单次测试唯一索引没有发现可重新执行原测试的有效路径。

## 验证范围

- 研究闭环/门禁、实验登记、时序、成交/账本、前瞻试验/版本衔接八组测试：**145 passed**。
- 修改 Python 文件 Ruff lint 与 format check、`git diff --check` 通过。
- 四项基础离线检查均在本轮重新通过；构造行情 E2E 通过（含 **161 passed**）。完整 research-loop CLI 五阶段回执在 `reports/research-loop-reviewed/`，代码指纹与最终源码一致，3 次验证及 1 次测试均成功且未超预算。
- 包结构回归 **7 passed, 1 deselected**；排除的全包导入检查缺口见下一项，不计为通过。
- 全包导入检查仍缺 Hull 可选依赖 `polars`，不宣称该项通过；它不属于本次构造研究入口的运行依赖。未为此接入竞赛数据。
- 所有研究结果继续 `promotion_allowed=false`、`independent_new_samples=0`、`total_return=null`；未触碰真实封存数据或推进已有账户。

## 交付与规则

用户授权提交远程、合并 GitHub main 并同步主工程。GitHub 目标从真实 push URL 核实为 `cylovelife/quant-agent`，默认分支为 main；Git author/committer 使用个人 `@foxmail.com` 身份，SSH 个人 key 与 `IdentitiesOnly=yes` 已核实，远端认证成功。只提交源码、测试及文档，不提交 `state/`、`reports/`、`logs/`。

本轮实际加载 Skill：`requesting-code-review`、`receiving-code-review`、`idempotency-patterns`、`test-driven-development`、`git-workflow`、`finishing-a-development-branch`、`verification-before-completion`。读取并应用项目 `AGENTS.md`、`skills-index.md`、`engineering-workflow.md`、`python/code-style.md`、`python/data-runtime.md`、`shared-code-change-safety.md`；未扩大真实数据或账户操作授权。

隔离仅针对受支持的应用入口和阶段传参，不能声称能抵御任意 Python 代码、直接改库或拥有主机权限的攻击者；跨独立登记库去重仍不在首版范围内。
