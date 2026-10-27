# 虚拟试验运行版本衔接

2026-10-27。`cold-start-20261027`已于北京时间11:01:01在新账本副本完成版本衔接，
运行状态为`active`、`ready=true`。原始登记、本金和继承历史保留；当天仍须16:00后才可结算。
证券ETF的前复权刷新也已恢复，来源及真实因子验证见[备用来源说明](etf_qfq_sources.md)。

## 衔接契约

`run.py --transition-paper-experiment REQUEST_JSON`从只读SQLite快照复制到一个不存在的新文件，
在副本中追加不可变运行版本。它不调用开户或注资，不修改原manifest，也不开放策略、数量、
本金、费用、风险或数据来源的变更。首版只支持单试验、策略与现金基准两账户的独立账本；
混有其他账户或试验时拒绝，避免其他账户的可变未完成订单破坏继承前缀。

每条版本记录绑定实验、原manifest散列、前一运行版本、生效日期、真实登记时刻、
代码SHA-256、Python版本、衔接原因、源快照及双账户检查点。原有表的schema、rowid水位、
行数和内容散列随记录封存。后续新记录可以追加到水位之后；已继承内容变化会触发拒绝。
第二次衔接必须继续原版本链、生效日期递增，并再次保留已有封存日终。

准备副本时验证所有账户及日终证据，核对双账户日期、资金流与起点；未结算事件、
生效前漏结算、覆盖既有日终或倒填日期均拒绝。首个买入日的版本须在9:15前冻结，
避免盘中更改代码后回填已经过去的开盘决策。只读原库不受失败影响，副本完成审计后独占发布，
不覆盖已有目标。同意图重试回读原版本；内容或目标运行版本改变时冲突。

结算从原登记及有效版本链解析运行代码，仍验证代码和Python指纹。新日不得早于生效日期；
旧日仅按原意图回读封存结果，不重新执行交易，也不把原结果替换为新版本计算值。
新封存日终另存`runtime_id`，后续未经登记的代码变化继续暂停。

## 请求与状态

请求字段为`source`、`target`、`experiment_id`、`transition_id`、`effective_from`、`reason`、
`calendar`；路径相对请求文件。日历使用原试验冻结的来源，并显式声明包含生效日及所需边界的有序交易日。
程序检查声明范围内的连续性，不把声明来源当作已证明的完整官方历史日历。

```json
{
  "source": "experiment.db",
  "target": "experiment-runtime-v2.db",
  "experiment_id": "cold-start-20261027",
  "transition_id": "review-fixes-runtime-v2-20261027",
  "effective_from": "2026-10-27",
  "reason": "已验证的代码修复，保持原策略、费用、风控和本金",
  "calendar": {
    "source": "https://www.sse.com.cn/disclosure/dealinstruc/closed/",
    "sessions": ["2026-10-27", "2026-10-28"]
  }
}
```

该示例说明字段，不应替换已执行的真实请求。真实请求为
`state/paper/cold-start-20261027/transition-runtime-v2.json`，原登记及旧请求保持原路径。
活动指针`active.json`记录新账本、原账本、版本ID及新推进／对比请求；CLI仍读取明确提供的请求文件。

对比入口输出`runtime`：代码匹配、Python匹配、生效日期、`activation_state`、`ready`以及
`can_settle_current_day`。未来生效版本显示`scheduled`，不宣称当前可运行；
盘中即使代码就绪，`can_settle_current_day`仍为false。它不替代实际订单、日历和公司行动的准入检查。

## 本次实际执行

原账本的SHA-256在衔接前后均为
`e918279a6d67b8ad062927311e6d1322545b3fc139d2d2bba6751501e97e5d65`。
新账本`experiment-runtime-v2.db`继承原6张业务表的全部记录，追加版本链，原manifest散列仍为
`79d1e37bb8a86ece05337f759c7ce856e910564346402757d0b1cdc71fa120a9`。
新运行版本为`2e2e4589ae1c5ef5950dc6a53cfacf1384bfd4f1943205ac59318881c457ba4b`。

真实命令已退出0并只读回查：

```bash
.venv/bin/python run.py --transition-paper-experiment state/paper/cold-start-20261027/transition-runtime-v2.json
.venv/bin/python run.py --compare-paper-experiment state/paper/cold-start-20261027/compare-active.json
```

状态为`sequence=1`、`active`、代码及Python匹配、`ready=true`。
策略与现金基准仍分别有50,000元现金、空仓、零订单、零日终记录。
11点盘中运行新推进请求，实际返回`daily close not yet available for prospective account`，退出2，
没有提前生成日终或收益。首个可交易日仍为10月28日，冻结标的及数量仍为510300、5900份。

16:00后手动命令为：

```bash
.venv/bin/python run.py --advance-paper-experiment state/paper/cold-start-20261027/advance-active-20261027.json
.venv/bin/python run.py --compare-paper-experiment state/paper/cold-start-20261027/compare-active.json
```

没有新增定时任务，也没有补造过去或未来记录。原文件指纹、迁移回执、活动指针、核验结果及新请求
均位于本地`state/paper/cold-start-20261027/`，按项目边界不提交。

## 前复权正式刷新结果

实际执行`run.py --sync-data --market etf --codes 512880`，采用隔离的诊断输出目录
`state/research/etf-refresh-recovered-20261027`，退出0，完整更新301根qfq日线。
窗口为2025-07-28至2026-10-26，最新收盘1.021元、成交额1,242,909,200元。

来源为`sina_etf_cash_qfq_v1:d865a3dd9427473ebe1861643dcf360707459d6ecf6744efdc59470fbda53292`。
对应完整因子凭据已从磁盘读回，SHA-256核验一致；512880实际返回单位因子，
而510300非零现金分红的640根共同窗口验证了算法确实执行调整。raw成交额仅作为未复权的成交事实补充，
价格必须经过已核验因子处理，不能把缺因子的raw改名作为备用。

随后`run.py --daily --offline`退出0，新快照为
`ad3301d887443a8c924910023caca524c6ab5033c5f29164fa42b981bfd47d69`，
12只标的均无缺失／无效／过期／倒退，最新报告仍截止10月26日。
新源带来的历史数值变化被记录为`historical_revision`，观察交易日计数仍为2，`promotion_allowed=false`。
新采集及新运行版本没有冒充新增独立样本或策略收益验证。

## 验证与路由

- 新增`tests/test_paper_runtime.py`17项，涵盖复制保真、原文件不变、幂等、日期缺口、
  旧日回读、链和前缀损坏、发布失败／竞争、后续版本、额外账户拒绝、Python漂移、真实CLI及未来／盘中状态。
- 新增`tests/test_etf_qfq_source.py`24项，涵盖真实因子及来源身份、非单位比例拒绝、成交额补充、
  覆盖缺口与错误输入、凭据完整性、备用路径及原源优先。
- 源码稳定后全量pytest **375 passed，45.14秒**。四项基础离线自检及
  `offline_e2e.py --synthetic`通过，后者内含161项既有回归及构造集成路径。
- 本轮模块、新测试Ruff及格式检查通过；69个Python文件AST解析、`git diff --check`通过。
- 独立复核发现多账户prefix问题，回归先失败后修复为独立账本约束；未来状态也补充确定性时钟回归。
  这些测试使用临时库，实际恢复使用真实当前时间。

实际加载`brainstorming`、`writing-plans`、`test-driven-development`及测试反模式、
`systematic-debugging`、`http-client-resilience`、`dispatching-parallel-agents`、
`subagent-driven-development`、`requesting-code-review`、`verification-before-completion`；
文档沿用`pages:write-page`及`writing_quality.md`的既有仓库目的地规则。
已读取项目`AGENTS.md`、`skills-index.md`、`engineering-workflow.md`、`python/code-style.md`、
`python/data-runtime.md`、`shared-code-change-safety.md`、`data-consistency.md`、`anti-hallucination.md`。
工程Harness仅补充新入口及验证，不新增推测性资金政策；未进行Git交付、公司数据库操作或外部发布。
