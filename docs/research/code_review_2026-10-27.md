# quant agent 全仓代码审查及行情诊断

2026-10-27，北京时间。依据投资与研究执行规划、分步建设计划及各模块契约，审查当前工作区的主入口和 `src/` 下共42个Python源码文件，包括原有未提交及新增实现。已修复可复现的资金记录保护、数据时点、研究比较、幂等和缓存问题；最终全量测试334项通过。本轮保留原工作区改动，没有提交或推送。

证券ETF 512880的前复权刷新仍受数据源覆盖限制：腾讯成功响应只包含未复权 `day`，不含 `qfqday`，备用东财接口在本次探测中不可达。当前代码会明确报告复权口径不可用，不将raw改名写入qfq。已启动虚拟试验因本轮修复触发源码漂移保护，暂停后续结算。

后续状态：上述为审查结束时的状态。2026-10-27 11:01已完成[虚拟试验版本衔接](paper_runtime_handoff.md)，
并通过[真实前复权因子备用来源](etf_qfq_sources.md)成功刷新512880；原始审查和失败证据保留。

## 审查覆盖

| 范围 | 源码 | 主要核查 |
|---|---|---|
| 主流程及价值观察 8个 | `run.py`；`portfolio`、`fundamentals`、`value_strategy`、`value_track`、`macro`、`report`、`csc_source` | 调用顺序、财报可见性、持仓保护、收益对照、报告输入 |
| 研究及计算 13个 | `indicators`、`strategy`、`trade_semantics`、`backtest`、`factor_eval`、`model_select`、`exit_model`、`ml_eval`、`temporal_validation`、`evaluate`、`phase_cache`、`research_experiments`、`research_experiment_cli` | 数值边界、标签成熟、日期隔离、现行参数保护、比较样本、冻结与缓存 |
| 数据及研究留档 14个 | `fetcher`、`store`、`local_data`、`data_snapshot`、`data_quality`、`daily_research`、`research_cli`、`research_report`、`research_tracking`；`datasources`的5个模块 | qfq与raw隔离、来源和版本、坏输入、快照完整性、确认条件、源降级 |
| 账户及成交 7个 | `execution`、`execution_data`、`execution_cli`、`paper_ledger`、`corporate_actions`、`paper_daily`、`paper_experiment` | 现金及数量、费用、事务回滚、重复请求、日终封存、双账户对照 |

相关测试、依赖清单、入口配置和研究文档一并核对。账户、研究、主流程与价值模块分别进行了独立只读评审；主线程核实发现并检查集成，修复分工没有重叠写入。

## 已修复的问题

### 数据和主流程

| 触发条件及影响 | 修复 | 回归证据 |
|---|---|---|
| `fetch_kline(use_db=False)`返回带`ingest_issues`的残余行，每日同步仍把它写入库，随后缺陷标记丢失；错误复权身份也能进入qfq分区 | `research_cli.synchronize`在落库前复核质量及来源身份，拒绝整批缺陷输入 | 独立SQLite确认拒绝后未写入行情 |
| 事件采集复用变量`ev`，覆盖已经计算的收益复盘结果，报告收到事件摘要 | 主循环使用独立的`event_result` | 构造主流程同时执行复盘和事件采集，报告仍收到原收益结果 |
| 行情中一条日期无法解析，会使批量同步直接异常退出 | 保留`NaT`及`invalid_dates`拒绝证据，交由准入检查拒收 | 无效源日期不再逃出适配器，且带明确质量标记 |
| 沪市两融响应含多个实际日期且与请求日不一致时，全部行被改成最新日期 | 保留每行实际日期，日期不匹配只作为诊断 | 同一代码两日记录保持两个日期 |
| 事件日期归一只拼接数字，会接受2月30日或13月等无效日期 | 用真实日历解析确认日期 | 三类非法日期均返回缺失，不形成有效主键 |

这些回归位于[数据与主流程回归](../../tests/test_review_data_regressions.py)，共10项。

### 价值观察和历史记录

| 触发条件及影响 | 修复 | 回归证据 |
|---|---|---|
| 只有季报、没有年报时，`derived_metrics`引用未定义变量 | 缺少年报的质量指标回退到最近一期已观察报告 | 季度财报可正常派生指标 |
| PB历史序列按报告期使用BPS，提前读到尚未披露的财报 | 仅使用行情日之前实际披露的最新报告期；未知及法定截止日代理排除 | 公告当天仍不使用新BPS，之后才生效；晚披露旧报告不覆盖新报告期 |
| 长期跟踪读取CSV丢失代码前导零，重跑再次追加同一里程碑 | 身份字段保持字符串，幂等键包含市场 | `000001`重复运行新增0行，样本数保持1 |
| 个股与基准按各自第N根比较，停牌或缺根时比较了不同区间 | 基准严格匹配个股实际起止日；缺任一日期时超额留空 | 构造例从错误的−10%修正为同区间−20% |
| `today`早于输入行情末日时仍能提前结算里程碑 | 行情先截断至结算截止日，坏价及无效日期拒用 | 未来里程碑不登记 |
| 现存长期跟踪CSV损坏或身份重复时，读取错误被当成空历史，可能重复计样本 | 严格校验格式、身份、里程碑及收益，异常停止；汇总复用已校验数据 | UTF-8、CSV、表头、行宽、身份、数值及重复身份异常均不追加或改写历史 |
| 持仓JSON损坏时返回空仓，后续可能覆盖旧账本；保存中断可能截断原文件 | 只有文件不存在才创建空结构；坏账本报错，保存采用临时文件原子替换 | 损坏或序列化失败后原文保持 |
| 平仓CSV成功但持仓文件尚未保存时进程中断，恢复会重复追加平仓 | v2交易按明确身份幂等、内容冲突拒绝；交易文件原子发布 | 从同一pending状态两次恢复仅保留一笔交易，冲突和发布失败保留历史 |

这些回归位于[价值与记录保护回归](../../tests/test_review_orchestration_regressions.py)，共26项。原有历史文件没有被去重、重算或迁移；若存在旧格式损坏或重复记录，当前流程会明确要求恢复，不能将它们继续作为新证据累计。

### 研究计算和缓存

| 触发条件及影响 | 修复 | 回归证据 |
|---|---|---|
| 离场模型选定`t_min=-2`，建议入口却可在`t=-1.2`时直接卖出，使用了回测未验证的较松门槛 | 建议与模拟执行同一选定阈值；未达门槛仅观察；`None`维持显式不设t门槛的语义 | 同一状态输入的建议与模拟一致 |
| 离场评估计算回撤遗漏初始本金，首笔亏损被漏掉 | 净值序列加入初始1 | 单笔−10%回撤为−10%；连续−10%、−20%回撤为−28% |
| 合法的`None`止损或止盈被强制转float，引发异常 | 相关入口统一使用`LabelSpec` | 离场建议、规则扫描及短线选型接受禁用止盈止损 |
| 候选网格的最长持有期短于当前配置，当前基线被截短 | 收集路径覆盖候选及当前配置的最大持有期 | 网格1日、当前5日时，基线仍完整持有5日 |
| 信号循环边界少收最后一个已经成熟的标签 | 修正末端边界 | 最后完整窗口被纳入，未成熟尾部仍不进入 |
| 阶段缓存签名遗漏指标配置，修改RSI周期仍可能命中旧模型 | 纳入`indicators`并将缓存语义格式升至5 | 三个依赖阶段均拒绝旧指标签名的缓存 |
| NumPy NaN及Infinity转换后写成非标准JSON | 统一降为`null`并使用严格JSON序列化 | 读回为`None`，严格JSON检查通过 |
| ML和规则分各自过滤缺失值或常数截面，比较样本与日期不同 | 先确定共同有限值行及双方都可评估的日期，再统一汇总 | 扰动排除行及不可比日期不改变双方比较结果，记录实际比较覆盖 |
| 缓存验证脚本遗漏日期边界，缺配置时返回None却强求命中缓存 | 默认缺边界提前返回不可验证；新增`--synthetic`，对三个非空结果验证重算、首次缓存和命中等价，检查真实计算次数 | 构造入口退出0，三个阶段均只计算两次且第三次命中；无结果不能假通过 |

这些回归位于[研究回归](../../tests/test_review_research_regressions.py)，共16项；缓存独立入口为[阶段缓存验证](../../tests/verify_phase_cache.py)。

### 账户日终恢复

同一每日请求首次结算后，如果本地raw行情只更新抓取时间，开盘及收盘数据版本散列也会改变。原逻辑会把重试判为新的日终意图，阻止多日请求继续推进未完成日期。

修复只在回读已封存日期时生效：先核对原完整请求及结果的哈希，再比较去除有效获取时间及对应版本散列后的业务输入。价格、来源、复权、证券、订单、事件、日期、日历和会话事实仍严格一致，JSON类型差异也会被拒绝。原完整证据和结果保持封存，不重新执行交易。

[账户恢复回归](../../tests/test_review_ledger_regressions.py)共16项，覆盖同价重抓回读、真实输入变更拒绝、原请求证据损坏识别，以及单账户和双账户的多日部分成功后恢复。恢复后买单仍仅一笔，原封存请求与结果未改写。

## 证券 ETF 行情失败原因

本次使用当前源码中的真实请求路径进行只读探测，没有把响应写入正式行情库：

- 旧`web.ifzq`入口请求失败；可用的腾讯代理入口对`sh512880,day,,,300,qfq`返回HTTP 200、业务状态0，但只有`day`，不含`qfqday`。
- 改用明确日期范围、1000根请求、另一`newfqkline`入口后仍只有`day`。同一路径对510300可返回`qfqday`，因此不是整条代理行情服务都无数据。
- 对512880显式请求后复权也只返回`day`；这不能证明两种复权历史与raw相同，不能据此互转。
- 按本机已安装AKShare实现核对东财ETF历史接口，并明确请求`fqt=1`；本次直连、环境代理及浏览器指纹请求链未取得可用响应。

`fetcher`现在为拒收空表附带`fetch_failure.kind=adjustment_unavailable`，每日同步在控制台和同步回执中保留具体原因。原失败统称`source_empty_or_unavailable`，无法区分网络失败与复权口径缺失。

修复后的真实入口验证：

```bash
.venv/bin/python run.py --sync-data --market etf --codes 512880 \
  --research-dir state/research/etf-refresh-diagnosis-20261027
```

退出2，`written=0`，原因是`adjustment_unavailable`，详情包含`qfqday missing; day (raw) rejected`。本地回执为`state/research/etf-refresh-diagnosis-20261027/last_sync.json`。刷新未恢复，先前每日报告仍使用该标的已有的10月26日前复权缓存，不能将本次探测写成刷新成功。

## 已启动虚拟试验的影响

只读检查`cold-start-20261027`原冻结清单，变更文件为`run.py`和`src/quant_agent/application/paper/daily.py`。`verify_runtime`按既有契约返回`frozen experiment code differs; settlement paused`。

策略与现金基准仍各有50,000元现金、空仓、零订单，日终记录为0。原账本及manifest没有改写。本轮没有绕过门禁恢复结算；继续前瞻试验需要带生效时点、保留原起点和全部记录的版本衔接。冷启动文档已同步这一状态，原手动推进命令不再被表述为当前可直接执行。

## 最终验证

所有测试使用构造行情、临时目录或独立SQLite。关键缺陷先由回归复现失败，再修复；四个新增回归文件合计68项。

| 命令或核验 | 结果 |
|---|---|
| `.venv/bin/python -m pytest -q` | 最终334 passed，34.03秒 |
| `.venv/bin/python tests/offline_store.py` | 通过 |
| `.venv/bin/python tests/offline_datasources.py` | 通过 |
| `.venv/bin/python tests/offline_long.py` | 通过；补充历史保护修复后再次通过 |
| `.venv/bin/python tests/offline_ml.py` | 通过 |
| `.venv/bin/python tests/offline_e2e.py --synthetic` | 通过，内含161项既有回归及构造集成路径 |
| `.venv/bin/python tests/verify_phase_cache.py --synthetic` | 三阶段重算、首次缓存、命中结果一致，第三次确实跳过计算 |
| 本轮修改的模块及新增测试Ruff | 通过；`run.py`、`fundamentals.py`按下方既有诊断单列 |
| 42个业务源码Ruff与HEAD逐文件对照 | 当前7项既有诊断，无新增诊断 |
| 4个新增测试文件`ruff format --check` | 通过 |
| 主入口、源码和测试共65个Python文件AST解析 | 通过 |
| `git diff --check` | 通过 |

全仓Ruff保留既有7项：`run.py`的E402、E731；`fundamentals.py`的F401；`macro.py`的F401；`report.py`的F841；`value_strategy.py`的两个F401。它们没有被记成全仓lint为零。

构造回归和缓存一致性支持工程行为正确性，不证明真实可成交容量、公司行动完整性、真实收益或长期样本外有效性。默认缓存行情E2E和默认缓存验证没有运行；本轮仅对明确列出的公网请求进行了真实联网探测。

## 保留的能力边界

研究登记尚未强制接入全部旧入口，跨登记库的测试日期复用约束仍未完成；报告语义统一和策略晋级等计划项按原状态保留。它们没有被测试通过或本次审查改写为已完成能力。

本轮未修改现行参数、历史推荐、历史研究记录、正式虚拟账本或冻结manifest，没有执行实际交易、调权、提交、推送或外部发布。个人本地SQLite按项目规则使用，不涉及公司业务数据库。

## 路由及规则记录

实际加载的Skill：`requesting-code-review`及`code-reviewer.md`、`dispatching-parallel-agents`、`subagent-driven-development`、`systematic-debugging`、`test-driven-development`及`testing-anti-patterns.md`、`verification-before-completion`；审查记录使用`pages:write-page`及`writing_quality.md`，保存于现有仓库。

实际读取的规则：`~/.codex/skills-index.md`、项目`AGENTS.md`、`~/.codex/rules/engineering-workflow.md`、`python/code-style.md`、`python/data-runtime.md`、`shared-code-change-safety.md`、`data-consistency.md`、`anti-hallucination.md`。项目Harness原边界适用，仅补充阶段缓存构造验证入口；未新增推测性业务约束。

本轮没有进行架构重设计、外部投资研究、Git交付或公司数据库操作，相应规划、调研、发布和DBPaaS入口未加载。
