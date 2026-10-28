# 首赛源码交付（2026-10-28）

完整代码入口见[CODE_MAP](CODE_MAP.md)：8个模块、3个测试文件、4份原始实验驱动快照，附数据契约、参数和复盘。源码提交`a649d51`，许可范围提交`d8f6704`；新增许可仅适用[明确列出的竞赛代码与配置](CODE_LICENSE.md)。

## 交付对象

- GitHub：[main分支](https://github.com/cylovelife/quant-agent/tree/main)，并保留[codex/kaggle-day2开发分支](https://github.com/cylovelife/quant-agent/tree/codex/kaggle-day2)。用户在初次分支推送后明确要求合入main；使用个人作者和GitHub SSH身份，不修改原投资本地worktree。
- Kaggle：[Stock Price Predictor Code Review 20261028](https://www.kaggle.com/code/leon0330/stock-price-predictor-code-review-20261028)，公开版本2，关联本场比赛，页面及分享面板均确认Public和Apache 2.0。

Kaggle最初因账号未完成手机验证返回403。用户完成验证后，同一份代码已成功公开；不需要更换token。最终标题通过网页修改，避免与平台已有标题冲突。

## 本轮验证证据

- 相关pytest：44 passed in 8.74s；竞赛模块及测试ruff检查通过。
- 最终CSV重新生成的SHA与此前有效提交逐字节相同；已有输出路径拒绝覆盖。
- 15项源码清单、4份历史驱动原始字节一致；JSON契约及文档链接通过。
- Kaggle版本2回读：`is_private=false`，22个Markdown单元格与本地逐字一致，0个可执行代码单元，比赛关联正确。保存传入`--no-run`；未训练模型或提交新预测。
- 暂存内容排除原始xlsx、预测CSV、真实账本及实际凭据值；作者、提交者、个人SSH身份和远程目标已核实。
- GitHub交付以`git ls-remote --heads origin main codex/kaggle-day2`中两分支均与本地HEAD相同为验收，远端SHA在最终回复及本地交付回执记录。

本轮未修改原模型或参数，未重跑已消耗的保留窗，未付费、未推进Hull。第二名的公开榜互补来源仍按[成绩复盘](rank2_2026-10-28.md)完整披露，代码公开不改变其证据性质。

本地回执、下载源码副本和公开状态截图保留在忽略的`state/competitions/stock-price-predictor/`，包括`code-delivery-readback.json`及`evidence/code-published.png`。

实际加载技能：git-workflow、finishing-a-development-branch、verification-before-completion、web-access。实际读取规则：项目AGENTS、skills-index、engineering-workflow、Python code-style/data-runtime、anti-hallucination。必要核验如上；没有把本地检查或保存回执替代公开状态及Git远端SHA回读。

## main合入核验

远程main基点`b621f9e9c401e0d81563a0a7210c8f0c9728dcef`是本次开发分支的祖先，采用快进更新，不改写历史。除本次竞赛新增文件和交付文档外，没有修改既有源码、run.py、生产参数或账本。Kaggle已公开源码的15项清单哈希保持不变，因此沿用此前44项竞赛及时间验证/实验测试和CSV复现证据。

合入前补充执行项目四项基础离线自检：offline_store、offline_datasources、offline_long、offline_ml全部通过；`tests/test_paper_runtime.py tests/test_local_research.py`为`53 passed in 6.53s`。这些验证使用临时数据，不推进真实账户或重新评价比赛模型。

原本地main位于`/Users/cylove0330/Documents/tech/mydev/quant-agent`，含未跟踪的`docs/competitions/`；本次未在那里拉取、切换或覆盖文件。其既有冻结运行版本保留。新增src模块会改变全src代码指纹，日后更新运行代码应沿用版本衔接规则，不排除竞赛文件或修改门禁来伪装旧版本。

本次合入实际技能：git-workflow、finishing-a-development-branch、verification-before-completion；实际规则：项目AGENTS、skills-index、engineering-workflow、Python code-style/data-runtime、anti-hallucination。现有Harness足以约束隔离和验证，无须修改工程规则。

## 包结构整理后的当前入口

随后对全部src进行了包结构重构，当前入口为`src/quant_agent/`，开发分支为`codex/package-architecture`，合入目标仍为main。此前44/53项及15项源码哈希对应初次交付时点；当前验收为428项pytest、四项离线自检、构造端到端与缓存等价、wheel安装入口和相同CSV的确定性复现，详见[包结构说明](../architecture/package_layout.md)。旧哈希清单单独保存，不回填原记录。Kaggle公开版本4已同步这些包路径及源码，25个Markdown单元格回读逐字一致，0个可执行单元；未提交新预测。
