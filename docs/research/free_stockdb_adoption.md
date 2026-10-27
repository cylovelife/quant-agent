# free-stockdb 借鉴与本地验收

## 2026-10-27 成交量精度与跟踪修订保护

已核实512880的历史成交量最大差0.5手，对量比的影响约3.59e-9，价格和成交额未变。
原统一修订检查却遮蔽了当日最低1.000跌破原低点1.007的事实。现按依赖分开量额修订：
价格修订仍暂停整轮，量能按有限取整兼容和逐根区间判断，实质量修订／跨阈值继续暂停量能，
可信低点失守优先终止。真实冻结快照重放、133项相关回归及四项基础离线自检通过；
原评分、参数、旧研究记录与交易日计数保持不变。
完整依据、边界和规则清单见[确认跟踪修订记录](confirmation_tracking.md)。

## 2026-10-26 确认条件逐日跟踪

已接入原 `run.py --daily`：主报告新增信号状态与前一交易日优先级变化，明细保留数值
依据；固定原高低点、登记时间与参数，重复运行不增加独立证据。缺数据/复权修订不
假报失效或排名退出，前一交易日无研究记录就明确无法比较。
完整方案、状态定义、路由/规则和51项测试证据见
[`confirmation_tracking.md`](confirmation_tracking.md)。本次不修改生产评分或参数。

## 2026-10-26 主报告结论汇总与明细分离

- 入口 `render_report` 现在生成可独立阅读的主报告：结论汇总在最前，股票与ETF分别
  按现有得分降序，仅选达阈值标的，每类最多3只；同分按代码，不强行填满、不暗改模型。
  当前ETF前三为510300/510500/512100，股票为601318/000001。
- 汇总逐项列出关注理由、当前风险，以及后续完整日线的价格高低点、MACD、动态MA60
  和回踩量能复核条件。它们是待研究的技术观察条件，没有被当作新交易规则或策略准入。
  数据阻断时不生成优先清单，预热不足/未达阈值不入选。
- 原完整证据由 `render_details` 生成 `.details.md`；运行id版、日期版及latest入口
  均同时产出主报告和明细并双向链接。`latest.json` 提供两份日期版文件路径；
  `research_summary` 随研究记录留档，不要求读者翻看72行评分拆解才能了解结论。
- 路由：test-driven-development、verification-before-completion；读取工程工作流、
  Python风格/数据运行时、共享代码调用方安全、标识核验规则及项目AGENTS.md。
  仅修改确定性报告呈现和产物接线，未改变现有评分、数据或生产参数。
- 两项新回归先复现失败，修复后
  `.venv/bin/python -m pytest -q tests/test_local_research.py`：36 passed（2.72s）；
  覆盖优先级与阈值、数据阻断、不凑数、数值确认参考、摘要在前、主报告/明细分离、
  日期版/latest/运行id版路径与双向链接。Ruff check/format、git diff --check通过。
- `.venv/bin/python run.py --daily --offline`实际生成两份报告；主报告54行、完整明细340行。
  仍采用2026-10-23冻结收盘数据，观察交易日数仍为1，没有新增收益证据。

## 2026-10-26 名称与逐标的研究说明

- 通过腾讯批量行情实际请求核对观察池 12 个代码及名称；ETF 与既有本地
  `rank_snapshot` 中的东财名称逐项一致。结果及来源、核对日期保存于
  `config.json.local_research.instrument_metadata`，按市场分区，离线日报无需联网查名。
  名称元数据进入运行指纹与台账；名称更新不增加收益样本或交易日证据。
- 所有行情/评分/覆盖表和逐标的章节显示名称。增加 MA60、当前/前一 MACD 柱，
  解释每项已有得分及实际权重贡献，不修改 `strategy.score_row` 或生产参数。
- 每只标的分别列出主要贡献、距满分的主要差距、趋势/量能/动能限制和阈值结论。
  结论明确区分放量上涨/下跌、负柱收窄/转正、低吸适配分/基本面评价。
  显示贡献根据已舍入单项分计算，总分保留原模型结果，报告说明末位舍入差异。
- 路由 Skill：test-driven-development、verification-before-completion；规则：
  engineering-workflow、python/code-style、python/data-runtime、shared-code-change-safety、
  anti-hallucination，以及项目 AGENTS.md。主流程为确定性 Python 报告增强，
  无需新 LLM、公司数据库流程、全局配置或策略模型变更。
- 三项新回归先观察失败，再执行
  `.venv/bin/python -m pytest -q tests/test_local_research.py`：34 passed（2.69s）。
  覆盖名称与逐只解释接线、元数据变更留痕且不增加交易日、权重贡献核对及语义边界。
  Ruff check/format、git diff --check 通过。
- `.venv/bin/python run.py --daily --offline` 返回 0，12 只均有名称和六项评分解释；
  仍为 2026-10-23 冻结行情、1 个观察交易日，8 只达到研究阈值。
  `params/params.json` 与 Git HEAD 字节相同，不把本次展示增强当作新 alpha。

## 2026-10-26 日报数据展示修复

- 原因：原 `daily_research._report` 只渲染历史 IC 和运行状态；冻结行情已经存在，
  但没有逐标的市场事实与评分，不能满足阅读每日研究报告的需要。
- `research_report.py` 直接从同一冻结数据和留档参数计算逐标的收盘价、相邻收盘
  涨跌幅、5/20 根日线收益、成交额、RSI、量比、布林位置及六因子评分。
  不新增行情源，不把盘中价和冻结收盘价混用，不改变策略参数。
- 报告增加行情覆盖与逐标的来源，生成 `state/research/latest.md` 和
  `research_YYYY-MM-DD.md` 阅读入口；历史台账和按 run_id 的报告保留。
- 路由：systematic-debugging → test-driven-development → verification-before-completion；
  读取本项目 AGENTS.md、skills-index，以及 engineering-workflow、python/code-style、
  python/data-runtime、shared-code-change-safety、anti-hallucination 规则。
- 先复现两项失败（缺 observations），修复后运行
  `.venv/bin/python -m pytest -q tests/test_local_research.py`：31 passed（2.74s）；
  覆盖截止日前的实际行情、5/20 日收益、短历史不伪造评分和日期版日报接线。
  Ruff check/format、git diff --check 通过。本次没有改数据源、历史 IC 或生产参数，
  不重复执行无关的全链路联网抓取。
- 实际运行 `.venv/bin/python run.py --daily --offline` 返回 0：12 只/3,600 根，
  行情交易日仍为 2026-10-23，只有 1 个不同交易日观察；8 只达到 62 分研究阈值。
  新增可见明细是报告修复，不代表新的样本外证据或新交易日。

## 范围与依据

审阅上游 commit `f352641d5184ed0f142ac7ec3d0aee3a95acbd14`：
`cpp/src/updater.cpp`、`cpp/src/client.cpp`、`cpp/src/server.cpp`、CMake 定义、
`cpp/include/stockdb/updater.hpp`、`docs/DATA_SOURCE.md`、
`pybao/stock_sdk.py`、`pybao/zhibiao.py`、`调用方式/ai_mcp/stock_mcp_server.py`、README 与 MIT LICENSE。
来源：<https://github.com/hello245m/free-stockdb/tree/f352641d5184ed0f142ac7ec3d0aee3a95acbd14>。

上游可核实的实现是：同步器按文件大小/SHA-256 跳过未变内容，临时文件校验后替换；
SDK 支持批量查询、字段投影、周期聚合和复权。README 的性能数字未在本机复测。
`zb_core`、`stockdb` 二进制不在本轮可直接复用的 Python 源码中；上游同步节点与行情
授权不随 MIT 代码许可证自动获得。本轮独立实现上述工程思路，不复制上游代码，
不安装二进制、不拉全市场分钟库，不改变现有候选池和评分公式。

源码核验也发现了不能直接照搬的边界：公开 C++ client 的 `get_stock_list`、
`get_kdata`、`get_factors` 仍返回空集合；server 的 `process_zb_get` 当前只生成 MA5，
不能据此声称公开 C++ 工程已经实现 README 所列的全部 39 指标。Python 指标包装层
依赖 `zb_core`，不能把包装层存在等同于本机已有可运行计算核心。SDK 聚合和同步器
可以借鉴，整套引擎的功能/性能不能直接继承为 quant-agent 的验证结论。

## 本轮实施计划

1. `src/local_data.py`：只读 SQLite 批量读取，市场/复权隔离，字段白名单，日期过滤，
   日/周/月 OHLCV 聚合。所有周期使用真实最后一根日线日期；先截断再聚合，避免前视。
2. `src/data_snapshot.py`：确定性 JSON 数据集、SHA-256、原子发布，重复数据复用，
   本地目录离线同步与完整性校验。快照不覆盖已有快照，也不直接还原/覆盖行情库。
3. `src/daily_research.py`：显式同步固定观察池，生成冻结快照，复用现有
   `rolling_factor_ic` 做描述性历史体检；按输入/参数/代码指纹幂等记录 SQLite 台账。
   历史修订与新增交易日分开，重复运行不增加证据天数。所有结论仅为观察，
   不修改 `params/`，不自动准入因子，不宣称通过样本外/FDR/影子验证。
4. 接入 `run.py` 新 CLI，保留双轨入口行为；补失败/边界测试并执行真实联网采集、
   离线复跑、快照校验、已有相关回归。每天收盘后运行此管线以积累证据。

不变量：盘中未收盘日线不参与每日研究；缺失/过期/坏 OHLCV 显式留痕；数据库
写入失败不能报采集成功；相同数据重抓不构成独立样本；qfq 历史修订须全窗口更新。
失败时保留之前的快照和台账，CLI 返回非零；重跑按同一内容恢复，无需清空用户状态。

## 路由与规则清单

- Skill：brainstorming、writing-plans、executing-plans、test-driven-development、
  systematic-debugging、verification-before-completion、OpenAI Docs（本地定时运行）。
- 规则：`engineering-workflow.md`、`python/code-style.md`、`python/data-runtime.md`、
  `agent/llm-app-design.md`、`agent/llm-app-runtime.md`、`shared-code-change-safety.md`、
  `anti-hallucination.md`，以及本项目 `AGENTS.md`。
- Harness：原项目缺少根级指令与新能力的验证边界，新增最小 `AGENTS.md`。
- 索引中的 `python-agent-engineering` 不在当前 catalog 且本地两处入口均不存在；
  本轮无需 LLM SDK/脚手架，使用已读取的 Python/Agent 规则独立实现。
- `skill-route-audit.sh`：索引解析、引用和软链检查通过。
- 不加载：公司 DBPaaS（本地个人 SQLite）、Git 交付（未要求提交/推送）、Phase 2
  因子移植/新模型流程（本轮只复用现有指标），不更改全局配置。

## 验证记录

日期：2026-10-26，Python 3.11.14，独立 `.venv`（关闭 system-site-packages）。
`requirements.lock` 固定 40 个依赖并带文件哈希；`uv pip sync --require-hashes` 安装，
`uv pip check --python .venv/bin/python` 通过。运行时实测 numpy 1.26.4、pandas 2.3.3、
scikit-learn 1.7.2、scipy 1.17.1；未修改系统 Python 环境。

| 验证 | 本轮结果 |
|---|---|
| `.venv/bin/python -m pytest -q tests/test_local_research.py` | 29 passed（3.28s）；覆盖查询/聚合/截止日、快照/镜像损坏与路径拒绝、幂等/修订/回退、离线禁网、CLI 真退出码、写库失败、源码/参数/运行时留痕与重复免重算 |
| `.venv/bin/python tests/offline_store.py` | 通过，日志 `logs/offline_store.log` |
| `.venv/bin/python tests/offline_datasources.py` | 通过，日志 `logs/offline_datasources.log` |
| `.venv/bin/python tests/offline_long.py` | 通过，日志 `logs/offline_long.log` |
| `.venv/bin/python tests/offline_ml.py` | 通过，日志 `logs/offline_ml.log` |
| `.venv/bin/python tests/verify_phase_cache.py --refresh` | 通过；3 阶段 fresh/prime/hit 深度等价，日志 `logs/verify_phase_cache.log` |
| Ruff check / format --check | 4 个新模块 + 新测试均通过；存量文件只检查本轮 diff，不批量格式化 |
| `py_compile run.py src/fetcher.py`、`git diff --check` | 通过 |
| `run.py --daily` 冷启动真实采集 | 12 只 × 301 根写入本地库；首次旧端点 501 的失败也保留在台账。修复兜底后 46.8s 完成，截止日安全选择 3,600 根，12 个序列均无 missing/invalid/stale/lagging |
| `run.py --daily --offline` 连续复跑 | 返回 0；同一 run_id、`reused=true`、`evidence_days=1`，没有把重复运行增加成新证据 |
| `--verify-snapshot` + `--sync-snapshot` + 镜像再校验 | 真实 12 个序列/3,600 根通过；临时镜像验收目录已自动清理 |
| `run.py --mode short --market cn,etf --quick --no-eval` | 原有双市场短线链路通过，110.7s，16 条候选、Markdown/HTML/完整报告与 manifest；只运行虚拟账本 |
| 原有生产参数 | 与 `git show HEAD:params/params.json` 字节相同；SHA-256 前缀 `fbddf7e71171db68` |

原有短线运行产物是盘中研究结果：`reports/report_2026-10-26.md` / `.html`，
不应当作定盘样本；新增每日研究入口单独排除了 10 月 26 日盘中 K 线，
其 `data_date=2026-10-23`，历史 IC 面板为 12 标的、2,700 行、450 个市场/日期截面，
标签完整日期区间 2025-11-11 至 2026-10-16。

综合评分的历史 Rank IC=0.0194、Rank ICIR=0.043。它是固定小观察池上的描述性
结果，不能证明新 alpha；原有 walk-forward 在本轮也判定候选规则样本外反向，
维持当前配置。`promotion_allowed=false` 是有意保留的门禁。

### 运行中发现并修复的原有缺陷

1. `web.ifzq.gtimg.cn` 在本机直连、环境代理、curl_cffi 均 HTTP 501；从实际安装的
   AKShare 1.19.3 `stock_zh_a_hist_tx` 源码确认当前 Tencent
   `proxy.finance.qq.com/ifzqgtimg/appstock/app/newfqkline/get`，实测 HTTP 200。
   `fetcher.fetch_kline` 增加 CN/ETF 同口径兜底，真实来源写入 `src`。
2. 旧解析对数值型 `item[6]` 调用 `.startswith`，会把整根 K 线丢弃；新增回归先
   复现失败后修复。扩展格式的成交额位于 `item[8]`、单位万元，现转换为元；
   成交量保持原项目约定，避免隐式改变既有因子口径。
3. 原入口调用 `main()` 时未传播返回码，探针/研究失败会看起来成功；现在通过
   `SystemExit` 传播。查询 JSON 的耗时日志写 stderr，stdout 可直接解析。

### 定时运行与剩余边界

已创建并回读 ACTIVE 的本聊天 heartbeat：`quant-agent` /「quant-agent 每日研究与进化」，
每天北京时间 18:30。每轮先跑数据与快照核验；仅在证据支持时最多实施一个可逆的本地
增量并做针对性回归，不强迫每日修改、不自动换生产参数、不自动交易或推送。
本地调度需要电脑及桌面应用运行，参见
[OpenAI 官方定时任务说明](https://developers.openai.com/training/walkthroughs/scheduled-tasks)。

- 本轮完成的是数据层、研究台账与每日执行基础，未完成设计 Phase 2–5。
  不把当前日报等同于「完整 RSI」或两周样本外验收。
- 后续先校准统一验证门禁（重叠标签、有效样本、FDR、边际增量），再扩因子/模型，
  最后实现预测锁盒、T+1 对账和影子替换。新台账保留完整参数与代码指纹，
  没有自动归档每一版源码；完整复现还需要保留相应源代码版本。
- `offline_e2e.py` 没有直接运行：它的首段硬编码 2026-09-20 并依赖此前的推荐文件；
  这个新 checkout 没有那份历史 fixture。没有伪造历史推荐来制造通过结果；本轮使用
  真实短线全链路、新增离线契约测试和阶段缓存等价性验证覆盖相关边界。
- 新研究同步只支持 CN/ETF 日线；本地查询可读现有其他市场/复权分区。
  周/月线是日线聚合，不宣称已有分钟线、hfq 因子或点时全市场成分股历史。
- 上述实现验收阶段尚未提交、推送代码；本地运行产物保留供复查。

### 2026-10-26 Git 交付核验

- 按用户授权提交并推送至个人仓库 `cylovelife/quant-agent`，使用个人 GitHub SSH；
  实际交付版本以远端 `main` 的 Git 提交为准，不将本地定时任务配置视为仓库文件。
- 交付流程读取 `git-workflow`、`finishing-a-development-branch`、
  `verification-before-completion`，以及项目 `AGENTS.md`、`engineering-workflow.md`、
  `python/code-style.md`、`python/data-runtime.md`、`anti-hallucination.md`。
- 交付前复核：两组新增测试共 51 项通过（3.28s）；8 个新增 Python 文件的
  Ruff check/format 与 `git diff --check` 通过。此前真实采集、离线研究与存量回归
  的适用证据见上文，不将本次交付复核表述为重新执行所有联网或长期验证。
- 本次仅提交源码、观察池配置、依赖、测试和文档；`state/`、`reports/`、`logs/`、
  `.venv/` 与密钥均不纳入提交。
