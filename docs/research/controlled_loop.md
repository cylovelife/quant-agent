# 构造数据受控研究入口

`run.py --research-loop REQUEST_JSON` 接通长期权重研究的验证选型、候选锁定、一次隔离测试和账户评估。首版仅支持 `synthetic_long_weights_v1`；不读取行情缓存，不调用前瞻试验登记、推进或参数更新。

## 运行与证据

在项目根目录使用项目 Python 环境：

```bash
python tests/offline_research_loop.py --output reports/research-loop-demo
python -m pytest -q tests/test_research_decision.py tests/test_research_loop.py tests/test_research_loop_gates.py tests/test_research_experiments.py tests/test_temporal_validation.py
```

输出目录必须不存在。示例生成两份构造阶段文件、manifest、独立实验库、五个阶段请求和回执、summary.json。它走真正 CLI 和子进程账户评估；临时账户账本自动清理，逐笔订单、每日现金/持仓/估值及费用保留在实验结果中。

请求格式：

```json
{"operation":"register","registry":"registry.db","manifest":"fixture/manifest.json"}
```

后续将 `manifest` 换成 `experiment_id`，依次执行 `validate`、`lock`、`test`；`status` 只读。路径相对请求文件，manifest 内路径相对 manifest 文件。CLI 执行失败退出码为 2，包括回读已失败测试；科学判定停止为正常完成（退出码 0），调用方必须检查 `research_state`，不能仅凭退出码推进。`status` 返回结果中的 JSON 字段供审计，不触发执行。

## 构造实验科学判定（2026-11-13）

本步在**验证候选准入**处区分计算成功与研究结论，不新增真实数据协议或统计显著性检验。manifest 必须显式携带以下策略；示例的四日仅用于构造场景覆盖，不是独立样本数或真实策略的最低研究长度：

```json
"scientific_policy": {
  "version": "synthetic_economic_v1",
  "min_sessions": 4,
  "min_filled_orders": 1
}
```

策略与候选、数据、代码一起冻结。版本 1 的经济条件固定为扣费后净利润严格大于零，且期末净资产不低于同阶段 default；相同正收益允许通过，平分仍按候选预声明顺序。`min_sessions` 必须为至少 2 的整数，`min_filled_orders` 必须为至少 1 的整数；禁止布尔值、未知字段和未知版本。旧 manifest 缺少策略时直接拒绝，不补默认值、不升级旧登记。

- `insufficient_evidence`：验证日期或实际成交订单数量未满足冻结的覆盖要求。成交次数不等于独立统计样本。
- `rejected`：覆盖充分，但净利润非正或期末净资产低于 default。
- `eligible_for_locked_test`：满足以上构造场景条件，可参与锁定排序；不代表统计确认、真实盈利或生产晋级。

全部验证计算及运行预算必须先通过，再从受控结果逐候选判定。仅合格候选参与排序。若无人合格，`research_state=stopped_no_eligible_candidate`；有证据不足候选时整体结论为 `insufficient_evidence`，否则为 `rejected`。三种判定均输出原因与覆盖、净利润、相对 default 净资产差，`statistical_significance=not_assessed`。

无合格候选时不写候选锁、不调用测试 worker、不创建测试 attempt。重复调用、进程重启以及继续请求 `lock`／`test` 均返回同一停止结论；验证计算记录仍为 `succeeded`，不会为科学拒绝伪造执行故障。`status` 从冻结策略和不可修改的受控验证记录推导结论；不新增可独立编辑的裁决副本，也不重新运行计算。代码／数据漂移或手工结果仍不能取得受控判定。

有合格候选时仍须显式锁定，之后沿用一次性测试机制。此步不改变测试期账户结果的含义，不把最终测试计算成功解释为科学通过；所有结果继续禁止晋级。失败候选、费用、订单与净资产曲线全部保留。

### 本步验证与路由

2026-11-13，实现阶段位于隔离分支 `codex/quant-scientific-decision`，基线 `4cc1b7c`；当时主工程未修改。本次没有使用真实封存数据、行情缓存或前瞻账本。

- TDD 反例：全部候选亏损且验证计算成功时，旧代码因缺少 `scientific_decision` 失败；随后增加正常停止行为。回归同时覆盖平价但扣费后亏损、覆盖不足、阈值边界、登记后修改输入策略无效、无效策略在读阶段文件前被拒、对象重建及 CLI 回读。
- `.venv/bin/python -m pytest -q -p no:cacheprovider tests/test_research_decision.py tests/test_research_loop.py tests/test_research_loop_gates.py tests/test_research_experiments.py tests/test_temporal_validation.py tests/test_package_architecture.py`：**68 passed，49.66 秒**。
- `offline_store.py`、`offline_datasources.py`、`offline_long.py`、`offline_ml.py`：四项均通过；运行时将 `QUANT_AGENT_HOME`、`TMPDIR` 指向临时目录。数据源使用既有桩，不采集真实行情。
- 完整成功 CLI 回执：`reports/scientific-decision-20261113/`。选中 `quality_first`，3 次验证和 1 次测试；账户、默认基准与现金净资产分别为 10018.99、9978.99、10000.00，仅构造情景。
- 独立进程停止 CLI 回执：`reports/scientific-stop-20261113/`。平价行情、扣费后全部拒绝；登记前删除构造测试文件，此后 `validate / lock / test / status` 均正常完成，**3 次验证、0 次测试、无候选锁**，测试文件仍不存在。
- 改动 Python 文件 Ruff lint、format check 与 `git diff --check` 通过。回执属于忽略的本地产物，不随源码提交；不代表真实收益、显著性或生产准入。

初始 Anaconda 环境未安装项目包、`akshare` 与 `polars`，曾导致数据源和两个包级检查失败；随后在本 worktree 的 `.venv`（继承现有科学计算依赖）中安装项目及缺项，以上检查复验通过。验证版本：Python 3.11、pytest 8.4.2、numpy 1.26.4、pandas 2.3.3、scikit-learn 1.7.2、akshare 1.19.3、polars 2.1.0；未改全局环境或项目依赖声明。

实现阶段实际采用 Skill：`test-driven-development`、`using-git-worktrees`、`verification-before-completion`；科学判定边界复用已读取的 `statistical-analysis`。规则：项目 `AGENTS.md`、全局 `skills-index.md`、`engineering-workflow.md`、`python/code-style.md`、`python/data-runtime.md`、`agent/llm-app-design.md`、`shared-code-change-safety.md`。已核对受控循环、通用实验登记、CLI、示例与测试调用方；本次不修改通用登记权限、真实研究入口、交易或前瞻策略。实现阶段未执行远程 Git 交付。

### 合并前复验（2026-11-13）

用户授权本地复验通过后合入 `main` 并提交远程。交付阶段新增加载 `git-workflow`、`finishing-a-development-branch`，复核 `verification-before-completion` 及上述 Python、运行时、Agent、共享代码和工程工作流规则。远端为个人 GitHub `cylovelife/quant-agent`，拉取后的 `origin/main` 仍为 `4cc1b7c`，日常主工程干净。

- 上述六个 pytest 文件复跑：**68 passed，50.65 秒**；四项基础离线脚本复跑均退出 0，仍使用临时工作目录。
- 新完整 CLI 回执：`reports/scientific-delivery-pass-20261113/`；选择 `quality_first`，净资产 10018.99，default 为 9978.99，现金基准为 10000.00；3 次验证、1 次测试，禁止晋级。
- 新独立进程停止回执：`reports/scientific-delivery-stop-20261113/`；构造测试文件在登记前删除，全部候选扣费后拒绝；**3 次验证、0 次测试、无候选锁**，后续 `lock/test/status` 正常返回停止，测试文件始终不存在。
- 五个改动 Python 文件 Ruff lint、format check 通过，`git diff --check` 通过。仅使用构造数据，未运行真实行情、封存数据或前瞻账本验证。

回执仅保留在本地隔离工作区，不随源码推送；冻结回执对应登记时的代码身份，提交后不得回写原登记或把重复运行计为新样本。

## 冻结和隔离

- manifest 固定候选全集及顺序（必须包含 `default`）、验证与测试 SHA-256、日期边界、初始现金、成交政策、科学判定策略及预算。代码与环境继续由现有实验登记冻结。
- 受控协议只允许通过专用登记入口创建；同事务写入不可修改的登记来源标记。通用 `--research-experiment register` 在打开快照前拒绝冒用该协议。执行入口先验证来源，再核对内外协议完整一致；旧版本没有来源标记的记录不会被自动升级为受控证据。
- 候选来自既有 `LONG_SCHEMES`，复用同一个五维评分函数；不纳入 macro，不训练模型、不接受自定义代码。验证先进行上述判定，仅对合格候选按扣费后期末净资产排序，平分按预声明顺序。不能把 IC 排名当账户收益。
- 登记仅校验验证文件，测试文件可以尚不存在；验证快照不含测试行。验证日期必须早于 `test_start`。测试日期必须在冻结测试窗口内。每个阶段至少两日、每天完整同一标的池。
- 每个候选验证经 `run_attempt` 执行。全部受控验证成功且在预算内，并存在合格候选，才允许锁定；手工 `finish` 的结果不能通过。候选和尝试预算分别为候选数、候选数加一，候选锁定后关闭搜索。
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
