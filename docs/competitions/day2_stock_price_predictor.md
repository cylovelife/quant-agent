# 第2天：工程选择、比赛规则与真实数据审计

实际执行：2026-10-28，Asia/Shanghai，提前推进原定10月29日的D2。**继续使用 quant-agent 仓库，以独立 worktree 和独立 Python 环境开展竞赛；Stock Price Predictor 仅保留一次提交闭环练习，后续额外人工投入上限4小时，随后提前转入 Hull 训练。**

已按用户当场授权加入比赛并下载官方数据；没有训练模型、生成测试标签或提交预测。当前不需要用户额外配合。本文是D2当前状态；初次选题记录中的“尚未加入/未下载”属于此前状态。

## 工程选择与已完成的隔离

| 方案 | 当前收益与代价 | 选择 |
|---|---|---|
| 另建独立仓库 | 生命周期和依赖独立，但当前需复制验证工具与规则、维护跨仓版本；首个小型日线任务尚无此必要 | 暂不采用；正式Cup若明显偏离金融再评估 |
| 在当前跑盘目录直接开发 | 改动立即进入当前 `src/`，会改变研究代码指纹；新增依赖也可能干扰冻结环境 | 不采用 |
| 同仓库的独立worktree、分支与虚拟环境 | 保留同一历史与验证来源，同时隔离文件、环境、行情和账本；未来合入仍需单独评估运行指纹 | 已采用 |

活动工作区：`/Users/cylove0330/.codex/worktrees/kaggle-stock-price/quant-agent`。
分支：`codex/kaggle-day2`，起点：`b621f9e9c401e0d81563a0a7210c8f0c9728dcef`。
原工作区：`/Users/cylove0330/Documents/tech/mydev/quant-agent`，保留原 `main`、环境、运行产物和三份未提交规划文档；本次将规划复制至活动工作区继续维护，原目录未同步新进度。

当前聊天已附着到活动worktree。后续源码沿用 `src/` 平铺布局，比赛入口保持独立；不重排投资系统、不把竞赛流程塞入日常跑盘。合入原工作区不是本轮动作，worktree隔离也不意味着日后合入不会改变指纹。

在活动工作区创建 `.venv`，用原仓库的 Python 3.11.14 和现有 `requirements.lock` 安装，锁文件未修改。首次纯缓存安装因缺少 `mini-racer` 缓存失败；随后按锁定哈希从包源下载，完整安装40个包，`uv pip check`通过。未使用付费资源。环境记录位于 `state/competitions/stock-price-predictor/environment.json`。

本次Excel只读审计按 `spreadsheets` 路径使用应用自带Python执行，未编辑、重算或导出源工作簿。审计运行时版本另存于 `audit_verified.json`，与后续模型环境分别留档。

## 规则与加入结果

已重新读取[比赛规则](https://www.kaggle.com/competitions/stock-price-predictor/rules)的比赛特定条款和Kaggle基础比赛规则：

- 练习赛，无奖牌或奖金；仅可使用提供的数据，不可使用测试目标列；提交跟随样例格式。
- 基础规则要求单一真实账号、满足个人参赛资格；队伍外不得私下共享比赛代码或数据。用户点击授权不等于本轮独立认证了个人资格。
- 基础规则与特定规则冲突时，基础规则优先；共享想法的鼓励不能替代代码共享约束。

用户已在本聊天明确授权当前账号 `leon0330` 接受本比赛的两类规则。随后实际点击 `I Understand and Accept`，平台返回 `Rules accepted. Good luck!`，规则页显示已接受，主按钮变为 `Submit Prediction`。本轮没有再次索取同一项授权。

加入凭据保存在 `state/competitions/stock-price-predictor/evidence/joined-confirmed.txt` 和 `joined-confirmed.jpg`；接受前的规则全文观察保存在同目录 `rules-observed.txt`。这些是本地证据，不提交Git。

只读打开提交界面核实：平台允许CSV或Parquet，原文为 **“365 rows and a header”**。D2曾将其解释成“365行含表头，即364行预测”，D3复核后撤回该解释：真实样例确有364条，但平台文字不能证明接受364条，须以服务器实际回执为准，不补造缺失记录。当时显示“今日剩余9次”，不能据此断言每日总上限就是9次。本轮没有选择文件，关闭了空白提交界面。随后Submissions页显示 `No submissions found`，最多可选2份最终提交；这不改变该练习榜使用全部测试数据的限制。格式和次数观察见 `evidence/submission-contract-page.txt`，空提交列表见 `evidence/submissions-observed.txt`；后续结果见[第3天记录](day3_stock_price_predictor.md)。

## 官方下载与文件身份

从[官方Data页](https://www.kaggle.com/competitions/stock-price-predictor/data)的 Download All → Download as zip 下载。

- ZIP大小：201,516字节；解压后六文件共223,212字节。
- ZIP SHA-256：`89068e5d5dcf4639fe52775b807e9b1f7909c86e708a21a722196ea64fda30e0`。
- 根目录和 `StockTrends/` 下的三对文件逐字节相同，各自哈希也相同；它们是三份内容的重复副本，不是两套样本。
- 保留下载ZIP和六个原文件不变；正式分析只读取根目录的三个文件，不能混合目录增加行数。

| 规范文件与工作表 | 实际内容 | SHA-256 |
|---|---|---|
| `Train.xlsx` / `Train` | 1,460行×6列：`Date, High, Low, Open, Close, Target` | `ca6e4e4621ca774e40180814cac5bda5872c6cc26b77aee8970a2cf004d0238c` |
| `Test.xlsx` / `Test` | 364行×5列：`Date, High, Low, Open, Close` | `3319a611dead47ca837b28fc1965965a113948031233dd04e880d94fe08aec4d` |
| `sample_submission.xlsx` / `Sample_submission` | 364行×2列：`Id, Target` | `eca72fc4865c535b67628285d92c4f15e49ed9029ef24f29fdb43137563c1fa5` |

源文件位于活动工作区 `state/competitions/stock-price-predictor/raw/`，每个文件的大小、路径与哈希见 `file_manifest.json`。网页文字所称csv与实际xlsx不同；未来adapter必须遵循实际列名、大小写和sheet，不静默猜测。

## 时间、价格与标签检查

| 检查 | 训练集 | 测试集 |
|---|---|---|
| 日期范围 | 2015-11-23 至 2019-11-21 | 2019-11-22 至 2020-11-19 |
| 日期完整性 | 升序、无重复、逐自然日连续 | 升序、无重复、逐自然日连续 |
| 周末行 | 416行，全部OHLC与前一行相同 | 104行，全部OHLC与前一行相同 |
| 所有OHLC相邻重复行 | 453行 | 113行 |
| 缺失单元格/非正价/高低价顺序错误 | 均为0 | 均为0 |
| train/test日期重叠 | 无；训练结束严格早于测试开始 | 同左 |

这些是**自然日重复行情**，不能按行数当成交易日样本。非周末重复行的具体来源未核查，不凭价格重复指定某个交易所的节假日。数据没有证券代码、交易所或复权类型字段；仅登记“提供的一条序列”，不根据熟悉的价格猜标的，也不引入外部行情补齐。

`Train!F2`等标签采用 `=IF(E3>E2,1,0)`，即比较下一条自然日记录的收盘价。前1,459个非末行标签与文件中的下一行收盘比较全部一致，无不匹配。最后一行 `F1461` 的公式引用空白 `E1462`，缓存为0，不能证明真实下一日标签。**训练及验证排除2019-11-21这一行，不改原文件，也不用测试行情修补它。**

排除后有1,459个可核对标签：0类909个、1类550个；训练多数类占比约62.30%。这是训练数据构成，不是验证成绩。周末的自然日目标和分类不平衡会使Accuracy较难直接反映市场预测能力。

样例的364个 `Id` 为0–363，对应Test原始行顺序；所有 `Target` 都是缓存的0占位值。样例里361个公式均位于Id列（例如 `=ROW()-2`），Target列无公式，**没有发现样例泄露测试答案**。未重算Excel、未从测试相邻收盘构造任何标签。

## 数据契约与选题调整

可机读契约已保存为 [stock_price_predictor_contract.json](stock_price_predictor_contract.json)。它记录实际文件身份、列与sheet、日期粒度、末行排除、预测可见边界、提交ID映射和禁止测试标签回推。这是已审计的输入约定，尚未实现为运行时adapter。

本题具有足够结构支持一次时间隔离的分类练习，但**自然日重复价格、末行标签缺陷、未声明标的/复权口径，加上全测试公开榜**，限制了它对正式金融预测的训练价值。保留一次闭环的理由是文件读取、时间边界、打包与平台评分仍是必须实际走通的能力。

因此执行以下取舍：

1. 后续最多再用两个主要工作时段、合计不超过4小时，完成最小adapter、时间验证、简单基线和一次有效提交；这不是要求本轮提交。
2. 官方练习指标仍保留周末行；不删除周末后静默改成另一个目标。标签结束时点取下一条提供记录的Date。
3. 只使用过去和当行可见信息，训练集内拟合预处理；不为争小榜排名反推测试标签或扩大搜索预算。
4. 完成提交链路或达到投入上限即转Hull；若平台暂不能提交，保留本地包与具体缺口，不无限延长热身。

当前未训练，因此不报告模型Accuracy、独立泛化或个人名次；当前未提交，因此不报告提交有效性或获奖结果。用户只需在之后出现新的实际协议或身份门槛时配合，本轮加入授权已经完成。

## 本轮验证与恢复入口

已通过：原始ZIP/文件哈希、三对副本一致、实际行列/sheet、日期与OHLC检查、1,459条训练标签一致性、末行空引用定位、样例ID映射、依赖兼容性、审计脚本静态检查。没有改动 `src/`、`run.py`、`config.json`、`params/` 或依赖锁文件；未跑与本轮无关的业务全量回归。

数据审计脚本位于忽略的本地运行目录，可以在活动工作区复核：

```bash
/Users/cylove0330/.cache/codex-runtimes/codex-primary-runtime/dependencies/python/bin/python3 state/competitions/stock-price-predictor/audit_day2.py
uv pip check --python .venv/bin/python
.venv/bin/ruff check state/competitions/stock-price-predictor/audit_day2.py
```

输出快照为 `state/competitions/stock-price-predictor/audit_verified.json`，包含分析环境、脚本哈希及数据哈希。审计重跑只读xlsx，不拟合模型、不生成测试标签、不写账本。活动worktree仍在使用，不归档；`state/`与`.venv/`被Git忽略，不能假定其内容已通过Git备份。

本轮加载：`executing-plans`、`brainstorming`（工程归属取舍）、`using-git-worktrees`、`web-access`、`spreadsheets`（只读xlsx审计）与 `verification-before-completion`。读取项目 `AGENTS.md`、`skills-index.md`、`engineering-workflow.md`、`python/code-style.md`、`python/data-runtime.md`、`anti-hallucination.md`；Excel审计另读只读工作流、API与格式指南。Harness中与本轮有关的数据/产物隔离和验证约束充分，未修改工程指令文件。未加载TDD或Git交付流程：本轮不改生产行为、不提交/推送。
