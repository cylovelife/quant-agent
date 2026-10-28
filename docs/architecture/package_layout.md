# Python 包结构与迁移执行记录

本次用户要求覆盖原 `src` 全部模块。沿用现有算法、CLI 参数、数据文件、SQL 事务和错误语义；替换平铺导入；初次重构在隔离worktree完成，随后按用户要求将主工程与远程main同步，不迁移真实账本。

## 结构与取舍

采用 `src/quant_agent/`，使用明确的绝对包导入和 `pyproject.toml`。领域内按市场数据、交易规则、策略、研究验证分包；用例按投资、研究、纸面账户分包；SQLite、快照、网络来源、报告和文件配置放基础设施；CLI 放 entrypoints；比赛在独立的 competitions/stock_price 上下文内。

依赖边界：领域只依赖领域和计算库，不依赖文件持久化、数据库、网络或命令入口。基础设施依赖领域，不反向导入用例和入口。应用负责事务与用例编排，显式组合本地基础设施，不回调 CLI；本项目只有一个本地实现，不批量制造 Repository/Service/Protocol 包装。后续出现可替换实现时，在具体用例的调用边界抽端口，不能让领域感知 SDK 或 SQLite。

本次不是重写交易、账本和研究算法。数据库事务型用例仍由 application/paper 和 application/research 持有；它们与基础设施的进一步依赖倒置属于明确的后续工作，不将分目录宣称为所有用例已完成严格的依赖倒置。

标准依据：[PyPA src layout](https://packaging.python.org/en/latest/discussions/src-layout-vs-flat-layout/)、[pyproject.toml](https://packaging.python.org/en/latest/guides/writing-pyproject-toml/)。原模块至新路径的完整映射见 [module_migration.json](module_migration.json)。

## 不变量与迁移风险

- qfq/raw、标签成熟时点、验证窗、费用、幂等键、不可变账本及失败关闭规则不变。
- 新路径通过统一的 workspace 定位，不把 state/params/logs 写到 Python 包目录。导入不创建目录、不联网、不修改 socket 默认超时。
- 根 `python run.py` 保留；安装后增加 `quant-agent` 和 `python -m quant_agent`。旧平铺模块导入改为显式包导入，不设置全局 sys.modules 旧名别名。
- 原竞赛历史代码清单和脚本保留为历史证据；新代码另记当前清单，不回写既有冻结实验或伪造哈希一致。
- 研究和纸面实验指纹覆盖新包及实际入口，旧版本漂移仍拒绝；新版本衔接仍需现有流程。
- 失败时在本工作区修复，不覆盖原投资目录；运行产物留在忽略的 state/。

## 执行步骤与验收

- [x] AST 调用依赖与路径/指纹/子进程入口盘点；确认整个 src 范围。
- [x] 更新失效的平铺布局项目约束，保留数据与账本门禁。
- [x] 增加包结构、领域依赖、导入副作用和跨目录运行的回归检查。
- [x] 移动模块，抽离策略评分/配置加载/契约持久化，更新全部源码及测试导入。
- [x] 拆 CLI 参数与投资编排，建立统一路径及真实源码指纹。
- [x] 配置安装、更新当前文档和竞赛清单，保留历史文件。
- [x] 全量 pytest、四项基础离线检查、synthetic e2e、synthetic 缓存等价、打包/入口/CSV 确定性复现通过。

## 路由与规则

主流程 brainstorming + writing-plans；关键行为使用 test-driven-development，完成使用 verification-before-completion。已读项目 AGENTS、skills-index、engineering-workflow、Python code-style/data-runtime、shared-code-change-safety、anti-hallucination。交付沿用此前已明确的远程提交与main合入方式，并在原Kaggle代码审阅Notebook更新同一份自有竞赛代码；不更新原冻结账本，不进行新的模型训练或预测提交。Git交付使用git-workflow与finishing-a-development-branch，仍按verification-before-completion回读。

## 安装与入口

```bash
uv pip sync --python .venv/bin/python --require-hashes requirements.lock
uv pip install --python .venv/bin/python --no-deps -e .
.venv/bin/quant-agent --help
.venv/bin/python -m quant_agent --help
.venv/bin/python run.py --help
.venv/bin/python -m quant_agent.competitions.stock_price.reproduce --help
```

日常推荐仍在明确工作区运行；`QUANT_AGENT_HOME=/path/to/checkout`可显式指定config、params和state根目录，包导入本身不创建该目录。从wheel运行同样需要自行提供工作区配置，不将用户配置、原始行情或账本打入wheel。requirements.lock继续固定完整验证环境，pyproject声明包与可选研究/来源/比赛依赖。

`execution.load_policy`的文件读取移到`infrastructure.configuration.load_policy`；领域仅保留`policy_from_definition`。`freeze_data_contract`移到`application.data_contracts`。宏观/价值纯评分、IC统计移入domain；外围模块保留显式重导出以维持既有调用语义。旧根run.py内部编排函数改从`application.investing.workflow`访问，根脚本仅承担CLI兼容。

## 结果

- 原51个src模块全部迁移；无旧平铺运行时模块、无sys.modules旧名别名。包的领域不依赖I/O、基础设施不反向依赖应用或入口、应用不反向调用入口，均有AST边界测试。
- 包导入逐模块测试：不创建工作区目录、不连接网络、不更改socket默认超时；CLI实际研究执行仍设置原20秒兜底超时并保留退出码。新源码文件及启动器改变会使运行版本指纹变化。
- 四项基础离线自检通过；synthetic端到端检查通过；exit_model、param_scan、select_short三个阶段的首次缓存/缓存命中与完整重算结果一致。
- 新包复现首赛365条预测，CSV SHA-256仍为`0d3e80c52bab50ab7865191b8a9f2914690f654518ae8a1e04f19b1bd06abc76`；未重跑保留窗、未上传预测。
- 归一化导入后，510个既有函数/类定义AST不变；差异限于创建目录延后到实际写入、源码指纹集中化及未使用报告变量清理。原run.py的CLI参数与工作流拆开，保持原参数和数据协议。
- 已完成editable安装、wheel构建及仓库外全部模块导入/帮助命令验证，wheel带竞赛限定许可证，不带数据/账本/测试文件。
- Ruff格式、PEP 8导入分组及全包/测试/入口检查通过。完整pytest为`428 passed in 49.72s`，其中8项包/架构回归覆盖导入副作用、依赖、安装与CLI语义；远程SHA在交付回执记录。本地明细为忽略的`state/refactor/`。

主工程源码现与远程main同步；本地真实行情、账本、参数与既有冻结证据不因本次源码同步被覆盖。代码路径和哈希变化不是新的样本外证据；旧paper运行版本仍因漂移被拒绝，新运行版本衔接需按既有流程显式登记到新副本。

运行时包共有81个Python模块（含包初始化文件），所有旧模块均有映射。规则清单与项目数据/账本门禁未因目录迁移而放宽。

Kaggle代码审阅Notebook已同步为公开版本4；25个Markdown源码单元与本地包版本一致，未执行模型或提交预测。

## 主工程同步约定

用户指定`~/Documents/tech/mydev/quant-agent`作为远程main的主检出。合入远端后应同步此目录，不能为保留旧实验代码而长期让主工程落后。此次同步前将3份不同于远端的未跟踪竞赛文档备份至忽略的`state/sync-backups/`，核验备份后再快进更新。主工程的虚拟环境安装本地包，并核验入口、测试、受控文件内容与数据库校验值。

同步不等于自动激活旧paper实验的新代码版本；其漂移校验和新副本衔接规则继续生效。同步明细与本地文档备份位置见`state/sync-backups/latest.json`指向的receipt。本轮实际技能为git-workflow、finishing-a-development-branch、verification-before-completion；沿用项目AGENTS及Python工程/数据运行规则，验证以主工程当前环境的结果为准。
