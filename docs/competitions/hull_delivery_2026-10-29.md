# Hull本轮验证、review与交付记录

日期：2026-10-29，Asia/Shanghai。交付范围为同一隔离工作区内尚未提交的完整Hull历史研究能力，以及本轮固定反转的组件／成本消融、金融解释和后续计划。基线为 `5d095c89ae7f2a2b5d769abc045d355759788a25`，工作分支 `codex/hull-baseline-20261029`。

## 用户范围与工程边界

用户已明确选择合入现有 `main`，不另建 `master`；提交、远程推送与主工程同步均在本次授权范围内。原始数据、模型、逐日预测及本地证据留在忽略目录，不进入Git。当前保留段 `[8544,9048)` 本轮为0预测、0评分；没有线上新提交、隐藏成绩、投资晋级或真实交易。

源码同步与冻结实验运行版本衔接分开。新增竞赛源码会改变原系统的全源码指纹；本次不迁移或续写既有投资实验，不改其账本、现金、参数和历史记录。后续运行仍须遵守原运行版本衔接门禁。

Harness核对：原AGENTS对投资与账本边界充分，但对新Hull模块缺入口，分类为该新模块的 `missing`。已最小补齐真理源、保留段、冻结引用、新目录、许可及验证入口；没有改写既有投资边界。

## 已运行验证

| 检查 | 当前证据与范围 |
|---|---|
| 全套pytest | `.venv/bin/python -m pytest -q`：502 passed，80.96s；这是Sharpe小修及最后两项新测试之前的全量结果，不冒称最终504例全套重跑 |
| 最终变更定向回归 | `.venv/bin/python -m pytest -q tests/test_hull_ablation.py`：8 passed，27.68s；包含Sharpe修正、完整执行器的保留段扰动、禁fit/predict、引用损坏和拒绝覆盖。最终收集为504例；修正后按变更范围复核，不重复无关全套 |
| 四项基础离线 | `offline_store.py`、`offline_datasources.py`、`offline_long.py`、`offline_ml.py` 均通过，使用隔离临时数据库／桩源。datasources第一次因worktree缺 `state/cache` 目录中断，读取代码确认前置目录后在本地忽略目录创建，再完整通过；未改产品代码掩盖失败 |
| 静态检查 | Hull模块与其测试 `ruff check`通过；新增ablation源码与测试 `ruff format --check`通过；自有文件的staged `git diff --check`、110个本地链接与新增自有Python作者标记检查通过；官方复制文件保留上游末尾空行，因此完整staged diff有一条blank-at-EOF提示，按冻结原文身份例外处理，不为格式改动其哈希 |
| 依赖 | `uv pip check --python .venv/bin/python`：46个已安装包兼容；冷环境按 `requirements-hull.lock` 的哈希离线安装45项依赖，再安装wheel |
| 打包 | `uv build --wheel`成功；wheel含新模块、官方metric许可与notice，不含state/reports/logs；工作区外可导入wheel中模块 |
| 真实开发研究 | 2窗×4策略×4组＝32个毛评分案例，192个成本情景；0模型新拟合／预测；八份输出各503行，最大评价ID8542；只解析原train前8544行、最大输入ID8543 |
| 旧行为等价 | 八份旧原始／完整风险仓位逐日float32一致；旧引用执行前核验哈希、复制、结束核对原文件未变；官方scorer哈希不变 |
| 最终版本指纹 | `ablation-20261029-v2/frozen.json`内全部源码哈希与最终Hull源码一致，毛评分和累计净收益与首次结果相同；修正只改变自定义Sharpe的分母 |
| 冷环境复现 | 新venv＋离线锁安装＋已构建wheel，在工作区外重新执行同一协议；结果JSON与八份逐日配置文件均逐字节一致，不增加新金融样本；不声称重新训练冻结学习器 |

本轮未运行需要真实行情缓存的投资E2E，也未用其替代竞赛证据。已有原API开发段重放证据见各轮报告；本次没有对官方mock或保留段重放，也没有在Kaggle服务器上运行新包。

最终本地证据目录为忽略的 `state/competitions/hull-tactical-market-prediction/ablation-20261029-v2/`。其中 `verification.json`保留指纹、冷环境复现和包检查；`package-verification/`保留wheel及安装日志。冷复跑输出为同级 `ablation-cold-20261029/`；首次 `ablation-20261029/`保留为指标口径修正前记录，不覆盖或引用成最终结果。临时冷环境已移除，必要复现材料保留。

- 协议SHA-256：`3ff255f4d7b106dd1236246b54f097ec9439ce2bba3a051c80771321fd38397f`
- 最终results SHA-256：`7bdb939242e3bc58290fdc7c9b01d2f5e74b276a3c6b6a82893fe51bfe6a852c`

## 独立review与处理

独立Agent只读检查全部待交付Hull模块、相关测试／协议、主要研究文档和包声明。未读取保留窗值、未运行真实研究；相关合成回归20例通过，修正专项1例通过。最终无未解决的P1／P2问题；这不是隐藏数据、生产或平台审批证明。

review提出的净收益Sharpe分母问题已修复并加入变化现金利率的测试，官方scorer保持原算法。文档核对另修正长期目标为KDD Cup **2028**、更新D13／D14实际接续，并明确新赛事先冻结切分再训练。保留段0预测表述限定本轮，和早期重叠mock常数协议检查区分。

## 路由清单与规则

实际读取Skill：`using-git-worktrees`、`test-driven-development`、`deep-research`（TECH的frameworks-tech、evidence-grading、output-templates）、`tech-blogger`、`requesting-code-review`及review输入模板、`git-workflow`、`finishing-a-development-branch`、`verification-before-completion`；遇到旧离线脚本失败时加载 `systematic-debugging`，临时环境清理加载 `careful`。

实际读取规则：项目 `AGENTS.md`、`~/.codex/skills-index.md`、`engineering-workflow.md`、`python/code-style.md`、`python/data-runtime.md`、`shared-code-change-safety.md`、`anti-hallucination.md`、`research/deep-research.md`、`writing/tech-blog.md`。任务语言为Python；未改Java、TypeScript或Rust，因此未加载其语言规则。

主流程为固定协议的开发研究与已授权Git交付；复用已有隔离worktree，独立review按对应Skill明确委派。没有未决架构选择，不额外叠加brainstorming/Superpowers；没有公司业务数据库操作，不加载DBPaaS；未创建自动化、外部消息或比赛提交。索引提及的tech-researcher不在本会话catalog，本轮使用实际可用的deep-research内核，不假称加载该入口。

## Git交付

目标为个人GitHub `cylovelife/quant-agent` 的 `main`；实际author／committer均为个人 `@foxmail.com` 身份，SSH解析为个人GitHub key且 `IdentitiesOnly=yes`，不套用公司Jira前缀。提交仅包含本任务Hull源码、测试、依赖、协议、研究与接续文档。主工程原有干净状态已核对；采用快进合入与普通推送，最终提交和远程一致性由交付后实际Git输出核验。

结果与金融原理见[消融报告](hull_ablation_2026-10-29.md)，收获与后续阶段见[复盘和Kaggle计划](hull_lessons_and_kaggle_plan_2026-10-29.md)。
