# 构造数据受控研究入口

`run.py --research-loop REQUEST_JSON` 接通长期权重研究的验证选型、候选锁定、一次隔离测试和账户评估。首版仅支持 `synthetic_long_weights_v1`；不读取行情缓存，不调用前瞻试验登记、推进或参数更新。

## 运行与证据

在项目根目录使用项目 Python 环境：

```bash
python tests/offline_research_loop.py --output reports/research-loop-demo
python -m pytest -q tests/test_research_loop.py tests/test_research_loop_gates.py tests/test_research_experiments.py tests/test_temporal_validation.py
```

输出目录必须不存在。示例生成两份构造阶段文件、manifest、独立实验库、五个阶段请求和回执、summary.json。它走真正 CLI 和子进程账户评估；临时账户账本自动清理，逐笔订单、每日现金/持仓/估值及费用保留在实验结果中。

请求格式：

```json
{"operation":"register","registry":"registry.db","manifest":"fixture/manifest.json"}
```

后续将 `manifest` 换成 `experiment_id`，依次执行 `validate`、`lock`、`test`；`status` 只读。路径相对请求文件，manifest 内路径相对 manifest 文件。CLI 失败退出码为 2，包括回读已失败测试。`status` 返回结果中的 JSON 字段供审计，不触发执行。

## 冻结和隔离

- manifest 固定候选全集及顺序（必须包含 `default`）、验证与测试 SHA-256、日期边界、初始现金、成交政策及预算。代码与环境继续由现有实验登记冻结。
- 受控协议只允许通过专用登记入口创建；同事务写入不可修改的登记来源标记。通用 `--research-experiment register` 在打开快照前拒绝冒用该协议。执行入口先验证来源，再核对内外协议完整一致；旧版本没有来源标记的记录不会被自动升级为受控证据。
- 候选来自既有 `LONG_SCHEMES`，复用同一个五维评分函数；不纳入 macro，不训练模型、不接受自定义代码。验证按扣费后期末净资产排序，平分按预声明顺序。不能把 IC 排名当账户收益。
- 登记仅校验验证文件，测试文件可以尚不存在；验证快照不含测试行。验证日期必须早于 `test_start`。测试日期必须在冻结测试窗口内。每个阶段至少两日、每天完整同一标的池。
- 每个候选验证经 `run_attempt` 执行。全部受控验证成功且在预算内，才允许锁定；手工 `finish` 的结果不能通过。候选和尝试预算分别为候选数、候选数加一，候选锁定后关闭搜索。
- 锁定前拒绝测试，锁定后测试尝试先记入库再读取测试文件。所选候选和预声明 default 共享一次测试预算；没有按测试结果重新选择候选的路径。
- 重复 attempt 回读原结果；测试摘要不符、执行异常或超时均消耗该测试机会。进程中断留下未完成尝试，禁止静默重跑。更换独立数据库不能被宣称为新增独立样本：跨库全局去重尚未实现。
- 验证与测试均检查完整尝试耗时，首调和回读一视同仁。计算完成但整体超预算时保留原始记录，成功门禁拒绝，不通过修改或重新执行旧记录恢复。
- 子进程仅收到当前阶段文件路径/摘要、阶段边界、标的池、候选、资金与政策。阶段读取、摘要核验、解析、校验和计算全部位于同一个限时 worker；临时账户目录位于父进程负责清理的 worker 目录内。它不是操作系统级文件访问沙箱，内部登记能力也不防任意 Python 执行或直接数据库篡改。构造标记是契约，不是外部文件真实性鉴定。

## 账户含义

前一收盘的完整五维分数决定下一声明交易日开盘的目标，固定最多一只股票、100 股；先卖后买，卖出未成交时不增加第二持仓，保留现有 T+1、整手、涨跌停、容量、可用现金、佣金、印花税和滑点规则。未知成交事实导致该尝试失败，拒单和未成交保留在订单证据中。

验证与测试各自从相同本金和空仓开始，阶段首日只观察；不从验证段继承资金、持仓或信号。期末按 raw 收盘价估值，不强制平仓，因此不含假设清仓费用。现金基准净资产等于初始本金。

输出净资产曲线、费用、滑点金额、最大回撤和双边成交额/初始本金。`simulated_account_return` 只是构造情景下的账户变化；既有 `total_return=null`、公司行动覆盖未核实语义不变。所有结果始终 `promotion_allowed=false`、`independent_new_samples=0`。

没有使用未来收益标签选因子，也没有学习训练段，故本入口不新增标签 purge 算法；原带标签研究入口的时点/标签结束日门禁保持原样。本轮不能据此宣称真实收益改善、历史 PIT 完整或已具备前瞻账户准入资格。

## 2026-11-06 首轮本地验证与规则清单

首轮实现位于隔离分支 `codex/quant-research-loop`，基础提交 `1b87ccd4c0a86685219e4944d39c33a25aba7d4c`，当时尚未提交或推送。随后授权的复核与 Git 交付记录见 [controlled_loop_review.md](controlled_loop_review.md)。下面保留首轮检查证据，不代替复核后的最终验证。

- 研究闭环、实验登记与时序回归：`pytest -q tests/test_research_loop.py tests/test_research_experiments.py tests/test_temporal_validation.py`，**34 passed**。包含测试文件不可得时验证正常、失败消费一次测试、手工证据拒绝、数据漂移、超时、全尝试预算、时点隔离、停牌退出及费用对账。
- 前瞻门禁兼容回归：`pytest -q tests/test_paper_experiment.py tests/test_paper_runtime.py`，**28 passed**；全部是临时账本测试，不是前瞻账户推进。
- 四项规定基础检查 `offline_store.py`、`offline_datasources.py`、`offline_long.py`、`offline_ml.py` 均通过。数据源测试首次因新 worktree 缺少空 `state/cache` 目录退出，补建该测试前置目录后通过，未修改数据源业务逻辑。
- `offline_e2e.py --synthetic` 通过，内含 **161 passed** 及构造行情端到端断言。成交与账本回归也已通过。
- 修改文件的 Ruff lint、format check、`git diff --check` 通过。四个新增模块在禁止 socket 连接、全新工作目录下可导入，不创建运行 workspace。
- 首轮扩展的全包导入检查因当时环境缺少 Hull 可选依赖 `polars` 未通过；保留该历史结果。2026-11-06 已按锁定版本补齐并单独复验通过，见 [依赖补齐后的全包导入复验](controlled_loop_review.md#依赖补齐后的全包导入复验)。
- 最终完整 CLI 回执位于 `reports/research-loop-verified/`，回执代码指纹与当前源码一致；3 次验证和 1 次测试均成功且在 30 秒预算内。构造情景选中 `quality_first`，本金 10000.00，期末净资产 10018.99，default 为 9978.99，现金为 10000.00。全部仅为构造数据对账证据。

实际加载的流程 Skill：`writing-plans`、`test-driven-development`（含 testing-anti-patterns）、`using-git-worktrees`、`verification-before-completion`。规则：项目 `AGENTS.md`、全局 `skills-index.md` 路由、`engineering-workflow.md`、`python/code-style.md`、`python/data-runtime.md`、`shared-code-change-safety.md`。本轮不涉及 Git 远程交付、真实行情采集或生产准入，未启动这些流程。
